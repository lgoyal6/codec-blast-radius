"""Compile baseline class-namespace observations into conservative masks."""

import argparse
import ast
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector import Baseline, union


def namespace_sites(source, baseline):
    for path, coverage in baseline.files.items():
        if not path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse((source / path).read_bytes())):
            attribute = (
                isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and node.attr == "__dict__"
            )
            named = (
                isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "__dict__"
                or isinstance(node, ast.Constant) and node.value == "__dict__"
            )
            if attribute or named:
                site = f"{path}:{node.lineno}:{node.col_offset}"
                yield site, coverage.lines.get(node.lineno, 0)


def compile_masks(baseline, source, observations, owners):
    sites = dict(namespace_sites(source, baseline))
    if union(sites.values()) != baseline.references.get("__dict__", 0):
        raise ValueError("source namespace references disagree with the baseline map")
    if observations.keys() - sites.keys():
        raise ValueError("reflection trace contains unknown source sites")
    generic = 0
    affected = defaultdict(int)
    for site, mask in sites.items():
        contexts = observations.get(site, {})
        resolved = 0
        for owner, tests in contexts.items():
            if owner == "?":
                if "@collection" in tests:
                    generic |= mask
                generic |= union(baseline.indices.get(test, 0) for test in tests)
                continue
            hits = union(baseline.indices.get(test, 0) for test in tests)
            resolved |= hits
            if owner in owners:
                affected[owner] |= baseline.all if "@collection" in tests else hits
        generic |= mask & ~resolved
    return {owner: format(generic | affected[owner], "x") for owner in sorted(owners)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    baseline = Baseline(args.baseline)
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    revision = subprocess.check_output(["git", "-C", args.source, "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", args.source, "status", "--porcelain"], text=True)
    if dirty or revision != profile["revision"] or set(profile["ids"]) != baseline.indices.keys():
        raise ValueError("the profile, coverage map and checkout must describe the pinned baseline")
    observations = defaultdict(lambda: defaultdict(set))
    seen = set()
    for path in args.reports:
        report = json.loads(path.read_bytes())
        seen.update(report["seen"])
        for site, receivers in report["reads"].items():
            for owner, contexts in receivers.items():
                observations[site][owner].update(contexts)
    if baseline.indices.keys() - seen:
        raise ValueError("the reflection trace must include every catalog test")
    owners = {key.rsplit(":", 1)[0] for key in profile["token_dispatch_keys"]}
    masks = compile_masks(baseline, args.source, observations, owners)
    profile["class_namespace_users"] = masks
    with args.output.open("wb") as target, gzip.GzipFile(
        filename="", fileobj=target, mode="wb", mtime=0,
    ) as output:
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    broad = baseline.references.get("__dict__", 0)
    narrow = min(int(mask, 16).bit_count() for mask in masks.values())
    print(f"{len(masks)} observed class masks; smallest retains {narrow} tests from {broad.bit_count()} broad references")


if __name__ == "__main__":
    main()

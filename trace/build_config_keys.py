"""Attach class configuration key reads from a pinned baseline trace."""

import argparse
import ast
from collections import defaultdict
import gzip
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--parser-dispatch", action="store_true")
    parser.add_argument("--pinned-source", type=Path)
    args = parser.parse_args()
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    seen, owners = set(), set()
    lookups = defaultdict(lambda: defaultdict(set))
    raw, unknown = defaultdict(set), defaultdict(set)
    for path in args.reports:
        report = json.loads(path.read_bytes())
        seen.update(report["seen"])
        owners.update(report["owners"])
        for owner, keys in report["keys"].items():
            for key, tests in keys.items():
                lookups[owner][key].update(tests)
        for field, tests in report["raw"].items():
            raw[field].update(tests)
        for field, tests in report["unknown"].items():
            unknown[field].update(tests)
    indices = {test: 1 << index for index, test in enumerate(profile["ids"])}
    unseen = set(indices) - seen
    previously_seen = int(profile["seen"], 16)
    unseen_in_both = {test for test in unseen if not indices[test] & previously_seen}
    allowed = {"FUNCTION_PARSERS"} if args.parser_dispatch else set(profile["runtime_literal_containers"])
    if args.parser_dispatch:
        if unseen or args.pinned_source is None:
            raise ValueError("parser dispatch profiling requires a pinned source tree and every catalog test")
        revision = subprocess.check_output(["git", "-C", args.pinned_source, "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", args.pinned_source, "status", "--porcelain"], text=True)
        if revision != profile["revision"] or dirty:
            raise ValueError("expected the clean pinned checkout")
        for path in (args.pinned_source / "tests").rglob("*.py"):
            tree = ast.parse(path.read_bytes())
            if any(
                isinstance(node, ast.Attribute) and node.attr == "FUNCTION_PARSERS"
                or isinstance(node, ast.Constant) and node.value == "FUNCTION_PARSERS"
                for node in ast.walk(tree)
            ):
                raise ValueError(f"parser dispatch is directly inspected by {path}")

    def mask(tests):
        return sum(indices[test] for test in set(tests) if test in indices)

    tables = {}
    for owner in sorted(owners):
        field = owner.rsplit(":", 1)[1]
        if field not in allowed:
            continue
        if args.parser_dispatch:
            if raw[owner] or not owner.startswith("sqlglot.parsers."):
                continue
        elif owner not in profile["attributes"]:
            continue
        keys = lookups[owner]
        whole = raw[owner] | unknown[field]
        observed = whole.union(*keys.values())
        if "@collection" in observed or not keys:
            continue
        broad = int(profile["attributes"].get(owner, "0"), 16)
        keep = mask(whole | unseen_in_both) | (broad & ~mask(observed))
        tables[owner] = [format(keep, "x"), {key: format(mask(tests), "x") for key, tests in sorted(keys.items())}]

    profile["parser_dispatch_keys" if args.parser_dispatch else "configuration_keys"] = tables
    with (
        args.output.open("wb") as target,
        gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as output,
    ):
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(f"{len(tables)} containers, {len(unseen)} tests absent from the key trace")


if __name__ == "__main__":
    main()

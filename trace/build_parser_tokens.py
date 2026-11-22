"""Build masks for observed token operands in the pinned SQLGlot parser."""

import argparse
import ast
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser_tokens import literal_arguments, sites
from selector import REVISION, union


def capture(reports, catalog):
    tokens = defaultdict(lambda: defaultdict(int))
    guard_users, guard_entered = defaultdict(int), defaultdict(int)
    seen, words = set(), set()
    types = {}
    for path in reports:
        report = json.loads(path.read_bytes())
        seen.update(report["seen"])
        words.update(report["words"])
        if report["types"]:
            if types and types != report["types"]:
                raise ValueError("observations disagree about TokenType")
            types = report["types"]
        for coordinate, operands in report["tokens"].items():
            for operand, mask in operands.items():
                tokens[coordinate][operand] |= int(mask, 16)
        for key, masks in (("guards", guard_users), ("guard_seen", guard_entered)):
            for coordinate, mask in report[key].items():
                masks[coordinate] |= int(mask, 16)
    if set(catalog) - seen or not types or not words:
        raise ValueError("the parser trace does not include the complete catalog")
    return tokens, guard_users, guard_entered, sorted(words), types


def source_sites(source):
    paths = [source/"sqlglot/parser.py", *sorted((source/"sqlglot/parsers").glob("*.py"))]
    roots = {path.relative_to(source).as_posix(): ast.parse(path.read_bytes()) for path in paths}
    calls = {
        f"{path}:{node.lineno}:{node.col_offset}": node
        for path, tree in roots.items()
        for node in ast.walk(tree) if literal_arguments(node)
    }
    guards = {f"sqlglot/parser.py:{key}": node
              for key, (node, _) in sites(roots["sqlglot/parser.py"]).items()}
    return calls, guards


def build(source, profile, reports):
    tokens, guard_users, guard_entered, words, types = capture(reports, profile["ids"])
    call_locations, guard_locations = source_sites(source)
    if tokens.keys() - call_locations.keys() or guard_entered.keys() - guard_locations.keys():
        raise ValueError("trace coordinates do not match the pinned parser")

    files = defaultdict(lambda: {"lines": set(), "calls": {}, "guards": {}})
    masks = {0: 0}

    def mask_index(mask):
        if not 0 <= mask < 1 << (len(profile["ids"]) + 1):
            raise ValueError("mask has an unknown test context")
        return masks.setdefault(mask, len(masks))

    def file_site(site, node):
        path, line, column = site.rsplit(":", 2)
        file = files[path]
        file["lines"].update(range(node.lineno, node.end_lineno + 1))
        return file, f"{line}:{column}"

    for site, operands in sorted(tokens.items()):
        file, key = file_site(site, call_locations[site])
        file["calls"][key] = [
            mask_index(union(operands.values())),
            mask_index(operands.get("unknown", 0)),
            {token.removeprefix("value:"): mask_index(mask)
             for token, mask in sorted(operands.items()) if token.startswith("value:")},
        ]
    for site, entered in sorted(guard_entered.items()):
        file, key = file_site(site, guard_locations[site].test)
        file["guards"][key] = [format(entered, "x"), format(guard_users[site], "x")]

    for file in files.values():
        file["lines"] = sorted(file["lines"])
    profile["parser_tokens"] = {
        "files": files, "masks": [format(mask, "x") for mask in masks], "words": words, "types": types,
    }
    print(f"{len(tokens)} matcher sites, {len(guard_entered)} optional blocks, {len(masks)} unique masks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()

    revision = subprocess.check_output(
        ["git", "-C", args.source, "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", args.source, "status", "--porcelain"], text=True
    )
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    catalog = json.loads(args.catalog.read_bytes())["tests"]
    if dirty or revision != REVISION or profile["revision"] != revision:
        raise ValueError("expected the clean pinned source and profile")
    if [item["id"] for item in catalog] != profile["ids"]:
        raise ValueError("capture catalog test bit order does not match the profile")
    build(args.source, profile, args.reports)
    with args.output.open("wb") as target, gzip.GzipFile(
        filename="", fileobj=target, mode="wb", mtime=0,
    ) as output:
        output.write(json.dumps(profile, separators=(",", ":")).encode())


if __name__ == "__main__":
    main()

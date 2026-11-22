"""Attach class configuration key reads from a pinned baseline trace."""

import argparse
import ast
from collections import defaultdict
import gzip
import json
import subprocess
from pathlib import Path


def direct_container_fields(source, fields):
    direct = set()
    paths = list((source / "sqlglot").rglob("*.py")) + list((source / "tests").rglob("*.py"))
    for path in paths:
        in_tests = path.is_relative_to(source / "tests")
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Constant) and type(node.value) is str and node.value in fields:
                direct.add(node.value)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in fields:
                direct.add(node.id)
            elif in_tests and isinstance(node, ast.Attribute) and node.attr in fields:
                direct.add(node.attr)
    return direct


def parser_functions(source):
    tree = ast.parse((source / "sqlglot/parser.py").read_bytes())
    return sorted(
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.decorator_list and node.name != "__getattr__"
    )


def uses_original_parser_module(source, owner):
    module = owner.split(":", 1)[0]
    tree = ast.parse((source / (module.replace(".", "/") + ".py")).read_bytes())
    imports = [
        node for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "sqlglot" and node.level == 0
        and any(alias.name == "parser" and alias.asname is None for alias in node.names)
    ]
    if len(imports) != 1:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "parser" and not isinstance(node.ctx, ast.Load):
            return False
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node is not imports[0]:
            if any((alias.asname or alias.name.split(".")[0]) == "parser" for alias in node.names):
                return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--parser-dispatch", action="store_true")
    mode.add_argument("--token-dispatch", action="store_true")
    mode.add_argument("--dictionary-membership", action="store_true")
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
    if args.parser_dispatch or args.token_dispatch:
        if unseen or args.pinned_source is None:
            raise ValueError("parser dispatch profiling requires a pinned source tree and every catalog test")
        revision = subprocess.check_output(["git", "-C", args.pinned_source, "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", args.pinned_source, "status", "--porcelain"], text=True)
        if revision != profile["revision"] or dirty:
            raise ValueError("expected the clean pinned checkout")
    if args.token_dispatch:
        tracked = {key.rsplit(":", 1)[1] for key in owners}
        allowed = tracked - direct_container_fields(args.pinned_source, tracked)
    if args.parser_dispatch:
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
        elif args.token_dispatch:
            if (
                raw[owner] or not owner.startswith("sqlglot.parsers.")
                or not uses_original_parser_module(args.pinned_source, owner)
            ):
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

    profile_key = "configuration_keys"
    if args.parser_dispatch:
        profile_key = "parser_dispatch_keys"
    elif args.token_dispatch:
        profile_key = "token_dispatch_keys"
        profile["token_dispatch_functions"] = parser_functions(args.pinned_source)
    elif args.dictionary_membership:
        profile_key = "dictionary_membership"
    profile[profile_key] = tables
    with (
        args.output.open("wb") as target,
        gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as output,
    ):
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(f"{len(tables)} containers, {len(unseen)} tests absent from the key trace")


if __name__ == "__main__":
    main()

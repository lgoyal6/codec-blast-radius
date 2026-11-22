"""Build conservative masks for literal bindings in pinned modules."""

import argparse
import ast
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from importlib.util import resolve_name
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector import Baseline, REVISION, union
from semantic_edits import module_literal

NAMESPACE_OPERATIONS = {"eval", "exec", "globals", "locals"}
EXTERNAL_METADATA = {
    "__globals__", "__getattribute__", "get_code", "getsource", "getsourcelines", "co_consts",
}


def module_name(path):
    return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def declarations(tree):
    names = defaultdict(int)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names[node.id] += 1
    return {
        value[0] for node in tree.body
        if (value := module_literal(node)) is not None and names[value[0]] == 1
    }


def references(trees, baseline):
    masks = defaultdict(dict)
    collected = defaultdict(set)
    exported = defaultdict(set)
    for path, tree in trees.items():
        file = baseline.files[path]
        module = module_name(path)
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                origin = "." * node.level + (node.module or "")
                if origin:
                    try:
                        exported[resolve_name(origin, package)].update(alias.name for alias in node.names)
                    except ImportError:
                        continue
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif isinstance(node, ast.Constant) and type(node.value) is str and node.value.isidentifier():
                name = node.value
            else:
                continue
            masks[name][path] = masks[name].get(path, 0) | file.lines.get(node.lineno, 0)
            if node.lineno in file.collection:
                collected[name].add(path)
    return masks, collected, exported


def masks_for_bindings(trees, baseline, selected, runtime_reads, loaded):
    masks, collected, exported = references(trees, baseline)
    metadata = union(baseline.references.get(name, 0) for name in EXTERNAL_METADATA)
    profiles = defaultdict(dict)
    for path, names in selected.items():
        module = module_name(path)
        module_reads = runtime_reads[module]
        if (
            module not in loaded or "@collection" in module_reads["__dict__"]
            or "*" in exported[module]
            or any(path in collected[operation] for operation in NAMESPACE_OPERATIONS)
        ):
            continue
        namespace_users = union(masks[operation].get(path, 0) for operation in NAMESPACE_OPERATIONS)
        for name in sorted(names - exported[module]):
            if collected[name] or "@collection" in module_reads[name]:
                continue
            users = union(masks[name].values())
            users |= union(baseline.indices.get(test, 0)
                           for test in module_reads[name] | module_reads["__dict__"])
            profiles[path][name] = format(users | metadata | namespace_users, "x")
    return dict(profiles)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--reports", type=Path, nargs="+")
    args = parser.parse_args()
    baseline = Baseline(args.baseline)
    revision = subprocess.check_output(["git", "-C", args.source, "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", args.source, "status", "--porcelain"], text=True)
    if dirty or revision != REVISION:
        raise ValueError("expected the clean pinned source")
    trees = {
        path: ast.parse((args.source / path).read_bytes())
        for path in baseline.files if path.endswith(".py")
    }
    selected = {
        path: names for path, tree in trees.items()
        if path.startswith("sqlglot/") and (names := declarations(tree))
    }
    if args.profile is None:
        targets = {module_name(path): sorted(names) for path, names in selected.items()}
        args.output.write_text(json.dumps(targets, sort_keys=True))
        return
    seen, loaded = set(), set()
    runtime_reads = defaultdict(lambda: defaultdict(set))
    for report in args.reports:
        data = json.loads(report.read_bytes())
        seen.update(data["seen"])
        loaded.update(data["loaded"])
        for module, fields in data["reads"].items():
            for name, contexts in fields.items():
                runtime_reads[module][name].update(contexts)
    if baseline.indices.keys() - seen:
        raise ValueError("the module capture did not observe the complete catalog")
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    if profile["revision"] != REVISION or set(profile["ids"]) != baseline.indices.keys():
        raise ValueError("the profile and coverage maps describe different baselines")
    profile["module_literals"] = masks_for_bindings(trees, baseline, selected, runtime_reads, loaded)
    with args.output.open("wb") as target, gzip.GzipFile(
        filename="", fileobj=target, mode="wb", mtime=0,
    ) as output:
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(sum(map(len, profile["module_literals"].values())), "module bindings with scoped readers")


if __name__ == "__main__":
    main()

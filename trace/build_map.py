"""Compile a baseline trace and public test IDs into the selector's data file."""

import argparse
import ast
import gzip
import importlib.util
import json
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path

REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
COLLECTION = "@collection"


def union(masks):
    result = 0
    for mask in masks:
        result |= mask
    return result


class Imports(ast.NodeVisitor):
    def __init__(self, package, modules):
        self.package = package
        self.modules = modules
        self.dependencies = set()

    def add(self, module):
        pieces = module.split(".")
        for end in range(1, len(pieces) + 1):
            path = self.modules.get(".".join(pieces[:end]))
            if path is not None:
                self.dependencies.add(path)

    def visit_If(self, node):
        check = node.test
        if (
            isinstance(check, ast.Name) and check.id == "TYPE_CHECKING"
            or isinstance(check, ast.Attribute) and check.attr == "TYPE_CHECKING"
        ):
            for statement in node.orelse:
                self.visit(statement)
        else:
            self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.add(alias.name)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if node.level:
            if not self.package:
                return
            module = importlib.util.resolve_name("." * node.level + module, self.package)
        self.add(module)
        for alias in node.names:
            self.add(f"{module}.{alias.name}")


def python_files(source):
    return {
        path.relative_to(source).as_posix(): ast.parse(path.read_bytes(), filename=path.as_posix())
        for directory in ("sqlglot", "tests")
        for path in sorted((source / directory).rglob("*.py"))
    }


def import_dependents(trees, test_files):
    modules = {
        filename.removesuffix(".py").removesuffix("/__init__").replace("/", "."): filename
        for filename in trees
    }
    graph = {}
    for path, tree in trees.items():
        package = path.removesuffix(".py").replace("/", ".").rpartition(".")[0]
        imports = Imports(package, modules)
        imports.visit(tree)
        graph[path] = imports.dependencies
    dependents = defaultdict(int)
    for path, mask in test_files.items():
        visited = set()
        pending = [path]
        while pending:
            dependency = pending.pop()
            if dependency in visited:
                continue
            visited.add(dependency)
            dependents[dependency] |= mask
            pending.extend(graph.get(dependency, ()))
    return dependents


def coverage_lines(directory, bits):
    lines = defaultdict(dict)
    collection = defaultdict(set)
    query = (
        "select file.path, context.context, line_bits.numbits from line_bits "
        "join file on file.id = line_bits.file_id "
        "join context on context.id = line_bits.context_id"
    )
    databases = sorted(directory.glob("*.coverage*"))
    if not databases:
        raise ValueError("missing baseline coverage databases")
    for database in databases:
        with sqlite3.connect(database) as connection:
            for absolute_path, context, covered in connection.execute(query):
                source_path = absolute_path.removeprefix("/src/")
                if not source_path.startswith(("sqlglot/", "tests/")):
                    raise ValueError(f"expected a project-relative coverage path, got {absolute_path}")
                if context != COLLECTION and context not in bits:
                    continue
                remaining = int.from_bytes(covered, "little")
                current = lines[source_path]
                while remaining:
                    bit = remaining & -remaining
                    line = bit.bit_length() - 1
                    remaining ^= bit
                    if context == COLLECTION:
                        collection[source_path].add(line)
                    else:
                        current[line] = current.get(line, 0) | bits[context]
    return lines, collection


def trace_events(directory, bits, usage_trace=None):
    readers = defaultdict(int)
    owners = defaultdict(int)
    raw = json.loads((directory / "baseline.json").read_text())
    unseen = set(bits).difference(raw["seen"])
    if unseen:
        raise ValueError(f"missing {len(unseen)} baseline tests, including {min(unseen)}")
    all_tests = union(bits.values())
    directories = (directory,) if usage_trace is None else (directory, usage_trace)
    reports = [
        path
        for root in directories
        for path in [root / "baseline.json", *sorted(root.glob("baseline.worker-*.json"))]
    ]
    for path in reports:
        trace = json.loads(path.read_text())
        for filename, contexts in trace["files"].items():
            readers[filename] |= (
                all_tests if COLLECTION in contexts else union(bits.get(name, 0) for name in contexts)
            )
        for owner, contexts in trace["owners"].items():
            owners[owner] |= union(bits.get(name, 0) for name in contexts)
    return readers, owners


def loaded_names(trees, lines):
    references = defaultdict(int)
    for path, tree in trees.items():
        file_lines = lines.get(path, {})
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                name = node.value
            else:
                continue
            references[name] |= file_lines.get(node.lineno, 0)
    return {name: mask for name, mask in references.items() if mask}


def build(source, trace, catalog, output, usage_trace=None, reader_trace=None):
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        raise ValueError(f"expected baseline {REVISION}, got {revision}")
    dirty = subprocess.check_output(["git", "-C", source, "status", "--porcelain"], text=True)
    if dirty:
        raise ValueError("baseline checkout has local changes")

    tests = sorted(json.loads(catalog.read_text())["tests"], key=lambda test: test["number"])
    ids = [test["id"] for test in tests]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate test ID")
    bits = {test_id: 1 << index for index, test_id in enumerate(ids)}
    test_files = defaultdict(int)
    for test in tests:
        test_files[test["file"]] |= bits[test["id"]]

    trees = python_files(source)
    imports = import_dependents(trees, test_files)
    lines, collection = coverage_lines(trace, bits)
    readers, owners = trace_events(trace, bits, usage_trace)
    if reader_trace is not None:
        source_readers, _ = trace_events(reader_trace, bits)
        for path in trees:
            if path.startswith("tests/") and path.endswith(".py"):
                readers[path] = source_readers[path]
    references = loaded_names(trees, lines)

    masks = {0: 0}

    def encode(mask):
        return masks.setdefault(mask, len(masks))

    files = {
        path: [
            [[line, encode(mask)] for line, mask in sorted(lines.get(path, {}).items())],
            sorted(collection.get(path, ())),
            encode(imports.get(path, 0)),
            encode(readers.get(path, 0)),
            encode(test_files.get(path, 0)),
        ]
        for path in sorted(set(trees) | set(readers))
    }
    result = {
        "format": 1,
        "revision": REVISION,
        "ids": ids,
        "files": files,
        "references": {name: encode(mask) for name, mask in sorted(references.items())},
        "owners": {name: encode(mask) for name, mask in sorted(owners.items())},
        "masks": [format(mask, "x") for mask in masks],
    }
    output.write_bytes(gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(ids)} tests, {len(files)} files, {len(owners)} runtime class owners, {output.stat().st_size} bytes")


def main():
    parser = argparse.ArgumentParser()
    for name in ("source", "trace", "catalog", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--usage-trace", type=Path)
    parser.add_argument("--reader-trace", type=Path)
    args = parser.parse_args()
    build(args.source, args.trace, args.catalog, args.output, args.usage_trace, args.reader_trace)


if __name__ == "__main__":
    main()

"""Find literal class fields read only through local membership, lookup, or iteration."""
import argparse
import ast
from collections import defaultdict
import gzip
import json
from pathlib import Path


def callable_name(call):
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def observe(trees):
    fields = set()
    unsafe = set()
    by_function = defaultdict(set)
    edges = defaultdict(set)
    setup = {"__new__", "__init__", "__init_subclass__", "__post_init__", "<module>"}
    for tree in trees:
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def function_name(node):
            while node in parents:
                owner = parents[node]
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return owner.name if node in owner.body else "<module>"
                node = owner
            return "<module>"

        for node in parents:
            if isinstance(node, ast.Call):
                name = callable_name(node)
                if name is not None:
                    edges[function_name(node)].add(name)
            if isinstance(node, ast.Name) and node.id.isupper() and isinstance(node.ctx, ast.Load):
                unsafe.add(node.id)
            if not isinstance(node, ast.Attribute) or not node.attr.isupper():
                continue
            name = node.attr
            parent = parents[node]
            method = parents.get(parent)
            lookup = (
                isinstance(parent, ast.Subscript) and parent.value is node
                and isinstance(parent.ctx, ast.Load)
                or isinstance(parent, ast.Attribute) and parent.attr == "get"
                and isinstance(method, ast.Call) and method.func is parent
                or isinstance(parent, ast.Compare)
                and len(parent.ops) == 1 and isinstance(parent.ops[0], (ast.In, ast.NotIn))
                and parent.comparators[0] is node
                or isinstance(parent, ast.For) and parent.iter is node
            )
            if isinstance(node.ctx, ast.Load) and lookup:
                fields.add(name)
                by_function[function_name(node)].add(name)
            else:
                unsafe.add(name)

    pending = list(setup)
    while pending:
        owner = pending.pop()
        unsafe.update(by_function[owner])
        new = edges[owner] - setup
        setup.update(new)
        pending.extend(new)
    return sorted(fields - unsafe)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "profile", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    trees = [
        ast.parse(path.read_bytes())
        for folder in ("sqlglot", "tests")
        for path in sorted((args.source / folder).rglob("*.py"))
    ]
    profile["runtime_literal_containers"] = observe(trees)
    with (
        args.output.open("wb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as output,
    ):
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(", ".join(profile["runtime_literal_containers"]))


if __name__ == "__main__":
    main()

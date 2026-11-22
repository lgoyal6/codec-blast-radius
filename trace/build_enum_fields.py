"""Record valid class configuration options from the pinned SQLGlot checkout."""

import ast
import sys
from collections import defaultdict
from enum import EnumType
from importlib import import_module

from build_map import COLLECTION, python_files
from selector import DIALECT_FAMILIES, SourceShape


def enum_field_options(source, reads):
    trees = python_files(source)
    # A bare or string-based access in SQLGlot can bypass the attribute trace.
    indirect = set()
    for path, tree in trees.items():
        if not path.startswith("sqlglot/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                indirect.add(node.id)
            elif isinstance(node, ast.Constant) and type(node.value) is str:
                indirect.add(node.value)

    fields = defaultdict(set)
    for key, contexts in reads.items():
        qualified, owner, name = key.split(":")
        path = qualified.replace(".", "/") + ".py"
        parts = path.split("/")
        if (
            COLLECTION not in contexts
            and len(parts) == 3
            and parts[1] in DIALECT_FAMILIES
            and path in trees
            and name not in indirect
            and "." not in owner
        ):
            fields[path].add((owner, name))

    result = {}
    sys.path.insert(0, str(source.resolve()))
    try:
        for path, observed in sorted(fields.items()):
            shape = SourceShape((source / path).read_bytes(), path)
            candidates = [
                (owner, name, node.value)
                for owner, name in sorted(observed)
                if (node := shape.assignments.get((owner, name))) is not None
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
            ]
            if not candidates:
                continue
            module = import_module(path.removesuffix(".py").replace("/", "."))
            if module.__file__ is None or not (source / path).samefile(module.__file__):
                raise ValueError(f"enum options must come from the pinned source: {path}")
            scope = vars(module)
            options = {}
            for owner, name, reference in candidates:
                enum_name = reference.value.id
                enum = scope.get(enum_name)
                cls = scope.get(owner)
                if type(enum) is not EnumType or not isinstance(cls, type):
                    continue
                member = enum.__members__.get(reference.attr)
                if member is not None and vars(cls).get(name) is member:
                    options[f"{owner}:{name}"] = [enum_name, sorted(enum.__members__)]
            if options:
                result[path] = options
    finally:
        sys.path.pop(0)
    return result

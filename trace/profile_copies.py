"""Observe detached baseline objects passed directly into SQLGlot's alias builder."""

import ast
import importlib.machinery
import json
import sys
from collections import defaultdict
from pathlib import Path

import profile_runtime as runtime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from semantic_edits import fresh_alias

SCALARS = (str, bool, type(None))
PRIMITIVES = None


class Inject(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Call(self, node):
        self.generic_visit(node)
        constructor = fresh_alias(node)
        if constructor is None:
            return node
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        built = ast.copy_location(
            ast.Call(
                ast.Name("_blast_copy_build", ast.Load()),
                [constructor.func, *constructor.args],
                constructor.keywords,
            ),
            constructor,
        )
        return ast.copy_location(
            ast.Call(
                ast.Name("_blast_copy_alias", ast.Load()),
                [ast.Constant(site), node.func, built, *node.args[1:]],
                node.keywords,
            ),
            node,
        )


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.path.removeprefix("/src/")
        tree = ast.parse(self.get_data(self.path), self.path)
        return compile(ast.fix_missing_locations(Inject(path).visit(tree)), self.path, "exec")

    def exec_module(self, module):
        module.__dict__.update(_blast_copy_build=build, _blast_copy_alias=alias)
        super().exec_module(module)


class Finder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if (
                spec is not None
                and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
                and spec.origin.startswith("/src/sqlglot/")
            ):
                spec.loader = Loader(spec.name, spec.origin)
            return spec
        return None


def detached(value, parent=None, key=None, depth=0):
    _, _, _, _, copy, deepcopy, roots, children = PRIMITIVES
    allowed = roots if parent is None else children
    cls = type(value)
    if (
        depth > 3
        or cls not in allowed
        or cls.copy is not copy
        or cls.__deepcopy__ is not deepcopy
        or value.parent is not parent
        or value.arg_key != key
        or value.index is not None
        or any(item is not None for item in (value.comments, value._meta, value._type, value._hash))
    ):
        return False
    return all(
        type(child) in SCALARS or detached(child, value, name, depth + 1)
        for name, child in value.args.items()
    )


def build(callee, /, *args, **kwargs):
    result = callee(*args, **kwargs)
    if PRIMITIVES is None:
        return result, False
    _, constructors, *_ = PRIMITIVES
    owns = (
        any(callee is constructor for constructor in constructors)
        and all(type(item) in SCALARS for item in (*args, *kwargs.values()))
    )
    return result, owns


def alias(site, callee, built, /, *args, **kwargs):
    expr, owned = built
    safe = False
    if owned:
        original, _, parser, deepcopy, *_ = PRIMITIVES
        safe = (
            callee is original
            and original.__globals__["maybe_parse"] is parser
            and parser.__globals__["deepcopy"] is deepcopy
            and detached(expr)
        )
    runtime.TRACE.witnesses[site][runtime.CONTEXT] &= safe
    return callee(expr, *args, **kwargs)


class Capture(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.witnesses = defaultdict(lambda: defaultdict(lambda: True))

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "fresh_copy": {
                site: dict(sorted(contexts.items()))
                for site, contexts in sorted(self.witnesses.items())
            },
        }, separators=(",", ":")))


def start(path):
    global PRIMITIVES
    runtime.TRACE = Capture(path)
    sys.meta_path.insert(0, Finder())
    from sqlglot.expressions import builders, core, query

    PRIMITIVES = (
        core.alias_, (builders.table_, core.column),
        core.maybe_parse, core.deepcopy, core.Expression.copy, core.Expression.__deepcopy__,
        (query.Table, core.Column), (core.Identifier, query.TableAlias),
    )
    return runtime.TRACE


def main():
    runtime.start = start
    return runtime.main()

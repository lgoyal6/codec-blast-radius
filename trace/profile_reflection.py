"""Attribute class namespace reads to their receiver on pinned SQLGlot."""

import ast
import importlib.machinery
import json
import sys
from collections import defaultdict
from functools import cache
from pathlib import Path
from types import MappingProxyType

import profile_runtime as runtime

SOURCE = Path("/src")


@cache
def standard_class_namespace(metaclass):
    namespace = type.__getattribute__(metaclass, "__dict__")
    return (
        "__dict__" not in namespace or metaclass is type
    ) and (
        type.__getattribute__(metaclass, "__getattribute__") is type.__getattribute__
    ) and (
        all("__dict__" not in vars(parent) for parent in metaclass.__mro__ if parent not in (type, object))
    )


def namespace(receiver, site):
    value = receiver.__dict__
    owner = "?"
    if isinstance(receiver, type) and type(value) is MappingProxyType and standard_class_namespace(type(receiver)):
        owner = f"{receiver.__module__}:{receiver.__qualname__}"
    runtime.TRACE.reads[site][owner].add(runtime.CONTEXT)
    return value


class Inject(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Attribute(self, node):
        node = self.generic_visit(node)
        if node.attr != "__dict__" or not isinstance(node.ctx, ast.Load):
            return node
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        return ast.copy_location(ast.Call(
            ast.Name("_blast_class_namespace", ast.Load()),
            [node.value, ast.Constant(site)], [],
        ), node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = Path(self.path).relative_to(SOURCE)
        tree = ast.parse(self.get_data(self.path), self.path)
        return compile(ast.fix_missing_locations(Inject(path).visit(tree)), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["_blast_class_namespace"] = namespace
        super().exec_module(module)


class Finder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (
            spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and spec.origin.startswith("/src/sqlglot/")
            and b"__dict__" in spec.loader.get_data(spec.origin)
        ):
            spec.loader = Loader(spec.name, spec.origin)
        return spec


class Trace(runtime.Trace):
    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "reads": {
                site: {owner: sorted(contexts) for owner, contexts in owners.items()}
                for site, owners in self.reads.items()
            },
        }, separators=(",", ":")))

    def __init__(self, path):
        super().__init__(path)
        self.reads = defaultdict(lambda: defaultdict(set))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    sys.meta_path.insert(0, Finder())
    return trace

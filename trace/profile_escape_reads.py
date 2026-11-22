"""Observe how tests consume the pinned dialect escape maps."""

import ast
import importlib.machinery
import json
import sys
from collections import defaultdict

import profile_runtime as runtime

MODULES = {
    "sqlglot.dialects.dialect",
    "sqlglot.tokenizer_core",
    "sqlglot.generator",
    "sqlglot.generators.bigquery",
}


class Inject(ast.NodeTransformer):
    def __init__(self, module):
        self.module = module

    def visit_Attribute(self, node):
        self.generic_visit(node)
        field = {
            ("sqlglot.dialects.dialect", "klass.tokenizer_class.IDENTIFIER_ESCAPES"): "delimiter",
            ("sqlglot.generators.bigquery", "self.dialect.ESCAPED_SEQUENCES"): "whole",
        }.get((self.module, ast.unparse(node)))
        if field is not None:
            return ast.copy_location(ast.Call(
                ast.Name("_blast_escape_observe", ast.Load()),
                [ast.Constant(field), node], [],
            ), node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        field = {
            ("sqlglot.tokenizer_core", "unescaped_sequences.get"): "unescaped",
            ("sqlglot.generator", "self.dialect.ESCAPED_SEQUENCES.get"): "escaped",
        }.get((self.module, ast.unparse(node.func)))
        if field is not None:
            return ast.copy_location(ast.Call(
                ast.Name("_blast_escape_get", ast.Load()),
                [ast.Constant(field), node.func, *node.args], node.keywords,
            ), node)
        return node


def observe(field, value):
    trace = runtime.TRACE
    if field == "whole":
        trace.whole.add(runtime.CONTEXT)
    elif type(value) not in (list, tuple, dict, set, frozenset, str):
        trace.unknown_delimiters.add(runtime.CONTEXT)
    elif "\\" in value:
        for key in value:
            if isinstance(key, str):
                trace.delimiters[key].add(runtime.CONTEXT)
    return value


def get(field, getter, key, /, *default):
    runtime.TRACE.keys[field][key].add(runtime.CONTEXT)
    return getter(key, *default)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        modified = Inject(fullname).visit(tree)
        return compile(ast.fix_missing_locations(modified), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["_blast_escape_get"] = get
        module.__dict__["_blast_escape_observe"] = observe
        runtime.TRACE.loaded.add(module.__name__)
        super().exec_module(module)


class Finder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname not in MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (
            spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and spec.origin.startswith("/src/sqlglot/")
        ):
            spec.loader = Loader(fullname, spec.origin)
        return spec


class Trace(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.keys = defaultdict(lambda: defaultdict(set))
        self.delimiters = defaultdict(set)
        self.unknown_delimiters = set()
        self.whole = set()
        self.loaded = set()

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "loaded": sorted(self.loaded),
            "whole": sorted(self.whole),
            "unknown_delimiters": sorted(self.unknown_delimiters),
            "delimiters": {name: sorted(tests) for name, tests in self.delimiters.items()},
            "keys": {
                field: {name: sorted(tests) for name, tests in keys.items()}
                for field, keys in self.keys.items()
            },
        }, separators=(",", ":")))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    sys.meta_path.insert(0, Finder())
    return trace

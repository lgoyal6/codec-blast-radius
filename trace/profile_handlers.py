"""Attribute entry into individual registry lambdas on pinned SQLGlot."""
import ast
import importlib.abc
import importlib.machinery
import json
import sys
from collections import defaultdict
from pathlib import Path

import profile_runtime as runtime

TOOL = 4
PACKAGE = "/src/sqlglot/"


class Inject(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Dict(self, node):
        self.generic_visit(node)
        for index, value in enumerate(node.values):
            if isinstance(value, ast.Lambda):
                site = f"{self.path}:{value.lineno}:{value.col_offset}"
                node.values[index] = ast.copy_location(
                    ast.Call(ast.Name("_blast_register_handler", ast.Load()), [ast.Constant(site), value], []),
                    value,
                )
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = Path(self.path).relative_to("/src").as_posix()
        tree = ast.parse(self.get_data(self.path), self.path)
        transformed = Inject(path).visit(tree)
        return compile(ast.fix_missing_locations(transformed), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["_blast_register_handler"] = runtime.TRACE.register
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        relative = spec.origin.removeprefix(PACKAGE)
        if relative.startswith(("parsers/", "generators/")) or relative in ("parser.py", "generator.py"):
            spec.loader = Loader(spec.name, spec.origin)
        return spec


class Trace(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.sites = defaultdict(set)
        self.codes = {}
        self.monitor = sys.monitoring
        self.monitor.use_tool_id(TOOL, "sqlglot registry handlers")
        self.monitor.register_callback(TOOL, self.monitor.events.PY_START, self.enter)

    def register(self, site, handler):
        code = handler.__code__
        self.codes[code] = site
        self.sites[site]
        self.monitor.set_local_events(TOOL, code, self.monitor.events.PY_START)
        return handler

    def enter(self, code, offset):
        self.sites[self.codes[code]].add(runtime.CONTEXT)

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "handlers": {
                site: sorted(contexts) for site, contexts in sorted(self.sites.items())
            },
        }, separators=(",", ":")))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    sys.meta_path.insert(0, Finder())
    return trace

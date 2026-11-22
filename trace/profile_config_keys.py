"""Record typed keys read from baseline class configuration containers."""
import ast
import importlib.abc
import importlib.machinery
import json
import sys
from collections import defaultdict
from pathlib import Path

import profile_runtime as runtime

FIELDS = frozenset(json.loads(Path(__file__).with_name("config_key_fields.json").read_bytes()))


def tracked(node):
    return isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in FIELDS


class Inject(ast.NodeTransformer):
    def visit_Call(self, node):
        method = node.func
        if isinstance(method, ast.Attribute) and method.attr == "get" and tracked(method.value):
            field = method.value
            node.func = ast.copy_location(
                ast.Call(ast.Name("_blast_config_get", ast.Load()), [self.visit(field.value), ast.Constant(field.attr)], []),
                method,
            )
            node.args = [self.visit(value) for value in node.args]
            node.keywords = [self.visit(keyword) for keyword in node.keywords]
            return node
        return self.generic_visit(node)

    def visit_Subscript(self, node):
        field = node.value
        if (
            tracked(field) and isinstance(node.ctx, ast.Load)
            and not isinstance(node.slice, (ast.Slice, ast.Tuple))
        ):
            method = ast.Call(ast.Name("_blast_config_index", ast.Load()), [self.visit(field.value), ast.Constant(field.attr)], [])
            return ast.copy_location(ast.Call(method, [self.visit(node.slice)], []), node)
        return self.generic_visit(node)

    def visit_Compare(self, node):
        if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.In, ast.NotIn)) or not tracked(node.comparators[0]):
            return self.generic_visit(node)
        field = node.comparators[0]
        args = [self.visit(node.left), self.visit(field.value), ast.Constant(field.attr)]
        call = ast.Call(ast.Name("_blast_config_contains", ast.Load()), args, [])
        return ast.copy_location(ast.UnaryOp(ast.Not(), call) if isinstance(node.ops[0], ast.NotIn) else call, node)

    def visit_Attribute(self, node):
        if tracked(node):
            args = [self.visit(node.value), ast.Constant(node.attr)]
            return ast.copy_location(ast.Call(ast.Name("_blast_config_raw", ast.Load()), args, []), node)
        return self.generic_visit(node)


def qualified(receiver, field, container):
    cls = receiver if isinstance(receiver, type) else type(receiver)
    for parent in cls.__mro__:
        declaration = vars(parent)
        if field in declaration:
            if declaration[field] is container:
                return f"{parent.__module__}:{parent.__qualname__}:{field}"
            return None
    return None


def users(receiver, field):
    container = getattr(receiver, field)
    owner = qualified(receiver, field, container)
    if owner is None:
        runtime.TRACE.unknown[field].add(runtime.CONTEXT)
    else:
        runtime.TRACE.owners.add(owner)
    return owner, container


def record(owner, container, key):
    if owner is None:
        return
    if type(container) in (tuple, list) and type(key) is int and key < 0:
        key += len(container)
    if type(key) in (str, int):
        token = json.dumps([type(key).__name__, key], separators=(",", ":"))
        runtime.TRACE.keys[owner][token].add(runtime.CONTEXT)
    else:
        runtime.TRACE.raw[owner].add(runtime.CONTEXT)


def getter(receiver, field):
    owner, container = users(receiver, field)
    original = container.get

    def get(key, *default):
        record(owner, container, key)
        return original(key, *default)

    return get


def indexing(receiver, field):
    owner, container = users(receiver, field)

    def index(key):
        record(owner, container, key)
        return container[key]

    return index


def contains(key, receiver, field):
    owner, container = users(receiver, field)
    if type(container) is dict:
        return key in container
    if type(container) is set:
        record(owner, container, key)
    elif owner is not None:
        runtime.TRACE.raw[owner].add(runtime.CONTEXT)
    return key in container


def whole(receiver, field):
    owner, container = users(receiver, field)
    if owner is not None:
        runtime.TRACE.raw[owner].add(runtime.CONTEXT)
    return container


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        return compile(ast.fix_missing_locations(Inject().visit(tree)), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__.update(
            _blast_config_get=getter,
            _blast_config_index=indexing,
            _blast_config_contains=contains,
            _blast_config_raw=whole,
        )
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (
            spec is not None
            and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and spec.origin.startswith("/src/sqlglot/")
        ):
            spec.loader = Loader(spec.name, spec.origin)
        return spec


class Trace(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.keys = defaultdict(lambda: defaultdict(set))
        self.raw = defaultdict(set)
        self.unknown = defaultdict(set)
        self.owners = set()

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "owners": sorted(self.owners),
            "keys": {owner: {token: sorted(tests) for token, tests in keys.items()} for owner, keys in self.keys.items()},
            "raw": {name: sorted(tests) for name, tests in self.raw.items()},
            "unknown": {name: sorted(tests) for name, tests in self.unknown.items()},
        }, separators=(",", ":")))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    sys.meta_path.insert(0, Finder())
    return trace

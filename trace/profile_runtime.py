"""Record baseline operands and class reads without exercising patched code."""

import ast
import atexit
import functools
import importlib.abc
import importlib.machinery
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from enum import Enum
from operator import eq, ge, gt, is_, is_not, le, lt, ne
from pathlib import Path
from types import FunctionType

import pytest

OPERATORS = {
    "Eq": eq,
    "NotEq": ne,
    "Is": is_,
    "IsNot": is_not,
    "Lt": lt,
    "LtE": le,
    "Gt": gt,
    "GtE": ge,
}
BUILTINS = (bool, int, float, str, bytes, type(None))
CONTEXT = "@collection"
TRACE = None
EXCLUDED = ("tests/test_parser.py::TestParser::test_max_nodes",)
LOCATIONS = (
    "dialects/",
    "executor/",
    "generators/",
    "parsers/",
    "expressions/",
    "optimizer/",
    "typing/",
)


def literal(node):
    if isinstance(node, ast.Constant):
        return type(node.value) in BUILTINS
    if isinstance(node, ast.Tuple):
        return all(literal(item) for item in node.elts)
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and literal(node.operand)
    )


class Inject(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        selected = (
            len(node.ops) == 1
            and type(node.ops[0]).__name__ in OPERATORS
            and (
                isinstance(node.left, (ast.Name, ast.Attribute))
                or isinstance(node.left, ast.Call)
                and isinstance(node.left.func, ast.Name)
                and node.left.func.id == "len"
            )
            and (
                literal(node.comparators[0])
                or isinstance(node.comparators[0], (ast.Name, ast.Attribute))
            )
        )
        self.generic_visit(node)
        if not selected:
            return node
        left, right = node.left, node.comparators[0]
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        TRACE.operands[site]
        return ast.copy_location(
            ast.Call(
                ast.Name("_blast_baseline_compare", ast.Load()),
                [
                    ast.Constant(site),
                    ast.Constant(type(node.ops[0]).__name__),
                    left,
                    right,
                ],
                [],
            ),
            node,
        )

    def visit_Call(self, node):
        self.generic_visit(node)
        target = node.func
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "get"
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "numeric_escapes"
        ):
            return ast.copy_location(
                ast.Call(
                    ast.Name("_blast_baseline_mapping_get", ast.Load()),
                    [target, *node.args],
                    node.keywords,
                ),
                node,
            )
        return node

    def visit_Dict(self, node):
        self.generic_visit(node)
        for index, value in enumerate(node.values):
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                site = f"{self.path}:{value.lineno}:{value.col_offset}"
                node.values[index] = ast.copy_location(
                    ast.Call(
                        ast.Name("_blast_baseline_factory_result", ast.Load()),
                        [ast.Constant(site), value],
                        [],
                    ),
                    value,
                )
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if (
            not isinstance(node.ctx, ast.Load)
            or node.attr.startswith("__")
            or not (node.attr.isupper() or node.attr in ("arg_types", "required_args"))
        ):
            return node
        site = f"{self.path}:{node.lineno}:{node.col_offset}"
        return ast.copy_location(
            ast.Call(
                ast.Name("_blast_baseline_attribute", ast.Load()),
                [node.value, ast.Constant(node.attr), ast.Constant(site)],
                [],
            ),
            node,
        )


class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, spec):
        super().__init__(spec.name, spec.origin)

    def get_code(self, fullname):
        path = Path(self.path).relative_to("/src").as_posix()
        tree = ast.parse(self.get_data(self.path), self.path)
        return compile(
            ast.fix_missing_locations(Inject(path).visit(tree)),
            self.path,
            "exec",
            dont_inherit=True,
        )

    def exec_module(self, module):
        module.__dict__["_blast_baseline_compare"] = compare
        module.__dict__["_blast_baseline_attribute"] = attribute
        module.__dict__["_blast_baseline_mapping_get"] = mapping_get
        module.__dict__["_blast_baseline_factory_result"] = factory_result
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not isinstance(
            spec.loader, importlib.machinery.SourceFileLoader
        ):
            return spec
        if spec.origin.startswith("/src/sqlglot/") and (
            spec.origin.removeprefix("/src/sqlglot/").startswith(LOCATIONS)
            or spec.origin.rsplit("/", 1)[-1]
            in (
                "generator.py",
                "parser.py",
                "tokens.py",
                "tokenizer_core.py",
                "__init__.py",
            )
        ):
            spec.loader = Loader(spec)
        return spec


def encode(value):
    cls = type(value)
    if cls is type(None) or cls is bool:
        return [cls.__name__, value]
    if cls is int:
        return ["int", value] if value.bit_length() <= 128 else None
    if cls is str or cls is bytes:
        if len(value) > 128:
            return None
        return ["str", value] if cls is str else ["bytes", value.hex()]
    if cls is float:
        return ["float", value.hex()]
    if cls is tuple:
        if len(value) > 8 or any(type(item) not in BUILTINS for item in value):
            return None
        if any(type(item) is float and item != item for item in value):
            return None
        items = [encode(item) for item in value]
        return ["tuple", items] if all(item is not None for item in items) else None
    if isinstance(value, str) and isinstance(value, Enum):
        if cls.__eq__ is str.__eq__ and cls.__ne__ is str.__ne__:
            raw = str.__str__(value)
            return ["str-enum", raw] if len(raw) <= 128 else None
    if (
        cls.__eq__ is object.__eq__
        and cls.__ne__ is object.__ne__
        and not isinstance(value, BUILTINS + (tuple,))
    ):
        return ["identity", None]
    return None


def factory_result(site, function):
    if type(function) is not FunctionType or not function.__closure__:
        return function

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        TRACE.calls[site].add(CONTEXT)
        return function(*args, **kwargs)

    TRACE.calls[site]
    return wrapped


def compare(site, operator, left, right):
    observed = OPERATORS[operator](left, right)
    pair = TRACE.operands[site]
    if CONTEXT == "@collection":
        pair["@collection"].add("?")
    elif "?" not in pair[CONTEXT]:
        a, b = encode(left), encode(right)
        if a is None or b is None:
            pair[CONTEXT].clear()
            pair[CONTEXT].add("?")
        else:
            pair[CONTEXT].add(json.dumps([left is right, a, b], separators=(",", ":")))
            if len(pair[CONTEXT]) > 32:
                pair[CONTEXT].clear()
                pair[CONTEXT].add("?")
    return observed


@functools.cache
def provider(cls, name):
    for owner in cls.__mro__:
        if name in vars(owner):
            return f"{owner.__module__}:{owner.__qualname__}"
    return ""


def attribute(receiver, name, site):
    result = getattr(receiver, name)
    owner = provider(receiver if isinstance(receiver, type) else type(receiver), name)
    if owner.startswith("sqlglot."):
        TRACE.reads[(owner, name)].add(CONTEXT)
        if CONTEXT == "@collection" and name == "arg_types":
            TRACE.schema_initialization[owner].add(site)
        if name == "NUMERIC_ESCAPES" and type(result) is dict:
            _, owners = TRACE.mappings.setdefault(id(result), (result, set()))
            owners.add(owner)
    return result


def mapping_get(getter, key, *default):
    result = getter(key, *default)
    table = TRACE.mappings.get(id(getter.__self__))
    if table and type(key) is str:
        _, owners = table
        for owner in owners:
            TRACE.mapping_reads[owner][key].add(CONTEXT)
    return result


class Trace:
    def __init__(self, path):
        self.path = path
        self.operands = defaultdict(lambda: defaultdict(set))
        self.reads = defaultdict(set)
        self.schema_initialization = defaultdict(set)
        self.mappings = {}
        self.mapping_reads = defaultdict(lambda: defaultdict(set))
        self.seen = set()
        self.calls = defaultdict(set)

    def save(self):
        self.path.write_text(
            json.dumps(
                {
                    "seen": sorted(self.seen),
                    "calls": {
                        site: sorted(tests) for site, tests in self.calls.items()
                    },
                    "mapping_reads": {
                        owner: {key: sorted(tests) for key, tests in entries.items()}
                        for owner, entries in self.mapping_reads.items()
                    },
                    "mapping_owners": sorted(
                        {
                            owner
                            for _, owners in self.mappings.values()
                            for owner in owners
                        }
                    ),
                    "schema_initialization": {
                        owner: sorted(sites)
                        for owner, sites in self.schema_initialization.items()
                    },
                    "operands": {
                        site: {name: sorted(pairs) for name, pairs in values.items()}
                        for site, values in self.operands.items()
                    },
                    "reads": {
                        owner + ":" + attr: sorted(names)
                        for (owner, attr), names in self.reads.items()
                    },
                },
                separators=(",", ":"),
            )
        )

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        global CONTEXT
        self.seen.add(item.nodeid)
        CONTEXT = item.nodeid
        try:
            return (yield)
        finally:
            CONTEXT = "@collection"


def start(path):
    global TRACE
    TRACE = Trace(path)
    sys.meta_path.insert(0, Finder())
    return TRACE


def worker(path, context, function, args, kwargs):
    global CONTEXT
    if TRACE is None:
        active = start(path.with_name(f"{path.stem}.worker-{os.getpid()}.json"))
        atexit.register(active.save)
    CONTEXT = context
    return function(*args, **kwargs)


def main():
    path = Path(sys.argv[1]).resolve()
    active = TRACE or start(path)
    active.path = path
    submit = ProcessPoolExecutor.submit

    def instrument(pool, function, /, *args, **kwargs):
        return submit(pool, worker, path, CONTEXT, function, args, kwargs)

    ProcessPoolExecutor.submit = instrument
    args = sys.argv[2:] or ["tests", *(f"--deselect={name}" for name in EXCLUDED)]
    result = pytest.main(
        ["-q", "--tb=short", "--timeout=420", "-p", "no:cacheprovider", *args],
        plugins=[active],
    )
    active.save()
    return result


if __name__ == "__main__":
    raise SystemExit(main())

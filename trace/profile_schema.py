"""Observe argument states when the pinned expression validator reads a schema."""

import ast
import atexit
import importlib.abc
import importlib.machinery
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from pathlib import Path

import pytest

CONTEXT = "@collection"
TRACE = None
STRESS = "tests/test_parser.py::TestParser::test_max_nodes"
MODULE = "sqlglot.expressions.core"


class ValidatorReads(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        if node.name != "error_messages":
            return node
        return self.generic_visit(node)

    def visit_Attribute(self, node):
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == "required_args"
        ):
            return ast.copy_location(
                ast.Call(
                    ast.Name("_observe_required_args", ast.Load()), [node.value], []
                ),
                node,
            )
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        return compile(
            ast.fix_missing_locations(ValidatorReads().visit(tree)), self.path, "exec"
        )

    def exec_module(self, module):
        module.__dict__["_observe_required_args"] = required_args
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname == MODULE:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and isinstance(
                spec.loader, importlib.machinery.SourceFileLoader
            ):
                spec.loader = Loader(spec.name, spec.origin)
            return spec
        return None


@cache
def schema_owner(cls):
    for ancestor in cls.__mro__:
        if "arg_types" in vars(ancestor):
            return f"{ancestor.__module__}:{ancestor.__qualname__}"
    return None


def required_args(receiver):
    current = receiver.required_args
    owner = schema_owner(type(receiver))
    if owner is None:
        TRACE.unknown.add(CONTEXT)
        return current
    entry = TRACE.states[owner]
    if CONTEXT in entry and entry[CONTEXT] is None:
        return current
    schema, args = receiver.arg_types, receiver.args
    if type(schema) is not dict or type(args) is not dict or type(current) is not set:
        entry[CONTEXT] = None
        return current
    if not all(type(key) is str and type(flag) is bool for key, flag in schema.items()):
        entry[CONTEXT] = None
        return current
    if current != {key for key, flag in schema.items() if flag}:
        entry[CONTEXT] = None
        return current
    # A changed required set can reorder old diagnostics, so retain a test that
    # already produces a missing-argument diagnostic even for a different key.
    missing = set()
    for key in schema:
        value = args.get(key)
        if isinstance(value, list) and type(value) is not list:
            entry[CONTEXT] = None
            return current
        if value is None or (type(value) is list and not value):
            missing.add(key)
    if missing & current:
        entry[CONTEXT] = None
    else:
        entry.setdefault(CONTEXT, set()).update(missing)
    return current


class Capture:
    def __init__(self, path):
        self.path = path
        self.states = defaultdict(dict)
        self.seen = set()
        self.unknown = set()

    def save(self):
        self.path.write_text(
            json.dumps(
                {
                    "seen": sorted(self.seen),
                    "unknown": sorted(self.unknown),
                    "states": {
                        owner: {
                            name: None if keys is None else sorted(keys)
                            for name, keys in sorted(tests.items())
                        }
                        for owner, tests in sorted(self.states.items())
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
    TRACE = Capture(path)
    sys.meta_path.insert(0, Finder())
    return TRACE


def worker(path, context, target, args, kwargs):
    global CONTEXT
    if TRACE is None:
        active = start(path.with_name(f"{path.stem}.worker-{os.getpid()}.json"))
        atexit.register(active.save)
    CONTEXT = context
    try:
        return target(*args, **kwargs)
    finally:
        CONTEXT = "@collection"


def main():
    path = Path(sys.argv[1]).resolve()
    active = TRACE or start(path)
    active.path = path
    submit = ProcessPoolExecutor.submit

    def instrument(pool, function, /, *args, **kwargs):
        return submit(pool, worker, path, CONTEXT, function, args, kwargs)

    ProcessPoolExecutor.submit = instrument
    tests = sys.argv[2:] or ["tests", f"--deselect={STRESS}"]
    code = pytest.main(
        ["-q", "--tb=short", "--timeout=480", "-p", "no:cacheprovider", *tests],
        plugins=[active],
    )
    active.save()
    return code

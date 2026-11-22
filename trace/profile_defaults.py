"""Record which default-valued arguments and signature reads the baseline observes."""

import ast
import atexit
import functools
import importlib.abc
import importlib.machinery
import inspect
import json
import os
import sys
import types
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

TRACE = None
CONTEXT = "@collection"
STRESS = "tests/test_parser.py::TestParser::test_max_nodes"
SOURCE = "/src/"
SCALARS = (bool, int, float, str, bytes, type(None))


def defaults(arguments):
    positional = arguments.posonlyargs + arguments.args
    pairs = zip(
        positional[len(positional) - len(arguments.defaults) :], arguments.defaults
    )
    keywords = zip(arguments.kwonlyargs, arguments.kw_defaults)
    return {
        argument.arg: value
        for argument, value in (*pairs, *keywords)
        if isinstance(value, ast.Constant) and type(value.value) in SCALARS
    }


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.path.removeprefix(SOURCE)
        tree = ast.parse(self.get_data(self.path), self.path)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.decorator_list:
                continue
            values = defaults(node.args)
            if not values:
                continue
            first = node.body[0]
            doc = (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            )
            position = int(doc)
            marker = ast.Expr(
                ast.Call(
                    ast.Name("_observe_literal_defaults", ast.Load()),
                    [
                        ast.Constant(f"{path}:{node.name}"),
                        ast.Tuple([ast.Constant(name) for name in values], ast.Load()),
                        ast.Tuple(
                            [ast.Name(name, ast.Load()) for name in values], ast.Load()
                        ),
                        ast.Tuple(list(values.values()), ast.Load()),
                    ],
                    [],
                )
            )
            node.body.insert(position, ast.copy_location(marker, first))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")

    def exec_module(self, module):
        module.__dict__["_observe_literal_defaults"] = arguments
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
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


def arguments(key, names, actual, original):
    TRACE.called[key].add(CONTEXT)
    for name, received, default in zip(names, actual, original):
        if type(received) is type(default) and (
            received is default or received == default
        ):
            TRACE.matches[key][name].add(CONTEXT)


def inspect_observer(original):
    @functools.wraps(original)
    def observed(cls, func, *args, **kwargs):
        if type(func) is types.FunctionType:
            filename = func.__code__.co_filename
            if filename.startswith("/src/sqlglot/"):
                key = f"{filename.removeprefix(SOURCE)}:{func.__qualname__}"
                TRACE.metadata[key].add(CONTEXT)
        return original(cls, func, *args, **kwargs)

    return observed


class Capture:
    def __init__(self, path):
        self.path = path
        self.seen = set()
        self.called = defaultdict(set)
        self.matches = defaultdict(lambda: defaultdict(set))
        self.metadata = defaultdict(set)

    def save(self):
        self.path.write_text(
            json.dumps(
                {
                    "seen": sorted(self.seen),
                    "called": {
                        name: sorted(tests)
                        for name, tests in sorted(self.called.items())
                    },
                    "matches": {
                        name: {
                            field: sorted(tests)
                            for field, tests in sorted(values.items())
                        }
                        for name, values in sorted(self.matches.items())
                    },
                    "metadata": {
                        name: sorted(tests)
                        for name, tests in sorted(self.metadata.items())
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
    inspect._signature_from_function = inspect_observer(
        inspect._signature_from_function
    )
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

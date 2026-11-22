"""Capture baseline requests for project modules and cached SQLGlot dialects."""

import atexit
import builtins
import functools
import importlib
import importlib.util
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
DIALECTS = "sqlglot.dialects"


def record(name):
    if name.startswith("sqlglot."):
        TRACE.requests[name].add(CONTEXT)


def imports(name, globals=None, locals=None, fromlist=(), level=0):
    package = globals.get("__package__") if globals else None
    if level and package:
        record(importlib.util.resolve_name("." * level + name, package))
    elif not level:
        record(name)
    if name == DIALECTS:
        for member in fromlist or ():
            request(member.lower())
    return TRACE.original_import(name, globals, locals, fromlist, level)


def import_module(name, package=None):
    record(
        importlib.util.resolve_name(name, package)
        if name.startswith(".") and package
        else name
    )
    return TRACE.original_import_module(name, package)


def request(dialect):
    if isinstance(dialect, str) and dialect in TRACE.dialects:
        record(f"{DIALECTS}.{dialect}")


def observe_registry():
    from sqlglot.dialects import DIALECT_MODULE_NAMES
    from sqlglot.dialects.dialect import _Dialect

    TRACE.dialects = frozenset(DIALECT_MODULE_NAMES)

    def observe_lookup(function):
        @functools.wraps(function)
        def observed(cls, key, *args, **kwargs):
            request(key)
            return function(cls, key, *args, **kwargs)

        return classmethod(observed)

    for name in ("get", "__getitem__"):
        setattr(_Dialect, name, observe_lookup(vars(_Dialect)[name].__func__))

    getter = vars(_Dialect)["classes"].fget

    @functools.wraps(getter)
    def classes(cls):
        for dialect in TRACE.dialects:
            request(dialect)
        return getter(cls)

    _Dialect.classes = property(classes)


class Capture:
    def __init__(self, path):
        self.path = path
        self.requests = defaultdict(set)
        self.dialects = frozenset()
        self.seen = set()
        self.stable_imports = set()
        self.original_import = builtins.__import__
        self.original_import_module = importlib.import_module

    def save(self):
        self.path.write_text(
            json.dumps(
                {
                    "seen": sorted(self.seen),
                    "requests": {
                        module: sorted(contexts)
                        for module, contexts in sorted(self.requests.items())
                    },
                    "stable_imports": sorted(self.stable_imports),
                },
                separators=(",", ":"),
            )
        )

    def pytest_collection_finish(self, session):
        # Only initialized stdlib leaf modules with ordinary attribute access.
        self.stable_imports = {
            name
            for name, module in sys.modules.copy().items()
            if name in sys.stdlib_module_names
            and type(module) is types.ModuleType
            and not {"__getattr__", "__path__"}.intersection(vars(module))
        }

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
    builtins.__import__ = imports
    importlib.import_module = import_module
    observe_registry()
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

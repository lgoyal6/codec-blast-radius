"""Measure optimizer source reached outside the replayable optimization call."""

import atexit
import functools
import importlib.machinery
import json
import os
import sys
import threading
from collections import defaultdict
from pathlib import Path

if os.environ.get("BLAST_OPTIMIZER_PHASES") != "1":
    raise RuntimeError("this site hook is only for the TPC-DS phase capture")

PREFIX = "/src/sqlglot/optimizer/"
TARGET = "sqlglot.optimizer.optimizer"
TOOL = 3
local = threading.local()
inside = defaultdict(set)
outside = defaultdict(set)
calls = []


def observe_line(code, lineno):
    filename = code.co_filename
    if not filename.startswith(PREFIX):
        return sys.monitoring.DISABLE
    bucket = inside if getattr(local, "depth", 0) else outside
    bucket[filename.removeprefix("/src/")].add(lineno)


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        super().exec_module(module)
        original = module.optimize

        @functools.wraps(original)
        def measured(expression, schema=None, *args, **kwargs):
            earlier = getattr(local, "depth", 0)
            calls.append({
                "type": type(expression).__name__,
                "schema": type(schema).__name__,
                "positional": len(args),
                "kwargs": {key: repr(value) for key, value in kwargs.items()},
            })
            local.depth = earlier + 1
            try:
                return original(expression, schema, *args, **kwargs)
            finally:
                local.depth = earlier

        module.optimize = measured


class Finder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname != TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.origin == PREFIX + "optimizer.py":
            spec.loader = Loader(fullname, spec.origin)
        return spec


def save():
    output = Path("/phase-reports") / f"{os.getpid()}.json"
    output.write_text(json.dumps({
        "inside": {path: sorted(lines) for path, lines in inside.items()},
        "outside": {path: sorted(lines) for path, lines in outside.items()},
        "calls": calls,
    }))


sys.meta_path.insert(0, Finder())
sys.monitoring.use_tool_id(TOOL, "TPC-DS optimizer phases")
sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, observe_line)
sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE)
atexit.register(save)

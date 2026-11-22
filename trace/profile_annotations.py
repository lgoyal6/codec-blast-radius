"""Observe calls to CPython's annotation and signature inspection functions."""

import inspect
import json
import sys
import typing
from collections import defaultdict
from pathlib import Path

import profile_runtime as runtime

TOOLS = {
    inspect.get_annotations: "get_annotations",
    inspect.signature: "signature",
    inspect.getfullargspec: "getfullargspec",
    typing.get_type_hints: "get_type_hints",
}
MONITOR = 4


class Trace(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.sites = defaultdict(set)
        self.functions = {function.__code__: (name, function.__code__.co_varnames[0])
                          for function, name in TOOLS.items()}
        monitoring = sys.monitoring
        monitoring.use_tool_id(MONITOR, "inspect annotation readers")
        monitoring.register_callback(MONITOR, monitoring.events.PY_START, self.observe)
        for code in self.functions:
            monitoring.set_local_events(MONITOR, code, monitoring.events.PY_START)

    def observe(self, code, offset):
        kind, parameter = self.functions[code]
        frame = sys._getframe(1)
        receiver = frame.f_locals[parameter]
        owner = getattr(receiver, "__module__", None)
        qualified = getattr(receiver, "__qualname__", None)
        if not isinstance(owner, str) or not owner.startswith("sqlglot") or not isinstance(qualified, str):
            return
        caller = frame.f_back
        while caller is not None:
            path = caller.f_code.co_filename
            if path.startswith("/src/"):
                filename = path.removeprefix("/src/")
                site = f"{filename}:{caller.f_lineno}"
                self.sites[(kind, owner, qualified, site)].add(runtime.CONTEXT)
                return
            caller = caller.f_back
        self.sites[(kind, owner, qualified, "?")].add(runtime.CONTEXT)

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "readers": [[*key, sorted(contexts)] for key, contexts in sorted(self.sites.items())],
        }, separators=(",", ":")))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    return trace

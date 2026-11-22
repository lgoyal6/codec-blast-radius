"""Observe attempted imports and filesystem access in the pinned package."""

import builtins
import functools
import importlib
import importlib.util
import json
import os
import sys
from collections import defaultdict

import profile_runtime as runtime

PACKAGE = "/src/sqlglot"
MODULE = "sqlglot"


class Capture(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.requested = set()
        self.looked_up = set()
        self.enumerated = set()

    def module(self, name):
        if isinstance(name, str) and name.startswith(MODULE + "."):
            self.requested.add(name.split(".", 2)[1])

    def filesystem(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return ""
        path = os.fsdecode(path)
        if not path.startswith((PACKAGE, "sqlglot")):
            return ""
        return os.path.abspath(path)

    def lookup(self, path):
        absolute = self.filesystem(path)
        if os.path.dirname(absolute) != PACKAGE:
            return
        filename = os.path.basename(absolute)
        self.looked_up.add(filename.removesuffix(".py"))

    def audit(self, name, args):
        if name == "open":
            self.lookup(args[0])
        elif name == "import":
            self.module(args[0])
        elif name in ("os.listdir", "os.scandir") and (
            isinstance(args[0], int) or self.filesystem(args[0]) == PACKAGE
        ):
            caller = sys._getframe(1).f_globals.get("__name__")
            if caller != "importlib._bootstrap_external":
                self.enumerated.add(runtime.CONTEXT)

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "requested": sorted(self.requested),
            "looked_up": sorted(self.looked_up),
            "enumerated": sorted(self.enumerated),
        }, separators=(",", ":")))


def start(path):
    capture = Capture(path)
    runtime.TRACE = capture
    original_import = builtins.__import__

    @functools.wraps(original_import)
    def import_observed(name, globals=None, locals=None, fromlist=(), level=0):
        package = globals.get("__package__") if globals else None
        absolute = importlib.util.resolve_name("." * level + name, package) if level and package else name
        if absolute == MODULE:
            for item in fromlist or ():
                if isinstance(item, str):
                    capture.module(f"{MODULE}.{item}")
        capture.module(absolute)
        return original_import(name, globals, locals, fromlist, level)

    def resolve(original):
        @functools.wraps(original)
        def observed(name, package=None):
            absolute = importlib.util.resolve_name(name, package) if name.startswith(".") and package else name
            capture.module(absolute)
            return original(name, package)
        return observed

    class ObserveFinder:
        def find_spec(self, fullname, path=None, target=None):
            capture.module(fullname)
            return None

    def stat(original):
        @functools.wraps(original)
        def observed(path, *args, **kwargs):
            capture.lookup(path)
            return original(path, *args, **kwargs)
        return observed

    builtins.__import__ = import_observed
    importlib.import_module = resolve(importlib.import_module)
    importlib.util.find_spec = resolve(importlib.util.find_spec)
    os.stat = stat(os.stat)
    os.lstat = stat(os.lstat)
    os.access = stat(os.access)
    sys.meta_path.insert(0, ObserveFinder())
    sys.addaudithook(capture.audit)
    return capture


def main():
    runtime.start = start
    return runtime.main()

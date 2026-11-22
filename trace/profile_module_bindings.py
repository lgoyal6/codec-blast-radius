"""Record external reads of module bindings in the pinned baseline."""

import dis
import importlib.machinery
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType

import profile_runtime as runtime

TARGETS = {name: frozenset(keys) for name, keys in
           json.loads(Path(os.environ["BLAST_MODULE_TARGETS"]).read_bytes()).items()}


class ObservedModule(ModuleType):
    def __getattribute__(self, name):
        module = ModuleType.__getattribute__(self, "__name__")
        fields = TARGETS.get(module, ())
        if name in fields or name == "__dict__":
            caller = sys._getframe(1)
            code = caller.f_code
            context = runtime.CONTEXT
            imports_binding = (
                name != "__dict__" and caller.f_lasti >= 0
                and dis.opname[code.co_code[caller.f_lasti]] == "IMPORT_FROM"
            )
            compiling_module = (
                name == "__dict__" and code.co_name == "exec_module"
                and code.co_filename == "<frozen importlib._bootstrap_external>"
            )
            if not (imports_binding or compiling_module):
                runtime.TRACE.reads[module][name].add(context)
        return ModuleType.__getattribute__(self, name)


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        module.__class__ = ObservedModule
        super().exec_module(module)


class Finder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname not in TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (
            spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and spec.origin.startswith("/src/sqlglot/")
        ):
            spec.loader = Loader(fullname, spec.origin)
            runtime.TRACE.loaded.add(fullname)
        return spec


class Trace(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.loaded = set()
        self.reads = defaultdict(lambda: defaultdict(set))

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen), "loaded": sorted(self.loaded),
            "reads": {module: {name: sorted(tests) for name, tests in fields.items()}
                      for module, fields in self.reads.items()},
        }, separators=(",", ":")))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    sys.meta_path.insert(0, Finder())
    return trace

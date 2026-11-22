"""Index module dependency and request evidence from the pinned baseline."""

import ast
import json
from collections import defaultdict
from pathlib import PurePosixPath

from build_map import python_files, static_import_graph

COLLECTION = "@collection"
FAMILIES = ("dialects", "parsers", "generators", "typing")


def import_profile(source, reports, baseline):
    if not reports:
        return {}

    requests = defaultdict(set)
    preregistered = []
    seen = set()
    for path in reports:
        report = json.loads(path.read_text())
        seen.update(report["seen"])
        if report["stable_imports"]:
            preregistered.append(set(report["stable_imports"]))
        for name, contexts in report["requests"].items():
            requests[name].update(contexts)
    stable = set.intersection(*preregistered) if preregistered else set()
    if not stable:
        return {}

    modules = python_files(source)
    callers = defaultdict(set)
    for module, imports in static_import_graph(modules).items():
        for dependency in imports:
            callers[dependency].add(module)

    def key(path):
        return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")

    demands = {}
    for module in sorted(modules):
        scope = baseline.files.get(module)
        if scope is None:
            continue
        demand = scope.imports | scope.executed | scope.readers
        for context in requests.get(key(module), ()):
            demand |= baseline.indices.get(context, 0)
        for cls in modules[module].body:
            if isinstance(cls, ast.ClassDef):
                demand |= baseline.owners.get(f"{key(module)}:{cls.name}", 0)
                demand |= baseline.references.get(cls.name, 0)
        demands[module] = demand

    unexplored = baseline.all
    for test_id in seen:
        unexplored &= ~baseline.indices.get(test_id, 0)

    lazy = {}
    for path, file in baseline.files.items():
        parts = PurePosixPath(path).parts
        if (
            len(parts) != 3
            or parts[0] != "sqlglot"
            or parts[1] not in FAMILIES
            or not path.endswith(".py")
            or file.collection
            or not file.executed
            or COLLECTION in requests.get(key(path), ())
        ):
            continue
        peers = {f"sqlglot/{directory}/{parts[-1]}" for directory in FAMILIES}
        pending = [path, *sorted(peers.intersection(demands))]
        visited = set()
        demand = unexplored
        while pending:
            caller = pending.pop()
            if caller in visited:
                continue
            visited.add(caller)
            demand |= demands.get(caller, 0)
            pending.extend(callers.get(caller, ()))
        if demand:
            lazy[path] = format(demand, "x")
    return {"stable_stdlib_modules": sorted(stable), "lazy_module_users": lazy}

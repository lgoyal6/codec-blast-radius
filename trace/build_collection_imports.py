"""Attribute collection-time module requests to baseline test files."""

import json
from collections import defaultdict

from build_map import COLLECTION, union


def module_key(path):
    return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def reverse_imports(callers, paths):
    reached = set(paths)
    pending = list(reached)
    while pending:
        next_paths = callers.get(pending.pop(), set()) - reached
        reached.update(next_paths)
        pending.extend(next_paths)
    return reached


def collection_import_users(baseline, callers, families, requests, unknown, existing, reports):
    if not reports:
        return {}
    if len(reports) != 2:
        raise ValueError("expected separate forward and reverse collection traces")

    file_requests = defaultdict(set)
    unowned = set()
    for path in reports:
        for name, contexts in json.loads(path.read_text())["requests"].items():
            if COLLECTION in contexts:
                unowned.add(name)
            for context in contexts:
                if context.startswith("@file:"):
                    file_requests[name].add(context.removeprefix("@file:"))

    found = {}
    for path, file in baseline.files.items():
        parts = path.split("/")
        if (
            len(parts) != 3
            or parts[0] != "sqlglot"
            or parts[1] not in families
            or path in existing
            or not file.collection
            or module_key(path) not in file_requests
        ):
            continue
        upstream = reverse_imports(callers, [path])
        keys = {module_key(caller) for caller in upstream}
        if keys & unowned:
            continue
        collectors = set().union(*(file_requests[name] for name in keys))
        affected = reverse_imports(callers, collectors)
        paths = upstream | {f"sqlglot/{family}/{parts[-1]}" for family in families}
        demand = unknown | union(
            baseline.files[name].owns for name in affected if name in baseline.files
        )
        for name in paths:
            scope = baseline.files.get(name)
            if scope is not None:
                demand |= scope.imports | scope.readers | scope.executed | scope.owns
            demand |= union(baseline.indices.get(item, 0) for item in requests.get(module_key(name), ()))
        if demand and demand != baseline.all:
            found[path] = format(demand, "x")
    return found

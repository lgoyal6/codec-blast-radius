"""Index observed missing arguments and validation contexts from the pinned source."""

import ast
import json
from collections import defaultdict

COLLECTION = "@collection"
CORE = "sqlglot/expressions/core.py"


def validator_is_only_reader(source):
    """Reject unknown readers of the derived required-argument set."""
    readers = []
    for path in (source / "sqlglot").rglob("*.py"):
        tree = ast.parse(path.read_bytes())
        validator_reads = {
            node.iter
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            and function.name == "error_messages"
            for node in ast.walk(function)
            if isinstance(node, ast.For)
            and isinstance(node.iter, ast.Attribute)
            and isinstance(node.iter.value, ast.Name)
            and node.iter.value.id == "self"
            and node.iter.attr == "required_args"
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "required_args"
                and isinstance(node.ctx, ast.Load)
            ):
                readers.append(
                    (path.relative_to(source).as_posix(), node in validator_reads)
                )
    return readers == [(CORE, True)]


def build_schema(source, reports, eligible, baseline):
    if not reports or not validator_is_only_reader(source):
        return {}
    states = defaultdict(dict)
    seen = set()
    unknown = set()
    for report in reports:
        data = json.loads(report.read_text())
        seen.update(data["seen"])
        unknown.update(data["unknown"])
        for owner, tests in data["states"].items():
            if owner not in eligible:
                continue
            entries = states[owner]
            for test, keys in tests.items():
                current = entries.setdefault(test, set())
                if keys is None:
                    entries[test] = None
                elif current is not None:
                    current.update(keys)
    if COLLECTION in unknown:
        return {}
    keep = (set(baseline.indices) - seen) | unknown
    keep_mask = 0
    for name in keep:
        keep_mask |= baseline.indices.get(name, 0)
    result = {}
    for owner, tests in sorted(states.items()):
        collection = tests.get(COLLECTION, ())
        if collection is None:
            continue
        uncertain = keep_mask
        missing = defaultdict(int)
        for name, keys in tests.items():
            if name == COLLECTION:
                continue
            bit = baseline.indices.get(name, 0)
            if keys is None:
                uncertain |= bit
            else:
                for key in keys:
                    missing[key] |= bit
        result[owner] = [
            sorted(collection),
            format(uncertain, "x"),
            {key: format(mask, "x") for key, mask in sorted(missing.items())},
        ]
    return result

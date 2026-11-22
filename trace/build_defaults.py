"""Index baseline default argument use and signature readers."""

import ast
import json
from collections import defaultdict

from build_map import python_files, union
from build_factory_defaults import factory_default_profile

COLLECTION = "@collection"
SPECIAL_ATTRIBUTES = {
    "__defaults__",
    "__kwdefaults__",
    "f_locals",
    "gi_frame",
    "cr_frame",
}


FACTORY_ATTRIBUTES = SPECIAL_ATTRIBUTES | {"__closure__", "cell_contents", "getclosurevars"}


def direct_readers(trees, baseline, attributes):
    hits = 0
    for path, tree in trees.items():
        file = baseline.files.get(path)
        if file is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.name
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            else:
                continue
            if name in attributes:
                if node.lineno in file.collection:
                    return baseline.all
                hits |= file.lines.get(node.lineno, 0)
    return hits


def default_profile(source, reports, baseline, registry_calls):
    if not reports:
        return {}
    seen = set()
    called = defaultdict(set)
    matches = defaultdict(lambda: defaultdict(set))
    metadata = defaultdict(set)
    for path in reports:
        report = json.loads(path.read_text())
        seen.update(report["seen"])
        for function, contexts in report["called"].items():
            called[function].update(contexts)
        for function, fields in report["matches"].items():
            for field, contexts in fields.items():
                matches[function][field].update(contexts)
        for function, contexts in report["metadata"].items():
            metadata[function].update(contexts)

    def mask(contexts):
        return union(baseline.indices.get(test, 0) for test in contexts)

    evidence = {}
    for name, tests in sorted(called.items()):
        readers = metadata.get(name, ())
        collection = COLLECTION in tests or COLLECTION in readers
        fields = {
            field: format(mask(users), "x")
            for field, users in sorted(matches[name].items())
        }
        evidence[name] = [
            format(mask(tests), "x"),
            format(baseline.all if collection else mask(readers), "x"),
            fields,
        ]
    trees = python_files(source)
    return {
        "seen": format(mask(seen), "x"),
        "direct": format(direct_readers(trees, baseline, SPECIAL_ATTRIBUTES), "x"),
        "functions": evidence,
        "factory_readers": format(direct_readers(trees, baseline, FACTORY_ATTRIBUTES), "x"),
        "factory_defaults": factory_default_profile(trees, registry_calls, metadata, baseline),
    }

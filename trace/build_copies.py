"""Compile positive ownership witnesses from unmodified baseline runs."""

import json
from collections import defaultdict

from build_map import COLLECTION, union


def copy_profile(reports, baseline):
    observations = defaultdict(dict)
    for path in reports:
        for site, tests in json.loads(path.read_text())["fresh_copy"].items():
            contexts = observations[site]
            for test, safe in tests.items():
                contexts[test] = contexts.get(test, True) and safe

    result = defaultdict(dict)
    for site, tests in sorted(observations.items()):
        if COLLECTION in tests:
            continue
        users = union(baseline.indices.get(test, 0) for test, safe in tests.items() if safe)
        if users:
            path, line, column = site.rsplit(":", 2)
            result[path][f"{line}:{column}"] = format(users, "x")
    return dict(result)

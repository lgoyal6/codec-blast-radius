"""Compile literal-safe comparison operands seen during baseline collection."""

import argparse
import gzip
import json
from collections import defaultdict
from pathlib import Path

COLLECTION = "@collection"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--forward", type=Path, required=True)
    parser.add_argument("--reverse", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    ids = {test: 1 << index for index, test in enumerate(profile["ids"])}
    collected = []
    for path in (args.forward, args.reverse):
        raw = json.loads(path.read_bytes())["operands"]
        collected.append({
            site: set(contexts[COLLECTION]) for site, contexts in raw.items()
            if contexts.get(COLLECTION)
        })
    forward, reverse = collected
    sites = {site for site in forward.keys() & reverse.keys() if "?" not in forward[site] | reverse[site]}

    seen = set()
    submitted, completed = {}, defaultdict(set)
    runtime = defaultdict(lambda: defaultdict(set))
    for path in args.runtime:
        raw = json.loads(path.read_bytes())
        seen.update(raw["seen"])
        session = path.name.split(".", 1)[0]
        if raw["submitted"]:
            if session in submitted:
                raise ValueError("multiple submission reports for a baseline session")
            submitted[session] = raw["submitted"]
        completed[session].update(raw["completed"])
        for site, test_values in raw["operands"].items():
            if site in sites:
                for test, facts in test_values.items():
                    runtime[site][test].update(facts)
    if ids.keys() - seen or not submitted:
        raise ValueError("expected a full baseline, parser stress and worker submission census")
    incomplete = {
        test for session, tests in submitted.items()
        for task, test in enumerate(tests) if task not in completed[session]
    }
    if COLLECTION in incomplete:
        raise ValueError("a collection worker was not observed before it loaded SQLGlot")
    keep = 0
    for test in incomplete:
        keep |= ids.get(test, 0)

    output = defaultdict(lambda: defaultdict(dict))
    for site, test_values in runtime.items():
        actual = test_values.pop(COLLECTION, set())
        if not actual or "?" in actual:
            continue
        initial = forward[site] | reverse[site] | actual
        file, line, col = site.rsplit(":", 2)
        groups = defaultdict(int)
        reached = keep
        for test, facts in test_values.items():
            bit = ids.get(test, 0)
            reached |= bit
            if bit and not bit & keep and facts and "?" not in facts:
                groups[tuple(sorted(facts))] |= bit
        output[file][line][col] = [
            sorted(initial), format(reached, "x"),
            [[format(value, "x"), facts] for facts, value in sorted(groups.items())],
        ]
    if not output:
        raise ValueError("no consistent comparison capture")
    profile["collection_operands"] = output
    with args.output.open("wb") as target, gzip.GzipFile(
        filename="", fileobj=target, mode="wb", mtime=0,
    ) as stream:
        stream.write(json.dumps(profile, separators=(",", ":")).encode())
    print(sum(len(rows) for file in output.values() for rows in file.values()), "comparison sites")
    print(sum(map(len, submitted.values())), "submitted tasks;", len(incomplete), "test contexts kept due to incomplete worker capture")


if __name__ == "__main__":
    main()

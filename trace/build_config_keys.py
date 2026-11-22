"""Attach class configuration key reads from a pinned baseline trace."""

import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    seen, owners = set(), set()
    lookups = defaultdict(lambda: defaultdict(set))
    raw, unknown = defaultdict(set), defaultdict(set)
    for path in args.reports:
        report = json.loads(path.read_bytes())
        seen.update(report["seen"])
        owners.update(report["owners"])
        for owner, keys in report["keys"].items():
            for key, tests in keys.items():
                lookups[owner][key].update(tests)
        for field, tests in report["raw"].items():
            raw[field].update(tests)
        for field, tests in report["unknown"].items():
            unknown[field].update(tests)
    indices = {test: 1 << index for index, test in enumerate(profile["ids"])}
    unseen = set(indices) - seen
    previously_seen = int(profile["seen"], 16)
    unseen_in_both = {test for test in unseen if not indices[test] & previously_seen}
    allowed = set(profile["runtime_literal_containers"])

    def mask(tests):
        return sum(indices[test] for test in set(tests) if test in indices)

    tables = {}
    for owner in sorted(owners):
        field = owner.rsplit(":", 1)[1]
        if field not in allowed or owner not in profile["attributes"]:
            continue
        keys = lookups[owner]
        whole = raw[owner] | unknown[field]
        observed = whole.union(*keys.values())
        if "@collection" in observed or not keys:
            continue
        broad = int(profile["attributes"][owner], 16)
        keep = mask(whole | unseen_in_both) | (broad & ~mask(observed))
        tables[owner] = [format(keep, "x"), {key: format(mask(tests), "x") for key, tests in sorted(keys.items())}]

    profile["configuration_keys"] = tables
    with (
        args.output.open("wb") as target,
        gzip.GzipFile(filename="", fileobj=target, mode="wb", mtime=0) as output,
    ):
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(f"{len(tables)} containers, {len(unseen)} tests absent from the key trace")


if __name__ == "__main__":
    main()

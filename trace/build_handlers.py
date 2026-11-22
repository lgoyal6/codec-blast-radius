"""Add baseline call evidence for inline registry lambdas to a semantic profile."""
import argparse
from collections import defaultdict
import gzip
import json
from pathlib import Path


def build(profile, reports):
    masks = {test: 1 << index for index, test in enumerate(profile["ids"])}
    seen = set()
    sites = defaultdict(set)
    for report in reports:
        raw = json.loads(report.read_bytes())
        seen.update(raw["seen"])
        for site, tests in raw["handlers"].items():
            sites[site].update(tests)

    def mask(test_ids):
        return format(sum(masks.get(test, 0) for test in test_ids), "x")

    by_file = defaultdict(dict)
    for key, tests in sorted(sites.items()):
        if "@collection" not in tests:
            path, line, column = key.rsplit(":", 2)
            by_file[path][f"{line}:{column}"] = mask(tests)
    profile["handler_profile"] = {"seen": mask(seen), "sites": by_file}
    return len(seen & masks.keys()), sum(len(entries) for entries in by_file.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    options = parser.parse_args()
    profile = json.loads(gzip.decompress(options.profile.read_bytes()))
    seen, sites = build(profile, options.reports)
    with (
        options.output.open("wb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as stream,
    ):
        stream.write(json.dumps(profile, separators=(",", ":")).encode())
    print(f"{seen} tests observed across {sites} handler sites")


if __name__ == "__main__":
    main()

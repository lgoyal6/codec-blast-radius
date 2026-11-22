"""Pack baseline function-annotation readers into the semantic profile."""

import argparse
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
    indices = {test: 1 << index for index, test in enumerate(profile["ids"])}
    seen = set()
    readers = {}
    for report in args.reports:
        capture = json.loads(report.read_bytes())
        seen.update(capture["seen"])
        for _, module, function, _, contexts in capture["readers"]:
            readers.setdefault(f"{module}:{function}", set()).update(contexts)
    if not indices.keys() <= seen:
        raise ValueError("annotation trace does not cover the complete baseline catalog")

    profile["annotation_profile"] = {
        "seen": format((1 << len(indices)) - 1, "x"),
        "readers": {
            function: (
                format(sum(indices.get(context, 0) for context in contexts), "x"),
                "@collection" in contexts,
            )
            for function, contexts in sorted(readers.items())
        },
    }
    args.output.write_bytes(gzip.compress(
        json.dumps(profile, separators=(",", ":")).encode(), mtime=0,
    ))


if __name__ == "__main__":
    main()

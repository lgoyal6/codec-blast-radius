"""Build an executor checkpoint from the pinned fixture workload."""

import argparse
import gzip
import json
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector import REVISION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query_coverage", type=Path)
    parser.add_argument("outputs", type=Path)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("phases", type=Path, nargs="+")
    args = parser.parse_args()
    outputs = json.loads(args.outputs.read_bytes())
    queries = json.loads(args.query_coverage.read_bytes())
    if set(outputs) != {str(row["index"]) for row in queries}:
        raise ValueError("fixture output and line coverage contain different queries")
    lines = defaultdict(lambda: defaultdict(set))
    for row in queries:
        for path, numbers in row["coverage"].items():
            for line in numbers:
                lines[path][str(line)].add(row["index"])
    outside = defaultdict(set)
    observed = 0
    for path in args.phases:
        trace = json.loads(path.read_bytes())
        observed += len(trace["calls"])
        for name, numbers in trace["outside"].items():
            outside[name].update(numbers)
    if observed != len(outputs):
        raise ValueError("the phase trace missed original executor queries")
    profile = {
        "revision": REVISION, "outputs": outputs,
        "lines": {path: {line: sorted(indices) for line, indices in rows.items()}
                  for path, rows in lines.items()},
        "outside": {path: sorted(numbers) for path, numbers in outside.items()},
    }
    args.checkpoint.write_bytes(gzip.compress(json.dumps(profile, separators=(",", ":")).encode(), mtime=0))


if __name__ == "__main__":
    main()

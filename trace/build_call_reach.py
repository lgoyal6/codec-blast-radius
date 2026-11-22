"""Compile call statement reachability checked by two baseline captures."""

import argparse
import ast
import gzip
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector import Baseline, REVISION

STATEMENTS = (ast.Assign, ast.Expr, ast.Return)


def candidate_spans(source, baseline):
    for path, coverage in baseline.files.items():
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            continue
        root = ast.parse((source / path).read_bytes())
        spans = set()
        for function in ast.walk(root):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, STATEMENTS):
                    continue
                if not any(isinstance(item, ast.Call) for item in ast.walk(node)):
                    continue
                lines = range(node.lineno, node.end_lineno + 1)
                if not coverage.covered(lines) and not coverage.collection.intersection(lines):
                    spans.add((node.lineno, node.end_lineno))
        if spans:
            yield path, sorted(spans)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--reports", type=Path, nargs="+")
    args = parser.parse_args()
    baseline = Baseline(args.baseline)
    revision = subprocess.check_output(["git", "-C", args.source, "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", args.source, "status", "--porcelain"], text=True)
    if dirty or revision != REVISION:
        raise ValueError("expected the clean pinned source")
    candidates = dict(candidate_spans(args.source, baseline))
    targets = {
        path: sorted({line for start, end in spans for line in range(start, end + 1)})
        for path, spans in candidates.items()
    }
    if args.profile is None:
        args.output.write_text(json.dumps(targets, separators=(",", ":")))
        print(sum(map(len, targets.values())), "source lines scheduled for an independent capture")
        return

    seen = set()
    reached = {}
    for report in args.reports:
        data = json.loads(report.read_bytes())
        seen.update(data["seen"])
        for path, lines in data["reached"].items():
            if not set(lines) <= set(targets[path]):
                raise ValueError("unexpected source lines in the capture")
            reached.setdefault(path, set()).update(lines)
    if baseline.indices.keys() - seen:
        raise ValueError("call capture did not observe the complete catalog")
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    if profile["revision"] != REVISION or set(profile["ids"]) != baseline.indices.keys():
        raise ValueError("the profile and coverage must describe the same revision and test catalog")
    profile["unreached_calls"] = {
        path: [f"{start}:{end}" for start, end in spans
               if not reached.get(path, set()).intersection(range(start, end + 1))]
        for path, spans in candidates.items()
    }
    with args.output.open("wb") as target, gzip.GzipFile(
        filename="", fileobj=target, mode="wb", mtime=0,
    ) as output:
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(sum(map(len, profile["unreached_calls"].values())), "statement spans absent from both traces")


if __name__ == "__main__":
    main()

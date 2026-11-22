"""Mark existing Markdown files unobserved by the pinned pytest baseline."""

import argparse
import gzip
import json
import re
import subprocess
from collections import Counter
from pathlib import Path


STAT = re.compile(rb'^[0-9]+ +newfstatat\(AT_FDCWD</src>, "([^"]+)", (.+), 0\) = (.+)$')


def safe_metadata(trace, path):
    observations = Counter()
    basename = path.name.encode("ascii")
    filename = f"/src/{path.as_posix()}".encode("ascii")
    for line in trace:
        if basename not in line:
            continue
        match = STAT.fullmatch(line)
        if match is None:
            return None
        name, details, outcome = match.groups()
        if name == filename and b"st_mode=S_IFREG|" in details and outcome == b"0":
            observations[name.decode("ascii")] += 1
        elif (
            name in (filename + b"/pyvenv.cfg", filename + b"/conda-meta/history")
            and outcome.startswith(b"-1 ENOTDIR")
        ):
            observations[name.decode("ascii")] += 1
        else:
            return None
    return observations


def discovery_metadata(records, path):
    observations = Counter()
    basename = path.name
    filename = f"/src/{path.as_posix()}"
    for name, stack in records:
        if basename not in name:
            continue
        if len(stack) < 3:
            return None
        stat, predicate, caller = [(Path(source).name, function) for source, function in stack[:3]]
        if stat != ("_local.py", "stat"):
            return None
        elif name == filename:
            if predicate != ("_abc.py", "is_dir") or caller != ("main.py", "pytest_ignore_collect"):
                return None
        elif name in (filename + "/pyvenv.cfg", filename + "/conda-meta/history"):
            if predicate != ("_abc.py", "is_file") or caller != ("main.py", "_in_venv"):
                return None
        else:
            return None
        observations[name] += 1
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("full", type=Path)
    parser.add_argument("stress", type=Path)
    parser.add_argument("collect", type=Path)
    args = parser.parse_args()
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    revision = subprocess.check_output(["git", "-C", args.source, "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", args.source, "status", "--porcelain"], text=True)
    if revision != profile["revision"] or dirty:
        raise ValueError("expected the clean pinned baseline")
    traces = []
    for folder, expected in (
        (args.full, b"1619 passed"), (args.stress, b"1 passed"),
        (args.collect, b"1620/1621 tests collected"),
    ):
        log = folder.with_suffix(".log").read_bytes()
        trace = folder.with_suffix(".trace").read_bytes()
        if expected not in log or b"sqlglot/parser.py" not in trace or b"execve(" not in trace:
            raise ValueError(f"incomplete baseline file trace: {folder}")
        traces.append(trace.splitlines())
    discovery = json.loads((args.collect.parent / "collect-stats.json").read_bytes())
    tracked = subprocess.check_output(["git", "-C", args.source, "ls-files", "-z", "--", "*.md"])
    unread = []
    for raw in tracked.split(b"\0"):
        if not raw or re.fullmatch(rb"[A-Za-z0-9_./-]+\.md", raw) is None:
            continue
        path = Path(raw.decode("ascii"))
        file = args.source / path
        if file.is_symlink() or not file.is_file():
            continue
        file.read_text(encoding="utf-8")
        expected = discovery_metadata(discovery, path)
        if expected is not None and all(safe_metadata(trace, path) == expected for trace in traces):
            unread.append(path.as_posix())
    profile["unread_docs"] = sorted(unread)
    with args.output.open("wb") as target, gzip.GzipFile(
        filename="", fileobj=target, mode="wb", mtime=0,
    ) as output:
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(len(unread), "tracked Markdown files with only type checks during discovery")


if __name__ == "__main__":
    main()

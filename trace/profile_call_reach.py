"""Independently observe candidate statement spans, including subprocesses."""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import profile_runtime as runtime

PREFIX = "/src/"
TOOL = 3
TARGETS = {
    path: frozenset(lines)
    for path, lines in json.loads(Path(os.environ["BLAST_CALL_TARGETS"]).read_bytes()).items()
}


def observe(code, lineno):
    file = code.co_filename
    if file.startswith(PREFIX):
        path = file.removeprefix(PREFIX)
        if lineno in TARGETS.get(path, ()):
            runtime.TRACE.reached[path].add(lineno)
    return sys.monitoring.DISABLE


class Trace(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.reached = defaultdict(set)

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "reached": {path: sorted(lines) for path, lines in self.reached.items()},
        }, separators=(",", ":")))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    if sys.monitoring.get_tool(TOOL) is None:
        sys.monitoring.use_tool_id(TOOL, "baseline call reachability")
        sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, observe)
        sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE)
    return trace

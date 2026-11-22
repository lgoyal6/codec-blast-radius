"""Observe metadata reads without opening any documentation files."""

import json
import os
import sys

original_stat = os.stat
watched = tuple(os.environ["BLAST_DOC_NAMES"].split(","))
observed = []


def tracked_stat(path, *args, **kwargs):
    if not isinstance(path, int):
        name = os.fsdecode(path)
        if any(doc in name for doc in watched):
            frame = sys._getframe(1)
            stack = []
            while frame is not None and len(stack) < 6:
                stack.append((frame.f_code.co_filename, frame.f_code.co_name))
                frame = frame.f_back
            observed.append((name, stack))
    return original_stat(path, *args, **kwargs)


os.stat = tracked_stat
import pytest

try:
    status = pytest.main(["-q", "--collect-only", "--timeout=420", "-p", "no:cacheprovider", *sys.argv[1:]])
finally:
    with open("/reports/collect-stats.json", "w") as out:
        json.dump(observed, out)
raise SystemExit(status)

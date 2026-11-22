"""Capture test coverage, file reads and class usage on pinned SQLGlot."""

import atexit
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

COLLECTION = "@collection"


class Capture:
    def __init__(self, output):
        self.output = output
        self.source = Path.cwd().as_posix() + "/"
        self.context = COLLECTION
        self.files = defaultdict(set)
        self.owners = defaultdict(set)
        self.owner_cache = {}
        self.seen = set()
        self.cov = None
        if os.environ.get("RECORD_COVERAGE", "1") == "1":
            import coverage

            self.cov = coverage.Coverage(
                include=[self.source + "sqlglot/*", self.source + "tests/*"],
                data_file=str(output.with_suffix(".coverage")),
            )

    def start(self):
        if self.cov is not None:
            self.cov.start()
        self.set_context(COLLECTION)
        sys.addaudithook(self.observe_open)
        if os.environ.get("RECORD_OWNERS", "1") == "0":
            return

        from sqlglot.dialects.dialect import Dialect
        from sqlglot.generator import Generator
        from sqlglot.parser import Parser
        from sqlglot.tokens import Tokenizer

        monitor = sys.monitoring
        tool = 4
        monitor.use_tool_id(tool, "sqlglot baseline class usage")
        monitor.register_callback(tool, monitor.events.PY_RETURN, self.observe_return)
        monitor.register_callback(tool, monitor.events.PY_START, self.observe_start)
        monitor.set_local_events(tool, Dialect.get_or_raise.__func__.__code__, monitor.events.PY_RETURN)
        for cls in (Dialect, Generator, Parser, Tokenizer):
            monitor.set_local_events(tool, cls.__init__.__code__, monitor.events.PY_START)

    def set_context(self, context):
        self.context = context
        if self.cov is not None:
            self.cov.switch_context(context)

    def observe_class(self, cls):
        if self.context == COLLECTION:
            return
        if cls not in self.owner_cache:
            self.owner_cache[cls] = tuple(
                f"{base.__module__}:{base.__qualname__}"
                for base in cls.__mro__
                if base.__module__.startswith("sqlglot.")
            )
        for owner in self.owner_cache[cls]:
            self.owners[owner].add(self.context)

    def observe_return(self, code, offset, value):
        self.observe_class(type(value))

    def observe_start(self, code, offset):
        instance = sys._getframe(1).f_locals.get("self")
        if instance is not None:
            self.observe_class(type(instance))

    def observe_open(self, event, args):
        if event != "open" or not isinstance(args[0], (str, bytes)):
            return
        filename = os.fsdecode(args[0])
        if filename.endswith((".pyc", ".coverage")) or args[2] & (os.O_WRONLY | os.O_RDWR):
            return
        if not filename.startswith((self.source, "sqlglot/", "tests/")):
            return
        path = os.path.abspath(filename)
        if not path.startswith(self.source):
            return
        origin = sys._getframe(1)
        if path.endswith(".py") and "importlib" in origin.f_code.co_filename:
            return
        relative = path.removeprefix(self.source)
        if self.context == COLLECTION and relative.startswith("tests/") and relative.endswith(".py"):
            reader = origin.f_back
            if (
                origin.f_globals.get("__name__") == "pathlib._local"
                and origin.f_code.co_name == "open"
                and reader is not None
                and reader.f_globals.get("__name__") == "pathlib._abc"
                and reader.f_code.co_name == "read_bytes"
            ):
                rewrite = reader.f_back
                if (
                    rewrite is not None
                    and rewrite.f_globals.get("__name__") == "_pytest.assertion.rewrite"
                    and rewrite.f_code.co_name == "_rewrite_test"
                    and os.fspath(rewrite.f_locals.get("fn", "")) == path
                ):
                    return
        self.files[relative].add(self.context)

    def finish(self):
        if self.cov is not None:
            self.cov.stop()
            self.cov.save()
        self.output.write_text(json.dumps({
            "seen": sorted(self.seen),
            "files": {name: sorted(ids) for name, ids in sorted(self.files.items())},
            "owners": {name: sorted(ids) for name, ids in sorted(self.owners.items())},
        }, sort_keys=True))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        self.seen.add(item.nodeid)
        self.set_context(item.nodeid)
        try:
            return (yield)
        finally:
            self.set_context(COLLECTION)


_worker = None


def capture_worker(output, context, function, args, kwargs):
    global _worker
    if _worker is None:
        _worker = Capture(output.with_name(f"{output.stem}.worker-{os.getpid()}.json"))
        _worker.start()
        atexit.register(_worker.finish)
    _worker.set_context(context)
    return function(*args, **kwargs)


def main():
    output = Path(sys.argv[1]).resolve()
    capture = Capture(output)
    capture.start()
    submit = ProcessPoolExecutor.submit

    def submit_with_test(pool, function, /, *args, **kwargs):
        return submit(pool, capture_worker, output, capture.context, function, args, kwargs)

    ProcessPoolExecutor.submit = submit_with_test
    result = pytest.main(["-q", "--tb=short", "--timeout=" + os.environ.get("TEST_TIMEOUT", "360"), "-p", "no:cacheprovider", *(sys.argv[2:] or ["tests"])], plugins=[capture])
    if result == pytest.ExitCode.OK:
        capture.finish()
    return result


if __name__ == "__main__":
    raise SystemExit(main())

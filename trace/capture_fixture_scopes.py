"""Capture shared pytest fixture reads and expand their owners through the catalog."""
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest


class FixtureReads:
    def __init__(self, root, catalog):
        self.root = root.as_posix() + "/"
        self.ids = [test["id"] for test in catalog]
        self.context = None
        self.readers = defaultdict(set)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        previous = self.context
        self.context = (
            (request.scope, request.node.nodeid)
            if request.scope != "function" else None
        )
        try:
            return (yield)
        finally:
            self.context = previous

    def audit(self, name, args):
        if name != "open" or self.context is None or not isinstance(args[0], (str, bytes)):
            return
        if args[2] & (os.O_WRONLY | os.O_RDWR):
            return
        path = os.path.abspath(os.fsdecode(args[0]))
        if not path.startswith(self.root) or path.endswith((".pyc", ".coverage")):
            return
        relative = path.removeprefix(self.root)
        if relative.endswith(".py") and "importlib" in sys._getframe(1).f_code.co_filename:
            return
        self.readers[relative].add(self.context)

    def tests(self, scope, owner):
        if scope == "session" or not owner:
            return self.ids
        separator = "::" if scope in {"class", "module"} else "/"
        return [node for node in self.ids if node.startswith(owner + separator)]

    def report(self):
        output = {}
        for path, contexts in sorted(self.readers.items()):
            if any(not self.tests(*context) for context in contexts):
                raise ValueError((path, contexts))
            output[path] = sorted({node for context in contexts for node in self.tests(*context)})
        return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("targets", nargs="*", default=["tests"])
    args = parser.parse_args()
    capture = FixtureReads(Path.cwd(), json.loads(args.catalog.read_bytes())["tests"])
    sys.addaudithook(capture.audit)
    result = pytest.main(["--setup-only", "-q", "-p", "no:cacheprovider", *args.targets], plugins=[capture])
    if result == pytest.ExitCode.OK:
        args.output.write_text(json.dumps(capture.report(), sort_keys=True, indent=2) + "\n")
    return result


if __name__ == "__main__":
    raise SystemExit(main())

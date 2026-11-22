"""Record the test files requesting modules during pinned pytest collection."""

import argparse
import json
import sys
import types
from pathlib import Path

import pytest

import profile_imports as imports


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text())["tests"]
    known = {row["file"] for row in catalog}
    root = Path.cwd()
    if not all((root / path).is_file() for path in known):
        raise ValueError("collection catalog does not match the pinned checkout")
    capture = imports.start(args.out)
    original = imports.record
    prefix = root.as_posix() + "/"

    def record_requester(name):
        original(name)
        if not name.startswith("sqlglot.") or not imports.CONTEXT.startswith("@file:"):
            return
        frame = sys._getframe(1)
        while frame is not None:
            filename = frame.f_code.co_filename.removeprefix(prefix)
            if filename in known:
                capture.requests[name].add(f"@file:{filename}")
                return
            frame = frame.f_back

    imports.record = record_requester

    class Context:
        preloaded = ()

        def pytest_sessionstart(self, session):
            self.preloaded = sorted(
                name
                for name, module in sys.modules.copy().items()
                if name in sys.stdlib_module_names
                and type(module) is types.ModuleType
                and not {"__getattr__", "__path__"}.intersection(vars(module))
            )

        @pytest.hookimpl(wrapper=True)
        def pytest_make_collect_report(self, collector):
            previous = imports.CONTEXT
            path = collector.path.relative_to(root).as_posix()
            if path in known:
                imports.CONTEXT = f"@file:{path}"
            try:
                return (yield)
            finally:
                imports.CONTEXT = previous

    files = sorted(known, reverse=args.reverse)
    context = Context()
    result = pytest.main(
        ["--collect-only", "-q", "-p", "no:cacheprovider", *files],
        plugins=[capture, context],
    )
    if result == 0:
        capture.save()
        report = json.loads(args.out.read_text())
        report["pre_collection_stable"] = context.preloaded
        args.out.write_text(json.dumps(report, separators=(",", ":")))
    return result


if __name__ == "__main__":
    raise SystemExit(main())

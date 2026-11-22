import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trace"))

from build_collection_imports import collection_import_users
from selector import FileMap


class CollectionImportsTest(unittest.TestCase):
    def test_keeps_transitive_collectors_and_rejects_unattributed_access(self):
        target = "sqlglot/parsers/example.py"
        bridge = "sqlglot/derived.py"
        requester = "tests/test_requester.py"
        dependent = "tests/test_dependent.py"
        baseline = SimpleNamespace(
            all=127,
            indices={"test-runtime": 16},
            files={
                target: FileMap({}, {1}, 0, 0, 0, 1),
                bridge: FileMap({}, set(), 0, 0, 0, 2),
                requester: FileMap({}, set(), 0, 0, 4, 0),
                dependent: FileMap({}, set(), 0, 0, 8, 0),
            },
        )
        callers = {target: {bridge}, requester: {dependent}}
        runtime = {"sqlglot.parsers.example": {"test-runtime"}}
        with tempfile.TemporaryDirectory() as directory:
            forward, reverse = (Path(directory) / filename for filename in ("forward", "reverse"))
            forward.write_text(json.dumps({"requests": {"sqlglot.parsers.example": [f"@file:{requester}"]}}))
            reverse.write_text(json.dumps({"requests": {}}))
            arguments = baseline, callers, ("parsers",), runtime, 32, {}, [forward, reverse]
            self.assertEqual(collection_import_users(*arguments), {target: "3f"})
            reverse.write_text(json.dumps({"requests": {"sqlglot.derived": ["@collection"]}}))
            self.assertEqual(collection_import_users(*arguments), {})

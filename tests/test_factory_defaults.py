import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trace"))

from build_factory_defaults import factory_default_profile
from semantics import Semantics


class FactoryDefaultsTest(unittest.TestCase):
    path = "sqlglot/parsers/example.py"
    source = """\
from __future__ import annotations

def _factory(label, flag=False):
    def build(args):
        return label, flag, args
    return build

class Example:
    FUNCTIONS = {"explicit": _factory("one", True), "implicit": _factory("two")}
"""

    def setUp(self):
        tree = ast.parse(self.source)
        self.trees = {self.path: tree}
        explicit, implicit = tree.body[-1].body[0].value.values
        self.explicit = f"{self.path}:{explicit.lineno}:{explicit.col_offset}"
        self.implicit = f"{self.path}:{implicit.lineno}:{implicit.col_offset}"
        self.calls = {self.explicit: {"explicit"}, self.implicit: {"implicit"}}
        self.baseline = SimpleNamespace(indices={"explicit": 1, "implicit": 2})

    def test_factory_uses_callbacks_with_omitted_argument(self):
        self.assertEqual(
            factory_default_profile(self.trees, self.calls, {}, self.baseline),
            {f"{self.path}:_factory": ["0", {"flag": "2"}]},
        )

    def test_missing_or_collection_context_falls_back(self):
        for contexts in (None, {"@collection", "implicit"}):
            with self.subTest(contexts=contexts):
                calls = self.calls.copy()
                if contexts is None:
                    del calls[self.implicit]
                else:
                    calls[self.implicit] = contexts
                self.assertEqual(factory_default_profile(self.trees, calls, {}, self.baseline), {})

    def test_factory_reference_outside_registry_falls_back(self):
        self.trees["tests/test_other.py"] = ast.parse(
            "from sqlglot.parsers.example import _factory as other"
        )
        self.assertEqual(factory_default_profile(self.trees, self.calls, {}, self.baseline), {})

    def test_selector_retains_unknown_execution_and_reflection(self):
        profile = Semantics.__new__(Semantics)
        profile.seen = 15
        profile.default_seen = 7
        profile.factory_readers = 4
        profile.factory_defaults = {f"{self.path}:_factory": ["2", {"flag": "1"}]}
        self.assertEqual(profile.factory_default_users(self.path, "_factory", ("flag",), 31), 31)
        self.assertIsNone(profile.factory_default_users(self.path, "_factory", ("other",), 31))

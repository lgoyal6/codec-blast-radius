import ast
import difflib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from patches import parse_patch
from selector import FileMap, Selector, SourceShape, schedule
from semantics import Semantics, same_result
from semantic_edits import compare_edit, copy_toggle, default_edit, only_preloaded_imports, operator_only


def patch_for(path, old, new):
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            f"a/{path}",
            f"b/{path}",
        )
    ).encode()


class PatchesTest(unittest.TestCase):
    def test_applies_insertion_and_deletion_using_old_offsets(self):
        changes = parse_patch(b"--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n one\n-two\n three\n+four\n")
        after, removed, added, anchors = changes[0].apply(b"one\ntwo\nthree\n")
        self.assertEqual((after, removed, added, anchors), (b"one\nthree\nfour\n", {2}, {3}, {3}))

    def test_honors_explicit_end_of_file_markers(self):
        change = parse_patch(
            b"--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
        )[0]
        self.assertEqual(change.apply(b"a")[0], b"b")

    def test_rejects_malformed_or_untrusted_context(self):
        for patch in (
            b"--- a/../x.py\n+++ b/../x.py\n@@ -1 +1 @@\n-a\n+b\n",
            b"--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n",
            b"--- a/x.py\n+++ b/x.py\n@@ -1 +2 @@\n-a\n+b\n",
            b"--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-z\n+b\n",
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                parse_patch(patch)[0].apply(b"a\n")


class SelectorTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.path = "sqlglot/example.py"
        (self.root / "sqlglot").mkdir()
        self.source = "def f(a, b):\n    if (\n        a\n        and b\n    ):\n        return 1\n    return 0\n"
        (self.root / self.path).write_text(self.source)
        coverage = {2: 1, 3: 1, 4: 2, 6: 2, 7: 4}
        self.baseline = SimpleNamespace(
            all=15, files={self.path: FileMap(coverage, set(), 8, 0, 0, 7)}, owners={}
        )

    def test_multiline_boolean_covers_the_earlier_operand(self):
        patch = patch_for(self.path, self.source, self.source.replace("and b", "or b"))
        self.assertEqual(Selector(self.baseline, self.root).select(patch), (3, {"statement"}))
        self.assertEqual(Selector(self.baseline, self.root, whole_function=True).select(patch), (7, {"whole-function"}))

    def test_insertion_covers_whole_function(self):
        patch = patch_for(self.path, self.source, self.source.replace("    if (", "    return -1\n    if ("))
        self.assertEqual(Selector(self.baseline, self.root).select(patch), (7, {"whole-function"}))

    def test_collection_falls_back(self):
        self.baseline.files[self.path].collection.add(3)
        patch = patch_for(self.path, self.source, self.source.replace("and b", "or b"))
        self.assertEqual(Selector(self.baseline, self.root).select(patch)[0], 15)

    def test_comments_include_source_readers(self):
        self.baseline.files[self.path].readers = 8
        patch = patch_for(self.path, self.source, self.source.replace("    return 0", "    # Explanation\n    return 0"))
        self.assertEqual(Selector(self.baseline, self.root).select(patch), (8, {"comment-readers"}))

    def test_multiple_scopes_widen_independently(self):
        text = "def first():\n    return 1\n\ndef second():\n    marker = 2\n    return marker\n"
        (self.root / self.path).write_text(text)
        self.baseline.files[self.path] = FileMap({2: 1, 6: 2}, set(), 8, 0, 0, 3)
        changed = text.replace("return 1", "return 3").replace("marker = 2", "marker = 4")
        patch = patch_for(self.path, text, changed)
        self.assertEqual(Selector(self.baseline, self.root).select(patch), (3, {"whole-function"}))

    def test_deferred_lambda_preserves_the_surrounding_definition(self):
        text = "class Parser:\n    TABLE = {'k': lambda x: (\n        x + 1\n    )}\n"
        (self.root / self.path).write_text(text)
        self.baseline.files[self.path] = FileMap({3: 2}, {1, 2}, 8, 0, 0, 2)
        patch = patch_for(self.path, text, text.replace("x + 1", "x + 2"))
        self.assertEqual(Selector(self.baseline, self.root).select(patch), (2, {"lambda-body"}))
        header_patch = patch_for(self.path, text, text.replace("'k'", "'j'"))
        self.assertEqual(Selector(self.baseline, self.root).select(header_patch)[0], 15)

    def test_collection_import_must_be_preloaded_before_collection(self):
        text = "from itertools import chain\n"
        file = self.root / self.path
        file.write_text(text)
        self.baseline.files[self.path].collection.add(1)
        profile = SimpleNamespace(
            lazy_modules={self.path: "1"}, stable_modules={"itertools", "decimal"},
            stable_collection_modules={"itertools"},
        )
        selector = Selector(self.baseline, self.root, semantics=profile)
        warm = patch_for(self.path, text, "from itertools import repeat\n")
        cold = patch_for(self.path, text, "from decimal import Decimal\n")
        self.assertEqual(selector.select(warm), (1, {"observed-lazy-import"}))
        self.assertEqual(selector.select(cold), (15, {"import-change"}))

    def test_enum_config_uses_observed_readers_only_for_known_members(self):
        path = "sqlglot/dialects/example.py"
        file = self.root / path
        file.parent.mkdir()
        before = "class Example:\n    MODE = Strategy.ORIGINAL\n"
        file.write_text(before)
        self.baseline.files[path] = FileMap({}, {1, 2}, 15, 4, 0, 8)
        profile = Semantics.__new__(Semantics)
        profile.seen = 7
        profile.enum_fields = {path: {"Example:MODE": ["Strategy", ["ORIGINAL", "OTHER"]]}}
        profile.attributes = {"sqlglot.dialects.example:Example:MODE": "1"}
        profile.direct = {"MODE": "2"}
        selector = Selector(self.baseline, self.root, semantics=profile)
        good = patch_for(path, before, before.replace("Strategy.ORIGINAL", "Strategy.OTHER"))
        self.assertEqual(selector.select(good), (15, {"observed-class"}))
        self.baseline.files[path].executed = 0
        self.assertEqual(selector.select(good), (7, {"observed-class"}))
        for replacement in ("Strategy.MISSING", "OtherStrategy.OTHER"):
            with self.subTest(replacement=replacement):
                patch = patch_for(path, before, before.replace("Strategy.ORIGINAL", replacement))
                self.assertEqual(selector.select(patch), (15, {"definition-or-collection"}))

    def test_compile_error_uses_fallback(self):
        patch = patch_for(self.path, self.source, self.source.replace("        return 1", "        break"))
        self.assertEqual(Selector(self.baseline, self.root).select(patch), (15, {"unsupported-patch"}))

    def test_unknown_diff_falls_back(self):
        self.assertEqual(Selector(self.baseline, self.root).select(b"Binary files differ\n")[0], 15)


class SemanticEvidenceTest(unittest.TestCase):
    def test_identity_comparison_uses_default_object_equality(self):
        fact = '[false,["identity",null],["NoneType",null]]'
        self.assertTrue(same_result(fact, "Is", "Eq"))
        self.assertFalse(same_result(fact, "Is", "NotEq"))

    def test_equal_non_singletons_keep_identity_change_uncertain(self):
        fact = '[true,["str","a"],["str","a"]]'
        self.assertFalse(same_result(fact, "Is", "Eq"))

    def test_unknown_operand_prevents_pruning(self):
        self.assertFalse(same_result("?", "Is", "Eq"))

    def test_operator_fast_path_requires_the_rest_of_the_file_to_match(self):
        build = lambda text: SourceShape(text, "sqlglot/test.py")
        old = build(b"if value is None:\n    result = 1\n")
        new = build(b"if value == None:\n    result = 1\n")
        mixed = build(b"if value == None:\n    result = 2\n")
        self.assertTrue(operator_only(old, new, old.comparisons[0], new.comparisons[0], "Is", "Eq"))
        self.assertFalse(operator_only(old, mixed, old.comparisons[0], mixed.comparisons[0], "Is", "Eq"))
        self.assertIsNone(compare_edit(old, mixed, {1}, {1}))

    def test_operator_equivalence_falls_back_for_parentheses_and_encoding(self):
        old = SourceShape(b"# coding: latin-1\nif (value) is None:\n    result = 1\n", "sqlglot/test.py")
        new = SourceShape(b"# coding: latin-1\nif (value) == None:\n    result = 1\n", "sqlglot/test.py")
        self.assertFalse(operator_only(old, new, old.comparisons[0], new.comparisons[0], "Is", "Eq"))
        self.assertEqual(compare_edit(old, new, {2}, {2})[:4], (2, 3, "Is", "Eq"))

    def test_fresh_alias_copy_toggle_preserves_other_syntax(self):
        def shape(source):
            return SourceShape(source.encode(), "sqlglot/example.py")

        original = shape('exp.alias_(exp.table_(name), alias="a")')
        changed = shape('exp.alias_(exp.table_(name), alias="a", copy=False)')
        shared = shape('exp.alias_(existing, alias="a", copy=False)')
        mixed = shape('exp.alias_(exp.table_(name), alias="b", copy=False)')
        self.assertEqual(copy_toggle(original, changed, {1}, {1}), (1, 0))
        self.assertIsNone(copy_toggle(original, shared, {1}, {1}))
        self.assertIsNone(copy_toggle(original, mixed, {1}, {1}))

    def test_fresh_copy_selection_keeps_unknowns_and_source_readers(self):
        path = "sqlglot/example.py"
        before = 'def f(name):\n    return exp.alias_(exp.column(name), alias="alias")\n'
        after = before.replace('alias="alias"', 'alias="alias", copy=False')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / path).parent.mkdir()
            (root / path).write_text(before)
            baseline = SimpleNamespace(all=7, files={path: FileMap({2: 3}, set(), 0, 4, 0, 3)})
            semantics = Semantics.__new__(Semantics)
            semantics.fresh_copies = {path: {"2:11": "1"}}
            selector = Selector(baseline, root, semantics=semantics)
            self.assertEqual(selector.select(patch_for(path, before, after)), (6, {"observed-fresh-copy"}))

    def test_default_edit_identifies_bound_parameters(self):
        old = SimpleNamespace(tree=ast.parse("def f(a, /, b=2, *, c=3):\n    return b + c\n"))
        new = SimpleNamespace(tree=ast.parse("def f(a, /, b=4, *, c=3):\n    return b + c\n"))
        self.assertEqual(default_edit(old, new, {1}, {1}), ("f", range(2, 3), ("b",), False))

    def test_default_readers_keep_metadata_and_unobserved_body_contexts(self):
        profile = Semantics.__new__(Semantics)
        profile.default_seen = 31
        profile.default_direct = 0
        profile.default_functions = {"sqlglot/test.py:example": ["3", "8", {"flag": "1"}]}
        self.assertEqual(
            profile.default_users("sqlglot/test.py", "example", ("flag",), 19),
            25,
        )

    def test_default_edit_marks_closure_as_escaping(self):
        old = SimpleNamespace(tree=ast.parse("def f(a=1):\n    return lambda: a\n"))
        new = SimpleNamespace(tree=ast.parse("def f(a=2):\n    return lambda: a\n"))
        self.assertTrue(default_edit(old, new, {1}, {1})[3])

    def test_import_directives_may_shift_with_unchanged_lines(self):
        before = "# header\nvalue = 1  # type: ignore[assignment]\n"
        old = SimpleNamespace(tree=ast.parse(before, type_comments=True))
        moved = "# type: ignore[assignment]\nvalue = 1\nimport math\n"
        for after, removed, added, expected in (
            ("import math\n" + before, set(), {1}, True),
            ("import math\n" + before.replace("assignment", "misc"), {2}, {1, 3}, False),
            (moved, {1, 2}, {1, 2, 3}, False),
            ("import math  # type: ignore[assignment]\n" + before, set(), {1}, False),
        ):
            with self.subTest(after=after):
                new = SimpleNamespace(tree=ast.parse(after, type_comments=True))
                self.assertEqual(only_preloaded_imports(old, new, removed, added, {"math"}), expected)

    def test_preloaded_import_guard_rejects_new_dependencies_and_body_edits(self):
        old = SimpleNamespace(tree=ast.parse("x = 1"))
        modules = {"math"}
        for source, expected in (
            ("from math import fabs\nx = 1", True),
            ("from third_party import f\nx = 1", False),
            ("from math import *\nx = 1", False),
            ("from math import fabs\nx = 2", False),
        ):
            with self.subTest(source=source):
                new = SimpleNamespace(tree=ast.parse(source))
                self.assertEqual(only_preloaded_imports(old, new, set(), {1, 2}, modules), expected)


class ScheduleTest(unittest.TestCase):
    def test_tradeoff_between_makespan_and_worker_fee(self):
        self.assertEqual(schedule([]), [])
        self.assertEqual(schedule([(1000, 0), (1000, 1)]), [[0, 1]])
        self.assertEqual(schedule([(5000, 0), (5000, 1)]), [[0], [1]])
        self.assertEqual(schedule([(0, 0), (0, 1)]), [[0, 1]])


if __name__ == "__main__":
    unittest.main()

import difflib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from patches import parse_patch
from selector import FileMap, Selector, schedule
from semantics import same_result


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


class ScheduleTest(unittest.TestCase):
    def test_tradeoff_between_makespan_and_worker_fee(self):
        self.assertEqual(schedule([]), [])
        self.assertEqual(schedule([(1000, 0), (1000, 1)]), [[0, 1]])
        self.assertEqual(schedule([(5000, 0), (5000, 1)]), [[0], [1]])
        self.assertEqual(schedule([(0, 0), (0, 1)]), [[0, 1]])


if __name__ == "__main__":
    unittest.main()

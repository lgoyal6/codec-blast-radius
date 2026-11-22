import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trace"))

from merge_fixture_scopes import merge


class FixtureScopeTest(unittest.TestCase):
    def test_merge_keeps_direct_readers_and_reuses_masks(self):
        document = {
            "ids": ["first", "later", "outside"],
            "masks": ["0", "4"],
            "files": {"fixture.sql": [[], [], 0, 1, 0]},
        }
        self.assertEqual(merge(document, {"fixture.sql": ["first", "later"]}), 1)
        self.assertEqual(document["masks"][document["files"]["fixture.sql"][3]], "7")
        self.assertEqual(merge(document, {"fixture.sql": ["first", "later"]}), 0)

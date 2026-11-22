import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fixture_pairs as fixtures


class FixtureFamilyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.indices = {
            fixtures.EXECUTES['tpch']: 1,
            fixtures.OPTIMIZED_TPCH: 2,
            fixtures.EXECUTES['tpcds']: 4,
            fixtures.EXECUTOR + 'another_test': 8,
            'tests/test_optimizer.py::test_tpc': 16,
        }
        self.baseline = SimpleNamespace(indices=self.indices)
        self.before = b'SELECT 1; SELECT 2;\n'
        for path in fixtures.FIXTURES.values():
            target = self.source / path
            target.parent.mkdir(parents=True)
            target.write_bytes(self.before)

    def check(self, kind, after):
        return fixtures.fixture_pair_users(fixtures.FIXTURES[kind], self.before, after, self.baseline, 31, self.source)

    def test_tpcds_expected_output_does_not_reach_executor(self):
        self.assertEqual(self.check('tpcds', b'SELECT 1; SELECT 3;\n'), 16)

    def test_input_edit_keeps_own_consumers_and_a_sibling_cache_collision(self):
        with patch.object(fixtures, 'literal_query_edit', return_value=True):
            self.assertEqual(self.check('tpch', b'SELECT 3; SELECT 2;\n'), 23)
            (self.source / fixtures.FIXTURES['tpcds']).write_bytes(b'SELECT 4; SELECT 5;\n')
            self.assertEqual(self.check('tpch', b'SELECT 3; SELECT 2;\n'), 19)

    def test_malformed_setup_and_unknown_metadata_use_broad_mask(self):
        self.assertIsNone(self.check('tpcds', b'# broken\nSELECT 1; SELECT 2;\n'))
        self.assertIsNone(self.check('tpcds', b'# schema: {}\nSELECT 1; SELECT 2;\n'))

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trace"))

from build_semantics import schema_membership_sites


class SchemaMembershipTest(unittest.TestCase):
    def test_only_expression_validator_key_membership_is_allowed(self):
        relative = "sqlglot/expressions/core.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(
                "class Expression:\n"
                "    def error_messages(self, k):\n"
                "        if k not in self.arg_types:\n"
                "            return self.arg_types[k]\n"
                "        return k in self.required_args\n"
                "class Other:\n"
                "    def error_messages(self, k):\n"
                "        return k in self.arg_types\n"
            )
            self.assertEqual(schema_membership_sites(root), {f"{relative}:3:20"})

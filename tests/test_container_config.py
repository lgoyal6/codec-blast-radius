import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trace"))

from build_container_config import observe


class ContainerConfigTest(unittest.TestCase):
    def test_read_only_runtime_lookup_is_eligible(self):
        tree = ast.parse("class Owner:\n    TABLE = {1: 2}\n    def method(self):\n        return self.TABLE.get(1)\n")
        self.assertEqual(observe([tree]), ["TABLE"])

    def test_startup_helper_and_alias_reads_fall_back(self):
        tree = ast.parse("class Owner:\n    TABLE = {1: 2}\n    def method(self):\n        return self.TABLE.get(1)\n")
        for other in (
            "class Child(Owner):\n    def __init__(self):\n        self.method()\n",
            "def external(owner):\n    return owner.TABLE\n",
        ):
            with self.subTest(other=other):
                self.assertEqual(observe([tree, ast.parse(other)]), [])

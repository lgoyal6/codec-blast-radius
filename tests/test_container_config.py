import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trace"))

from build_container_config import observe
from selector import configuration_keys, parser_dispatch_keys, token_dispatch_keys


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

    def test_key_edits_use_set_membership_and_preserve_dictionary_shape(self):
        def parse(expression):
            return ast.parse(expression, mode="eval").body

        self.assertEqual(configuration_keys(parse("{'a', 'b'}"), parse("{'b', 'c'}")), {"a", "c"})
        self.assertEqual(configuration_keys(parse("{'a': 1, 'b': 2}"), parse("{'a': 3, 'b': 2}")), {"a"})
        self.assertIsNone(configuration_keys(parse("{'a': 1}"), parse("{'b': 1}")))
        self.assertIsNone(configuration_keys(parse("{'a'}"), parse("{'a', value}")))

    def test_key_set_changes_require_dictionary_membership_observations(self):
        def parse(expression):
            return ast.parse(expression, mode="eval").body

        before = parse("{'a': 1, 'b': 2, 'old': 3}")
        after = parse("{'a': 1, 'b': 4, 'new': 5}")
        self.assertIsNone(configuration_keys(before, after))
        self.assertEqual(configuration_keys(before, after, True), {"b", "new", "old"})
        for unsafe in ("{'b': 2, 'a': 1}", "{'a': 1, 'b': x}", "{'a': 1, 'a': 6}"):
            self.assertIsNone(configuration_keys(before, parse(unsafe), True))

    def test_dispatch_entries_preserve_initializer_evaluation(self):
        def parse(expression):
            return ast.parse(expression, mode="eval").body

        before = parse("{**Base.FUNCTION_PARSERS, 'A': lambda self: self.foo()}")
        self.assertEqual(parser_dispatch_keys(before, parse("{**Base.FUNCTION_PARSERS}")), {"A"})
        self.assertEqual(parser_dispatch_keys(before, parse("{**Base.FUNCTION_PARSERS, 'B': lambda self: self.bar()}")), {"A", "B"})
        for unsafe in (
            "{**Base.FUNCTION_PARSERS, 'A': lambda self=build(): foo()}",
            "{**Other.FUNCTION_PARSERS, 'A': lambda self: self.foo()}",
            "{'A': lambda self: self.foo(), **Base.FUNCTION_PARSERS}",
            "{**Base.FUNCTION_PARSERS, 'A': dynamic_factory()}",
        ):
            self.assertIsNone(parser_dispatch_keys(before, parse(unsafe)))

    def test_token_dispatch_requires_unchanged_keys_and_safe_values(self):
        def parse(expression):
            return ast.parse(expression, mode="eval").body

        functions = {"left", "right"}
        before = parse("{TokenType.A: parser.left, TokenType.B: parser.right}")
        changed = parse("{TokenType.A: parser.right, TokenType.B: parser.right}")
        self.assertEqual(token_dispatch_keys(before, changed, functions), {"A"})
        for unsafe in (
            "{TokenType.A: parser.right}",
            "{TokenType.A: parser.unknown, TokenType.B: parser.right}",
            "{TokenType.B: parser.right, TokenType.A: parser.left}",
            "{TokenType.A: lambda t=produce(): call(t), TokenType.B: parser.right}",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(token_dispatch_keys(before, parse(unsafe), functions))

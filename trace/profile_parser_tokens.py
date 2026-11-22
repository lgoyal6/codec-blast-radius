"""Observe current-token values before calls into the pinned parser matchers."""

import ast
import importlib.machinery
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from types import MethodType

import profile_runtime as runtime
from parser_tokens import literal_arguments, sites

FILES = Path("/src/sqlglot")
CORE = "sqlglot.parser"
METHODS = ("_match", "_match_pair", "_match_text_seq", "_retreat")
CATALOG = Path(os.environ["BLAST_TOKEN_CATALOG"])
BITS = {item["id"]: 1 << index for index, item in enumerate(json.loads(CATALOG.read_text())["tests"])}
BITS["@collection"] = 1 << len(BITS)
WORDS = {
    values[0] for path in [FILES/"parser.py", *sorted((FILES/"parsers").glob("*.py"))]
    for node in ast.walk(ast.parse(path.read_bytes()))
    if (matched := literal_arguments(node)) is not None
    for method, values in [matched] if method == "_match_text_seq"
}
EXPECTED = {}


def scope(parser, bound, method):
    if type(bound) is not MethodType or bound.__func__ is not EXPECTED.get(method):
        return "unknown"
    cls = type(parser)
    if cls.__getattribute__ is not object.__getattribute__ or cls._curr is not EXPECTED["current"]:
        return "unknown"
    current = parser._curr
    if type(current) is not EXPECTED["Token"]:
        return "unknown"
    if method == "_match_text_seq":
        value = current.text
        if type(value) is not str:
            return "unknown"
        text = value.upper()
        return "value:" + text if text in WORDS else "other"
    token = current.token_type
    if type(token) is not EXPECTED.get("TokenType"):
        return "unknown"
    return "value:" + token.name


def observe(site, parser, method, bound, /, *args):
    trace = runtime.TRACE
    scope_mask = trace.tokens[site]
    key = scope(parser, bound, method)
    scope_mask[key] |= BITS.get(runtime.CONTEXT, BITS["@collection"])
    return bound(*args)


def guard(site, parser, prefixes, condition):
    trace = runtime.TRACE
    mask = BITS.get(runtime.CONTEXT, BITS["@collection"])
    for method, prefix in prefixes:
        token = scope(parser, getattr(parser, method), method)
        if method == "_match_text_seq":
            retreat = getattr(parser, "_retreat")
            if (
                type(retreat) is not MethodType or retreat.__func__ is not EXPECTED.get("_retreat")
                or type(parser.TEXT_MATCH_EXCLUDED_TOKENS) is not frozenset
            ):
                token = "unknown"
        if type(condition) is not bool or token == "unknown" or token == "value:" + prefix:
            trace.guards[site] |= mask
            break
    trace.guard_seen[site] |= mask
    return condition


class Inject(ast.NodeTransformer):
    def __init__(self, path, tree):
        self.path = path
        self.guards = {id(node): guards for node, guards in sites(tree).values()} if path == "sqlglot/parser.py" else {}

    def key(self, node):
        return f"{self.path}:{node.lineno}:{node.col_offset}"

    def visit_If(self, node):
        self.generic_visit(node)
        if prefixes := self.guards.get(id(node)):
            node.test = ast.copy_location(ast.Call(
                ast.Name("_blast_optional_guard", ast.Load()),
                [ast.Constant(self.key(node)), ast.Name("self", ast.Load()), ast.Constant(prefixes), node.test],
                [],
            ), node.test)
        return node

    def visit_Call(self, node):
        match = literal_arguments(node)
        self.generic_visit(node)
        if match is None:
            return node
        method, _ = match
        return ast.copy_location(ast.Call(
            ast.Name("_blast_parser_token", ast.Load()),
            [ast.Constant(self.key(node)), ast.Name("self", ast.Load()), ast.Constant(method), node.func, *node.args],
            [],
        ), node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = Path(self.path).relative_to("/src").as_posix()
        tree = ast.parse(self.get_data(self.path), self.path)
        transformed = Inject(path, tree).visit(tree)
        return compile(ast.fix_missing_locations(transformed), self.path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["_blast_parser_token"] = observe
        module.__dict__["_blast_optional_guard"] = guard
        super().exec_module(module)
        if module.__name__ == CORE:
            EXPECTED.update({method: getattr(module.Parser, method) for method in METHODS})
            EXPECTED["TokenType"] = module.TokenType
            EXPECTED["Token"] = module.Token
            EXPECTED["current"] = module.Parser._curr


class Finder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname != CORE and not fullname.startswith("sqlglot.parsers."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (
            spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and spec.origin.startswith("/src/sqlglot/")
        ):
            spec.loader = Loader(fullname, spec.origin)
        return spec


class Trace(runtime.Trace):
    def __init__(self, path):
        super().__init__(path)
        self.tokens = defaultdict(lambda: defaultdict(int))
        self.guards = defaultdict(int)
        self.guard_seen = defaultdict(int)

    def save(self):
        self.path.write_text(json.dumps({
            "seen": sorted(self.seen),
            "tokens": {site: {key: format(mask, "x") for key, mask in rows.items()} for site, rows in self.tokens.items()},
            "guards": {site: format(mask, "x") for site, mask in self.guards.items()},
            "guard_seen": {site: format(mask, "x") for site, mask in self.guard_seen.items()},
            "words": sorted(WORDS),
            "types": {
                name: value.name
                for name, value in EXPECTED["TokenType"].__members__.items()
            } if "TokenType" in EXPECTED else {},
        }, separators=(",", ":")))


def start(path):
    trace = Trace(path)
    runtime.TRACE = trace
    sys.meta_path.insert(0, Finder())
    return trace

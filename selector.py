"""Build a scorer schedule using dependencies observed at the pinned baseline."""

import argparse
import ast
import gzip
import json
import re
import sys
from dataclasses import dataclass
from functools import cache, cached_property
from itertools import accumulate
from pathlib import Path

from fixture_pairs import fixture_pair_users
from patches import parse_patch, split_lines
from semantic_edits import (
    default_edit, escape_table_changes, inserted_branch_exit, module_literal_edit,
    only_preloaded_imports, unreached_call_edit,
)
from semantics import Semantics

REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
COLLECTION_IMPORTS = (ast.Import, ast.ImportFrom)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
EXPRESSION_ROOTS = (
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Assign, ast.AnnAssign, ast.AugAssign,
    ast.Return, ast.Expr, ast.Assert, ast.Raise,
)
DIALECT_FAMILIES = ("dialects", "parsers", "generators", "typing")
DIRECTIVE = re.compile(rb"coding[:=]|type:|pragma:|noqa|fmt:|pytest|pylint|ruff", re.I)


def union(masks):
    result = 0
    for mask in masks:
        result |= mask
    return result


@dataclass
class FileMap:
    lines: dict[int, int]
    collection: set[int]
    imports: int
    readers: int
    owns: int
    executed: int

    def covered(self, lines):
        return union(self.lines.get(line, 0) for line in lines)


class Baseline:
    def __init__(self, path):
        document = json.loads(gzip.decompress(path.read_bytes()))
        if (document["format"], document["revision"]) != (1, REVISION):
            raise ValueError("map format or baseline revision mismatch")
        self.indices = {test_id: 1 << index for index, test_id in enumerate(document["ids"])}
        self.all = (1 << len(document["ids"])) - 1
        masks = [int(value, 16) for value in document["masks"]]
        self.files = {}
        for path, (rows, collection, imports, readers, owns) in document["files"].items():
            lines = {line: masks[index] for line, index in rows}
            self.files[path] = FileMap(
                lines, set(collection), masks[imports], masks[readers], masks[owns], union(lines.values())
            )
        self.references = {name: masks[index] for name, index in document["references"].items()}
        self.owners = {name: masks[index] for name, index in document["owners"].items()}


@dataclass(frozen=True)
class Function:
    name: str
    start: int
    end: int

    @property
    def lines(self):
        return range(self.start, self.end + 1)


class SourceShape:
    def __init__(self, source, filename, tree=None):
        self.source = source
        self.tree = tree if tree is not None else ast.parse(source, filename=filename, type_comments=True)
        self.parents = []
        self.functions = []
        self.assignments = {}
        self.imports = set()
        self.definition_lines = set()
        self.expressions = []
        self.comparisons = []
        self.lambda_bodies = []
        self.visit(self.tree)
        self.functions.sort(key=lambda scope: scope.end - scope.start)

    @cached_property
    def source_offsets(self):
        if self.source.startswith(b"\xef\xbb\xbf") or b"\r" in self.source:
            return ()
        rows = split_lines(self.source)
        if any(re.match(rb"^[ \t\f]*#.*coding[:=]", row) for row in rows[:2]):
            return ()
        return tuple(accumulate(map(len, rows), initial=0))

    def ascii_span(self, line, start_column, end_column):
        offsets = self.source_offsets
        if not 1 <= line < len(offsets):
            return None
        start, end = offsets[line - 1], offsets[line]
        if end_column > end - start or not self.source[start:end].isascii():
            return None
        return start + start_column, start + end_column

    def visit(self, node):
        kind = type(node)
        if not node._fields or kind in (ast.Name, ast.Constant):
            return
        if kind is ast.ClassDef:
            return self.visit_class(node)
        if kind in FUNCTIONS:
            return self.visit_function(node)
        if kind is ast.Lambda:
            return self.visit_lambda(node)
        if kind in COLLECTION_IMPORTS:
            self.imports.update(range(node.lineno, node.end_lineno + 1))
        if kind in EXPRESSION_ROOTS:
            self.expressions.append(range(node.lineno, node.end_lineno + 1))
        if kind is ast.Compare:
            self.comparisons.append(node)
        self.visit_children(node)

    def visit_class(self, node):
        owner = ".".join([*self.parents, node.name])
        for statement in node.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
            elif isinstance(statement, ast.AnnAssign):
                target = statement.target
            else:
                continue
            if isinstance(target, ast.Name) and (target.id.isupper() or target.id == "arg_types") and statement.value is not None:
                key = (owner, target.id)
                if key in self.assignments:
                    self.assignments[key] = None
                else:
                    self.assignments[key] = statement
        self.record_header(node)
        self.parents.append(node.name)
        self.visit_children(node)
        self.parents.pop()

    def visit_function(self, node):
        name = ".".join([*self.parents, node.name])
        start = self.record_header(node)
        self.functions.append(Function(name, start, node.end_lineno))
        self.parents.append(node.name)
        self.visit_children(node)
        self.parents.pop()

    def record_header(self, node):
        first = node.body[0]
        body_start = min((first.lineno, *(item.lineno for item in getattr(first, "decorator_list", ()))))
        header_start = min((node.lineno, *(item.lineno for item in node.decorator_list)))
        self.definition_lines.update(range(header_start, max(node.lineno + 1, body_start)))
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            self.definition_lines.update(range(first.lineno, first.end_lineno + 1))
        return body_start

    def visit_lambda(self, node):
        first = max(node.lineno + 1, node.body.lineno)
        if first <= node.body.end_lineno:
            self.lambda_bodies.append((range(first, node.body.end_lineno + 1), node))
        self.visit_children(node)

    def visit_children(self, node):
        for field in node._fields:
            value = getattr(node, field, None)
            if type(value) is list:
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)
            elif isinstance(value, ast.AST):
                self.visit(value)

    def scopes_at(self, lines):
        matches = {}
        for line in lines:
            match = next((scope for scope in self.functions if line in scope.lines), None)
            if match is None:
                return {}
            matches.setdefault(match, set()).add(line)
        return matches



class LineSourceShape(SourceShape):
    def __init__(self, source, filename, lines, tree=None):
        self.first = min(lines)
        self.last = max(lines)
        super().__init__(source, filename, tree=tree)

    def visit(self, node):
        start = getattr(node, "lineno", None)
        if start is not None:
            end = getattr(node, "end_lineno", None)
            if end is not None and end < self.first:
                return None
            if start > self.last:
                decorators = getattr(node, "decorator_list", ())
                if not decorators or decorators[0].lineno > self.last:
                    return None
        return super().visit(node)


def comment_only(before, after, old_lines, new_lines):
    for source, positions in ((before, old_lines), (after, new_lines)):
        rows = split_lines(source)
        for position in positions:
            row = rows[position - 1].lstrip()
            if not row.startswith(b"#") or (position == 1 and row.startswith(b"#!")) or DIRECTIVE.search(row):
                return False
    return True


def same_indentation(before, after, removed, added):
    old_rows = split_lines(before)
    new_rows = split_lines(after)
    for old_line, new_line in zip(sorted(removed), sorted(added)):
        earlier = old_rows[old_line - 1]
        later = new_rows[new_line - 1]
        old_indent = earlier[:len(earlier) - len(earlier.lstrip(b" \t\f"))]
        new_indent = later[:len(later) - len(later.lstrip(b" \t\f"))]
        if old_indent != new_indent:
            return False
    return True


def inert_module(tree):
    statements = tree.body
    if (
        statements and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and type(statements[0].value.value) is str
    ):
        statements = statements[1:]
    literals = (str, bytes, int, bool, float, type(None))

    def literal(value):
        if isinstance(value, ast.Tuple):
            return all(literal(item) for item in value.elts)
        return isinstance(value, ast.Constant) and type(value.value) in literals

    return all(
        isinstance(node, ast.Assign)
        and all(isinstance(target, ast.Name) for target in node.targets)
        and literal(node.value)
        for node in statements
    )


def literal_shape(value):
    if isinstance(value, dict):
        return (dict, tuple((type(key), key, literal_shape(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(literal_shape(item) for item in value))
    if type(value) in (bool, int, float, str, bytes, type(None)):
        return type(value)
    raise ValueError("unsupported class literal")


def without_values(tree, assignments):
    values = [node.value for node in assignments]
    try:
        for node in assignments:
            node.value = ast.Constant(None)
        return ast.dump(tree)
    finally:
        for node, value in zip(assignments, values):
            node.value = value


def without_lambda_body(tree, node):
    body = node.body
    try:
        node.body = ast.Constant(None)
        return ast.dump(tree)
    finally:
        node.body = body


def registry_lambda(shape, lines):
    first, last = min(lines), max(lines)
    candidates = []
    for node in ast.walk(shape.tree):
        if not isinstance(node, ast.Dict) or not (node.lineno <= first <= last <= node.end_lineno):
            continue
        for value in node.values:
            if (
                isinstance(value, ast.Lambda)
                and value.lineno == value.body.lineno
                and value.body.lineno <= first <= last <= value.body.end_lineno
            ):
                candidates.append(value)
    return candidates[0] if len(candidates) == 1 else None


def deferred_registry_site(old, new, removed, added):
    if not removed or not added:
        return None
    prior, updated = registry_lambda(old, removed), registry_lambda(new, added)
    if (
        prior is not None and updated is not None
        and without_lambda_body(old.tree, prior) == without_lambda_body(new.tree, updated)
    ):
        return f"{prior.lineno}:{prior.col_offset}"
    return None


def deferred_lambda(old, new, removed, added):
    if not removed or not added:
        return ()
    for old_span, old_node in old.lambda_bodies:
        if not removed.issubset(old_span):
            continue
        for new_span, new_node in new.lambda_bodies:
            if added.issubset(new_span) and (
                without_lambda_body(old.tree, old_node) == without_lambda_body(new.tree, new_node)
            ):
                return old_span
    return ()


def schema_key_edit(before, after):
    if (
        not isinstance(before, dict) or not isinstance(after, dict)
        or not all(type(key) is str and type(value) is bool
                   for schema in (before, after) for key, value in schema.items())
    ):
        return False
    shared_before = [(key, value) for key, value in before.items() if key in after]
    shared_after = [(key, value) for key, value in after.items() if key in before]
    return before.keys() != after.keys() and shared_before == shared_after


def touched_assignments(source, positions):
    members = {}
    accounted = set()
    for key, node in source.assignments.items():
        if node is None:
            continue
        covered = set(range(node.value.lineno, node.value.end_lineno + 1)) & positions
        if covered:
            members[key] = node
            accounted.update(covered)
    return members if accounted == positions else None


def class_updates(before, after, removed, added, enum_options=None, schema_keys=False):
    if not removed or not added:
        return ()
    earlier, later = touched_assignments(before, removed), touched_assignments(after, added)
    if earlier is None or later is None:
        return ()
    if earlier.keys() != later.keys():
        return ()
    try:
        for key, node in earlier.items():
            values = (node.value, later[key].value)
            known = enum_options.get(":".join(key)) if enum_options else None
            if known and all(isinstance(value, ast.Attribute) for value in values):
                enum_name, allowed = known
                if not all(
                    isinstance(value.value, ast.Name)
                    and value.value.id == enum_name
                    and value.attr in allowed
                    for value in values
                ):
                    return ()
            else:
                old_value, new_value = map(ast.literal_eval, values)
                if literal_shape(old_value) != literal_shape(new_value) and not (
                    schema_keys and key[1] == "arg_types"
                    and all(isinstance(value, ast.Dict) for value in values)
                    and len(values[0].keys) == len(old_value) and len(values[1].keys) == len(new_value)
                    and schema_key_edit(old_value, new_value)
                ):
                    return ()
    except (TypeError, ValueError, OverflowError):
        return ()
    if without_values(before.tree, list(earlier.values())) != without_values(after.tree, list(later.values())):
        return ()
    return earlier.keys()


def configuration_keys(before, after, membership_observed=False):
    if isinstance(before, ast.Set) and isinstance(after, ast.Set):
        values = [node.elts for node in (before, after)]
        if all(isinstance(item, ast.Constant) and type(item.value) is str for items in values for item in items):
            old, new = ({item.value for item in items} for items in values)
            return old ^ new
    if isinstance(before, ast.Dict) and isinstance(after, ast.Dict):
        if not all(isinstance(key, ast.Constant) and type(key.value) is str for node in (before, after) for key in node.keys):
            return None
        old, new = ([key.value for key in node.keys] for node in (before, after))
        if len(old) != len(set(old)) or len(new) != len(set(new)):
            return None
        if old != new and not membership_observed:
            return None
        old_nodes = dict(zip(old, before.values))
        new_nodes = dict(zip(new, after.values))
        shared = old_nodes.keys() & new_nodes.keys()
        if [key for key in old if key in shared] != [key for key in new if key in shared]:
            return None
        try:
            old_shapes, new_shapes = ({
                key: literal_shape(ast.literal_eval(node)) for key, node in values.items()
            } for values in (old_nodes, new_nodes))
            if any(old_shapes[key] != new_shapes[key] for key in shared):
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        return (old_nodes.keys() ^ new_nodes.keys()) | {
            key for key in shared if ast.dump(old_nodes[key]) != ast.dump(new_nodes[key])
        }
    return None


def parser_dispatch_entries(value, token_keys=False):
    if not isinstance(value, ast.Dict):
        return None
    entries = {}
    sources = []
    for key, body in zip(value.keys, value.values):
        if key is None and not entries:
            sources.append(ast.dump(body))
            continue
        if token_keys:
            if not (
                isinstance(key, ast.Attribute) and isinstance(key.value, ast.Name)
                and key.value.id == "TokenType"
            ):
                return None
            name = key.attr
        else:
            if not isinstance(key, ast.Constant) or type(key.value) is not str:
                return None
            name = key.value
        if name in entries:
            return None
        entries[name] = body
    return sources, entries


def parser_dispatch_keys(before, after):
    old, new = parser_dispatch_entries(before), parser_dispatch_entries(after)
    if old is None or new is None or old[0] != new[0]:
        return None
    old_entries, new_entries = old[1], new[1]
    shared = old_entries.keys() & new_entries.keys()
    if [key for key in old_entries if key in shared] != [key for key in new_entries if key in shared]:
        return None
    changed = (old_entries.keys() ^ new_entries.keys()) | {
        key for key in shared if ast.dump(old_entries[key]) != ast.dump(new_entries[key])
    }
    for entries in (old_entries, new_entries):
        for key in changed & entries.keys():
            body = entries[key]
            if not isinstance(body, ast.Lambda) or body.args.defaults or any(body.args.kw_defaults):
                return None
    return changed


def token_dispatch_value(node, stable_functions, existing):
    if isinstance(node, ast.Lambda):
        return not (node.args.defaults or any(node.args.kw_defaults))
    return (
        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "parser" and node.attr in stable_functions
        and ast.dump(node) in existing
    )


def token_dispatch_keys(before, after, stable_functions):
    old = parser_dispatch_entries(before, token_keys=True)
    new = parser_dispatch_entries(after, token_keys=True)
    if old is None or new is None or old[0] != new[0] or list(old[1]) != list(new[1]):
        return None
    existing = {ast.dump(node) for node in old[1].values()}
    changed = set()
    for key, prior in old[1].items():
        updated = new[1][key]
        if ast.dump(prior) == ast.dump(updated):
            continue
        if not all(token_dispatch_value(node, stable_functions, existing) for node in (prior, updated)):
            return None
        changed.add(key)
    return changed


class Selector:
    def __init__(self, baseline, source, whole_function=False, semantics=None):
        self.baseline = baseline
        self.source = source
        self.whole_function = whole_function
        self.semantics = None if whole_function else semantics

    @cache
    def load(self, path):
        return (self.source / path).read_bytes()

    @cache
    def source_tree(self, path):
        return ast.parse(self.load(path), filename=path, type_comments=True)

    @cache
    def shape(self, path):
        return SourceShape(self.load(path), path, tree=self.source_tree(path))

    def class_candidates(self, path, file, old, new, removed, added):
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "sqlglot" or parts[1] not in DIALECT_FAMILIES:
            return 0
        members = class_updates(old, new, removed, added)
        if not members:
            return 0
        module = path.removesuffix(".py").replace("/", ".")
        owners = union(self.baseline.owners.get(f"{module}:{owner}", 0) for owner, _ in members)
        peers = union(
            self.baseline.files[peer].executed
            for family in DIALECT_FAMILIES
            if (peer := f"sqlglot/{family}/{parts[-1]}") in self.baseline.files
        )
        direct_references = union(
            self.baseline.references.get(owner.split(".", 1)[0], 0) for owner, _ in members
        )
        return owners | peers | direct_references | file.imports | file.executed | file.readers

    def keyed_class_candidates(self, path, old, new, removed, added):
        if not getattr(self.semantics, "configuration_keys", None):
            return None
        parts = path.split("/")
        if len(parts) != 3 or parts[1] not in DIALECT_FAMILIES:
            return None
        if not removed or not added:
            changed = removed or added
            if not changed or max(changed) - min(changed) + 1 != len(changed):
                return None
            removed, added = removed or {min(changed)}, added or {min(changed)}
        before, after = touched_assignments(old, removed), touched_assignments(new, added)
        if not before or not after or len(before) != 1 or before.keys() != after.keys():
            return None
        owner, field = next(iter(before))
        module = path.removesuffix(".py").replace("/", ".")
        old_assignment, new_assignment = before[(owner, field)], after[(owner, field)]
        old_value, new_value = old_assignment.value, new_assignment.value
        membership_changed = (
            isinstance(old_value, ast.Dict) and isinstance(new_value, ast.Dict)
            and {ast.dump(key) if key else None for key in old_value.keys}
            != {ast.dump(key) if key else None for key in new_value.keys}
        )
        parser_module = path.startswith("sqlglot/parsers/")
        dispatch = field == "FUNCTION_PARSERS" and parser_module
        table_key = f"{module}:{owner}:{field}"
        token_dispatch = parser_module and table_key in getattr(self.semantics, "token_dispatch_keys", {})
        if dispatch:
            table = getattr(self.semantics, "parser_dispatch_keys", {})
        elif token_dispatch:
            table = self.semantics.token_dispatch_keys
        elif membership_changed:
            table = getattr(self.semantics, "dictionary_membership", {})
        else:
            table = self.semantics.configuration_keys
        evidence = table.get(table_key)
        if evidence is None:
            return None
        key_type = "str"
        metadata = 0
        if dispatch:
            keys = parser_dispatch_keys(old_value, new_value)
        elif token_dispatch:
            references = self.baseline.references
            metadata = union(references.get(name, 0) for name in (
                "__code__", "getmembers", "getsource", "getsourcelines", "vars",
            ))
            namespace_users = self.semantics.class_namespace_users.get(f"{module}:{owner}")
            metadata |= references.get("__dict__", 0) if namespace_users is None else int(namespace_users, 16)
            keys = token_dispatch_keys(old_value, new_value, self.semantics.token_dispatch_functions)
            key_type = "TokenType"
        else:
            keys = configuration_keys(old_value, new_value, membership_changed)
        if keys is None or without_values(old.tree, [old_assignment]) != without_values(new.tree, [new_assignment]):
            return None
        whole, lookup = evidence
        direct = int(self.semantics.direct.get(field, "0"), 16)
        return int(whole, 16) | direct | metadata | union(
            int(lookup.get(json.dumps([key_type, key], separators=(",", ":")), "0"), 16)
            for key in keys
        )

    def observed_class_candidates(self, path, file, old, new, removed, added, single_file):
        options = self.semantics.enum_fields.get(path, {}) if single_file else {}
        schema_keys = single_file and path.startswith("sqlglot/expressions/")
        if schema_keys and not (removed and added):
            changed = removed or added
            if changed and max(changed) - min(changed) + 1 == len(changed):
                removed, added = removed or {min(changed)}, added or {min(changed)}
        members = class_updates(old, new, removed, added, options, schema_keys)
        if not members:
            return None
        unknown = self.baseline.all & ~self.semantics.seen
        module = path.removesuffix(".py").replace("/", ".")
        owners = union(self.baseline.owners.get(f"{module}:{owner}", 0) for owner, _ in members)
        if path.startswith("sqlglot/expressions/") and len(members) == 1:
            owner, name = next(iter(members))
            value = old.assignments[(owner, name)].value
            if (
                name == "arg_types" and isinstance(value, ast.Dict) and value.keys
                and all(isinstance(key, ast.Constant) and type(key.value) is str for key in value.keys)
                and all(isinstance(item, ast.Constant) and type(item.value) is bool for item in value.values)
            ):
                if single_file:
                    before = ast.literal_eval(value)
                    after = ast.literal_eval(new.assignments[(owner, name)].value)
                    if before.keys() == after.keys():
                        changed = {key for key in before if before[key] != after[key]}
                        validators = self.semantics.schema_flag_users(path, owner, changed)
                        if validators is not None:
                            return validators | file.readers | (file.executed & unknown)
                schema = self.semantics.schema_users(path, owner)
                if schema is not None:
                    if single_file and before.keys() != after.keys():
                        schema |= int(self.semantics.direct.get("arg_types", "0"), 16)
                    return schema | file.readers | (file.executed & unknown)
        if path.startswith("sqlglot/dialects/") and len(members) == 1:
            owner, name = next(iter(members))
            key = (owner, name)
            value = old.assignments[key].value
            if name == "NUMERIC_ESCAPES" and isinstance(value, ast.Dict):
                before = ast.literal_eval(value)
                after = ast.literal_eval(new.assignments[key].value)
                keys = [item for item in before if before[item] != after[item]]
                observed = self.semantics.lookup_users(path, owner, keys)
                if observed is not None:
                    return observed | file.readers | (file.executed & unknown)
        if len(path.split("/")) != 3 or path.split("/")[1] not in DIALECT_FAMILIES:
            return None
        containers = getattr(self.semantics, "literal_containers", ())
        if not all(
            isinstance(old.assignments[key].value, (ast.Constant, ast.UnaryOp))
            or ":".join(key) in options
            or (
                key[1] in containers
                and isinstance(old.assignments[key].value, (ast.Dict, ast.List, ast.Tuple))
            )
            for key in members
        ):
            return None
        observed = self.semantics.attribute_users(path, members)
        if observed is not None:
            return observed | file.readers | (unknown & (owners | file.executed))
        return None

    def select_file(self, change, single_file=True):
        path = change.path
        if change.created:
            module = re.fullmatch(r"sqlglot/([a-z][a-z0-9_]*)\.py", path)
            seen, enumerators, looked_up = getattr(self.semantics, "added_modules", (0, 0, ()))
            if (
                single_file and module and seen == self.baseline.all
                and module[1] not in looked_up
                and path not in self.baseline.files and not (self.source / path).exists()
            ):
                after, *_ = change.apply(b"")
                tree = ast.parse(after, filename=path, type_comments=True)
                compile(tree, path, "exec", dont_inherit=True)
                if inert_module(tree):
                    return enumerators, "inert-added-module"
            return self.baseline.all, "added-file"
        file = self.baseline.files.get(path)
        if file is None:
            if single_file and path in getattr(self.semantics, "unread_docs", ()):
                updated, *_ = change.apply(self.load(path))
                updated.decode("utf-8")
                return 0, "unread-documentation"
            return self.baseline.all, "unmapped-file"
        before = self.load(path)
        after, removed, added, anchors = change.apply(before)
        if before == after:
            return 0, "identical"
        if not path.endswith(".py"):
            if single_file:
                users = fixture_pair_users(path, before, after, self.baseline, file.readers, self.source)
                if users is not None:
                    return users, "validated-fixture-pairs"
            return file.readers or self.baseline.all, "file-readers"

        old_lines = removed | anchors
        old = (
            LineSourceShape(before, path, old_lines, self.source_tree(path))
            if old_lines else self.shape(path)
        )
        new = LineSourceShape(after, path, added) if added else SourceShape(after, path)
        compile(new.tree, path, "exec", dont_inherit=True)
        if comment_only(before, after, removed, added) and ast.dump(old.tree) == ast.dump(new.tree):
            return file.readers, "comment-readers"
        if removed & old.imports or added & new.imports:
            if path.startswith("tests/") and file.owns and not path.endswith("/__init__.py"):
                return file.owns | file.imports | file.executed | file.readers, "test-import"
            if single_file and self.semantics is not None:
                lazy = self.semantics.lazy_modules.get(path)
                stable = (
                    self.semantics.stable_collection_modules
                    if file.collection
                    else self.semantics.stable_modules
                )
                warm = getattr(self.semantics, "warm_imports", {}).get(path)
                if warm:
                    stable = stable | warm
                if lazy is not None and only_preloaded_imports(old, new, removed, added, stable):
                    return int(lazy, 16), "observed-lazy-import"
            return self.baseline.all, "import-change"

        if single_file and self.semantics is not None and path.startswith("sqlglot/"):
            collection_sites = getattr(self.semantics, "collection_operands", None)
            if collection_sites:
                scoped = self.semantics.collection_comparison_users(
                    path, file, old, new, removed, added,
                )
                if scoped is not None:
                    metadata = union(self.baseline.references.get(name, 0) for name in (
                        "__code__", "__traceback__", "co_code", "co_consts", "co_firstlineno",
                        "co_lines", "co_names", "co_positions", "getsource", "getsourcelines",
                    ))
                    return scoped | file.readers | metadata, "observed-collection-comparison"

        escape_tables = getattr(self.semantics, "escape_tables", None)
        if single_file and escape_tables and path == "sqlglot/dialects/dialect.py":
            changes = escape_table_changes(old, new, removed, added)
            if changes:
                observed = self.semantics.escape_table_users(changes)
                if observed is not None:
                    return observed | file.readers, "observed-escape-table"

        constants = getattr(self.semantics, "module_literals", {}).get(path)
        if single_file and constants and old_lines:
            name = module_literal_edit(old, new, old_lines, added, constants)
            if name is not None:
                return int(constants[name], 16) | file.readers, "observed-module-binding"

        old_scopes = old.scopes_at(old_lines)
        new_scopes = new.scopes_at(added) if added else old_scopes
        within_body = (
            old_scopes
            and {scope.name for scope in old_scopes} == {scope.name for scope in new_scopes}
            and not (removed & old.definition_lines or added & new.definition_lines)
        )
        if within_body:
            absent = getattr(self.semantics, "unreached_calls", {}).get(path)
            if single_file and absent and len(old_scopes) == 1 and not file.covered(removed):
                scope, = old_scopes
                before_lines, after_lines = split_lines(before), split_lines(after)
                changed_source = [before_lines[line - 1] for line in removed]
                changed_source.extend(after_lines[line - 1] for line in added)
                if (
                    not file.collection.intersection(scope.lines)
                    and not any(DIRECTIVE.search(line) for line in changed_source)
                    and unreached_call_edit(old, new, removed, added, absent)
                ):
                    metadata = union(self.baseline.references.get(name, 0) for name in (
                        "__code__", "__traceback__", "co_code", "co_consts", "co_firstlineno",
                        "co_lines", "co_names", "co_positions", "getsource", "getsourcelines",
                        "getframeinfo", "f_lineno", "tb_lineno", "linecache",
                    ))
                    return file.readers | metadata, "unreached-literal-call"
            whole = (
                self.whole_function or anchors or len(removed) != len(added)
                or not same_indentation(before, after, removed, added)
            )
            if (
                not removed and single_file and not self.whole_function and len(old_scopes) == 1
                and path.startswith("sqlglot/")
            ):
                scope, = old_scopes
                edge = inserted_branch_exit(old, new, added, anchors, scope)
                if edge and not file.collection.intersection(scope.lines):
                    hits = file.covered(edge)
                    if hits:
                        references = getattr(self.baseline, "references", {})
                        metadata = union(references.get(name, 0) for name in (
                            "__code__", "co_code", "co_consts", "co_lines", "co_names",
                            "co_positions", "getsource", "getsourcelines", "f_lineno",
                        ))
                        return hits | file.readers | metadata, "inserted-branch-exit"
            hits = 0
            widened = False
            for scope, changed in old_scopes.items():
                relevant = set(changed)
                for expression in old.expressions:
                    if expression.start in scope.lines and any(line in expression for line in changed):
                        relevant.update(expression)
                if relevant & file.collection:
                    break
                scoped_hits = 0 if whole else file.covered(relevant)
                if not scoped_hits:
                    if file.collection.intersection(scope.lines):
                        break
                    scoped_hits = file.covered(scope.lines)
                    widened = True
                if not scoped_hits:
                    break
                hits |= scoped_hits
            else:
                if self.semantics is not None and path.startswith("sqlglot/"):
                    if single_file:
                        owned = self.semantics.fresh_copy_users(path, old, new, removed, added)
                        if owned & hits:
                            return (hits & ~owned) | file.readers, "observed-fresh-copy"
                    same = self.semantics.equivalent(path, file.lines, old, new, removed, added)
                    if same & hits:
                        return (hits & ~same) | file.readers, "observed-comparison"
                return hits | file.readers, "whole-function" if widened else "statement"

        if not file.collection.intersection(removed):
            lambda_body = deferred_lambda(old, new, removed, added)
            hits = file.covered(lambda_body)
            if hits:
                return hits | file.readers, "lambda-body"

        if single_file and getattr(self.semantics, "handlers", None):
            handlers = self.semantics.handlers.get(path)
            if handlers is not None:
                site = deferred_registry_site(old, new, removed, added)
                users = handlers.get(site)
                if users is not None:
                    metadata = self.baseline.references.get("__code__", 0)
                    unseen = file.executed & ~self.semantics.handler_seen
                    return users | unseen | metadata | file.readers, "observed-inline-handler"

        if path.startswith("tests/") and file.owns and not path.endswith("/__init__.py"):
            return file.owns | file.imports | file.executed | file.readers, "test-collection"
        semantic_source = self.semantics is not None and path.startswith("sqlglot/")
        if semantic_source:
            default = default_edit(old, new, removed, added)
            if default is not None:
                name, body, changed, may_escape = default
                if single_file and changed and may_escape:
                    users = self.semantics.factory_default_users(path, name, changed, file.executed)
                    if users is not None:
                        return users | file.readers, "observed-factory-default"
                if not file.collection.intersection(body):
                    called = file.covered(body)
                    if called:
                        if single_file and changed and not may_escape:
                            users = self.semantics.default_users(path, name, changed, called)
                            if users is not None:
                                return users | file.readers, "observed-default-readers"
                        references = self.baseline.references.get(name, 0)
                        return called | references | file.readers, "observed-default"
        if semantic_source and single_file and path.startswith("sqlglot/parsers/"):
            composed = self.semantics.composed_factory_users(path, old, new, file.executed)
            if composed is not None:
                return composed | file.readers, "observed-composed-factory"
        if isinstance(old, LineSourceShape):
            old = self.shape(path)
        if isinstance(new, LineSourceShape):
            new = SourceShape(after, path, tree=new.tree)
        if semantic_source:
            if single_file:
                keyed = self.keyed_class_candidates(path, old, new, removed, added)
                if keyed is not None:
                    return keyed | file.readers, "observed-configuration-keys"
            observed = self.observed_class_candidates(path, file, old, new, removed, added, single_file)
            if observed is not None:
                return observed, "observed-class"
            registry = self.semantics.registry_users(self, path, old, new, removed, added)
            if registry is not None:
                unseen = file.executed & ~self.semantics.seen
                return registry | file.readers | unseen, "observed-registry"
        owners = self.class_candidates(path, file, old, new, removed, added)
        if owners:
            return owners, "dialect-class"
        return self.baseline.all, "definition-or-collection"

    def select(self, patch):
        selected = 0
        reasons = set()
        try:
            changes = parse_patch(patch)
            for change in changes:
                candidates, reason = self.select_file(change, len(changes) == 1)
                selected |= candidates
                reasons.add(reason)
                if selected == self.baseline.all:
                    break
        except (OSError, ValueError, SyntaxError, RecursionError, OverflowError):
            return self.baseline.all, {"unsupported-patch"}
        return selected, reasons


def schedule(jobs):
    if not jobs:
        return []
    total = sum(duration for duration, _ in jobs)
    longest = jobs[0][0]
    best_cost = float("inf")
    best = []
    for count in range(1, min(8, len(jobs)) + 1):
        cost_bound = max(longest, (total + count - 1) // count) + 2000 * count
        if cost_bound >= best_cost:
            continue
        groups = [[] for _ in range(count)]
        loads = [0] * count
        for duration, number in jobs:
            worker = loads.index(min(loads))
            groups[worker].append(number)
            loads[worker] += duration
        cost = max(loads) + 2000 * sum(bool(group) for group in groups)
        if cost < best_cost:
            best_cost, best = cost, groups
    return [group for group in best if group]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/baseline"))
    parser.add_argument("--map", type=Path, default=Path(__file__).with_name("baseline.json.gz"))
    parser.add_argument("--whole-function", action="store_true")
    parser.add_argument("--explain", action="store_true")
    options = parser.parse_args()

    baseline = Baseline(options.map)
    semantics = None
    profile = options.map.with_name("semantics.json.gz")
    if not options.whole_function and profile.is_file():
        semantics = Semantics(profile, REVISION, baseline.indices)
    selector = Selector(baseline, options.source, options.whole_function, semantics)
    catalog = json.loads((options.data / "tests.json").read_text())["tests"]
    tests = sorted(
        (
            int(test["seconds"] * 1000 + 0.5),
            test["number"],
            baseline.indices.get(test["id"], 0),
        )
        for test in catalog
    )[::-1]
    for patch in sorted((options.data / "commits").glob("*.patch")):
        try:
            selected, reasons = selector.select(patch.read_bytes())
        except OSError:
            selected, reasons = baseline.all, {"unreadable-patch"}
        jobs = [(duration, number) for duration, number, mask in tests if not mask or selected & mask]
        workers = schedule(jobs)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        if options.explain:
            print(f"{patch.stem} tests={len(jobs)} workers={len(workers)} {','.join(sorted(reasons))}", file=sys.stderr)


if __name__ == "__main__":
    main()

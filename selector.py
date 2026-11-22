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

from patches import parse_patch, split_lines
from semantic_edits import default_edit, only_preloaded_imports
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


class SourceShape(ast.NodeVisitor):
    def __init__(self, source, filename):
        self.source = source
        self.tree = ast.parse(source, filename=filename, type_comments=True)
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
        if not node._fields or type(node) in (ast.Name, ast.Constant):
            return None
        return super().visit(node)

    def visit_ClassDef(self, node):
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
        self.generic_visit(node)
        self.parents.pop()

    def visit_FunctionDef(self, node):
        name = ".".join([*self.parents, node.name])
        start = self.record_header(node)
        self.functions.append(Function(name, start, node.end_lineno))
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def record_header(self, node):
        first = node.body[0]
        body_start = min((first.lineno, *(item.lineno for item in getattr(first, "decorator_list", ()))))
        header_start = min((node.lineno, *(item.lineno for item in node.decorator_list)))
        self.definition_lines.update(range(header_start, max(node.lineno + 1, body_start)))
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            self.definition_lines.update(range(first.lineno, first.end_lineno + 1))
        return body_start

    def visit_Lambda(self, node):
        first = max(node.lineno + 1, node.body.lineno)
        if first <= node.body.end_lineno:
            self.lambda_bodies.append((range(first, node.body.end_lineno + 1), node))
        self.generic_visit(node)

    def generic_visit(self, node):
        if isinstance(node, COLLECTION_IMPORTS):
            self.imports.update(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, EXPRESSION_ROOTS):
            self.expressions.append(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.Compare):
            self.comparisons.append(node)
        super().generic_visit(node)

    def scopes_at(self, lines):
        matches = {}
        for line in lines:
            match = next((scope for scope in self.functions if line in scope.lines), None)
            if match is None:
                return {}
            matches.setdefault(match, set()).add(line)
        return matches


def comment_only(before, after, old_lines, new_lines):
    for source, positions in ((before, old_lines), (after, new_lines)):
        rows = split_lines(source)
        for position in positions:
            row = rows[position - 1].lstrip()
            if not row.startswith(b"#") or (position == 1 and row.startswith(b"#!")) or DIRECTIVE.search(row):
                return False
    return True


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


def class_updates(before, after, removed, added):
    if not removed or not added:
        return ()
    matched = []
    for source, positions in ((before, removed), (after, added)):
        members = {}
        accounted = set()
        for key, node in source.assignments.items():
            if node is None:
                continue
            covered = set(range(node.value.lineno, node.value.end_lineno + 1)) & positions
            if covered:
                members[key] = node
                accounted.update(covered)
        if accounted != positions:
            return ()
        matched.append(members)
    earlier, later = matched
    if earlier.keys() != later.keys():
        return ()
    try:
        if any(
            literal_shape(ast.literal_eval(node.value))
            != literal_shape(ast.literal_eval(later[key].value))
            for key, node in earlier.items()
        ):
            return ()
    except (TypeError, ValueError, OverflowError):
        return ()
    if without_values(before.tree, list(earlier.values())) != without_values(after.tree, list(later.values())):
        return ()
    return earlier.keys()


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
    def shape(self, path):
        return SourceShape(self.load(path), path)

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

    def observed_class_candidates(self, path, file, old, new, removed, added, single_file):
        members = class_updates(old, new, removed, added)
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
                    changed = {key for key in before if before[key] != after[key]}
                    validators = self.semantics.schema_flag_users(path, owner, changed)
                    if validators is not None:
                        return validators | file.readers | (file.executed & unknown)
                schema = self.semantics.schema_users(path, owner)
                if schema is not None:
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
        if not all(
            isinstance(old.assignments[key].value, (ast.Constant, ast.UnaryOp)) for key in members
        ):
            return None
        observed = self.semantics.attribute_users(path, members)
        if observed is not None:
            return observed | file.readers | (unknown & (owners | file.executed))
        return None

    def select_file(self, change, single_file=True):
        path = change.path
        file = self.baseline.files.get(path)
        if file is None:
            return self.baseline.all, "unmapped-file"
        before = self.load(path)
        after, removed, added, anchors = change.apply(before)
        if before == after:
            return 0, "identical"
        if not path.endswith(".py"):
            return file.readers or self.baseline.all, "file-readers"

        old, new = self.shape(path), SourceShape(after, path)
        compile(new.tree, path, "exec", dont_inherit=True)
        if comment_only(before, after, removed, added) and ast.dump(old.tree) == ast.dump(new.tree):
            return file.readers, "comment-readers"
        if removed & old.imports or added & new.imports:
            if path.startswith("tests/") and file.owns and not path.endswith("/__init__.py"):
                return file.owns | file.imports | file.executed | file.readers, "test-import"
            if single_file and self.semantics is not None:
                lazy = self.semantics.lazy_modules.get(path)
                if lazy is not None and only_preloaded_imports(
                    old, new, removed, added, self.semantics.stable_modules
                ):
                    return int(lazy, 16), "observed-lazy-import"
            return self.baseline.all, "import-change"

        old_scopes = old.scopes_at(removed | anchors)
        new_scopes = new.scopes_at(added) if added else old_scopes
        within_body = (
            old_scopes
            and {scope.name for scope in old_scopes} == {scope.name for scope in new_scopes}
            and not (removed & old.definition_lines or added & new.definition_lines)
        )
        if within_body:
            hits = 0
            widened = False
            for scope, changed in old_scopes.items():
                relevant = set(changed)
                for expression in old.expressions:
                    if expression.start in scope.lines and any(line in expression for line in changed):
                        relevant.update(expression)
                if relevant & file.collection:
                    break
                whole = self.whole_function or anchors or len(removed) != len(added)
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
                    same = self.semantics.equivalent(path, file.lines, old, new, removed, added)
                    if same & hits:
                        return (hits & ~same) | file.readers, "observed-comparison"
                return hits | file.readers, "whole-function" if widened else "statement"

        if not file.collection.intersection(removed):
            lambda_body = deferred_lambda(old, new, removed, added)
            hits = file.covered(lambda_body)
            if hits:
                return hits | file.readers, "lambda-body"

        if path.startswith("tests/") and file.owns and not path.endswith("/__init__.py"):
            return file.owns | file.imports | file.executed | file.readers, "test-collection"
        if self.semantics is not None and path.startswith("sqlglot/"):
            default = default_edit(old, new, removed, added)
            if default is not None:
                name, body, changed, may_escape = default
                if not file.collection.intersection(body):
                    called = file.covered(body)
                    if called:
                        if single_file and changed and not may_escape:
                            users = self.semantics.default_users(path, name, changed, called)
                            if users is not None:
                                return users | file.readers, "observed-default-readers"
                        references = self.baseline.references.get(name, 0)
                        return called | references | file.readers, "observed-default"
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

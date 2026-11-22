"""Prove optimizer edits leave eligible executor fixture queries unchanged."""

import ast
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

from patches import parse_patch
from semantic_edits import COMPARISONS, inserted_branch_exit

EXECUTOR_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
MINIMUM_SAVINGS = 12000
MAXIMUM_QUERIES = 8
MAXIMUM_PROBES = 4

def identical_replacement(earlier, later, receiver, field, expected):
    actual = getattr(receiver, field)
    setattr(receiver, field, expected)
    try:
        return ast.dump(earlier) == ast.dump(later)
    finally:
        setattr(receiver, field, actual)


def projected_edit(path, before, after, removed, added, anchors):
    if not path.startswith("sqlglot/optimizer/") or path.endswith("/__init__.py"):
        return None
    from selector import SourceShape

    old = SourceShape(before, path)
    new = SourceShape(after, path)
    scopes = old.scopes_at(removed | anchors)
    if len(scopes) != 1:
        return None
    scope, = scopes
    if not removed:
        return ("early-return", anchors) if inserted_branch_exit(old, new, added, anchors, scope) else None
    if removed != added:
        return None
    if old.definition_lines.intersection(removed):
        return None
    old_expression = old.comparisons
    new_expression = {(node.lineno, node.col_offset): node for node in new.comparisons}
    for node in old_expression:
        if not removed.issubset(range(node.lineno, node.end_lineno + 1)):
            continue
        target = new_expression.get((node.lineno, node.col_offset))
        if target is None or len(node.ops) != 1 or len(target.ops) != 1:
            continue
        before_op, after_op = type(node.ops[0]).__name__, type(target.ops[0]).__name__
        if before_op != after_op and {before_op, after_op} <= COMPARISONS.keys():
            if identical_replacement(old.tree, new.tree, target, "ops", node.ops):
                return "comparison", set(range(node.lineno, node.end_lineno + 1))
    tests = {
        (node.lineno, node.col_offset): node for node in ast.walk(new.tree)
        if isinstance(node, ast.If)
    }
    for node in ast.walk(old.tree):
        if not isinstance(node, ast.If) or not removed.issubset(range(node.test.lineno, node.test.end_lineno + 1)):
            continue
        updated = tests.get((node.lineno, node.col_offset))
        if updated is None:
            continue
        original, proposed = node.test, updated.test
        for operand, negation in ((original, proposed), (proposed, original)):
            if not (isinstance(negation, ast.UnaryOp) and isinstance(negation.op, ast.Not)
                    and ast.dump(operand) == ast.dump(negation.operand)):
                continue
            if identical_replacement(old.tree, new.tree, updated, "test", node.test):
                return "negated-condition", set(range(node.test.lineno, node.test.end_lineno + 1))
    return None


class ExecutorProof:
    def __init__(self, path, source, revision):
        self.path = path
        self.source = source
        self.profile = json.loads(gzip.decompress(path.read_bytes()))
        if self.profile["revision"] != revision:
            raise ValueError("the executor proof has a different baseline revision")
        self.probes = 0

    def queries(self, patch):
        changes = parse_patch(patch.read_bytes())
        if len(changes) != 1:
            return ()
        change, = changes
        if change.created or change.path not in self.profile["lines"]:
            return ()
        initial = (self.source/change.path).read_bytes()
        updated, removed, added, anchors = change.apply(initial)
        matched = projected_edit(change.path, initial, updated, removed, added, anchors)
        if matched is None:
            return ()
        _, lines = matched
        if not lines.isdisjoint(self.profile["outside"].get(change.path, ())):
            return ()
        captured = self.profile["lines"][change.path]
        return sorted({index for line in lines for index in captured.get(str(line), ())})

    def permits_omission(self, patch):
        if self.probes >= MAXIMUM_PROBES:
            return False
        try:
            queries = self.queries(patch)
            if not 0 < len(queries) <= MAXIMUM_QUERIES:
                return False
            self.probes += 1
            env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=os.pathsep.join((str(self.source),str(Path(__file__).parent))))
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("executor_replay.py")),
                 str(self.source), str(patch), str(self.path), *(str(index) for index in queries)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=False, timeout=10,
            )
            return result.returncode == 0 and result.stdout == b"equivalent\n"
        except (OSError, ValueError, SyntaxError, RecursionError, subprocess.TimeoutExpired):
            return False

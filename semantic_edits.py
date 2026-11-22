"""Recognize source changes covered by the baseline semantic profile."""

import ast
import operator

EQUALITY = {
    "Eq": operator.eq,
    "NotEq": operator.ne,
    "Is": operator.is_,
    "IsNot": operator.is_not,
}
ORDERING = {
    "Lt": operator.lt,
    "LtE": operator.le,
    "Gt": operator.gt,
    "GtE": operator.ge,
}
COMPARISONS = EQUALITY | ORDERING
CONSTANT_COMPARISONS = {"Eq", "NotEq", *ORDERING}
UNCHANGED = object()
LITERAL_VALUES = (bool, int, float, str, bytes, type(None))


def comparable_literal(node):
    if isinstance(node, ast.Constant):
        return type(node.value) in LITERAL_VALUES
    if isinstance(node, ast.Tuple):
        return len(node.elts) <= 8 and all(
            isinstance(item, ast.Constant) and comparable_literal(item)
            for item in node.elts
        )
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and (
            isinstance(node.operand, ast.Constant)
            and type(node.operand.value) in (int, float)
        )
    )


def compare_edit(old, new, removed, added):
    if not removed or not added:
        return None
    matches = []
    for shape, lines in ((old, removed), (new, added)):
        nodes = [
            node
            for node in ast.walk(shape.tree)
            if isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and type(node.ops[0]).__name__ in COMPARISONS
            and min(lines) >= node.lineno
            and max(lines) <= node.end_lineno
        ]
        if len(nodes) != 1:
            return None
        matches.append(nodes[0])
    earlier, later = matches
    before, after = (type(node.ops[0]).__name__ for node in matches)
    original_ops, original_right = later.ops, later.comparators
    replacement = UNCHANGED
    if before == after:
        first, last = earlier.comparators[0], later.comparators[0]
        if before not in CONSTANT_COMPARISONS or not all(
            comparable_literal(item) for item in (first, last)
        ):
            return None
        initial, replacement = ast.literal_eval(first), ast.literal_eval(last)
        if type(initial) is not type(replacement) or initial == replacement:
            return None
        later.comparators = earlier.comparators
    else:
        later.ops = earlier.ops
    try:
        if ast.dump(old.tree) == ast.dump(new.tree):
            return earlier.lineno, earlier.col_offset, before, after, replacement
    finally:
        later.ops, later.comparators = original_ops, original_right
    return None


def default_edit(old, new, removed, added):
    if not removed or not added:
        return None
    matches = []
    for shape, lines in ((old, removed), (new, added)):
        functions = []
        for node in shape.tree.body:
            if not isinstance(node, ast.FunctionDef) or node.decorator_list:
                continue
            values = [
                *node.args.defaults,
                *(item for item in node.args.kw_defaults if item is not None),
            ]
            if values and all(
                isinstance(item, ast.Constant) and comparable_literal(item)
                for item in values
            ):
                if lines <= {item.lineno for item in values}:
                    functions.append((node, [type(item.value) for item in values]))
        if len(functions) != 1:
            return None
        matches.append(functions[0])
    (earlier, first_types), (later, last_types) = matches
    if first_types != last_types:
        return None
    original_defaults, original_keywords = later.args.defaults, later.args.kw_defaults
    try:
        later.args.defaults, later.args.kw_defaults = (
            earlier.args.defaults,
            earlier.args.kw_defaults,
        )
        if ast.dump(old.tree) == ast.dump(new.tree):
            return earlier.name, range(earlier.body[0].lineno, earlier.end_lineno + 1)
    finally:
        later.args.defaults, later.args.kw_defaults = (
            original_defaults,
            original_keywords,
        )
    return None


def only_preloaded_imports(old, new, removed, added, stable_modules):
    def stripped(tree, lines):
        body = []
        count = 0
        for statement in tree.body:
            covered = range(statement.lineno, statement.end_lineno + 1)
            if not lines.intersection(covered) or not isinstance(
                statement, (ast.Import, ast.ImportFrom)
            ):
                body.append(statement)
                continue
            if not lines.issuperset(covered):
                return None
            if isinstance(statement, ast.Import):
                stable = all(alias.name in stable_modules for alias in statement.names)
            else:
                stable = (
                    not statement.level
                    and statement.module in stable_modules
                    and all(alias.name != "*" for alias in statement.names)
                )
            if not stable:
                return None
            count += 1
        return ast.dump(ast.Module(body=body, type_ignores=tree.type_ignores)), count

    before = stripped(old.tree, removed)
    after = stripped(new.tree, added)
    return (
        before is not None
        and after is not None
        and before[0] == after[0]
        and before[1] + after[1] > 0
    )

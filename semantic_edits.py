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


OPERATOR_TOKENS = {
    "Eq": [b"=="],
    "NotEq": [b"!="],
    "Is": [b"is"],
    "IsNot": [b"is", b"not"],
    "Lt": [b"<"],
    "LtE": [b"<="],
    "Gt": [b">"],
    "GtE": [b">="],
}


def equal_outside_spans(old, new, old_span, new_span):
    if old_span is None or new_span is None:
        return False
    start, end = old_span
    latest_start, latest_end = new_span
    return (
        start == latest_start
        and old.source[:start] == new.source[:latest_start]
        and old.source[end:] == new.source[latest_end:]
    )


def operator_only(old, new, earlier, later, before, after):
    spans = []
    for shape, node, operator in ((old, earlier, before), (new, later, after)):
        first, last = node.left, node.comparators[0]
        if not first.end_lineno == last.lineno == node.lineno == node.end_lineno:
            return False
        if first.end_col_offset >= last.col_offset:
            return False
        span = shape.ascii_span(node.lineno, first.end_col_offset, last.col_offset)
        if span is None or shape.source[span[0]:span[1]].split() != OPERATOR_TOKENS[operator]:
            return False
        spans.append(span)
    return equal_outside_spans(old, new, *spans)


def literal_only(old, new, first, last):
    if not (
        isinstance(first, ast.Constant) and isinstance(last, ast.Constant)
        and first.lineno == first.end_lineno == last.lineno == last.end_lineno
    ):
        return False
    first_span = old.ascii_span(first.lineno, first.col_offset, first.end_col_offset)
    last_span = new.ascii_span(last.lineno, last.col_offset, last.end_col_offset)
    return equal_outside_spans(old, new, first_span, last_span)


def compare_edit(old, new, removed, added):
    if not removed or not added:
        return None
    matches = []
    for shape, lines in ((old, removed), (new, added)):
        first_line, last_line = min(lines), max(lines)
        nodes = [
            node
            for node in shape.comparisons
            if len(node.ops) == 1
            and type(node.ops[0]).__name__ in COMPARISONS
            and node.lineno <= first_line
            and last_line <= node.end_lineno
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
        if literal_only(old, new, first, last):
            return earlier.lineno, earlier.col_offset, before, after, replacement
        later.comparators = earlier.comparators
    else:
        if operator_only(old, new, earlier, later, before, after):
            return earlier.lineno, earlier.col_offset, before, after, replacement
        later.ops = earlier.ops
    try:
        if ast.dump(old.tree) == ast.dump(new.tree):
            return earlier.lineno, earlier.col_offset, before, after, replacement
    finally:
        later.ops, later.comparators = original_ops, original_right
    return None


def argument_defaults(arguments):
    positional = arguments.posonlyargs + arguments.args
    pairs = zip(
        positional[len(positional) - len(arguments.defaults) :], arguments.defaults
    )
    keywords = zip(arguments.kwonlyargs, arguments.kw_defaults)
    return {
        argument.arg: value.value
        for argument, value in (*pairs, *keywords)
        if isinstance(value, ast.Constant)
    }


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
    old_values = argument_defaults(earlier.args)
    new_values = argument_defaults(later.args)
    if old_values.keys() != new_values.keys():
        return None
    changed = tuple(
        name for name, value in old_values.items() if value != new_values[name]
    )
    may_escape = any(
        node is not earlier
        and isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.Global,
                ast.Nonlocal,
                ast.Yield,
                ast.YieldFrom,
            ),
        )
        for node in ast.walk(earlier)
    )
    try:
        later.args.defaults, later.args.kw_defaults = (
            earlier.args.defaults,
            earlier.args.kw_defaults,
        )
        if ast.dump(old.tree) == ast.dump(new.tree):
            return (
                earlier.name,
                range(earlier.body[0].lineno, earlier.end_lineno + 1),
                changed,
                may_escape,
            )
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

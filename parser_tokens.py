"""Find optional parser blocks whose only entry is a literal token match."""

import ast


def prefixes(node):
    if node.orelse or len(node.body) != 1 or not isinstance(node.body[0], ast.If):
        return ()
    branch = node.body[0]
    found = []
    while True:
        match = literal_arguments(branch.test)
        if match is None:
            return ()
        method, values = match
        found.append((method, values[0]))
        if not branch.orelse:
            return tuple(found)
        if len(branch.orelse) != 1 or not isinstance(branch.orelse[0], ast.If):
            return ()
        branch = branch.orelse[0]


def sites(tree):
    parser = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Parser"), None)
    if parser is None:
        return {}
    return {
        f"{node.lineno}:{node.col_offset}": (node, guards)
        for function in parser.body if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.If) and (guards := prefixes(node))
    }


def literal_arguments(call):
    if (
        not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute)
        or not isinstance(call.func.value, ast.Name) or call.func.value.id != "self"
        or not call.args or call.keywords
    ):
        return None
    method = call.func.attr
    if method == "_match_text_seq" and all(
        isinstance(arg, ast.Constant) and type(arg.value) is str for arg in call.args
    ):
        return method, tuple(arg.value for arg in call.args)
    if method in ("_match", "_match_pair") and len(call.args) == (
        1 if method == "_match" else 2
    ) and all(
        isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
        and arg.value.id == "TokenType" for arg in call.args
    ):
        return method, tuple(arg.attr for arg in call.args)
    return None


def covering_nodes(tree, lines, accepts):
    return {
        (node.lineno, node.col_offset): node
        for node in ast.walk(tree)
        if accepts(node) and all(node.lineno <= line <= node.end_lineno for line in lines)
    }


def same_tree(before, after, parent, field, replacement):
    original = getattr(parent, field)
    try:
        setattr(parent, field, replacement)
        return ast.dump(before) == ast.dump(after)
    finally:
        setattr(parent, field, original)


def negates(left, right):
    return (
        isinstance(left, ast.UnaryOp) and isinstance(left.op, ast.Not)
        and ast.dump(left.operand) == ast.dump(right)
    )


def affected_tests(profile, path, before, after, removed, added):
    if not removed or removed != added:
        return None
    file = profile["files"].get(path)
    if not file or not removed.intersection(file["lines"]):
        return None

    guards = file["guards"]
    if guards:
        old_conditions = covering_nodes(before, removed, lambda node: isinstance(node, ast.If))
        new_conditions = covering_nodes(after, added, lambda node: isinstance(node, ast.If))
        for location, old in old_conditions.items():
            coordinate = "%d:%d" % location
            new = new_conditions.get(location)
            if coordinate not in guards or new is None:
                continue
            if not (negates(old.test, new.test) or negates(new.test, old.test)):
                continue
            if same_tree(before, after, new, "test", old.test):
                entered, possible = guards[coordinate]
                return int(entered, 16), int(possible, 16)

    old_calls = covering_nodes(before, removed, literal_arguments)
    new_calls = covering_nodes(after, added, literal_arguments)
    for location, old in old_calls.items():
        entry = file["calls"].get("%d:%d" % location)
        new = new_calls.get(location)
        if not entry or new is None:
            continue
        old_method, old_values = literal_arguments(old)
        new_method, new_values = literal_arguments(new)
        if old_method != new_method or old_values == new_values:
            continue
        domain = profile["words"] if old_method == "_match_text_seq" else profile["types"]
        if any(value not in domain for value in (*old_values, *new_values)):
            continue
        if not same_tree(before, after, new, "args", old.args):
            continue
        masks = profile["masks"]
        entered, unknown, values = entry
        affected = int(masks[unknown], 16)
        for token in (old_values[0], new_values[0]):
            value = token if old_method == "_match_text_seq" else domain[token]
            affected |= int(masks[values.get(value, 0)], 16)
        return int(masks[entered], 16), affected
    return None

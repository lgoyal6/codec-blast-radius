"""Keep the executor consumers of validated fixture edits."""
import sys

EXECUTOR = "tests/test_executor.py::TestExecutor::"
FIXTURES = {
    f"tpc{kind}": f"tests/fixtures/optimizer/tpc-{kind}/tpc-{kind}.sql"
    for kind in ("h", "ds")
}
PAIRS = {path: kind for kind, path in FIXTURES.items()}
EXECUTES = {kind: EXECUTOR + f"test_execute_{kind}" for kind in FIXTURES}
OPTIMIZED_TPCH = EXECUTOR + "test_optimized_tpch"
CONSUMERS = {
    "tpch": (EXECUTES["tpch"], OPTIMIZED_TPCH),
    "tpcds": (EXECUTES["tpcds"],),
}


def pairs(data):
    lines = data.decode("utf-8").splitlines()
    statements = "\n".join(line for line in lines if line and not line.startswith("--")).split(";")
    result = []
    for index in range(0, len(statements) - 1, 2):
        lines = statements[index].strip().split("\n")
        metadata = {}
        offset = 0
        while lines[offset].startswith("#"):
            key, value = lines[offset].split(":", maxsplit=1)
            metadata[key.lstrip("#").strip()] = value.strip()
            offset += 1
        result.append((tuple(sorted(metadata.items())), "\n".join(lines[offset:]), statements[index + 1].strip()))
    return result


def read_only_asts(queries, source):
    sys.path.insert(0, str(source))
    try:
        from sqlglot import exp, parse_one
        from sqlglot.errors import SqlglotError
    finally:
        sys.path.pop(0)
    prohibited = tuple(getattr(exp, name) for name in (
        "Anonymous", "Command", "Create", "Drop", "Insert", "Update", "Delete",
        "Merge", "Into", "Copy", "Pragma", "Set", "Transaction", "Commit", "Rollback",
    ) if hasattr(exp, name))
    result = []
    try:
        for query in queries:
            tree = parse_one(query)
            if not isinstance(tree, exp.Query) or any(isinstance(node, prohibited) for node in tree.walk()):
                return None
            result.append(tree)
    except (SqlglotError, ValueError):
        return None
    return result


def literal_query_edit(old, new, source):
    trees = read_only_asts((old, new), source)
    if trees is None:
        return False
    from sqlglot import exp

    def erase_values(node):
        if type(node) is exp.Literal:
            return exp.Literal(this="0", is_string=node.is_string)
        return node

    before, after = (tree.transform(erase_values, copy=False) for tree in trees)
    return before == after


def fixture_pair_users(path, before, after, baseline, readers, source):
    kind = PAIRS.get(path)
    if kind is None:
        return None
    try:
        original, revised = pairs(before), pairs(after)
    except (IndexError, ValueError):
        return None
    if len(original) != len(revised):
        return None

    selected = set()
    cache_keys = set()
    for old, new in zip(original, revised):
        old_meta, old_input, old_expected = old
        new_meta, new_input, new_expected = new
        metadata = dict(old_meta), dict(new_meta)
        if {key for key in metadata[0].keys() | metadata[1].keys()
                if metadata[0].get(key) != metadata[1].get(key)} - {"execute"}:
            return None
        if old_input != new_input:
            if not literal_query_edit(old_input, new_input, source):
                return None
            selected.update(CONSUMERS[kind])
            cache_keys.update((old_input, new_input))
        if old_meta != new_meta:
            if read_only_asts((old_input, new_input), source) is None:
                return None
            selected.add(EXECUTES[kind])
            cache_keys.update((old_input, new_input))
        if old_expected != new_expected and kind == "tpch":
            if read_only_asts((old_expected, new_expected), source) is None:
                return None
            selected.add(OPTIMIZED_TPCH)

    if cache_keys:
        peer = "tpcds" if kind == "tpch" else "tpch"
        try:
            sibling_inputs = {sql for _, sql, _ in pairs((source / FIXTURES[peer]).read_bytes())}
        except (IndexError, OSError, ValueError):
            return None
        if cache_keys & sibling_inputs:
            selected.update(CONSUMERS[peer])
    if any(not baseline.indices.get(name, 0) & readers for name in CONSUMERS[kind]):
        return None
    if any(name not in baseline.indices for name in selected):
        return None
    scoped_readers = sum(mask for test, mask in baseline.indices.items() if test.startswith(EXECUTOR) and mask & readers)
    return (readers & ~scoped_readers) | sum(baseline.indices[name] for name in selected)

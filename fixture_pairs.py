"""Narrow read-only expected-fixture changes to their actual consumers."""
import sys

EXECUTOR = "tests/test_executor.py::TestExecutor::"
PAIRS = {
    f"tests/fixtures/optimizer/tpc-{kind}/tpc-{kind}.sql": f"tpc{kind}"
    for kind in ("h", "ds")
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


def read_only_queries(queries, source):
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
    try:
        for query in queries:
            ast = parse_one(query)
            if not isinstance(ast, exp.Query) or any(isinstance(node, prohibited) for node in ast.walk()):
                return False
    except (SqlglotError, ValueError):
        return False
    return True


def fixture_pair_users(path, before, after, baseline, readers, source):
    kind = PAIRS.get(path)
    if kind is None:
        return None
    try:
        original = pairs(before)
        revised = pairs(after)
    except (IndexError, ValueError):
        return None
    if [item[:2] for item in original] != [item[:2] for item in revised]:
        return None
    changes = [query for old, new in zip(original, revised) if old[2] != new[2] for query in (old[2], new[2])]
    name = EXECUTOR + f"test_optimized_{kind}"
    if not baseline.indices.get(name, 0) & readers or not read_only_queries(changes, source):
        return None
    scoped_readers = sum(mask for test, mask in baseline.indices.items() if test.startswith(EXECUTOR) and mask & readers)
    return (readers & ~scoped_readers) | baseline.indices[name]

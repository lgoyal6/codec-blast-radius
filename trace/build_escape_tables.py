"""Compile observations of the pinned dialect escape tables into masks."""

import argparse
import ast
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector import Baseline, REVISION, union


DIALECT = "sqlglot.dialects.dialect"
COLLECTION = "@collection"
MODULES = {DIALECT, "sqlglot.tokenizer_core", "sqlglot.generator", "sqlglot.generators.bigquery"}
FIELDS = {"UNESCAPED_SEQUENCES", "ESCAPED_SEQUENCES", "IDENTIFIER_ESCAPED_SEQUENCES"}
INTERNAL_READS = {
    "sqlglot/dialects/dialect.py": {
        "UNESCAPED_SEQUENCES", "klass.UNESCAPED_SEQUENCES", "klass.ESCAPED_SEQUENCES",
    },
    "sqlglot/tokens.py": {"self.dialect.UNESCAPED_SEQUENCES"},
    "sqlglot/generator.py": {
        "self.dialect.ESCAPED_SEQUENCES", "self.dialect.IDENTIFIER_ESCAPED_SEQUENCES",
    },
    "sqlglot/generators/bigquery.py": {"self.dialect.ESCAPED_SEQUENCES"},
}
NAMESPACE_LOOKUPS = {
    "sqlglot/dialects/dialect.py": {
        'INVERSE_TIME_MAPPING', 'Tokenizer', 'JSONPathTokenizer', 'Parser',
        'parser_class', 'Generator', 'generator_class',
    },
}
REFLECTION = (
    "__getattribute__", "__globals__", "co_consts", "get_code",
    "getmembers", "getattr_static", "getsource", "getsourcelines", "vars",
)


def direct_readers(source, baseline):
    scopes = set()
    mask = 0
    for path, file in baseline.files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse((source / path).read_bytes())
        internal = INTERNAL_READS.get(path, ())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif isinstance(node, ast.Constant) and type(node.value) is str:
                name = node.value
            elif isinstance(node, ast.ImportFrom) and any(
                alias.name in FIELDS for alias in node.names
            ):
                scopes.add(COLLECTION)
                continue
            else:
                continue
            if name in FIELDS and ast.unparse(node) not in internal:
                mask |= file.lines.get(node.lineno, 0)
                if node.lineno in file.collection:
                    scopes.add(COLLECTION)
    return mask, scopes



def namespace_readers(source, baseline):
    mask = 0
    observed = 0
    for path, file in baseline.files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse((source / path).read_bytes())
        scoped = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Attribute)
                and ast.unparse(node.func.value) == "klass.__dict__"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in NAMESPACE_LOOKUPS.get(path, ())
            ):
                scoped.add(node.func.value)
            if (
                path == "sqlglot/tokens.py" and isinstance(node, ast.Compare)
                and ast.unparse(node) == "'BYTE_STRING_ESCAPES' in k.__dict__"
            ):
                scoped.add(node.comparators[0])
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and node.attr == "__dict__"
                or isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id == "__dict__"
                or isinstance(node, ast.Constant) and node.value == "__dict__"
            ):
                hits = file.lines.get(node.lineno, 0)
                observed |= hits
                if node not in scoped:
                    mask |= hits
    if observed != baseline.references.get("__dict__", 0):
        raise ValueError("namespace reference masks do not match the pinned source")
    return mask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("semantics", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--lookups", type=Path, nargs="+", required=True)
    parser.add_argument("--module-reads", type=Path, nargs="+", required=True)
    args = parser.parse_args()

    revision = subprocess.check_output(
        ["git", "-C", args.source, "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", args.source, "status", "--porcelain"], text=True
    )
    if dirty or revision != REVISION:
        raise ValueError("expected the clean pinned baseline")

    baseline = Baseline(args.baseline)
    raw = json.loads(gzip.decompress(args.semantics.read_bytes()))
    if raw["revision"] != REVISION or raw["ids"] != list(baseline.indices):
        raise ValueError("profile and coverage describe different baselines")
    keys = defaultdict(lambda: defaultdict(set))
    seen, loaded, broad, delimiters = set(), set(), set(), set()
    for path in args.lookups:
        data = json.loads(path.read_bytes())
        seen.update(data["seen"])
        loaded.update(data["loaded"])
        broad.update(data["whole"])
        delimiters.update(data["delimiters"])
        if data["unknown_delimiters"]:
            raise ValueError("unsupported identifier delimiter collection")
        for field, mapping in data["keys"].items():
            for key, contexts in mapping.items():
                keys[field][key].update(contexts)
    if (baseline.indices.keys() - seen or MODULES - loaded
        or not broad or not keys["escaped"] or not keys["unescaped"] or not delimiters):
        raise ValueError("incomplete escape-map capture")

    seen, loaded = set(), set()
    for path in args.module_reads:
        data = json.loads(path.read_bytes())
        seen.update(data["seen"])
        loaded.update(data["loaded"])
        fields = data["reads"].get(DIALECT, {})
        for name in ("UNESCAPED_SEQUENCES", "__dict__"):
            broad.update(fields.get(name, ()))
    if baseline.indices.keys() - seen or DIALECT not in loaded:
        raise ValueError("incomplete module namespace capture")

    direct, scopes = direct_readers(args.source, baseline)
    broad.update(scopes)
    metadata = union(baseline.references.get(name, 0) for name in REFLECTION)
    metadata |= namespace_readers(args.source, baseline)

    def encode(contexts, extra=0):
        mask = union(baseline.indices.get(test, 0) for test in contexts)
        return [format(mask | extra, "x"), COLLECTION in contexts]

    raw["escape_tables"] = {
        "whole": encode(broad, direct | metadata),
        "delimiters": sorted(delimiters),
        **{
            field: {key: encode(contexts) for key, contexts in sorted(mapping.items())}
            for field, mapping in keys.items()
        },
    }
    with args.output.open("wb") as target, gzip.GzipFile(
        filename="", fileobj=target, mode="wb", mtime=0,
    ) as output:
        output.write(json.dumps(raw, separators=(",", ":")).encode())
    print(json.dumps({
        "full_catalog": len(baseline.indices), "whole_map_or_metadata": int(raw["escape_tables"]["whole"][0], 16).bit_count(),
        "whole_map_collected": COLLECTION in broad, "identifier_delimiters": sorted(delimiters),
    }))


if __name__ == "__main__":
    main()

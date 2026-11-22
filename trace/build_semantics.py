"""Compile baseline observations and test IDs into a reproducible profile."""

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
from build_schema import build_schema
from build_imports import import_profile
from build_defaults import default_profile
from build_enum_fields import enum_field_options

COLLECTION = "@collection"


def load_reports(paths):
    operands = defaultdict(lambda: defaultdict(set))
    reads, calls = defaultdict(set), defaultdict(set)
    schema_initialization = defaultdict(set)
    mapping = defaultdict(lambda: defaultdict(set))
    seen = set()
    for path in paths:
        raw = json.loads(path.read_text())
        seen.update(raw["seen"])
        for site, tests in raw["operands"].items():
            events = operands[site]
            for test, values in tests.items():
                events[test].update(values)
        for key, contexts in raw["reads"].items():
            reads[key].update(contexts)
        for key, contexts in raw["calls"].items():
            calls[key].update(contexts)
        for owner, sites in raw["schema_initialization"].items():
            schema_initialization[owner].update(sites)
        for owner in raw["mapping_owners"]:
            mapping[owner]
        for owner, entries in raw["mapping_reads"].items():
            for key, contexts in entries.items():
                mapping[owner][key].update(contexts)
    return seen, operands, reads, calls, schema_initialization, mapping


def direct_test_attributes(source, baseline):
    fields = defaultdict(int)
    for path in sorted((source / "tests").rglob("*.py")):
        coverage = baseline.files.get(path.relative_to(source).as_posix())
        if coverage is None:
            continue
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            else:
                continue
            if name.isupper() or name in ("arg_types", "required_args"):
                fields[name] |= (
                    coverage.owns
                    if node.lineno in coverage.collection
                    else coverage.lines.get(node.lineno, 0)
                )
    return {name: format(mask, "x") for name, mask in sorted(fields.items()) if mask}


def schema_derivation_sites(source):
    path = "sqlglot/expressions/core.py"
    tree = ast.parse((source / path).read_bytes())
    return {
        f"{path}:{attribute.lineno}:{attribute.col_offset}"
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init_subclass__"
        for attribute in ast.walk(node)
        if isinstance(attribute, ast.Attribute)
        and attribute.attr == "arg_types"
        and isinstance(attribute.value, ast.Name)
        and attribute.value.id == "cls"
    }


def expression_schemas(source, baseline, reads, initialization, mask):
    classes = {}
    for path in sorted((source / "sqlglot" / "expressions").glob("*.py")):
        module = (
            path.relative_to(source).as_posix().removesuffix(".py").replace("/", ".")
        )
        for node in ast.parse(path.read_bytes()).body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {
                base.id if isinstance(base, ast.Name) else base.attr
                for base in node.bases
                if isinstance(base, (ast.Name, ast.Attribute))
            }
            classes[f"{module}:{node.name}"] = bases
    result = {}
    stable_derivation = set()
    derivation = schema_derivation_sites(source)
    for owner in classes:
        field = owner + ":arg_types"
        if field not in reads:
            continue
        names = {owner.rsplit(":", 1)[1]}
        related = {owner}
        pending = classes.keys() - related
        while children := {item for item in pending if classes[item] & names}:
            related.update(children)
            pending.difference_update(children)
            names.update(item.rsplit(":", 1)[1] for item in children)
        if any(not initialization.get(item, set()) <= derivation for item in related):
            continue
        stable_derivation.add(owner)
        if any(COLLECTION in reads.get(item + ":required_args", ()) for item in related):
            continue
        result[owner] = format(
            union(baseline.references.get(name, 0) for name in names)
            | union(
                mask(reads.get(item + ":" + field, ()))
                for item in related
                for field in ("arg_types", "required_args")
            ),
            "x",
        )
    return result, stable_derivation


def build(source, coverage, reports, output, schema_reports=(), import_reports=(), default_reports=()):
    revision = subprocess.check_output(
        ["git", "-C", source, "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", source, "status", "--porcelain"], text=True
    )
    if revision != REVISION or dirty:
        raise ValueError("expected a clean pinned baseline")
    baseline = Baseline(coverage)
    seen, operands, reads, calls, initialization, mapping = load_reports(reports)
    unknown = set(baseline.indices) - seen
    if unknown:
        raise ValueError(f"baseline observations missing for {len(unknown)} tests")

    def mask(contexts):
        return union(baseline.indices.get(test, 0) for test in contexts)

    observations = {}
    for site, tests in sorted(operands.items()):
        if COLLECTION in tests:
            continue
        groups = defaultdict(int)
        for test, facts in sorted(tests.items()):
            bit = baseline.indices.get(test, 0)
            if bit and facts and "?" not in facts:
                groups[tuple(sorted(facts))] |= bit
        observations[site] = (
            format(mask(tests), "x"),
            [(format(bit, "x"), facts) for facts, bit in sorted(groups.items())],
        )

    schemas, stable_derivation = expression_schemas(source, baseline, reads, initialization, mask)
    profile = {
        "revision": REVISION,
        "ids": list(baseline.indices),
        "seen": format(mask(seen), "x"),
        "operands": observations,
        "attributes": {
            site: format(mask(tests), "x")
            for site, tests in sorted(reads.items())
            if COLLECTION not in tests
        },
        "registry": {
            site: format(mask(tests), "x")
            for site, tests in sorted(calls.items())
            if COLLECTION not in tests
        },
        "direct": direct_test_attributes(source, baseline),
        "enum_fields": enum_field_options(source, reads),
        "lookups": {
            owner: {
                key: "collection"
                if COLLECTION in contexts
                else format(mask(contexts), "x")
                for key, contexts in sorted(entries.items())
            }
            for owner, entries in sorted(mapping.items())
        },
        "schemas": schemas,
        "import_profile": import_profile(source, import_reports, baseline),
        "default_profile": default_profile(source, default_reports, baseline, calls),
        "schema_validations": build_schema(source, schema_reports, stable_derivation, baseline),
    }
    with (
        output.open("wb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as stream,
    ):
        stream.write(json.dumps(profile, separators=(",", ":")).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("coverage", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--schema-reports", nargs="+", type=Path, default=())
    parser.add_argument("--import-reports", nargs="+", type=Path, default=())
    parser.add_argument("--default-reports", nargs="+", type=Path, default=())
    args = parser.parse_args()
    build(args.source, args.coverage, args.reports, args.output, args.schema_reports, args.import_reports, args.default_reports)


if __name__ == "__main__":
    main()

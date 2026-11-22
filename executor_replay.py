"""Compare pinned TPC-DS executor inputs and rows without pandas or DuckDB."""

import ast
import csv
import gzip
import hashlib
import importlib.machinery
import resource
import json
import sys
from datetime import date, datetime, time as clock_time
from decimal import Decimal
from functools import cache
from pathlib import Path

from patches import parse_patch


def inject(source, patch):
    change, = parse_patch(patch.read_bytes())
    if change.created or not change.path.startswith("sqlglot/optimizer/") or not change.path.endswith(".py"):
        raise ValueError("expected one existing optimizer source file")
    filename = source / change.path
    new, *_ = change.apply(filename.read_bytes())
    module = change.path.removesuffix(".py").replace("/", ".")

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            return compile(new, self.path, "exec", dont_inherit=True)

    class Finder:
        @staticmethod
        def find_spec(fullname, path=None, target=None):
            if fullname != module:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and spec.origin == str(filename):
                spec.loader = Loader(fullname, spec.origin)
                return spec
            return None

    sys.meta_path.insert(0, Finder())


def scalar(value):
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return value
    if kind is float:
        return ["float", value.hex()]
    if kind in (date, datetime, clock_time, Decimal):
        return [kind.__name__, str(value)]
    if kind is bytes:
        return ["bytes", value.hex()]
    if kind in (tuple, list):
        return [kind.__name__, [scalar(item) for item in value]]
    raise ValueError(f"unsupported executor output {kind.__name__}")


def digest(data):
    return hashlib.sha256(json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def fixture_tables(directory, schema):
    from sqlglot.executor.table import Table

    @cache
    def load(name):
        with gzip.open(directory / f"{name}.csv.gz", "rt", newline="") as stream:
            reader = csv.reader(stream, delimiter="|")
            next(reader)
            rows, ctypes = [], []
            for row in reader:
                if not ctypes:
                    for value in row:
                        try:
                            ctypes.append(type(ast.literal_eval(value)))
                        except (SyntaxError, ValueError):
                            ctypes.append(str)
                rows.append(tuple(None if kind is not str and not value else kind(value)
                                  for kind, value in zip(ctypes, row)))
            return Table(columns=schema[name], rows=rows)
    return load


def replay(indices):
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs

    directory = Path(FIXTURES_DIR)/"optimizer/tpc-ds"
    load_table = fixture_tables(directory, TPCDS_SCHEMA)
    fixtures = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    selected = range(len(fixtures)) if indices is None else indices
    cases = {}
    for index in selected:
        metadata, sql, _ = fixtures[index]
        expression = parse_one(sql)
        if not metadata.get("execute"):
            raise ValueError("expected an executed executor query")
        tables = sorted(table.name for table in find_tables(expression))
        schema = ensure_schema(TPCDS_SCHEMA)
        optimized = optimize(expression, schema, leave_tables_isolated=True)
        optimized.sql(pretty=True)
        reference_sql = transpile(sql, write="duckdb")[0]
        plan = Plan(optimized)
        data = ensure_tables({name: load_table(name) for name in tables})
        result = PythonExecutor(tables=data).execute(plan)
        row_hash = digest([list(result.columns), [scalar(row) for row in result.rows]])
        cases[str(index)] = [row_hash, digest(reference_sql)]
    return cases



def main():
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    memory = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    source, patch, profile, *indices = sys.argv[1:]
    checkpoint = json.loads(gzip.decompress(Path(profile).read_bytes()))["outputs"]
    inject(Path(source), Path(patch))
    results = replay([int(index) for index in indices])
    if set(results) == set(indices) and all(checkpoint[index] == results[index] for index in indices):
        print("equivalent")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

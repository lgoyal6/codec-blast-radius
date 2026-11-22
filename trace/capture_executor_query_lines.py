"""Record baseline optimizer lines independently for each TPC-DS subtest."""

import json
import sys
import time
from collections import defaultdict

from sqlglot import parse_one
from sqlglot.optimizer import optimize
from sqlglot.schema import ensure_schema
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

TOOL = 3
PREFIX = "/baseline/"
active = None


def observe(code, lineno):
    filename = code.co_filename
    if not filename.startswith(PREFIX + "sqlglot/optimizer/"):
        return sys.monitoring.DISABLE
    if active is not None:
        active[filename.removeprefix(PREFIX)].add(lineno)


sys.monitoring.use_tool_id(TOOL, "optimizer query coverage")
sys.monitoring.register_callback(TOOL, sys.monitoring.events.LINE, observe)
sys.monitoring.set_events(TOOL, sys.monitoring.events.LINE)
rows = []
for index, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
    if not meta.get("execute"):
        continue
    expression = parse_one(sql)
    schema = ensure_schema(TPCDS_SCHEMA)
    t = time.process_time()
    active = defaultdict(set)
    optimize(expression, schema, leave_tables_isolated=True)
    rows.append({
        "index": index,
        "coverage": {path: sorted(lines) for path, lines in active.items()},
        "cpu_s": time.process_time() - t,
    })
    active = None
print(json.dumps(rows, separators=(",", ":")))

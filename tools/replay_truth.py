"""Everything the three replayed tests (see replay.py) compute with sqlglot, for a sqlglot checkout.

usage: PYTHONHASHSEED=0 python3 tools/replay_truth.py <checkout>     (prints JSON)

Written separately from replay.py and with no shortcuts: every query is parsed, pickled,
transpiled and executed. tools/replay_check.py compares replay.py's verdicts with it."""
import sys, os, pickle, hashlib, json, csv, ast, gzip, time
import unittest  # as in the tests: sqlglot.expressions.core.UNITTEST
root = sys.argv[1]; sys.path.insert(0, root)
sys.stdout_real = sys.stdout
from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA, load_sql_fixture_pairs, FIXTURES_DIR
from sqlglot import parse_one, transpile, find_tables
from sqlglot.executor import execute
from sqlglot.executor.table import Table
def h(x): return hashlib.blake2b(x if isinstance(x, bytes) else x.encode(), digest_size=8).hexdigest()
def load(directory, schema):
    tables = {}
    for table, columns in schema.items():
        reader = csv.reader(gzip.open(directory + table + ".csv.gz", "rt", newline=""), delimiter="|")
        rows = []; ctypes = []; next(reader)
        for row in reader:
            if not ctypes:
                for v in row:
                    try: ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError): ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = Table(columns=columns, rows=rows)
    return tables
out = {}
try:
    tables = load(FIXTURES_DIR + "/optimizer/tpc-ds/", TPCDS_SCHEMA)
    sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    for i, (meta, sql, _) in enumerate(sqls):
        try:
            e = parse_one(sql); blob = pickle.dumps(e); sig = [h(blob)]
            if not meta.get("execute"):
                pickle.loads(blob); sig = ["ok"]
            else:
                e2 = pickle.loads(blob)
                sub = {t.name: tables[t.name] for t in find_tables(e2)}
                result = execute(e2, schema=TPCDS_SCHEMA, tables=sub)
                result = pickle.loads(pickle.dumps(result))
                sig.append(h(repr((result.columns, result.rows))))
                sig.append(h(transpile(sql, write="duckdb")[0]))
        except Exception as error:
            sig = ["error:" + type(error).__name__]
        out[i] = sig
    hq = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    for i, (meta, sql, optimized) in enumerate(hq):
        try:
            pickle.loads(pickle.dumps(parse_one(sql))); a = "ok"
        except Exception as error:
            a = "error"
        out["hA%d" % i] = a
        try:
            b = [h(transpile(sql, write="duckdb")[0]), h(transpile(optimized, write="duckdb")[0])]
        except Exception as error:
            b = ["error"]
        out["hB%d" % i] = b
except Exception as error:
    out["fatal"] = type(error).__name__
print(json.dumps(out))

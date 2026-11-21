"""Recompute what three slow tests compute with sqlglot, without the slow parts around it.

    tests/test_executor.py::TestExecutor::test_execute_tpcds    21.5 s recorded
    tests/test_executor.py::TestExecutor::test_execute_tpch      3.9 s
    tests/test_executor.py::TestExecutor::test_optimized_tpch    3.5 s

Most of their recorded time is duckdb, pandas, CSV loading and a process pool. What they
ask of sqlglot is small and splits into independent units, one per query and step:

    ds.A.<i>   parse TPC-DS query i and pickle it (the test sends it to a worker)
    ds.B.<i>   transpile query i to duckdb (the test runs that text in duckdb for the expected rows)
    ds.C.<i>   in the worker: unpickle, find the tables, run sqlglot's executor, pickle the result
    h.A.<i>    parse TPC-H query i, pickle and unpickle it (no TPC-H query is executed)
    h.B.<i>    transpile TPC-H query i to duckdb
    h.O.<i>    transpile its optimized form to duckdb

A test passes on a patched tree when every unit gives what it gave on the pinned tree:
the same duckdb text, the same result rows, and no exception. The selector reruns only the
units whose recorded line coverage (maps/replay.json) contains a changed line; the others
execute exactly the code they executed before, on the same inputs.

Inside ds.C the executor first optimizes the query and then plans and runs it. When the
changed lines were only ever executed in the optimizing part and the optimized expression
comes out identical, the rest is skipped: it would run unchanged code on unchanged input.

    python -S -P replay.py sigs <tree>            pinned signatures and timings -> <tree>/replay-sigs.json
    python -S -P replay.py check <tree> <plan>    rerun the units in a plan, print which tests still pass

<tree> holds the sqlglot package (pinned or patched), the pinned tests/helpers.py and the
TPC fixtures. Both must run with the same PYTHONHASHSEED (pickles of sets depend on it),
so not with -I or -E, which ignore it.
"""

import ast
import csv
import gzip
import hashlib
import json
import os
import pickle
import sys
import time

TESTS = {
    "ds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "h.A": "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "h.B": "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    "h.O": "tests/test_executor.py::TestExecutor::test_optimized_tpch",
}
PHASES = ("pre", "opt", "post")
SAME = "="


class _Same(BaseException):
    """Raised inside the executor once the optimized query is known to be the pinned one."""


class _Slow(BaseException):
    """Raised when a unit takes far longer than it did on the pinned tree."""


def _alarm(_signal, _frame):
    raise _Slow()


def limit(seconds):
    """Interrupt the current unit after `seconds` (0 clears): a patched tree may be very slow."""
    import signal

    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except (AttributeError, ValueError):
        pass


def digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    return hashlib.blake2b(data, digest_size=8).hexdigest()


def test_of(unit):
    return TESTS["ds"] if unit.startswith("ds.") else TESTS[unit[:3]]


class Units:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        sys.path.insert(0, self.root)
        import tests.helpers as helpers

        # What tests/test_executor.py imports, so lazily loaded modules are loaded here too.
        from sqlglot import exp, find_tables, parse_one, transpile  # noqa: F401
        import sqlglot.executor as executor
        from sqlglot.executor.python import Python  # noqa: F401
        from sqlglot.executor.table import Table, ensure_tables  # noqa: F401
        from sqlglot.optimizer import optimize  # noqa: F401
        from sqlglot.planner import Plan  # noqa: F401

        self.helpers = helpers
        self.parse_one = parse_one
        self.transpile = transpile
        self.find_tables = find_tables
        self.executor = executor
        self.Table = Table
        self.schema = helpers.TPCDS_SCHEMA
        self.ds = list(helpers.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
        self.h = list(helpers.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        self.tables = None

    def names(self):
        """Every unit, in a fixed order."""
        units = []
        for index, (meta, _sql, _expected) in enumerate(self.ds):
            units.append("ds.A.%d" % index)
            if meta.get("execute"):
                units.append("ds.B.%d" % index)
                units.append("ds.C.%d" % index)
        for index in range(len(self.h)):
            units.extend(("h.A.%d" % index, "h.B.%d" % index, "h.O.%d" % index))
        return units

    # ---- data

    def rows_path(self):
        return os.environ.get("BR_ROWS") or os.path.join(self.root, "tpcds-rows.pkl")

    def read_rows(self):
        """The TPC-DS tables as the test's setUpClass reads them from the CSV files."""
        directory = self.helpers.FIXTURES_DIR + "/optimizer/tpc-ds/"
        data = {}
        for table in self.schema:
            with gzip.open(directory + table + ".csv.gz", "rt", newline="") as handle:
                reader = csv.reader(handle, delimiter="|")
                rows = []
                kinds = []
                next(reader)
                for row in reader:
                    if not kinds:
                        for value in row:
                            try:
                                kinds.append(type(ast.literal_eval(value)))
                            except (ValueError, SyntaxError):
                                kinds.append(str)
                    rows.append(tuple(None if (kind is not str and value == "") else kind(value) for kind, value in zip(kinds, row)))
            data[table] = rows
        return data

    def load_tables(self):
        if self.tables is not None:
            return
        if os.path.exists(self.rows_path()):
            with open(self.rows_path(), "rb") as handle:
                data = pickle.load(handle)
        else:
            data = self.read_rows()
        self.tables = {table: self.Table(columns=columns, rows=data[table]) for table, columns in self.schema.items()}

    # ---- units

    def blob(self, index):
        return pickle.dumps(self.parse_one(self.ds[index][1]))

    def run_c(self, blob, allow_short=False, pinned_opt=None, mark=None):
        """The worker's part for one executed query. Returns (optimized signature, result signature)."""
        executor = self.executor
        real = executor.optimize
        box = {}

        def optimize(*args, **kwargs):
            if mark:
                mark("opt")
            out = real(*args, **kwargs)
            if mark:
                mark(None)  # the signature below is ours, not the test's
            box["opt"] = digest(pickle.dumps(out))
            if mark:
                mark("post")
            if allow_short and box["opt"] == pinned_opt:
                raise _Same()
            return out

        if mark:
            mark("pre")
        expression = pickle.loads(blob)
        tables = {}
        for table in self.find_tables(expression):
            name = table.name
            tables[name] = self.tables[name]
        executor.optimize = optimize
        try:
            result = executor.execute(expression, schema=self.schema, tables=tables)
            result = pickle.loads(pickle.dumps(result))
            signature = digest(repr((result.columns, result.rows)))
        except _Same:
            signature = SAME
        finally:
            executor.optimize = real
            if mark:
                mark(None)
        return box.get("opt"), signature

    def run(self, unit, blob=None):
        """Signature of a unit other than ds.C (which is run_c)."""
        group, kind, index = unit.split(".")
        index = int(index)
        if group == "ds":
            meta, sql, _expected = self.ds[index]
            if kind == "A":
                data = self.blob(index)
                if not meta.get("execute"):
                    pickle.loads(data)  # the worker unpickles it and returns at once
                return digest(data)
            return digest(self.transpile(sql, write="duckdb")[0])
        _meta, sql, optimized = self.h[index]
        if kind == "A":
            pickle.loads(pickle.dumps(self.parse_one(sql)))
            return "ok"
        return digest(self.transpile(sql if kind == "B" else optimized, write="duckdb")[0])


def sigs(root):
    """Signatures and CPU seconds of every unit on the tree as it is."""
    units = Units(root)
    if not os.path.exists(units.rows_path()):
        with open(units.rows_path(), "wb") as handle:
            pickle.dump(units.read_rows(), handle, protocol=pickle.HIGHEST_PROTOCOL)
    units.load_tables()
    out = {"python": sys.version.split()[0], "seed": os.environ.get("PYTHONHASHSEED"), "units": {}}
    for unit in units.names():
        started = time.process_time()
        if ".C." in unit:
            spent = {}

            def mark(phase, _spent=spent, _state=[None, 0.0]):
                now = time.process_time()
                if _state[0]:
                    _spent[_state[0]] = _spent.get(_state[0], 0.0) + now - _state[1]
                _state[0], _state[1] = phase, now

            signature = units.run_c(units.blob(int(unit.rsplit(".", 1)[1])), mark=mark)
            out["units"][unit] = {"sig": list(signature), "seconds": round(time.process_time() - started, 4),
                                  "opt_seconds": round(spent.get("pre", 0.0) + spent.get("opt", 0.0), 4)}
        else:
            out["units"][unit] = {"sig": units.run(unit), "seconds": round(time.process_time() - started, 4)}
    with open(os.path.join(units.root, "replay-sigs.json"), "w") as handle:
        json.dump(out, handle, separators=(",", ":"))


def check(root, plan, budget):
    """plan: {test id: [[unit, how], ...]}, cheapest units first. Returns {test id: true|false}.

    how (ds.C units only): 0 run it all; 1 stop after optimizing when the optimized query is
    the pinned one; 2 nothing to run when the pickled query is the pinned one.
    A test is left out of the answer when finishing it is expected to take more than `budget`
    CPU seconds in total; how often early stops fail so far sets the expectation for the
    units still to come."""
    with open(os.environ.get("BR_SIGS") or os.path.join(root, "replay-sigs.json")) as handle:
        pinned = json.load(handle)
    if pinned["python"] != sys.version.split()[0] or pinned["seed"] != os.environ.get("PYTHONHASHSEED"):
        return {}
    pinned = pinned["units"]
    units = Units(root)
    verdict = {}
    started = time.process_time()
    tried = full = 0  # units with a possible early stop, and how many had to run in full
    for test, wanted in plan.items():
        passed = True
        for position, (unit, how) in enumerate(wanted):
            share = (full + 0.5) / (tried + 6.0) if tried else 0.0  # expected share of full runs ahead
            ahead = 0.0
            for later, later_how in wanted[position:]:
                entry = pinned[later]
                quick = entry.get("opt_seconds", entry["seconds"]) if later_how == 1 else 0.01 if later_how == 2 else entry["seconds"]
                ahead += quick + share * (entry["seconds"] - quick)
            if time.process_time() - started + ahead > budget:
                passed = None
                break
            try:
                if ".C." in unit:
                    units.load_tables()
                limit(0.5 + 5.0 * pinned[unit]["seconds"])  # much slower than pinned counts as different
                if ".C." in unit:
                    index = int(unit.rsplit(".", 1)[1])
                    blob = units.blob(index)
                    same_input = digest(blob) == pinned["ds.A.%d" % index]["sig"]
                    if how >= 1:
                        tried += 1
                    if how == 2 and same_input:
                        continue
                    allow = how >= 1 and same_input
                    _opt, signature = units.run_c(blob, allow, pinned[unit]["sig"][0])
                    if how >= 1 and signature != SAME:
                        full += 1
                    good = signature == SAME or signature == pinned[unit]["sig"][1]
                elif ".A." in unit:
                    units.run(unit)
                    good = True  # the test only needs parsing and pickling to succeed here
                else:
                    good = units.run(unit) == pinned[unit]["sig"]
            except BaseException:  # noqa: a patched tree may fail in any way
                good = False
            finally:
                limit(0)
            if not good:
                passed = False
                break
        if passed is not None:
            verdict[test] = passed
    return verdict


def main():
    command, root = sys.argv[1], os.path.abspath(sys.argv[2])
    write = sys.stdout.write
    sys.stdout = sys.stderr = open(os.devnull, "w")  # sqlglot may print
    if command == "sigs":
        sigs(root)
        write("ok\n")
    elif command == "check":
        with open(sys.argv[3]) as handle:
            plan = json.load(handle)
        write(json.dumps(check(root, plan["tests"], float(plan["budget"]))) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

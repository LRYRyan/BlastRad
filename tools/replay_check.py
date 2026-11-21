"""Checks the replay (replay.py) against the truth (tools/replay_truth.py) on random mutants.

usage: python3 tools/replay_check.py <scratch copy of the sqlglot checkout> <count> <seed> [def]

With `def`, only class- and module-level lines are mutated (changes at import time).

For mutants that select one of the replayed tests, runs the selector with the replay, then
recomputes everything those tests compute on the mutated tree. A test the replay dropped
although its computation changed is reported as WRONG-DROP. Needs the tree/ directory made
by `python3 selector.py --prepare tree`. The scratch copy is edited in place and restored
after every mutant; make a fresh copy if the run is killed."""
import ast, difflib, json, os, random, re, sys, importlib.util, subprocess, time
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUT, N, SEED = os.path.abspath(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
MODE = sys.argv[4] if len(sys.argv) > 4 else "any"
_inside = {}
def in_function(path, text, number):
    """Is the line part of a function or lambda body?"""
    spans = _inside.get(path)
    if spans is None:
        spans = _inside[path] = [(node.lineno, node.end_lineno) for node in ast.walk(ast.parse(text))
                                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
    return any(first <= number <= last for first, last in spans)
TRUTH = os.path.join(REPO, "tools", "replay_truth.py")
spec = importlib.util.spec_from_file_location("selector", os.path.join(REPO, "selector.py"))
sel = importlib.util.module_from_spec(spec); spec.loader.exec_module(sel)
maps = sel.Maps(os.path.join(REPO, "maps"))
tests = json.load(open(os.path.join(REPO, "sample/tests.json")))["tests"]
ms = {t["id"]: round(t["seconds"] * 1000) for t in tests}
pos = {i: n for n, i in enumerate(maps.ids)}
TPCDS = 1 << pos["tests/test_executor.py::TestExecutor::test_execute_tpcds"]
TPCH = 1 << pos["tests/test_executor.py::TestExecutor::test_execute_tpch"]
OPTH = 1 << pos["tests/test_executor.py::TestExecutor::test_optimized_tpch"]
tree = sel.Tree(maps)
replayer = sel.Replay(maps, tree)
PIN = json.loads(subprocess.run([sys.executable, TRUTH, "."], cwd=MUT, env=dict(os.environ, PYTHONHASHSEED="0"),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True).stdout)
wrong = 0; gain = 0.0; cpus = 0.0; chances = 0
def ids_of(mask):
    out = []
    while mask:
        low = mask & -mask; out.append(maps.ids[low.bit_length() - 1]); mask ^= low
    return out
def cost(mask): return sel.split([(ms[i], n) for n, i in enumerate(ids_of(mask))])[0]
SWAPS = [(" == ", " != "), (" != ", " == "), (" and ", " or "), (" or ", " and "), ("True", "False"), ("False", "True"),
         (" is not ", " is "), (" < ", " <= "), (" > ", " >= "), ("not ", "")]
def mutate_line(line, rng):
    options = []
    for a, b in SWAPS:
        for m in re.finditer(re.escape(a), line): options.append((m.start(), m.end(), b))
    for m in re.finditer(r"(?<![\w.\"'])(\d+)(?![\w.\"'])", line): options.append((m.start(), m.end(), str(int(m.group(1)) + 1)))
    if not options: return None
    s, e, new = rng.choice(options); return line[:s] + new + line[e:]
rng = random.Random(SEED)
files = sorted(p for p in maps.src if p.startswith("sqlglot/") and p.endswith(".py"))
weights = [len(maps.text(p)) for p in files]
done = 0; same = 0; saved = 0.0; spent = 0.0
while done < N:
    path = rng.choices(files, weights)[0]
    text = maps.text(path); lines = text.split("\n")
    number = rng.randrange(1, len(lines) + 1)
    if lines[number - 1].lstrip().startswith("#"): continue
    if MODE == "def" and in_function(path, text, number): continue
    new_line = mutate_line(lines[number - 1], rng)
    if not new_line or new_line == lines[number - 1]: continue
    new_lines = lines[:]; new_lines[number - 1] = new_line; new_text = "\n".join(new_lines)
    try:
        if ast.dump(ast.parse(new_text)) == ast.dump(ast.parse(text)): continue
        compile(new_text, path, "exec")
    except SyntaxError: continue
    diff = "".join(difflib.unified_diff(text.splitlines(True), new_text.splitlines(True), "a/" + path, "b/" + path))
    patch = "diff --git a/%s b/%s\n%s" % (path, path, diff)
    mask, active = sel.select(maps, patch, tree, cost)
    if not mask & (TPCDS | TPCH | OPTH): continue
    tree.spent = 0.0
    t0 = time.process_time(); c0 = os.times()
    mask2, _ = sel.select(maps, patch, tree, cost, replayer)
    c1 = os.times(); cpu = (c1.children_user + c1.children_system) - (c0.children_user + c0.children_system) + time.process_time() - t0
    done += 1
    target = os.path.join(MUT, path); open(target, "w").write(new_text)
    try:
        try:
            p = subprocess.run([sys.executable, TRUTH, "."], cwd=MUT, env=dict(os.environ, PYTHONHASHSEED="0"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=150)
            got = json.loads(p.stdout) if p.stdout.strip() else {"fatal": "crash"}
        except subprocess.TimeoutExpired:
            got = {"fatal": "timeout"}
    finally:
        open(target, "w").write(text)
    def same(keys): return "fatal" not in got and all(got.get(k) == PIN[k] for k in keys)
    truth = {TPCDS: same([k for k in PIN if k.isdigit()]),
             TPCH: "fatal" not in got and all(got.get(k) == "ok" for k in PIN if k.startswith("hA")),
             OPTH: same([k for k in PIN if k.startswith("hB")])}
    dropped = mask & ~mask2
    notes = []
    for bit, name in ((TPCDS, "tpcds"), (TPCH, "tpch"), (OPTH, "opt_tpch")):
        if not mask & bit: continue
        if dropped & bit and not truth[bit]:
            wrong += 1; notes.append(name + ":WRONG-DROP")
        elif dropped & bit: notes.append(name + ":dropped")
        elif truth[bit]: notes.append(name + ":kept-but-same"); chances += 1
        else: notes.append(name + ":kept")
    c_before, c_after = cost(mask) / 1000, cost(mask2) / 1000
    gain += c_before - c_after; cpus += cpu
    print("%5.1f -> %5.1f  cpu %4.1f  %-44s %s:%d  %s -> %s" % (c_before, c_after, cpu, " ".join(notes), path, number, lines[number - 1].strip()[:38], new_line.strip()[:38]), flush=True)
tree.close()
print("%d mutants: simulated cost down by %.1f s, extra cpu %.1f s, wrong drops %d, missed chances %d" % (done, gain, cpus, wrong, chances))

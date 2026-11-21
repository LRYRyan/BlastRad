"""Soundness check: mutate the pinned tree at random, ask the selector, then really run the
tests it left out (among those that ever touched the mutated file) and report any that fail.

usage: python3 tools/mutation_check.py <scratch copy of the sqlglot checkout> <count> <seed> <def|body|both|cmp>

cmp makes only same-family comparison swaps (is/==, is not/!=, </<=, >/>=) inside functions
and keeps the mutants the comparison profile let the selector drop tests for.

The scratch copy is edited in place and restored after every mutant; if the run is killed,
make a fresh copy before the next one. It needs the test dependencies of requirements.lock;
BR_EXTRA_PATH can add a directory to PYTHONPATH. Do not rebuild maps/ while it runs.
The two slowest tests are not run. Results go to stdout.
"""
import ast, difflib, json, os, random, re, subprocess, sys, time, importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = REPO
MUT = os.path.abspath(sys.argv[1])
COUNT, SEED, MODE = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]   # MODE: def | body | both
SKIP_BIG = {"tests/test_parser.py::TestParser::test_max_nodes", "tests/test_executor.py::TestExecutor::test_execute_tpcds"}
os.environ["BR_DATA"] = os.path.join(KIT, "sample")
spec = importlib.util.spec_from_file_location("selector", os.path.join(REPO, "selector.py"))
sel = importlib.util.module_from_spec(spec); spec.loader.exec_module(sel)
maps = sel.Maps(os.path.join(REPO, "maps"))
tests = json.load(open(os.path.join(KIT, "sample/tests.json")))["tests"]
ms = {t["id"]: round(t["seconds"] * 1000) for t in tests}
tree = sel.Tree(maps)

def ids_of(mask):
    out = []
    while mask:
        low = mask & -mask
        out.append(maps.ids[low.bit_length() - 1]); mask ^= low
    return out

def cost(mask):
    return sel.split([(ms[i], n) for n, i in enumerate(ids_of(mask))])[0]

SWAPS = [(" == ", " != "), (" != ", " == "), (" and ", " or "), (" or ", " and "), ("True", "False"), ("False", "True"),
         (" is not ", " is "), (" < ", " <= "), (" > ", " >= "), (" <= ", " < "), (" >= ", " > "), (" + ", " - "), ("not ", "")]

def candidates(path, text):
    """(line number, is import-time) for single lines worth mutating."""
    tree_ = ast.parse(text)
    out = []
    def visit(body, in_function):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(node.body, True)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, False if not in_function else True)
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                out.append((node.lineno, not in_function))
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []) or [], in_function)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, in_function)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            else:
                lambdas = [(n.lineno, n.end_lineno) for n in ast.walk(node) if isinstance(n, ast.Lambda)]
                for line in range(node.lineno, node.end_lineno + 1):
                    inside_lambda = any(a <= line <= b for a, b in lambdas)
                    out.append((line, (not in_function) and not inside_lambda))
    visit(tree_.body, False)
    return out

CMP_SWAPS = [(" is not ", " != "), (" != ", " is not "), (" is ", " == "), (" == ", " is "),
             (" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > ")]


def mutate_line(line, rng):
    options = []
    for a, b in (CMP_SWAPS if MODE == "cmp" else SWAPS):
        for m in re.finditer(re.escape(a), line):
            options.append((m.start(), m.end(), b))
    if MODE == "cmp":
        if not options:
            return None
        start, end, new = rng.choice(options)
        return line[:start] + new + line[end:]  # "is not" -> "== not" does not compile and is skipped
    for m in re.finditer(r"(?<![\w.\"'])(\d+)(?![\w.\"'])", line):
        options.append((m.start(), m.end(), str(int(m.group(1)) + 1)))
    for m in re.finditer(r"\"([A-Za-z_ ]{2,})\"", line):
        options.append((m.start(1), m.end(1), m.group(1) + "X"))
    if not options:
        return None
    start, end, new = rng.choice(options)
    return line[:start] + new + line[end:]

rng = random.Random(SEED)
files = sorted(p for p in maps.src if p.startswith("sqlglot/") and p.endswith(".py"))
weights = [len(maps.text(p)) for p in files]
made = 0; problems = 0; attempts = 0
def say(*a):
    print(*a, flush=True)

while made < COUNT and attempts < COUNT * 60:
    attempts += 1
    path = rng.choices(files, weights)[0]
    text = maps.text(path)
    lines = text.split("\n")
    cands = candidates(path, text)
    if not cands:
        continue
    number, at_import = rng.choice(cands)
    if MODE == "def" and not at_import: continue
    if MODE in ("body", "cmp") and at_import: continue
    if lines[number - 1].lstrip().startswith("#"): continue
    new_line = mutate_line(lines[number - 1], rng)
    if new_line is None or new_line == lines[number - 1]:
        continue
    new_lines = lines[:]; new_lines[number - 1] = new_line
    new_text = "\n".join(new_lines)
    try:
        if ast.dump(ast.parse(new_text)) == ast.dump(ast.parse(text)):
            continue
        compile(new_text, path, "exec")
    except SyntaxError:
        continue
    diff = "".join(difflib.unified_diff(text.splitlines(True), new_text.splitlines(True), "a/" + path, "b/" + path))
    patch = "diff --git a/%s b/%s\n%s" % (path, path, diff)
    mask, active = sel.select(maps, patch, tree, cost)
    touched_all = maps.whole(path)
    if MODE == "cmp" and mask:
        continue  # the profile made no difference here
    if not at_import and not (touched_all & ~mask):
        continue
    left_out = [i for i in ids_of(touched_all & ~mask) if i not in SKIP_BIG]
    if not left_out:
        continue
    made += 1
    say("== %d %s:%d %s | %r -> %r | selected %d (%.1fs), left out %d of %d" % (
        made, path, number, "import-time" if at_import else "call-time", lines[number - 1].strip()[:60], new_line.strip()[:60],
        bin(mask).count("1"), cost(mask) / 1000, len(left_out), bin(touched_all).count("1")))
    target = os.path.join(MUT, path)
    open(target, "w").write(new_text)
    try:
        # two halves in parallel
        left_out.sort(key=lambda i: -ms[i])
        halves = [[], []]; loads = [0, 0]
        for i in left_out:
            k = loads.index(min(loads)); halves[k].append(i); loads[k] += ms[i]
        procs = []
        for k, half in enumerate(halves):
            if not half: continue
            args = os.path.join(os.path.dirname(MUT), ".mutation-args-%s-%d-%d.txt" % (MODE, SEED, k))
            open(args, "w").write("\n".join(half) + "\n")
            env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join(p for p in (os.environ.get("BR_EXTRA_PATH", ""), MUT) if p))
            procs.append(subprocess.Popen([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", "@" + args],
                                          cwd=MUT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        failed = set()
        for proc in procs:
            try:
                output = proc.communicate(timeout=900)[0]
            except subprocess.TimeoutExpired:
                proc.kill(); output = proc.communicate()[0]; say("   TIMEOUT")
            for line in output.splitlines():
                m = re.match(r"^(FAILED|ERROR|SUBFAIL\w*(\[.*?\])?|SUBFAILED\S*)\s+(tests/\S+)", line)
                if m:
                    failed.add(m.group(3).split(" ")[0])
            tail = output.strip().splitlines()[-1:] 
            say("   ", " ".join(tail)[:120])
        failed = {f for f in failed if f in ms}
        if failed:
            problems += 1
            say("   MISSED:", sorted(failed)[:8], len(failed))
            say(patch)
    finally:
        open(target, "w").write(text)
say("done: %d mutants, %d with missed failures" % (made, problems))
tree.close()

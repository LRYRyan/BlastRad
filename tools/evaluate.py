"""Run the selector on the sample set and compare with the answers, commit by commit.

usage: python3 tools/evaluate.py            (from the repository root, no Docker needed)

Prints what each commit cost, what the cheapest possible answer would have cost, and any
failing test that was left out.
"""
import json, os, subprocess, sys, time, resource
repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
kit = repo
tests = json.load(open(os.path.join(kit, "sample/tests.json")))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ids = {t["number"]: t["id"] for t in tests}
answers = json.load(open(os.path.join(kit, "sample/answers.json")))["commits"]
sys.path.insert(0, repo)
env = dict(os.environ, BR_DATA=os.path.join(kit, "sample"))
t0 = time.time()
before = resource.getrusage(resource.RUSAGE_CHILDREN)
proc = subprocess.run([sys.executable, os.path.join(repo, "selector.py")], env=env, stdout=subprocess.PIPE)
after = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
open(os.path.join(repo, "output.jsonl"), "wb").write(proc.stdout)
import importlib.util
spec = importlib.util.spec_from_file_location("selector", os.path.join(repo, "selector.py"))
sel = importlib.util.module_from_spec(spec); spec.loader.exec_module(sel)
total = 0; oracle_total = 0; misses = 0
print("%-5s %-8s %6s %6s %8s %8s  %s" % ("id", "kind", "fail", "picked", "cost", "oracle", "note"))
for line in proc.stdout.decode().splitlines():
    row = json.loads(line)
    a = answers[row["commit"]]
    picked = [n for w in row["workers"] for n in w]
    cost = (max(sum(ms[n] for n in w) for w in row["workers"]) + 2000 * len(row["workers"])) if row["workers"] else 0
    oracle = sel.split([(ms[n], n) for n in a["failing"]])[0] if a["failing"] else 0
    missed = sorted(set(a["failing"]) - set(picked))
    note = ""
    if missed:
        misses += 1; cost += 300000
        note = "MISS " + ", ".join(ids[n] for n in missed[:3])
    total += cost; oracle_total += oracle
    print("%-5s %-8s %6d %6d %8.3f %8.3f  %s" % (row["commit"], a["kind"], len(a["failing"]), len(picked), cost / 1000, oracle / 1000, note))
print("simulated %.3f  oracle %.3f  misses %d  selector cpu %.3fs (wall %.2fs)" % (total / 1000, oracle_total / 1000, misses, cpu, time.time() - t0))

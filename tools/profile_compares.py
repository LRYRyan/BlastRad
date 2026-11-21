"""Record the comparison profile of the pinned commit (see br_compare.py).

usage: profile_compares.py <sqlglot checkout> <tests.json> <out dir> [jobs]

Runs the whole suite with comparisons rewritten, test files spread over `jobs`
pytest processes, and writes <out dir>/*.txt plus <out dir>/failed.txt, the tests
that did not pass while profiling (their comparisons may be incomplete, so the
selector never relies on the profile where one of them is involved).
"""

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    repo, tests_json, out = sys.argv[1], sys.argv[2], os.path.abspath(sys.argv[3])
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else os.cpu_count() or 2
    tests = json.load(open(tests_json))["tests"]
    seconds = {}
    for test in tests:
        seconds[test["file"]] = seconds.get(test["file"], 0.0) + test["seconds"]
    files = sorted(seconds, key=lambda name: -seconds[name])
    os.makedirs(out, exist_ok=True)
    extra = os.environ.get("BR_EXTRA_PATH", "")
    failed = set()

    def run(name):
        env = dict(os.environ)
        env.update(
            BR_CMP_DIR=out,
            BR_CMP_ROOT=os.path.realpath(repo),
            SKIP_INTEGRATION="0",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED=os.environ.get("BR_HASHSEED", "0"),
            PYTHONPATH=os.pathsep.join(part for part in (HERE, extra, repo) if part),
        )
        started = time.time()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", name],
            cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout.splitlines():
            match = re.match(r"^(FAILED|ERROR)\s+(\S+)", line)
            if match:
                failed.add(match.group(2))
        tail = proc.stdout.strip().splitlines()[-1:] if proc.stdout.strip() else []
        print("%-45s %6.1fs rc=%d %s" % (name, time.time() - started, proc.returncode, " ".join(tail)[:90]), flush=True)
        if proc.returncode not in (0, 1):
            failed.update(test["id"] for test in tests if test["file"] == name)

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(run, files))
    with open(os.path.join(out, "failed.txt"), "w") as handle:
        handle.write("".join(name + "\n" for name in sorted(failed)))
    print("%d tests did not pass while profiling" % len(failed))


if __name__ == "__main__":
    main()

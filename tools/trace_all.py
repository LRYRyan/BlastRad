"""Trace every test file of the pinned sqlglot checkout, one pytest process per file.

usage: trace_all.py <sqlglot checkout> <tests.json> <out dir> [jobs]

The tests run the way they were labeled: from the checkout, with the checkout on
PYTHONPATH and SKIP_INTEGRATION=0. BR_HASHSEED picks the hash seed (default 0).
"""

import json
import os
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

    def run(name):
        env = dict(os.environ)
        env.update(
            BR_OUT=out,
            BR_FILE=name,
            SKIP_INTEGRATION="0",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED=os.environ.get("BR_HASHSEED", "0"),
            PYTHONPATH=os.pathsep.join(part for part in (HERE, extra, repo) if part),
        )
        started = time.time()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "br_tracer", "-p", "no:cacheprovider", name],
            cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        tail = proc.stdout.strip().splitlines()[-1:] if proc.stdout.strip() else []
        print("%-45s %6.1fs rc=%d %s" % (name, time.time() - started, proc.returncode, " ".join(tail)[:90]), flush=True)

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(run, files))


if __name__ == "__main__":
    main()

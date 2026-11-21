"""pytest plugin: records, for every test, what it depends on at the pinned commit.

Run one pytest process per test file (see trace_all.py). The parent process
imports and collects the file; everything recorded up to that point is the
file's "collection" context, shared by all of its tests. Every test then runs
in a forked child, so lazily imported modules and class-level setup are
attributed to each test that needs them, not just to the first one in the file.
"""

import os
import pickle
import shutil
import tempfile

import br_core

ROOT = os.path.realpath(os.getcwd()) + os.sep
OUT = os.environ["BR_OUT"]
os.environ["BR_ROOT"] = ROOT
os.environ.pop("BR_SPAWN_DIR", None)
br_core.install(ROOT)

_state = {"outcome": "passed", "in_test": False}


def _after_fork_in_child():
    # A process forked by test code (not by this plugin) reports to a file.
    if _state["in_test"] and os.environ.get("BR_SPAWN_DIR"):
        br_core.to_file(os.environ["BR_SPAWN_DIR"])


os.register_at_fork(after_in_child=_after_fork_in_child)


def pytest_runtest_logreport(report):
    if report.failed:
        _state["outcome"] = "failed"
    elif report.skipped and _state["outcome"] == "passed":
        _state["outcome"] = "skipped"


def _merge_helpers(directory, data):
    kinds = {"R": "run", "I": "imp", "D": "def", "U": "used"}
    extra = {name: set() for name in ("run", "imp", "def", "used", "data", "dirs")}
    helpers = 0
    for name in os.listdir(directory):
        helpers += 1
        with open(os.path.join(directory, name)) as lines:
            for entry in lines:
                parts = entry.rstrip("\n").split("\t")
                if len(parts) != 3 or not parts[2].isdigit():
                    continue
                if parts[0] in kinds:
                    extra[kinds[parts[0]]].add((parts[1], int(parts[2])))
                elif parts[0] == "F":
                    extra["data"].add(parts[1])
                elif parts[0] == "L":
                    extra["dirs"].add(parts[1])
    for name, values in extra.items():
        if values:
            data[name] = sorted(set(map(tuple, data[name])) | values) if name in ("run", "imp", "def", "used") else sorted(set(data[name]) | values)
    data["helpers"] = helpers


def pytest_runtestloop(session):
    collection = br_core.snapshot()
    results = {}
    classes = {}
    for item in session.items:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                os.close(read_fd)
                _state["outcome"] = "passed"
                spawn_dir = tempfile.mkdtemp(prefix="br-")
                os.environ["BR_SPAWN_DIR"] = spawn_dir
                br_core.reset()
                _state["in_test"] = True
                try:
                    item.config.hook.pytest_runtest_protocol(item=item, nextitem=None)
                except BaseException as error:  # noqa
                    _state["outcome"] = "crashed: %r" % (error,)
                _state["in_test"] = False
                br_core.mon.set_events(br_core.TOOL, 0)
                data = br_core.snapshot()
                _merge_helpers(spawn_dir, data)
                shutil.rmtree(spawn_dir, ignore_errors=True)
                data["outcome"] = _state["outcome"]
                data["classes"] = br_core.class_table()
                with os.fdopen(write_fd, "wb") as pipe:
                    pickle.dump(data, pipe, protocol=4)
            except BaseException:
                code = 1
            finally:
                os._exit(code)
        os.close(write_fd)
        chunks = []
        with os.fdopen(read_fd, "rb") as pipe:
            while True:
                chunk = pipe.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        os.waitpid(pid, 0)
        try:
            result = pickle.loads(b"".join(chunks))
        except Exception:
            result = {"outcome": "crashed", "run": [], "imp": [], "def": [], "data": [], "dirs": [], "used": [], "classes": {}}
        classes.update(result.pop("classes", {}))
        results[item.nodeid] = result
    br_core.mon.set_events(br_core.TOOL, 0)
    classes.update(br_core.class_table())
    target = os.environ["BR_FILE"]
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, target.replace("/", "__") + ".pkl"), "wb") as handle:
        pickle.dump(
            {"file": target, "collection": collection, "tests": results, "classes": classes},
            handle,
            protocol=4,
        )
    return True

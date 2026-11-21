"""Record which lines every replay unit executes at the pinned commit (see replay.py).

usage: trace_units.py <sqlglot checkout> <out file>

Every unit runs in its own forked child of a process that has only imported sqlglot and
loaded the tables, as a pool worker has. So each unit is recorded cold: lines that fill
a cache are attributed to every unit that needs the cached value. The parent never parses
a query; ds.C units get their pickled query from the ds.A child of the same query.
"""

import os
import pickle
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import br_core  # noqa: E402

root = os.path.realpath(sys.argv[1])
os.chdir(root)
br_core.install(root + os.sep)

import replay  # noqa: E402


def lines():
    return set(br_core.RUN) | set(br_core.IMP) | set(br_core.DEF)


def in_child(work):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(read_fd)
            with os.fdopen(write_fd, "wb") as pipe:
                pickle.dump(work(), pipe, protocol=4)
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
    return pickle.loads(b"".join(chunks))


def main():
    units = replay.Units(root)
    units.load_tables()
    cover = {}
    blobs = {}
    for unit in units.names():
        index = int(unit.rsplit(".", 1)[1])
        if ".C." in unit:
            blob = blobs[index]

            def work(blob=blob):
                found = {}
                state = [None]

                def mark(phase):
                    if state[0]:
                        found.setdefault(state[0], set()).update(lines())
                    state[0] = phase
                    br_core.reset()

                br_core.reset()
                units.run_c(blob, mark=mark)
                return found

            found = in_child(work)
            for phase in replay.PHASES:
                cover["%s/%s" % (unit, phase)] = sorted(found.get(phase, ()))
        else:
            def work(unit=unit, index=index):
                br_core.reset()
                units.run(unit)
                found = lines()
                br_core.mon.set_events(br_core.TOOL, 0)
                blob = units.blob(index) if unit.startswith("ds.A.") else None
                return found, blob

            found, blob = in_child(work)
            cover[unit] = sorted(found)
            if blob is not None:
                blobs[index] = blob
    with open(sys.argv[2], "wb") as handle:
        pickle.dump({"units": units.names(), "cover": cover}, handle, protocol=4)
    print("%d units, %d coverage sets" % (len(units.names()), len(cover)))


if __name__ == "__main__":
    main()

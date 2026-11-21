"""Turn traces of the pinned commit into the compact maps the selector reads.

usage: build_maps.py <sqlglot checkout> <tests.json> <maps dir> <trace dir> [<trace dir> ...]

Several trace dirs (e.g. runs with different hash seeds) are unioned.

Output (all plain files, read lazily by selector.py):
  meta.json        test ids in bit order, per-file row ranges, data/dir dependencies, class usage,
                   source offsets
  rows.bin         per source file: (line, run id, import id, def id) as little-endian uint32s;
                   an id names the set of tests that executed the line in that way
  bitsets.bin      the distinct test sets, one fixed-width little-endian bitset each (id 0 is empty)
  src.bin          zlib-compressed text of every .py file at the pinned commit (to diff patches against)
  fingerprint.bin  zlib-compressed import-time fingerprint of the pinned tree (see fingerprint.py)
"""

import ast
import glob
import json
import os
import pickle
import struct
import subprocess
import sys
import zlib
from array import array


def main():
    repo, tests_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
    trace_dirs = sys.argv[4:]
    tests = json.load(open(tests_json))["tests"]
    ids = [test["id"] for test in tests]
    index = {test_id: position for position, test_id in enumerate(ids)}
    count = len(ids)
    width = (count + 7) // 8

    run = {}   # (file, line) -> int bitset
    imp = {}
    dfn = {}
    used = {}  # (file, class body line) -> bitset of tests that instantiated the class
    class_names = {}  # (file, class body line) -> (qualname, instantiation observable)
    data = {}  # file -> bitset (opened as data)
    dirs = {}
    seen = [0] * len(trace_dirs)
    passed = [0] * len(trace_dirs)

    def add(table, keys, mask):
        get = table.get
        for key in keys:
            table[key] = get(key, 0) | mask

    for which, trace_dir in enumerate(trace_dirs):
        for path in sorted(glob.glob(os.path.join(trace_dir, "*.pkl"))):
            with open(path, "rb") as handle:
                trace = pickle.load(handle)
            file_mask = 0
            for test_id, result in trace["tests"].items():
                position = index.get(test_id)
                if position is None:
                    continue
                bit = 1 << position
                file_mask |= bit
                seen[which] |= bit
                if result.get("outcome") == "passed":
                    passed[which] |= bit
                add(run, result["run"], bit)
                add(imp, result["imp"], bit)
                add(dfn, result["def"], bit)
                add(used, result["used"], bit)
                add(data, result["data"], bit)
                add(dirs, result["dirs"], bit)
            collection = trace["collection"]
            add(run, collection["run"], file_mask)
            add(imp, collection["imp"], file_mask)
            add(dfn, collection["def"], file_mask)
            add(used, collection["used"], file_mask)
            add(data, collection["data"], file_mask)
            add(dirs, collection["dirs"], file_mask)
            for where, (qualname, observable) in trace.get("classes", {}).items():
                previous = class_names.get(where)
                class_names[where] = (qualname, observable and (previous is None or previous[1]))

    everything = (1 << count) - 1
    mapped = everything
    for which in range(len(trace_dirs)):
        mapped &= passed[which]
    unmapped = [position for position in range(count) if not (mapped >> position) & 1]

    tracked = subprocess.run(
        ["git", "-C", repo, "ls-files"], check=True, stdout=subprocess.PIPE, text=True
    ).stdout.split("\n")
    tracked = sorted(name for name in tracked if name)
    tracked_set = set(tracked)

    bitsets = {0: 0}
    blob = [bytes(width)]

    def bitset_id(mask):
        found = bitsets.get(mask)
        if found is None:
            found = len(blob)
            bitsets[mask] = found
            blob.append(mask.to_bytes(width, "little"))
        return found

    per_file = {}
    for column, table in enumerate((run, imp, dfn)):
        for (name, line), mask in table.items():
            per_file.setdefault(name, {}).setdefault(line, [0, 0, 0])[column] = mask

    # Every line that has bytecode gets a row, executed or not, so the selector can tell
    # "no test ran this line" from "this line never shows up in a trace" (e.g. `global x`).
    for name in tracked:
        if not name.endswith(".py"):
            continue
        try:
            with open(os.path.join(repo, name), "rb") as handle:
                todo = [compile(handle.read(), name, "exec", dont_inherit=True)]
        except (SyntaxError, ValueError):
            continue
        lines = per_file.setdefault(name, {})
        while todo:
            code = todo.pop()
            for _start, _end, line in code.co_lines():
                if line is not None and line > 0:
                    lines.setdefault(line, [0, 0, 0])
            todo.extend(const for const in code.co_consts if hasattr(const, "co_lines"))

    rows = array("I")
    files = {}
    for name in sorted(per_file):
        if name not in tracked_set:
            continue
        start = len(rows) // 4
        for line in sorted(per_file[name]):
            run_mask, imp_mask, def_mask = per_file[name][line]
            rows.extend((line, bitset_id(run_mask), bitset_id(imp_mask), bitset_id(def_mask)))
        files[name] = [start, len(rows) // 4 - start]

    # Class usage: file -> {qualname: [bitset id of tests that instantiated it, observable]}
    class_usage = {}
    for (name, line), (qualname, observable) in sorted(class_names.items()):
        if name not in tracked_set or "<locals>" in qualname:
            continue
        entry = class_usage.setdefault(name, {}).setdefault(qualname, [0, 1])
        entry[0] |= used.get((name, line), 0)
        entry[1] = entry[1] and int(bool(observable))
    for name in class_usage:
        for qualname, entry in class_usage[name].items():
            entry[0] = bitset_id(entry[0])

    data_ids = {name: bitset_id(mask) for name, mask in sorted(data.items()) if name in tracked_set}
    dir_ids = {name: bitset_id(mask) for name, mask in sorted(dirs.items()) if os.path.isdir(os.path.join(repo, name))}

    # Which test file each test lives in: a test also depends on its own file being importable.
    test_files = {}
    for position, test in enumerate(tests):
        test_files[test["file"]] = test_files.get(test["file"], 0) | (1 << position)
    test_file_ids = {name: bitset_id(mask) for name, mask in sorted(test_files.items())}

    sources = {}
    src_blob = bytearray()
    classes = {}
    for name in tracked:
        if not name.endswith(".py"):
            continue
        with open(os.path.join(repo, name), "rb") as handle:
            raw = handle.read()
        packed = zlib.compress(raw, 9)
        sources[name] = [len(src_blob), len(packed)]
        src_blob += packed
        try:
            tree = ast.parse(raw)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = []
                for base in node.bases:
                    while isinstance(base, ast.Subscript):
                        base = base.value
                    if isinstance(base, ast.Attribute):
                        bases.append(base.attr)
                    elif isinstance(base, ast.Name):
                        bases.append(base.id)
                classes.setdefault(node.name, set()).update(bases)

    # class name -> names of all classes deriving from it (by simple name, an over-approximation)
    children = {}
    for name, bases in classes.items():
        for base in bases:
            children.setdefault(base, set()).add(name)
    descendants = {}
    for name in children:
        todo, found = list(children[name]), set()
        while todo:
            current = todo.pop()
            if current not in found:
                found.add(current)
                todo.extend(children.get(current, ()))
        found.discard(name)
        if found:
            descendants[name] = sorted(found)

    commit = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE, text=True
    ).stdout.strip()

    os.makedirs(out, exist_ok=True)
    if sys.byteorder != "little":
        rows.byteswap()
    rows_bytes = rows.tobytes()
    with open(os.path.join(out, "rows.bin"), "wb") as handle:
        handle.write(rows_bytes)
    with open(os.path.join(out, "bitsets.bin"), "wb") as handle:
        handle.write(b"".join(blob))
    with open(os.path.join(out, "src.bin"), "wb") as handle:
        handle.write(bytes(src_blob))
    meta = {
        "commit": commit,
        "ids": ids,
        "width": width,
        "unmapped": unmapped,
        "files": files,
        "data": data_ids,
        "dirs": dir_ids,
        "test_files": test_file_ids,
        "src": sources,
        "paths": tracked,
        "descendants": descendants,
        "classes": class_usage,
        "python": sys.version.split()[0],
    }
    with open(os.path.join(out, "meta.json"), "w") as handle:
        json.dump(meta, handle, separators=(",", ":"))
    meta["holders"], meta["typeddicts"] = fingerprint(repo, out)
    compare_dir = os.environ.get("BR_COMPARE_DIR")
    if compare_dir:
        compare_profile(compare_dir, index, out)
    if os.environ.get("BR_UNITS") and os.environ.get("BR_ALWAYS_DIRS"):
        replay_maps(os.environ["BR_UNITS"].split(os.pathsep), os.environ["BR_ALWAYS_DIRS"].split(os.pathsep), repo, out)
    with open(os.path.join(out, "meta.json"), "w") as handle:
        json.dump(meta, handle, separators=(",", ":"))
    for name in ("meta.json", "rows.bin", "bitsets.bin", "src.bin", "fingerprint.bin", "compare.json", "replay.json"):
        if not os.path.exists(os.path.join(out, name)):
            continue
        os.chmod(os.path.join(out, name), 0o644)
        print("%-12s %8d bytes" % (name, os.path.getsize(os.path.join(out, name))))
    print("tests %d, unmapped %d, distinct test sets %d, files with line data %d" % (count, len(unmapped), len(blob), len(files)))
    for which, trace_dir in enumerate(trace_dirs):
        missing = count - bin(seen[which]).count("1")
        failed = bin(seen[which] & ~passed[which]).count("1")
        print("  %s: %d tests not traced, %d did not pass" % (trace_dir, missing, failed))
    for position in unmapped[:20]:
        print("  unmapped:", ids[position])


def replay_maps(unit_files, always_dirs, repo, out):
    """maps/replay.json: line coverage of every replay unit (see replay.py), what the replayed
    tests execute outside their queries, and the fixtures the units need (maps/tpc/)."""
    import shutil

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import replay

    names = None
    cover = {}
    for path in unit_files:
        with open(path, "rb") as handle:
            data = pickle.load(handle)
        keys = sorted(data["cover"])
        if names is None:
            names = keys
        elif names != keys:
            raise SystemExit("unit traces disagree on the units")
        for key, lines in data["cover"].items():
            cover.setdefault(key, set()).update(map(tuple, lines))
    by_line = {}
    for position, key in enumerate(names):
        for name, line in cover[key]:
            by_line.setdefault(name, {})
            by_line[name][line] = by_line[name].get(line, 0) | (1 << position)
    sets = {}
    lines = {}
    for name in sorted(by_line):
        flat = []
        for line in sorted(by_line[name]):
            flat.append(line)
            flat.append(sets.setdefault(by_line[name][line], len(sets)))
        lines[name] = flat
    tests = {}
    for position, key in enumerate(names):
        tests.setdefault(replay.test_of(key.split("/")[0]), []).append(position)
    always = {}
    for directory in always_dirs:
        for path in sorted(glob.glob(os.path.join(directory, "*.pkl"))):
            with open(path, "rb") as handle:
                trace = pickle.load(handle)
            shared = [entry for kind in ("run", "imp") for entry in trace["collection"][kind]]
            for test, result in trace["tests"].items():
                if result.get("outcome") != "passed":
                    raise SystemExit("%s did not pass with its queries removed" % test)
                found = always.setdefault(test, {})
                for name, line in [entry for kind in ("run", "imp") for entry in result[kind]] + shared:
                    found.setdefault(name, set()).add(line)
    missing = sorted(set(tests) - set(always))
    if missing:
        raise SystemExit("no query-free trace for %s" % missing)
    data = {
        "units": names,
        "tests": tests,
        "sets": ["%x" % mask for mask, _ in sorted(sets.items(), key=lambda item: item[1])],
        "lines": lines,
        "always": {test: {name: sorted(found) for name, found in sorted(files.items())} for test, files in sorted(always.items())},
    }
    with open(os.path.join(out, "replay.json"), "w") as handle:
        json.dump(data, handle, separators=(",", ":"))
    for group, wanted in (("tpc-ds", (".csv.gz", "tpc-ds.sql")), ("tpc-h", ("tpc-h.sql",))):
        source = os.path.join(repo, "tests", "fixtures", "optimizer", group)
        target = os.path.join(out, "tpc", group)
        os.makedirs(target, exist_ok=True)
        for name in sorted(os.listdir(source)):
            if name.endswith(wanted):
                shutil.copyfile(os.path.join(source, name), os.path.join(target, name))
    print("replay: %d coverage sets over %d files, %d distinct line sets" % (len(names), len(lines), len(sets)))


def compare_profile(directory, index, out):
    """Merges the per-process files of profile_compares.py into maps/compare.json."""
    sites = {}
    given_up = set()
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".txt") or name == "failed.txt":
            continue
        with open(os.path.join(directory, name)) as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 2:
                    continue
                if parts[0] == "!":
                    given_up.add(parts[1])
                else:
                    sites.setdefault(parts[0], set()).add(parts[1])
    failed = []
    path = os.path.join(directory, "failed.txt")
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                test = line.strip()
                if test:
                    failed.append(index.get(test, -1))
    if -1 in failed:
        failed = list(range(len(index)))  # a failure we cannot place: trust nothing
    data = {
        "sites": {site: ",".join(sorted(found)) for site, found in sorted(sites.items()) if site not in given_up},
        "given_up": sorted(given_up),
        "failed": sorted(set(failed)),
    }
    with open(os.path.join(out, "compare.json"), "w") as handle:
        json.dump(data, handle, separators=(",", ":"))
    print("comparison profile: %d sites, %d given up, %d tests failed while profiling" % (len(data["sites"]), len(given_up), len(failed)))


def fingerprint(repo, out, runs=16):
    """Fingerprint the pinned tree several times and keep, per name, a digest that is stable.

    Every run but the first shifts memory addresses around first (BR_FP_NOISE), because the
    order of sets of classes depends on addresses and a patched tree allocates differently.
    """
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fingerprint.py")
    tables = []
    version = ""
    for attempt in range(runs):
        env = dict(os.environ, PYTHONHASHSEED="0")
        env.pop("BR_FP_NOISE", None)
        if attempt:
            env["BR_FP_NOISE"] = str(attempt)
        text = subprocess.run(
            [sys.executable, "-I", "-S", "-B", script, repo], check=True, stdout=subprocess.PIPE, text=True, env=env
        ).stdout
        table = {}
        for line in text.split("\n"):
            if line.startswith("#python"):
                version = line.split("\t")[1]
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                table[(parts[0], parts[1], parts[2])] = parts[3:]
        tables.append(table)
    lines = ["#python\t" + version]
    modes = {"o": 0, "b": 0, "v": 0}
    holders = {}  # class -> classes that keep it as an attribute (e.g. a dialect and its parser)
    typeddicts = []
    for key in tables[0]:
        rows = [table.get(key) for table in tables]
        if any(row is None for row in rows):
            mode = "v"
        elif all(row[1] == rows[0][1] for row in rows):
            mode = "o"
        elif all(row[2] == rows[0][2] for row in rows):
            mode = "b"
        else:
            mode = "v"
        modes[mode] += 1
        first = rows[0] if rows[0] is not None else ["o", "", ""]
        which = 1 if mode == "b" else 0
        row = list(key) + [first[0], mode, "" if mode == "v" else first[1 + which]]
        if key[2] == "!typeddict":
            typeddicts.append(key[0] + "." + key[1])
        if len(first) > 3 and first[3][:1] == "@":
            row.append(first[3])
            if key[1]:
                holders.setdefault(first[3][1:], set()).add(key[0] + "." + key[1])
        elif mode != "v" and len(first) > 3 and first[3]:
            names = [entry.split("=")[0] for entry in first[3].split(",")]
            if all(len(other) > 3 and [e.split("=")[0] for e in other[3].split(",")] == names for other in rows):
                row.append(",".join(e.split("=")[0] + "=" + e.split("=")[1].split("/")[which] for e in first[3].split(",")))
        lines.append("\t".join(row))
    with open(os.path.join(out, "fingerprint.bin"), "wb") as handle:
        handle.write(zlib.compress("\n".join(lines).encode(), 9))
    print("fingerprint: %d names (%d exact, %d order-free, %d unstable and ignored)" % (len(lines) - 1, modes["o"], modes["b"], modes["v"]))
    return {name: sorted(found) for name, found in sorted(holders.items())}, sorted(typeddicts)


if __name__ == "__main__":
    main()

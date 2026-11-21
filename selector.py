"""Blast Radius selector.

For every patch, work out which tests can possibly be affected, using maps
recorded once from the pinned sqlglot commit (see tools/), and print the
cheapest worker split for them. No test runs here.

How a patch is read:

* A Python file is patched in memory and its syntax tree is compared with the
  pinned one, statement by statement. Statements whose tree is unchanged
  (comments, blank lines, formatting) select nothing.
* A changed statement inside a function selects the tests that executed that
  statement's lines at the pinned commit. An inserted statement selects the
  tests that reached the statement before it (or the enclosing block).
* A changed statement at class or module level runs at import time, so it
  selects the tests whose process imported the module. When that set is
  expensive, the patched package is imported once (fingerprint.py) and its
  import-time state is compared with the pinned one. That shows exactly which
  class attributes and module names ended up different, including tables
  derived from the changed one, and the selection shrinks to the tests that
  instantiated those classes and executed a line reading those names.
* A file that no longer parses or compiles, or a module that no longer
  imports, selects every test that touched it.
* Any other file (fixtures, data, config) selects the tests that opened it.
* Anything the maps cannot explain falls back to wider and wider sets.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
import zlib
from array import array
from bisect import bisect_left, bisect_right

DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.environ.get("BR_MAPS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
DEBUG = bool(os.environ.get("BR_DEBUG"))
WORKER_MS = 2000
MAX_WORKERS = 8
RUN, IMP, DEF = 1, 2, 4
PACKAGE = "sqlglot/"          # the tree fingerprint.py can import
DYNAMIC_GAIN_MS = int(os.environ.get("BR_DYNAMIC_GAIN_MS", "2000"))  # import the patch only if it can save this much
REPLAY_SHARE = 0.5            # rerun parts of a slow test only if that costs at most this share of what it can save
REPLAY_FULL_SHARE = 0.9       # ... or, when even rerunning every planned step in full is sure to cost less, this share
REPLAY_RESERVE = 15           # seconds of DYNAMIC_BUDGET a full rerun must leave for the commits after it
DYNAMIC_TIMEOUT = 8          # seconds for one import of the patched package (it normally takes half a second)
DYNAMIC_BUDGET = 60          # seconds for all such imports and replays in a batch
BATCH_BUDGET = 110           # after this many seconds every remaining commit simply runs everything
SCRATCH = os.environ.get("BR_SCRATCH", "/tmp/blast-radius-%d" % os.getpid())


def log(*parts):
    if DEBUG:
        print(*parts, file=sys.stderr)


# --------------------------------------------------------------------------- maps


class Maps:
    def __init__(self, directory):
        self.directory = directory
        with open(os.path.join(directory, "meta.json")) as handle:
            meta = json.load(handle)
        self.ids = meta["ids"]
        self.position = {test: index for index, test in enumerate(self.ids)}
        self.width = meta["width"]
        self.files = meta["files"]
        self.data = meta["data"]
        self.dirs = meta["dirs"]
        self.test_files = meta["test_files"]
        self.src = meta["src"]
        self.classes = meta["classes"]
        self.holders = meta.get("holders", {})
        self.typeddicts = set(meta.get("typeddicts", ()))
        self.python = meta.get("python")
        self.paths = set(meta["paths"])
        self.unmapped = 0
        for position in meta["unmapped"]:
            self.unmapped |= 1 << position
        self.everything = (1 << len(self.ids)) - 1
        self._rows = open(os.path.join(directory, "rows.bin"), "rb")
        self._bits = open(os.path.join(directory, "bitsets.bin"), "rb")
        self._src = open(os.path.join(directory, "src.bin"), "rb")
        self._row_cache = {}
        self._bit_cache = {0: 0}
        self._text_cache = {}
        self._line_cache = {}
        self._text_lines = {}
        self._word_cache = {}
        self._top = None
        self._compare = None

    def bitset(self, ident):
        found = self._bit_cache.get(ident)
        if found is None:
            self._bits.seek(ident * self.width)
            found = int.from_bytes(self._bits.read(self.width), "little")
            self._bit_cache[ident] = found
        return found

    def rows(self, path):
        """(lines, run ids, import ids, def ids) for a source file, or None when no test touched it."""
        found = self._row_cache.get(path, 0)
        if found == 0:
            found = None
            entry = self.files.get(path)
            if entry:
                self._rows.seek(entry[0] * 16)
                raw = array("I")
                raw.frombytes(self._rows.read(entry[1] * 16))
                if sys.byteorder != "little":
                    raw.byteswap()
                found = (raw[0::4], raw[1::4], raw[2::4], raw[3::4])
            self._row_cache[path] = found
        return found

    def hits(self, path, first, last, kinds=RUN | IMP | DEF):
        """Tests that executed any line of path in [first, last] in one of the given ways."""
        rows = self.rows(path)
        if rows is None or last < first:
            return 0
        lines = rows[0]
        columns = [rows[column] for column, kind in ((1, RUN), (2, IMP), (3, DEF)) if kinds & kind]
        mask = 0
        seen = set()
        for position in range(bisect_left(lines, first), bisect_right(lines, last)):
            for column in columns:
                ident = column[position]
                if ident and ident not in seen:
                    seen.add(ident)
                    mask |= self.bitset(ident)
        return mask

    def traceable(self, path, first, last):
        """True when some line in [first, last] has bytecode, i.e. would show up in a trace if executed."""
        rows = self.rows(path)
        if rows is None:
            return False
        lines = rows[0]
        return bisect_left(lines, first) < bisect_right(lines, last)

    def whole(self, path):
        """Every test that executed or read any part of a file."""
        mask = self.hits(path, 0, 1 << 30)
        mask |= self.readers(path)
        ident = self.test_files.get(path)
        if ident:
            mask |= self.bitset(ident)
        return mask

    def readers(self, path):
        ident = self.data.get(path)
        return self.bitset(ident) if ident else 0

    def listers(self, directory):
        ident = self.dirs.get(directory)
        return self.bitset(ident) if ident else 0

    def text(self, path):
        found = self._text_cache.get(path)
        if found is None:
            entry = self.src.get(path)
            if entry is None:
                return None
            self._src.seek(entry[0])
            found = zlib.decompress(self._src.read(entry[1])).decode("utf-8", "surrogateescape")
            self._text_cache[path] = found
        return found

    def top_level(self):
        if self._top is None:
            self._top = {path.split("/", 1)[0] for path in self.paths}
        return self._top

    # ---- who reads a name

    def words(self, path):
        """line -> identifiers that occur on it in code or in string literals: names, attribute
        names, imported names, keyword names. Comments do not count, and neither do type
        annotations in a module with `from __future__ import annotations` (never evaluated)."""
        found = self._line_cache.get(path)
        if found is not None:
            return found
        found = {}
        text = self.text(path)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            # Fall back to every word of every line.
            for number, content in enumerate(text.split("\n"), 1):
                found[number] = set(IDENTIFIER.findall(content))
            self._line_cache[path] = found
            return found
        lazy = any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body[:6]
        )

        def add(line, word):
            if word:
                found.setdefault(line, set()).add(word)

        todo = [tree]
        while todo:
            node = todo.pop()
            for field, value in ast.iter_fields(node):
                if lazy and field in ("annotation", "returns"):
                    continue
                if isinstance(value, ast.AST):
                    todo.append(value)
                elif isinstance(value, list):
                    todo.extend(item for item in value if isinstance(item, ast.AST))
            if isinstance(node, ast.Name):
                add(node.lineno, node.id)
            elif isinstance(node, ast.Attribute):
                add(node.end_lineno, node.attr)
            elif isinstance(node, ast.Constant):
                if isinstance(node.value, str):
                    for word in IDENTIFIER.findall(node.value):
                        for line in range(node.lineno, node.end_lineno + 1):
                            add(line, word)
            elif isinstance(node, ast.alias):
                for word in (node.name or "").split(".") + [node.asname]:
                    add(node.lineno, word)
            elif isinstance(node, ast.keyword):
                add(node.value.lineno, node.arg)
            elif isinstance(node, ast.arg):
                add(node.lineno, node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                add(node.lineno, node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                for word in node.names:
                    add(node.lineno, word)
            elif isinstance(node, ast.MatchClass):
                for word in node.kwd_attrs:
                    add(node.lineno, word)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
                add(node.lineno, node.name)
            elif isinstance(node, ast.MatchMapping):
                add(node.lineno, node.rest)
            elif isinstance(node, ast.ExceptHandler):
                add(node.lineno, node.name)
        self._line_cache[path] = found
        return found

    def mentions(self, words, strict=False):
        """Tests that executed, at call time, a line containing every one of the given identifiers.

        Inside the package only call-time execution counts (import-time flows are
        what the fingerprint comparison follows). Outside it, e.g. in test
        modules, any execution counts. A line contains an identifier when its text
        does; with strict, when its code does (see words).
        """
        key = tuple(words) + (strict,)
        found = self._word_cache.get(key)
        if found is not None:
            return found
        patterns = [re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(word)) for word in words]
        mask = 0
        idents = set()
        for path in self.files:
            if not path.endswith(".py"):
                continue
            text = self.text(path)
            if text is None or any(word not in text for word in words):
                continue
            rows = self.rows(path)
            if rows is None:
                continue
            source = self._text_lines.get(path)
            if source is None:
                source = self._text_lines[path] = text.split("\n")
            on_line = None
            lines, run_ids, imp_ids, def_ids = rows
            inside = path.startswith(PACKAGE)
            limit = len(source)
            for position, number in enumerate(lines):
                if number > limit:
                    break
                content = source[number - 1]
                if any(word not in content for word in words):
                    continue
                if not all(pattern.search(content) for pattern in patterns):
                    continue
                if strict:
                    if on_line is None:
                        on_line = self.words(path)
                    present = on_line.get(number, ())
                    if any(word not in present for word in words):
                        continue
                idents.add(run_ids[position])
                if not inside:
                    idents.add(imp_ids[position])
                    idents.add(def_ids[position])
        for ident in idents:
            if ident:
                mask |= self.bitset(ident)
        self._word_cache[key] = mask
        return mask

    def mention_ranges(self, words):
        """{path: [(line, line)]} for every line of a Python file whose text contains one of
        the identifiers."""
        pattern = re.compile(r"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])" % "|".join(re.escape(word) for word in sorted(words)))
        found = {}
        for path in self.files:
            if not path.endswith(".py"):
                continue
            text = self.text(path)
            if text is None or not any(word in text for word in words):
                continue
            lines = [(number, number) for number, content in enumerate(text.split("\n"), 1) if pattern.search(content)]
            if lines:
                found[path] = lines
        return found

    def comparisons(self):
        """Comparison profile of the pinned run: site -> set of outcome strings, or None when absent."""
        if self._compare is None:
            path = os.path.join(self.directory, "compare.json")
            if not os.path.exists(path):
                self._compare = ({}, set(), self.everything)
            else:
                with open(path) as handle:
                    data = json.load(handle)
                failed = 0
                for position in data["failed"]:
                    failed |= 1 << position
                self._compare = (data["sites"], set(data["given_up"]), failed)
        return self._compare

    def users(self, path, qualname):
        """Tests that instantiated the class, a class it is nested in, or a class that keeps it as an
        attribute (a dialect and its parser, say). None when that cannot be observed."""
        seen = set()
        todo = [(path, qualname)]
        mask = 0
        while todo:
            where = todo.pop()
            if where in seen:
                continue
            seen.add(where)
            table = self.classes.get(where[0])
            if table is None:
                return None
            parts = where[1].split(".")
            for size in range(1, len(parts) + 1):
                name = ".".join(parts[:size])
                entry = table.get(name)
                if entry is None or not entry[1]:
                    return None
                mask |= self.bitset(entry[0])
                for holder in self.holders.get(where[0][:-3].replace("/", ".").replace(".__init__", "") + "." + name, ()):
                    found = class_of(self, holder)
                    if found is None:
                        return None
                    todo.append(found)
        return mask


# --------------------------------------------------------------------------- patches


class FileChange:
    __slots__ = ("old", "new", "hunks", "added", "deleted", "binary", "renamed")

    def __init__(self):
        self.old = None
        self.new = None
        self.hunks = []
        self.added = False
        self.deleted = False
        self.binary = False
        self.renamed = False


HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _clean(path):
    path = path.rstrip("\r")
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
    return path


def parse_patch(text):
    changes = []
    current = None
    lines = text.split("\n")
    total = len(lines)
    position = 0
    while position < total:
        line = lines[position]
        position += 1
        if line.startswith("diff --git "):
            current = FileChange()
            changes.append(current)
            rest = _clean(line[11:])
            if rest.startswith("a/"):
                half = (len(rest) - 1) // 2
                if rest[half] == " " and rest[2:half] == rest[half + 3:]:
                    current.old = current.new = rest[2:half]
                else:
                    cut = rest.find(" b/")
                    if cut > 0:
                        current.old, current.new = rest[2:cut], rest[cut + 3:]
            continue
        if line.startswith("--- ") and position < total and lines[position].startswith("+++ "):
            if current is None or current.hunks:
                current = FileChange()
                changes.append(current)
            old, new = _clean(line[4:]), _clean(lines[position][4:])
            position += 1
            if old == "/dev/null":
                current.added = True
            else:
                current.old = old[2:] if old[:2] in ("a/", "b/") else old
            if new == "/dev/null":
                current.deleted = True
            else:
                current.new = new[2:] if new[:2] in ("a/", "b/") else new
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            match = HUNK.match(line)
            if not match:
                current.binary = True
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            body = []
            while (old_count > 0 or new_count > 0) and position < total:
                entry = lines[position]
                position += 1
                tag = entry[:1]
                if tag == "\\":
                    continue
                if tag == "+":
                    new_count -= 1
                elif tag == "-":
                    old_count -= 1
                else:
                    tag = " "
                    old_count -= 1
                    new_count -= 1
                body.append((tag, entry[1:]))
            current.hunks.append((old_start, new_start, body))
        elif line.startswith("new file mode"):
            current.added = True
        elif line.startswith("deleted file mode"):
            current.deleted = True
        elif line.startswith("rename from "):
            current.old = _clean(line[12:])
            current.renamed = True
        elif line.startswith("rename to "):
            current.new = _clean(line[10:])
            current.renamed = True
        elif line.startswith("copy from "):
            current.old = None
            current.added = True
        elif line.startswith("copy to "):
            current.new = _clean(line[8:])
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
    return changes


def apply_hunks(old_lines, hunks):
    """Returns (new lines, blocks) or None when the patch does not fit the pinned text.

    A block is (old first, old last, new first, new last) in 1-based line
    numbers; an empty side has last == first - 1 and sits in the gap before
    line `first`.
    """
    new_lines = []
    blocks = []
    cursor = 0  # index into old_lines
    for old_start, _new_start, body in hunks:
        start = old_start - 1 if old_start > 0 else 0
        if not any(tag != "+" for tag, _ in body):
            start = old_start  # pure insertion: "-N,0" means after line N
        if start < cursor or start > len(old_lines):
            return None
        new_lines.extend(old_lines[cursor:start])
        cursor = start
        index = 0
        size = len(body)
        while index < size:
            tag, content = body[index]
            if tag == " ":
                if cursor >= len(old_lines) or old_lines[cursor].rstrip("\r") != content.rstrip("\r"):
                    return None
                new_lines.append(old_lines[cursor])
                cursor += 1
                index += 1
                continue
            old_first = cursor + 1
            new_first = len(new_lines) + 1
            while index < size and body[index][0] != " ":
                tag, content = body[index]
                if tag == "-":
                    if cursor >= len(old_lines) or old_lines[cursor].rstrip("\r") != content.rstrip("\r"):
                        return None
                    cursor += 1
                else:
                    new_lines.append(content)
                index += 1
            blocks.append((old_first, cursor, new_first, len(new_lines)))
    new_lines.extend(old_lines[cursor:])
    return new_lines, blocks


# --------------------------------------------------------------------------- python analysis

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
BLOCKS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler)
if hasattr(ast, "TryStar"):
    BLOCKS = BLOCKS + (ast.TryStar,)
BODY_FIELDS = ("body", "orelse", "finalbody", "handlers")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def first_line(node):
    line = node.lineno
    for decorator in getattr(node, "decorator_list", ()):
        if decorator.lineno < line:
            line = decorator.lineno
    return line


def touched(first, last, ranges):
    for low, high in ranges:
        if high >= low:
            if first <= high and last >= low:
                return True
        elif first < low <= last:
            return True
    return False


def header_dump(node):
    saved = {}
    for field in BODY_FIELDS:
        value = getattr(node, field, None)
        if isinstance(value, list):
            saved[field] = value
            setattr(node, field, [])
    try:
        return ast.dump(node)
    finally:
        for field, value in saved.items():
            setattr(node, field, value)


def header_range(node):
    """Lines of a compound statement before its body starts."""
    body = getattr(node, "body", None)
    last = node.lineno
    if body:
        last = max(last, first_line(body[0]) - 1)
    return first_line(node), last


INTROSPECTION = {"locals", "vars", "eval", "exec", "dir", "globals"}


def code_shape(code):
    """Everything about a code object that decides what it does: no line numbers and no names
    of plain local variables."""
    if INTROSPECTION.intersection(code.co_names):
        raise ValueError("looks at its own names")
    named = code.co_argcount + code.co_kwonlyargcount + bool(code.co_flags & 4) + bool(code.co_flags & 8)
    return (
        code.co_code, code.co_names, code.co_varnames[:named], len(code.co_varnames),
        code.co_freevars, code.co_cellvars, code.co_argcount, code.co_posonlyargcount,
        code.co_kwonlyargcount, code.co_flags, code.co_exceptiontable,
        tuple(code_shape(const) if hasattr(const, "co_code") else (type(const).__name__, repr(const)) for const in code.co_consts),
    )


def without_docstrings(node):
    """A copy of a function definition with every docstring in it blanked."""
    import copy

    node = copy.deepcopy(node)
    for inner in ast.walk(node):
        if isinstance(inner, FUNCTIONS + (ast.ClassDef,)) and inner.body:
            first = inner.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                first.value.value = ""
    return node


def function_shape(node, future_annotations):
    """Shape of a function definition, or None when it cannot be compiled on its own."""
    import __future__

    flags = __future__.annotations.compiler_flag if future_annotations else 0
    try:
        module = compile(
            ast.Module(body=[without_docstrings(node)], type_ignores=[]), "<function>", "exec",
            flags=flags, dont_inherit=True,
        )
        body = [const for const in module.co_consts if hasattr(const, "co_code") and const.co_name == node.name]
        if len(body) != 1:
            return None
        arguments = node.args
        signature = [
            node.name,
            [arg.arg for arg in arguments.posonlyargs], [arg.arg for arg in arguments.args],
            arguments.vararg.arg if arguments.vararg else None,
            [arg.arg for arg in arguments.kwonlyargs],
            arguments.kwarg.arg if arguments.kwarg else None,
            [ast.dump(default) for default in arguments.defaults],
            [ast.dump(default) if default is not None else None for default in arguments.kw_defaults],
            [ast.dump(decorator) for decorator in node.decorator_list],
        ]
        if not future_annotations:
            # Annotations are evaluated when the function is defined.
            every = arguments.posonlyargs + arguments.args + arguments.kwonlyargs + [arguments.vararg, arguments.kwarg]
            signature.append([ast.dump(arg.annotation) if arg is not None and arg.annotation else None for arg in every])
            signature.append(ast.dump(node.returns) if node.returns else None)
        return signature, code_shape(body[0])
    except (SyntaxError, ValueError, RecursionError, TypeError):
        return None


COMPARE_OPS = {ast.Eq: 0, ast.Is: 1, ast.NotEq: 2, ast.IsNot: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}
COMPARE_FAMILY = {0: (0, 1), 1: (0, 1), 2: (2, 3), 3: (2, 3), 4: (4, 5, 6, 7), 5: (4, 5, 6, 7), 6: (4, 5, 6, 7), 7: (4, 5, 6, 7)}


def operator_swaps(old, new, skip_bodies):
    """[(old Compare node, new operator)] when the two trees differ only in the operator of
    single-operator comparisons, else None. With skip_bodies, statement bodies are ignored."""
    swaps = []

    def walk(a, b, top):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.AST):
            if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
                if type(a.ops[0]) not in COMPARE_OPS or type(b.ops[0]) not in COMPARE_OPS:
                    return False
                swaps.append((a, COMPARE_OPS[type(b.ops[0])]))
                return walk(a.left, b.left, False) and walk(a.comparators[0], b.comparators[0], False)
            for field in a._fields:
                if top and skip_bodies and field in BODY_FIELDS:
                    continue
                if not walk(getattr(a, field, None), getattr(b, field, None), False):
                    return False
            return True
        if isinstance(a, list):
            return len(a) == len(b) and all(walk(x, y, False) for x, y in zip(a, b))
        return a == b

    if walk(old, new, True) and swaps:
        return swaps
    return None


def masked_dump(node):
    """Dump of a statement with every lambda body blanked out."""
    lambdas = [inner for inner in ast.walk(node) if isinstance(inner, ast.Lambda)]
    saved = [(inner, inner.body) for inner in lambdas]
    for inner in lambdas:
        inner.body = ast.Constant(value=None)
    try:
        return ast.dump(node)
    finally:
        for inner, body in saved:
            inner.body = body


class PyDiff:
    """Compares the pinned and patched trees of one file and collects the affected tests.

    body: tests selected by call-time changes (function bodies, lambdas).
    importers: tests selected by import-time changes (class and module level), i.e. the tests
    whose process executed the changed statements while importing.
    """

    def __init__(self, maps, path, old_tree, new_tree, blocks):
        self.maps = maps
        self.path = path
        self.old_ranges = [(block[0], block[1]) for block in blocks]
        self.new_ranges = [(block[2], block[3]) for block in blocks]
        self.old_tree = old_tree
        self.new_tree = new_tree
        self.body = 0
        self.importers = 0
        self.changed = False
        self.at_import = False
        self.ranges = []   # line ranges whose call-time execution selected tests
        self.strong = 0    # tests selected for any other reason than executing those ranges at call time
        self.imp = 0       # tests that executed edited function lines while sqlglot was being imported
        self.neutral = []  # (old first, old last, new first, new last) of edited top-level functions
        self.outer = None  # (old, new) top-level function being compared
        self.checks = []  # new-side nodes worth compiling for late syntax errors
        self.future_annotations = any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for tree in (old_tree, new_tree) for node in tree.body[:6]
        ) and all(
            any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names) for node in tree.body[:6])
            for tree in (old_tree, new_tree)
        )

    def run_hits(self, first, last):
        return self.maps.hits(self.path, first, last, RUN | IMP)

    def reached(self, first, last, function):
        """The range whose execution stands for lines [first, last] of a function: the lines
        themselves, or the whole function when nothing in them can show up in a trace
        (`global x`, a bare annotation, ...)."""
        if self.maps.traceable(self.path, first, last):
            return first, last, RUN | IMP | DEF
        return first_line(function), function.end_lineno, RUN | IMP

    def call_time(self, what, first, last, kinds=RUN | IMP | DEF):
        """A change that matters to the tests that executed lines [first, last].

        A range that is a whole function takes kinds=RUN | IMP: its `def` line and decorators
        also execute in the enclosing class or module body at import, which is not a call."""
        self.ranges.append((first, last))
        during_import = self.maps.hits(self.path, first, last, kinds & IMP)
        if during_import and self.outer is not None:
            # The function also runs while sqlglot is imported. What that leaves behind is
            # compared separately (fingerprint.py, with this function's own code set aside);
            # here only the tests that ran the lines at call time are taken.
            old, new = self.outer
            span = (first_line(old), old.end_lineno, first_line(new), new.end_lineno)
            if span not in self.neutral:
                self.neutral.append(span)
            self.imp |= during_import
            self.strong |= self.maps.hits(self.path, first, last, kinds & DEF)
            self.note(what, first, last, self.maps.hits(self.path, first, last, kinds & ~IMP))
            log("    %-22s %d tests ran these lines during import" % ("", bin(during_import).count("1")))
            return
        self.strong |= self.maps.hits(self.path, first, last, kinds & (IMP | DEF))
        self.note(what, first, last, self.maps.hits(self.path, first, last, kinds))

    def def_hits(self, first, last):
        return self.maps.hits(self.path, first, last, DEF)

    def note(self, what, first, last, body=0, importers=None):
        self.changed = True
        self.body |= body
        if importers is not None:
            self.at_import = True
            self.importers |= importers
        log("    %-22s %s:%d-%d -> %d call-time, %s import-time" % (
            what, self.path, first, last, bin(body).count("1"),
            "-" if importers is None else bin(importers).count("1")))

    # ---- statement lists

    def diff_module(self):
        self.diff_list(self.old_tree.body, self.new_tree.body, None, None, None, None)

    def diff_list(self, old, new, function, klass, parent, new_function):
        """function: innermost enclosing old function (None at class/module level)."""
        old_ranges, new_ranges = self.old_ranges, self.new_ranges
        low = 0
        limit = min(len(old), len(new))
        while low < limit:
            a, b = old[low], new[low]
            if touched(first_line(a), a.end_lineno, old_ranges) or touched(first_line(b), b.end_lineno, new_ranges):
                break
            low += 1
        old_high, new_high = len(old), len(new)
        while old_high > low and new_high > low:
            a, b = old[old_high - 1], new[new_high - 1]
            if touched(first_line(a), a.end_lineno, old_ranges) or touched(first_line(b), b.end_lineno, new_ranges):
                break
            old_high -= 1
            new_high -= 1
        old_mid, new_mid = old[low:old_high], new[low:new_high]
        if not old_mid and not new_mid:
            return
        if len(old_mid) == 1 and len(new_mid) == 1:
            self.diff_pair(old_mid[0], new_mid[0], function, klass, new_function)
            return
        old_dumps = [ast.dump(node) for node in old_mid]
        new_dumps = [ast.dump(node) for node in new_mid]
        if old_dumps == new_dumps:
            return
        # Align the two runs: strip equal statements at both ends, the rest is replaced.
        head = 0
        while head < len(old_dumps) and head < len(new_dumps) and old_dumps[head] == new_dumps[head]:
            head += 1
        tail = 0
        while (
            tail < len(old_dumps) - head
            and tail < len(new_dumps) - head
            and old_dumps[len(old_dumps) - 1 - tail] == new_dumps[len(new_dumps) - 1 - tail]
        ):
            tail += 1
        gone = old_mid[head:len(old_mid) - tail]
        fresh = new_mid[head:len(new_mid) - tail]
        self.checks.append((new_function, fresh))
        if len(gone) == 1 and len(fresh) == 1:
            self.diff_pair(gone[0], fresh[0], function, klass, new_function)
            return
        for node in gone:
            self.statement_changed(node, None, function, klass)
        if fresh and (not gone or function is None):
            self.inserted(old, low + head, function, klass, parent)

    def equivalent_swaps(self, old, new, skip_bodies):
        """True when old and new differ only in comparison operators that, at every execution
        in the pinned test run, gave the same bool as the original operator."""
        swaps = operator_swaps(old, new, skip_bodies)
        if swaps is None:
            return False
        sites, given_up, failed = self.maps.comparisons()
        for node, new_op in swaps:
            old_op = COMPARE_OPS[type(node.ops[0])]
            family = COMPARE_FAMILY[old_op]
            if new_op not in family:
                return False
            key = "%s:%d:%d:%d:%d" % (self.path, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
            reached = self.maps.hits(self.path, node.lineno, node.end_lineno)
            if key in given_up or reached & failed:
                return False
            outcomes = sites.get(key)
            if outcomes is None:
                if reached:
                    return False  # executed, but not seen by the profile
                continue
            a, b = family.index(old_op), family.index(new_op)
            for outcome in outcomes.split(","):
                if outcome[a] not in "TF" or outcome[a] != outcome[b]:
                    return False
        log("    %-22s %s:%d (%d comparison%s)" % (
            "same comparison result", self.path, first_line(old), len(swaps), "" if len(swaps) == 1 else "s"))
        self.changed = True
        return True

    def diff_pair(self, old, new, function, klass, new_function):
        if type(old) is type(new) and isinstance(old, FUNCTIONS):
            # Same bytecode, constants, defaults and decorators once docstrings are set aside:
            # the function does what it did (a docstring, an annotation or the name of a
            # local variable changed).
            shape = function_shape(old, self.future_annotations)
            if shape is not None and shape == function_shape(new, self.future_annotations):
                log("    %-22s %s:%d-%d" % ("same bytecode", self.path, first_line(old), old.end_lineno))
                return
        if type(old) is type(new) and isinstance(old, FUNCTIONS + (ast.ClassDef,) + BLOCKS):
            if header_dump(old) == header_dump(new):
                if isinstance(old, FUNCTIONS):
                    entered = function is None and self.outer is None
                    if entered:
                        self.outer = (old, new)
                    try:
                        self.diff_list(old.body, new.body, old, klass, old, new_function or new)
                    finally:
                        if entered:
                            self.outer = None
                elif isinstance(old, ast.ClassDef):
                    # A class defined inside a function runs its body at call time: keep the function.
                    self.diff_list(old.body, new.body, function, old, old, new_function)
                else:
                    for field in BODY_FIELDS:
                        a, b = getattr(old, field, None), getattr(new, field, None)
                        if isinstance(a, list) and isinstance(b, list) and (a or b):
                            self.diff_list(a, b, function, klass, old, new_function)
                return
            if self.equivalent_swaps(old, new, True):
                for field in BODY_FIELDS:
                    a, b = getattr(old, field, None), getattr(new, field, None)
                    if isinstance(a, list) and isinstance(b, list) and (a or b):
                        if isinstance(old, FUNCTIONS):
                            entered = function is None and self.outer is None
                            if entered:
                                self.outer = (old, new)
                            try:
                                self.diff_list(a, b, old, klass, old, new_function or new)
                            finally:
                                if entered:
                                    self.outer = None
                        elif isinstance(old, ast.ClassDef):
                            self.diff_list(a, b, function, old, old, new_function)
                        else:
                            self.diff_list(a, b, function, klass, old, new_function)
                return
            self.checks.append((new_function, [new]))
            self.header_changed(old, new, function)
            return
        if ast.dump(old) == ast.dump(new):
            return
        if self.equivalent_swaps(old, new, False):
            return
        self.checks.append((new_function, [new]))
        self.statement_changed(old, new, function, klass)

    # ---- events

    def header_changed(self, old, new, function):
        first, last = first_line(old), old.end_lineno
        if isinstance(old, FUNCTIONS):
            # Callers run the body; a new name or decorator also changes what the import leaves behind.
            body = self.run_hits(first, last)
            if function is not None:
                self.call_time("nested function", first, last, RUN | IMP)
            elif old.name != new.name or ast.dump(ast.Module(body=old.decorator_list, type_ignores=[])) != ast.dump(
                ast.Module(body=new.decorator_list, type_ignores=[])
            ):
                self.note("function definition", first, last, body, self.def_hits(first, old.lineno))
            else:
                self.call_time("function signature", first, last, RUN | IMP)
        elif isinstance(old, ast.ClassDef):
            if function is not None:
                self.call_time("nested class header", first, old.lineno)
            else:
                self.note("class header", first, old.lineno, 0, self.def_hits(first, old.lineno))
        else:
            first, last = header_range(old)
            if function is None:
                self.note("import-time block", first, last, 0, self.def_hits(first, last))
            else:
                self.call_time("block header", *self.reached(first, last, function))

    def statement_changed(self, old, new, function, klass):
        first, last = first_line(old), old.end_lineno
        if function is not None:
            self.call_time("statement", *self.reached(first, last, function))
            return
        # Import-time statement.
        if isinstance(old, FUNCTIONS):
            self.note("function", first, last, self.run_hits(first, last), self.def_hits(first, old.lineno))
            return
        if isinstance(old, ast.ClassDef):
            self.note("class", first, last, self.run_hits(first, last), self.def_hits(first, last))
            return
        if new is not None and type(old) is type(new) and masked_dump(old) == masked_dump(new):
            # Only the inside of lambdas changed: they run when called, not at import.
            spans = [
                (inner.lineno, inner.end_lineno) for inner in ast.walk(old)
                if isinstance(inner, ast.Lambda) and touched(inner.lineno, inner.end_lineno, self.old_ranges)
            ]
            if spans:
                # Lines of a lambda also run at import (building the table): only call-time
                # execution, or execution inside a function during an import, is the lambda itself.
                mask = 0
                for low, high in spans:
                    mask |= self.run_hits(low, high)
                    self.ranges.append((low, high))
                    self.strong |= self.maps.hits(self.path, low, high, IMP)
                self.note("lambda", first, last, mask)
                return
        self.note("import-time statement", first, last, self.run_hits(first, last), self.def_hits(first, last))

    def inserted(self, siblings, index, function, klass, parent):
        """New statements placed before siblings[index] of the pinned tree."""
        if function is not None:
            cursor = index - 1
            while cursor >= 0:
                anchor = siblings[cursor]
                first, last = first_line(anchor), anchor.end_lineno
                if self.maps.traceable(self.path, first, last):
                    self.call_time("insert after", first, last)
                    return
                cursor -= 1
            if parent is not function and parent is not None:
                first, last = header_range(parent)
                if isinstance(parent, ast.Try) or (hasattr(ast, "TryStar") and isinstance(parent, ast.TryStar)):
                    last = parent.end_lineno
                if self.maps.traceable(self.path, first, last):
                    self.call_time("insert in block", first, last)
                    return
            self.call_time("insert in function", first_line(function), function.end_lineno, RUN | IMP)
            return
        # Import time: everything that imported the enclosing class or module.
        if klass is not None:
            self.note("insert in class", first_line(klass), klass.end_lineno, 0,
                      self.def_hits(first_line(klass), klass.end_lineno))
        else:
            self.note("insert in module", 1, 1 << 30, 0, self.def_hits(1, 1 << 30))


def compiles(new_function, nodes):
    """Catches errors the parser accepts but the compiler rejects (e.g. `return` outside a function)."""
    try:
        if new_function is not None:
            module = ast.Module(body=[new_function], type_ignores=[])
        else:
            module = ast.Module(body=list(nodes), type_ignores=[])
        compile(module, "<patched>", "exec", dont_inherit=True)
        return True
    except (SyntaxError, ValueError, RecursionError, TypeError):
        return False


OLD_TREES = {}


class Outcome:
    """What one changed file selects. importers is None when nothing changed at import time."""

    __slots__ = ("mask", "importers", "why", "new_text", "ranges", "strong", "neutral")

    def __init__(self, mask, why, importers=None, new_text=None, ranges=None, strong=None, neutral=()):
        self.neutral = neutral      # edited top-level functions, as line spans in the old and new file
        self.mask = mask
        self.why = why
        self.importers = importers
        self.new_text = new_text
        self.ranges = ranges        # None: not explained by executed line ranges alone
        self.strong = mask if strong is None else strong


def python_file(maps, path, change):
    """Tests affected by a modification of a tracked Python file."""
    old_text = maps.text(path)
    if old_text is None:
        return Outcome(maps.whole(path), "no pinned source")
    old_lines = old_text.split("\n")
    applied = apply_hunks(old_lines, change.hunks)
    if applied is None:
        return Outcome(maps.whole(path), "patch does not apply")
    new_lines, blocks = applied
    if not blocks:
        return Outcome(0, "no change")
    for old_first, old_last, new_first, new_last in blocks:
        for lines, first, last in ((new_lines, new_first, new_last), (old_lines, old_first, old_last)):
            for number in range(first, min(last, 2) + 1):
                if CODING.match(lines[number - 1]):
                    return Outcome(maps.whole(path), "encoding declaration")
    new_text = "\n".join(new_lines)
    try:
        new_tree = ast.parse(new_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return Outcome(maps.whole(path), "does not parse")
    old_tree = OLD_TREES.get(path)
    if old_tree is None:
        try:
            old_tree = ast.parse(old_text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return Outcome(maps.whole(path), "pinned source does not parse")
        OLD_TREES[path] = old_tree
    diff = PyDiff(maps, path, old_tree, new_tree, blocks)
    diff.diff_module()
    if not diff.changed:
        return Outcome(0, "same syntax tree", ranges=[], new_text=new_text)
    for new_function, nodes in diff.checks:
        if not compiles(new_function, nodes):
            return Outcome(maps.whole(path), "does not compile")
    mask = diff.body | maps.readers(path)
    if diff.at_import or diff.imp:
        return Outcome(mask, "changed at import time", diff.importers | diff.imp, new_text, neutral=diff.neutral)
    return Outcome(mask, "changed", new_text=new_text, ranges=diff.ranges, strong=diff.strong | maps.readers(path))


# --------------------------------------------------------------------------- import-time state


class Tree:
    """A copy of the pinned package on disk, patched for one commit at a time."""

    def __init__(self, maps):
        self.maps = maps
        self.root = None
        here = os.path.dirname(os.path.abspath(__file__))
        self.script = os.path.join(here, "fingerprint.py")
        self.prebuilt = os.path.join(here, "tree")       # sources and bytecode laid out at image build
        self.baseline = os.path.join(maps.directory, "fingerprint.bin")
        if os.path.exists(os.path.join(self.prebuilt, "fingerprint.bin")):
            self.baseline = os.path.join(self.prebuilt, "fingerprint.bin")  # redone at image build for its Python
        self.sigs = os.path.join(self.prebuilt, "replay-sigs.json")   # pinned replay results, made at image build
        self.spent = 0.0

    def prepare(self):
        if self.root is not None:
            return
        self.root = os.path.join(SCRATCH, "tree")
        if os.path.isdir(os.path.join(self.prebuilt, "sqlglot")):
            # The table rows and CSV files stay where they are: only replay.py reads them.
            shutil.copytree(self.prebuilt, self.root, ignore=shutil.ignore_patterns("tpcds-rows.pkl", "*.csv.gz"))
        else:
            lay_out(self.maps, self.root)

    def run(self, *arguments):
        env = {"PYTHONHASHSEED": "0", "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
        started = time.monotonic()
        try:
            done = subprocess.run(
                [sys.executable, "-I", "-S", "-B", self.script, self.root] + list(arguments),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=DYNAMIC_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None, None
        finally:
            self.spent += time.monotonic() - started
        return done.returncode, done.stdout.decode("utf-8", "replace")

    def patch(self, files, saved):
        """Writes the patched files into the tree, remembering how to put the pinned ones back."""
        for path, text in files.items():
            target = os.path.join(self.root, path)
            cache = bytecode_path(target)
            state = None
            if os.path.exists(target):
                with open(target, "rb") as handle:
                    state = (handle.read(), os.stat(target))
            compiled = None
            if os.path.exists(cache):
                with open(cache, "rb") as handle:
                    compiled = handle.read()
                os.remove(cache)
            saved[path] = (state, compiled)
            if text is None:
                if state is not None:
                    os.remove(target)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
                    handle.write(text)

    def replay(self, files, plan, budget):
        """Reruns replay units (replay.py) on the patched tree, for at most `budget` CPU seconds.
        Returns {test id: still passes}; a test it could not finish is absent."""
        if self.spent > DYNAMIC_BUDGET or not os.path.exists(self.sigs):
            return {}
        self.prepare()
        saved = {}
        started = time.monotonic()
        try:
            self.patch(files, saved)
            plan_path = os.path.join(SCRATCH, "plan.json")
            with open(plan_path, "w") as handle:
                json.dump({"budget": budget, "tests": plan}, handle)
            env = {"PYTHONHASHSEED": "0", "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
                   "BR_ROWS": os.path.join(self.prebuilt, "tpcds-rows.pkl"), "BR_SIGS": self.sigs}
            done = subprocess.run(
                [sys.executable, "-S", "-P", "-B", os.path.join(os.path.dirname(self.script), "replay.py"), "check", self.root, plan_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=min(45.0, 3.0 + 1.5 * budget),
            )
            if done.returncode != 0:
                return {}
            return json.loads(done.stdout.decode("utf-8", "replace") or "{}")
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return {}
        finally:
            self.spent += time.monotonic() - started
            self.restore(saved)

    def compare(self, files):
        """files: {path: new text or None}. Returns the names whose import-time value differs,
        as (module, class qualname, name, kind, differing entry classes or None), or None
        when the comparison could not be made."""
        if self.spent > DYNAMIC_BUDGET:
            return None
        self.prepare()
        saved = {}
        try:
            self.patch(files, saved)
            code, output = self.run("--against", self.baseline)
            if code == 3:
                # Maps were built by another Python: make a baseline with this one, once.
                self.restore(saved)
                saved = {}
                if not self.rebase():
                    return None
                return self.compare(files)
        finally:
            self.restore(saved)
        if code != 0 or output is None:
            return None
        changed = []
        for line in output.split("\n"):
            parts = line.split("\t")
            if len(parts) == 5 and parts[0][:1] != "#":
                changed.append((parts[0], parts[1], parts[2], parts[3], None if parts[4] == "-" else parts[4].split(",")))
        return changed

    def compare_edited(self, files, neutral):
        """Like compare, for patches that also edit the body of functions that run during
        import: the pinned and the patched tree are both fingerprinted with those functions'
        code set aside, so that only what importing leaves behind is compared. neutral:
        (path, old first, old last, new first, new last)."""
        if self.spent > DYNAMIC_BUDGET:
            return None
        self.prepare()
        before = ",".join("%s:%d-%d" % (path, a, b) for path, a, b, _c, _d in neutral)
        after = ",".join("%s:%d-%d" % (path, c, d) for path, _a, _b, c, d in neutral)
        code, pinned = self.run("--neutral", before, "--emit", self.baseline)
        if code != 0 or not pinned:
            return None
        saved = {}
        try:
            self.patch(files, saved)
            code, patched = self.run("--neutral", after, "--emit", self.baseline)
        finally:
            self.restore(saved)
        if code != 0 or not patched:
            return None

        def table(output):
            found = {}
            for line in output.split("\n"):
                parts = line.split("\t")
                if len(parts) == 6:
                    found[(parts[0], parts[1], parts[2])] = parts[3:]
            return found

        old, new = table(pinned), table(patched)
        changed = []
        for key in set(old) | set(new):
            a, b = old.get(key), new.get(key)
            if a is not None and b is not None and a[1] == b[1]:
                continue
            if key[2] == "!import" and b is None:
                continue
            entries = None
            if a is not None and b is not None and a[2] != "-" and b[2] != "-":
                left = dict(entry.split("=") for entry in a[2].split(","))
                right = dict(entry.split("=") for entry in b[2].split(","))
                entries = sorted(ref for ref in set(left) | set(right) if left.get(ref) != right.get(ref)) or None
            kind = (b or a)[0] if a is None or b is None or a[0] == b[0] else "o"
            changed.append(key + (kind, entries))
        return changed

    def restore(self, saved):
        for path, (state, compiled) in saved.items():
            target = os.path.join(self.root, path)
            cache = bytecode_path(target)
            try:
                if os.path.exists(cache):
                    os.remove(cache)
                if state is None:
                    if os.path.exists(target):
                        os.remove(target)
                    continue
                with open(target, "wb") as handle:
                    handle.write(state[0])
                os.utime(target, ns=(state[1].st_atime_ns, state[1].st_mtime_ns))
                if compiled is not None:
                    with open(cache, "wb") as handle:
                        handle.write(compiled)
            except OSError:
                pass

    def rebase(self):
        """Recompute the pinned fingerprint with the running interpreter, keeping the stability modes."""
        code, output = self.run()
        if code != 0 or not output:
            return False
        shipped = self.baseline
        self.baseline = os.path.join(SCRATCH, "fingerprint.bin")
        with open(self.baseline, "wb") as handle:
            handle.write(rebased(output, shipped))
        return True

    def close(self):
        if self.root is not None:
            shutil.rmtree(SCRATCH, ignore_errors=True)


def rebased(output, shipped):
    """A fingerprint baseline from fingerprint.py's full output for the pinned tree, keeping for
    every name the stability mode recorded in the shipped baseline (bytecode digests only
    compare within one Python version; which names are order-unstable does not change)."""
    with open(shipped, "rb") as handle:
        modes = {}
        for line in zlib.decompress(handle.read()).decode().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 6:
                modes[(parts[0], parts[1], parts[2])] = parts[4]
    lines = []
    for line in output.split("\n"):
        parts = line.split("\t")
        if parts[0] == "#python":
            lines.append(line)
        elif len(parts) >= 6:
            mode = modes.get((parts[0], parts[1], parts[2]), "o")
            which = 1 if mode == "b" else 0
            row = parts[:4] + [mode, "" if mode == "v" else parts[4 + which]]
            if len(parts) > 6 and parts[6][:1] == "@":
                row.append(parts[6])
            elif mode != "v" and len(parts) > 6 and parts[6]:
                row.append(",".join(e.split("=")[0] + "=" + e.split("=")[1].split("/")[which] for e in parts[6].split(",")))
            lines.append("\t".join(row))
    return zlib.compress("\n".join(lines).encode(), 6)


def bytecode_path(source):
    import importlib.util

    return importlib.util.cache_from_source(source)


def lay_out(maps, root):
    """Writes the pinned package sources under root, and what replay.py needs beside them."""
    for path in maps.src:
        if path.startswith(PACKAGE) or path in ("tests/__init__.py", "tests/helpers.py"):
            target = os.path.join(root, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
                handle.write(maps.text(path))
    fixtures = os.path.join(maps.directory, "tpc")
    if os.path.isdir(fixtures):
        shutil.copytree(fixtures, os.path.join(root, "tests", "fixtures", "optimizer"), dirs_exist_ok=True)


class Replay:
    """Decides which of the slow replayable tests (replay.py) a call-time change can be checked
    against cheaply, and drops the ones whose computation comes out unchanged."""

    def __init__(self, maps, tree):
        self.maps = maps
        self.tree = tree
        self.data = None

    def load(self):
        if self.data is None:
            self.data = False
            path = os.path.join(self.maps.directory, "replay.json")
            if os.path.exists(path) and os.path.exists(self.tree.sigs):
                with open(path) as handle:
                    data = json.load(handle)
                with open(self.tree.sigs) as handle:
                    sigs = json.load(handle)
                if sigs.get("python") == sys.version.split()[0]:
                    position = {test: index for index, test in enumerate(self.maps.ids)}
                    data["sets"] = [int(text, 16) for text in data["sets"]]
                    data["bit"] = {test: 1 << position[test] for test in data["tests"] if test in position}
                    data["mask"] = {test: sum(1 << unit for unit in units) for test, units in data["tests"].items()}
                    data["seconds"] = sigs["units"]
                    self.data = data
        return self.data

    def possible(self, mask, strong):
        data = self.load()
        return bool(data) and any(mask & bit and not strong & bit for bit in data["bit"].values())

    def drop(self, mask, strong, ranges, files, cost, thorough=False):
        """mask: selected tests; strong: tests selected for more than executing `ranges`
        ({path: [(first, last)]}) at call time. Returns the tests proven unaffected.
        thorough: rerun every parse, transpile and optimize step of a candidate, whether or
        not its recorded execution contains a line of `ranges` (used when the lines are not
        the change itself but where a changed attribute is named)."""
        data = self.load()
        if not data:
            return 0
        maps = self.maps
        touched = 0          # units whose recorded execution contains a changed line
        for path, spans in ranges.items():
            flat = data["lines"].get(path)
            if not flat:
                continue
            lines = flat[0::2]
            for first, last in spans:
                for position in range(bisect_left(lines, first), bisect_right(lines, last)):
                    touched |= data["sets"][flat[2 * position + 1]]
        candidates = {}
        for test, bit in data["bit"].items():
            if not mask & bit or strong & bit:
                continue
            units = data["mask"][test]
            always = data["always"].get(test, {})
            explained = True
            for path, spans in ranges.items():
                rows = maps.rows(path)
                if rows is None:
                    continue
                flat = data["lines"].get(path, [])
                covered = dict(zip(flat[0::2], flat[1::2]))
                outside = set(always.get(path, ()))
                inside = path.startswith(PACKAGE)
                for first, last in spans:
                    for position in range(bisect_left(rows[0], first), bisect_right(rows[0], last)):
                        ran = maps.bitset(rows[1][position])
                        if not inside:
                            ran |= maps.bitset(rows[2][position]) | maps.bitset(rows[3][position])
                        if not ran & bit:
                            continue  # the test never executed this line at call time
                        line = rows[0][position]
                        if line in outside or line not in covered or not data["sets"][covered[line]] & units:
                            explained = False
            if explained:
                entry = self.plan(data, test, touched, thorough)
                if entry[0]:  # nothing to rerun can only mean the maps disagree: keep the test
                    candidates[test] = entry
        if not candidates:
            return 0
        # Cheapest useful subset: everything, or everything but the expensive ones.
        for limit in (None, 1.5):
            chosen = {test: entry for test, entry in candidates.items() if limit is None or entry[1] <= limit}
            if not chosen:
                continue
            bits = 0
            for test in chosen:
                bits |= data["bit"][test]
            gain = (cost(mask) - cost(mask & ~bits)) / 1000.0
            estimate = 0.5 + sum(entry[1] for entry in chosen.values())
            # Rerunning every step in full, with a tenth on top for a slower machine. The rerun
            # stops at the first difference, so a test that really fails costs little of this.
            worst = 1.1 * (0.5 + sum(entry[2] for entry in chosen.values()))
            if gain <= 0:
                continue
            if worst <= REPLAY_FULL_SHARE * gain and self.tree.spent + 1.3 * worst <= DYNAMIC_BUDGET - REPLAY_RESERVE:
                budget = 1.3 * worst      # certain to pay off if nothing differs: let it finish
            elif estimate <= REPLAY_SHARE * gain:
                budget = REPLAY_SHARE * gain
            else:
                continue
            log("    replay: %d units for %d tests, about %.1f s (at most %.1f s) for up to %.1f s" % (
                sum(len(entry[0]) for entry in chosen.values()), len(chosen), estimate, worst, gain))
            verdict = self.tree.replay(files, {test: entry[0] for test, entry in chosen.items()}, budget)
            dropped = 0
            for test in chosen:
                if verdict.get(test) is not True:
                    log("      %s: %s" % ("changed" if test in verdict else "not finished", test))
                if verdict.get(test) is True:
                    dropped |= data["bit"][test]
                    log("      unchanged: %s" % test)
            return dropped
        return 0

    def plan(self, data, test, touched, thorough=False):
        """([[unit, how]], estimated seconds) for the units of a test that must be rerun.
        how: 0 run it all; 1 stop after optimizing if the optimized query is the pinned one;
        2 nothing to run if the pickled query is the pinned one."""
        names = data["units"]
        seconds = data["seconds"]
        index = {name: position for position, name in enumerate(names)}
        wanted = []
        seen = set()
        for position in data["tests"][test]:
            name = names[position].split("/")[0]   # ds.C units are recorded as three parts
            if name in seen:
                continue
            seen.add(name)
            if ".C." in name:
                pre, opt, post = (touched >> index["%s/%s" % (name, phase)] & 1 for phase in ("pre", "opt", "post"))
                parsed = touched >> index[name.replace(".C.", ".A.")] & 1
                if pre or post:
                    wanted.append((seconds[name]["seconds"], name, 0))
                elif opt or thorough:
                    wanted.append((seconds[name]["opt_seconds"], name, 1))
                elif parsed:
                    wanted.append((0.01, name, 2))
            elif thorough or touched >> position & 1:
                wanted.append((seconds[name]["seconds"], name, 0))
        wanted.sort()
        worst = sum(seconds[name]["seconds"] for _cost, name, _how in wanted)
        return [[name, how] for _cost, name, how in wanted], sum(entry[0] for entry in wanted), worst




def module_path(maps, module):
    base = module.replace(".", "/")
    for candidate in (base + ".py", base + "/__init__.py"):
        if candidate in maps.paths:
            return candidate
    return None


def class_of(maps, ref):
    """'package.module.Outer.Inner' -> (path, 'Outer.Inner') or None."""
    parts = ref.split(".")
    for size in range(len(parts) - 1, 0, -1):
        path = module_path(maps, ".".join(parts[:size]))
        if path is not None:
            return path, ".".join(parts[size:])
    return None


def reached(maps, changed, notes=None):
    """Tests that can observe the given import-time differences.

    notes, when given, receives "words": the names whose mention on an executed line is the
    whole reason some tests were chosen, and "strong": the tests chosen for any other reason."""
    mask = 0
    words = set()
    strong = 0
    for module, qualname, name, kind, entry_classes in changed:
        path = module_path(maps, module)
        by_mention = ()
        if name == "!import":
            # The module stopped importing: everything that ever executed it.
            part = maps.whole(path) if path else maps.everything
            log("      %s no longer imports -> %d tests" % (module, bin(part).count("1")))
            mask |= part
            strong |= part
            continue
        if path is None:
            # A module that did not exist at the pinned commit: only names can lead to it.
            part = maps.mentions((name,))
            mask |= part
            strong |= part
            continue
        importers = maps.hits(path, 0, 1 << 30, DEF)
        if not qualname:
            part = maps.mentions((name,))
            if kind == "c":
                # A class can be looked up by a name held in data (serde): count whoever made one.
                part |= maps.users(path, name) or 0
            elif kind != "f":
                by_mention = (name,)
        else:
            users = maps.users(path, qualname)
            simple = qualname.rsplit(".", 1)[-1]
            special = name.startswith("__") and name.endswith("__") and name != "__annotations__"
            if users is None and "%s.%s" % (module, qualname) in maps.typeddicts:
                # Calling a TypedDict class gives a plain dict, so only code that names the class
                # can read anything from it. (A TypedDict built on it has names of its own here.)
                part = maps.mentions((simple,), strict=True) & importers
            elif special or kind in ("f", "c"):
                # Methods can be found by computed names (e.g. "<key>_sql"), and special
                # attributes are read by the interpreter itself: every user counts.
                part = users if users is not None else importers
            elif users is not None:
                part = (users & maps.mentions((name,))) | maps.mentions((simple, name))
                by_mention = (name,)
            else:
                part = (maps.mentions((name,)) | maps.mentions((simple,))) & importers
                by_mention = (name, simple)
        if entry_classes:
            narrowed = 0
            for ref in entry_classes:
                # An entry is found through an instance of its key class, or by naming the class.
                found = class_of(maps, ref)
                users = maps.users(found[0], found[1]) if found else None
                if users is None:
                    narrowed = None
                    break
                narrowed |= users | maps.mentions((ref.rsplit(".", 1)[-1],))
            if narrowed is not None:
                part &= narrowed
        log("      %s %s.%s (%s%s) -> %d tests" % (
            module, qualname, name, kind,
            ", %d entries" % len(entry_classes) if entry_classes else "", bin(part).count("1")))
        mask |= part
        if by_mention:
            words.update(by_mention)
        else:
            strong |= part
    if notes is not None:
        notes["words"] = words
        notes["strong"] = strong
    return mask


# --------------------------------------------------------------------------- one patch

PYTEST_FILES = {"pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "pytest.toml", ".pytest.toml"}
STARTUP_MODULES = {"conftest.py", "sitecustomize.py", "usercustomize.py"}


# What the tests' fixture loaders (tests/helpers.py at the pinned commit) do with a .sql
# fixture: every reader goes through _filter_comments, which drops empty lines and lines that
# start with "--" before anything else looks at the text.
LINE_BREAKS = ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")

# The two TPC query files are loaded once for the whole TestExecutor class (setUpClass), so
# all its tests open them, but only these use what was loaded, and each a different part:
# "all" the metadata, query and expected text of every pair; "input" metadata and query;
# "pair" query and expected text.
FIXTURE_PAIR_USERS = {
    "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": {
        "tests/test_optimizer.py::TestOptimizer::test_tpcds": "all",
        "tests/test_executor.py::TestExecutor::test_execute_tpcds": "input",
    },
    "tests/fixtures/optimizer/tpc-h/tpc-h.sql": {
        "tests/test_optimizer.py::TestOptimizer::test_tpch": "all",
        "tests/test_executor.py::TestExecutor::test_execute_tpch": "input",
        "tests/test_executor.py::TestExecutor::test_optimized_tpch": "pair",
    },
}


def fixture_pairs(text):
    """tests.helpers.load_sql_fixture_pairs, on a text instead of a file. Raises what it raises."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # as a file opened in text mode reads
    kept = "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))
    statements = kept.split(";")
    pairs = []
    for index in range(0, len(statements) - 1, 2):
        lines = statements[index].strip().split("\n")
        meta = {}
        cursor = 0
        while lines[cursor].startswith("#"):
            key, value = lines[cursor].split(":", maxsplit=1)
            meta[key.lstrip("#").strip()] = value.strip()
            cursor += 1
        pairs.append((meta, "\n".join(lines[cursor:]), statements[index + 1].strip()))
    return pairs


def data_file(maps, path, change):
    """(tests, reason) for a change to a file tests read as data."""
    readers = maps.readers(path)
    if not readers or change.binary or not (path.startswith("tests/fixtures/") and path.endswith(".sql")):
        return readers, "data file"
    changed = [content for _o, _n, body in change.hunks for tag, content in body if tag != " "]
    if changed and all(
        line.rstrip("\r") == "" or (line.startswith("--") and not any(mark in line.rstrip("\r") for mark in LINE_BREAKS))
        for line in changed
    ) and not any("\r" in line[:-1] for line in changed):
        return 0, "fixture: only comment and empty lines differ"
    users = FIXTURE_PAIR_USERS.get(path)
    pinned = os.path.join(maps.directory, "tpc", path[len("tests/fixtures/optimizer/"):]) if users else None
    if not users or not os.path.exists(pinned) or any(test not in maps.position for test in users):
        return readers, "data file"
    try:
        with open(pinned, encoding="utf-8", newline="") as handle:
            old_text = handle.read()
        applied = apply_hunks(old_text.split("\n"), change.hunks)
        if applied is None:
            return readers, "data file (patch does not apply)"
        new_text = "\n".join(applied[0])
        new_text.encode("utf-8")           # undecodable bytes would stop the loader for everyone
        old_pairs = fixture_pairs(old_text)
        new_pairs = fixture_pairs(new_text)
    except Exception:
        return readers, "data file (does not load)"
    views = {
        "all": lambda pairs: pairs,
        "input": lambda pairs: [(meta, sql) for meta, sql, _expected in pairs],
        "pair": lambda pairs: [(sql, expected) for _meta, sql, expected in pairs],
    }
    part = 0
    for test, view in users.items():
        if views[view](old_pairs) != views[view](new_pairs):
            part |= 1 << maps.position[test]
    return part & readers, "fixture pairs (%d of %d differ)" % (
        sum(1 for a, b in zip(old_pairs, new_pairs) if a != b) + abs(len(old_pairs) - len(new_pairs)), len(old_pairs))


def added_text(change):
    return "\n".join(content for _o, _n, body in change.hunks for tag, content in body if tag == "+")


def select(maps, text, tree, cost, replay=None):
    """Returns (bitset of affected tests, True when anything might have changed)."""
    mask = 0
    active = False
    strong = 0        # tests selected for more than executing changed lines of the package at call time
    ranges = {}       # package path -> those line ranges
    bodies = {}       # package path -> patched text, for files changed only inside functions
    at_import = {}    # path -> (importers, new text) for package files changed at import time
    moved = {}        # package files the patch adds (text) or removes (None)
    neutral = []      # (path, old first, old last, new first, new last) of functions edited inside
    for change in parse_patch(text):
        old, new = change.old, change.new
        if change.added and not change.renamed:
            old = None
        if change.deleted:
            new = None
        if old is None and new is None:
            continue
        if old is not None and new is not None and old == new and not change.hunks and not change.binary:
            log("  %s: mode change only" % old)
            continue
        part = 0
        why = ""
        explained = False
        if old is not None and (new is None or new != old):
            # Removed or moved away.
            part |= maps.whole(old) | maps.listers(os.path.dirname(old))
            why = "removed"
            if old.startswith(PACKAGE) and old.endswith(".py"):
                moved[old] = None
        if new is not None and (old is None or new != old):
            # A path that did not exist at the pinned commit.
            part |= new_path(maps, new, change)
            why = (why + " added").strip()
            if new.startswith(PACKAGE) and new.endswith(".py") and old is None and not change.binary:
                moved[new] = added_text(change) + "\n"
        if old is not None and new == old:
            if old not in maps.paths:
                part |= new_path(maps, new, change)
                why = "unknown path"
            elif old.endswith(".py") and change.binary:
                part = maps.whole(old)
                why = "binary change to a Python file"
            elif old.endswith(".py"):
                outcome = python_file(maps, old, change)
                part, why = outcome.mask, outcome.why
                if outcome.importers is not None:
                    if old.startswith(PACKAGE) and not old.endswith("/__main__.py"):
                        at_import[old] = (outcome.importers, outcome.new_text)
                        for span in outcome.neutral:
                            neutral.append((old,) + span)
                    else:
                        part |= outcome.importers
                elif outcome.ranges is not None and old.startswith(PACKAGE):
                    strong |= outcome.strong
                    ranges[old] = outcome.ranges
                    bodies[old] = outcome.new_text
                    explained = True
            else:
                part, why = data_file(maps, old, change)
        base = os.path.basename(new or old)
        if base in PYTEST_FILES and (new or old).count("/") <= 1:
            if base in ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml") or "pytest" in added_text(change):
                part = maps.everything
                why = "pytest configuration"
        if part or why not in ("same syntax tree", "no change"):
            active = True
        if not explained or why == "pytest configuration":
            strong |= part
        log("  %s: %s -> %d tests" % (new or old, why, bin(part).count("1")))
        mask |= part
    if at_import:
        importers = 0
        for part, _text in at_import.values():
            importers |= part
        if importers & ~mask:
            narrowed = None
            if cost(mask | importers) - cost(mask) >= DYNAMIC_GAIN_MS * (1.75 if neutral else 1):
                files = dict(moved)
                files.update({path: new_text for path, (_m, new_text) in at_import.items()})
                changed = tree.compare_edited(files, neutral) if neutral else tree.compare(files)
                if changed is not None:
                    log("    import-time state: %d names differ" % len(changed))
                    notes = {}
                    narrowed = reached(maps, changed, notes) if len(changed) <= 2000 else None
            if narrowed is None:
                mask |= importers
            else:
                broken = any(entry[2] == "!import" for entry in changed)
                mask |= narrowed if broken else narrowed & importers
                if not broken and not moved and replay is not None and notes["words"]:
                    # Tests chosen only because they executed a line naming a changed attribute:
                    # for the replayed ones, those lines stand in for the change.
                    strong |= notes["strong"]
                    if replay.possible(mask, strong | maps.unmapped):
                        for path, spans in maps.mention_ranges(notes["words"]).items():
                            ranges[path] = sorted(set(ranges.get(path, [])) | set(spans))
                        for path, (_m, new_text) in at_import.items():
                            bodies[path] = new_text
                        mask &= ~replay.drop(mask, strong | maps.unmapped, ranges, bodies, cost, thorough=True)
    if replay is not None and mask and ranges and not at_import and not moved:
        mask &= ~replay.drop(mask, strong | maps.unmapped, ranges, bodies, cost)
    if active:
        mask |= maps.unmapped  # tests the maps know nothing about (none when all passed while tracing)
    return mask, active


def new_path(maps, path, change):
    """Tests that can notice a file appearing where there was none."""
    directory = os.path.dirname(path)
    base = os.path.basename(path)
    mask = maps.listers(directory)
    if path.endswith(".py"):
        if base in STARTUP_MODULES:
            return maps.everything
        parts = path.split("/")
        if len(parts) == 1 or parts[0] not in maps.top_level():
            return maps.everything  # a new importable top-level name can shadow anything
        if base == "__init__.py":
            shadowed = directory + ".py"
            if shadowed in maps.paths:
                mask |= maps.whole(shadowed)  # a package now hides the module of the same name
            if not any(other.startswith(directory + "/") for other in maps.paths):
                parent = os.path.dirname(directory)
                mask |= maps.listers(parent)
    elif path.endswith((".pth", ".so", ".pyd")):
        return maps.everything  # importable without being Python source
    return mask


# --------------------------------------------------------------------------- workers


def pack(durations, count):
    """Longest first onto the lightest worker. Returns (largest load, loads, assignment)."""
    loads = [0] * count
    groups = [[] for _ in range(count)]
    for duration, number in durations:
        lightest = loads.index(min(loads))
        loads[lightest] += duration
        groups[lightest].append(number)
    return max(loads), groups


def fits(durations, count, capacity):
    """First-fit decreasing: can everything go into `count` workers of `capacity`?"""
    loads = [0] * count
    groups = [[] for _ in range(count)]
    for duration, number in durations:
        for slot in range(count):
            if loads[slot] + duration <= capacity:
                loads[slot] += duration
                groups[slot].append(number)
                break
        else:
            return None
    return max(loads), groups


def split(tests):
    """tests: [(milliseconds, number)]. Cheapest split over 1..8 workers."""
    if not tests:
        return 0, []
    durations = sorted(tests, reverse=True)
    total = sum(duration for duration, _ in durations)
    longest = durations[0][0]
    best = None
    for count in range(1, min(MAX_WORKERS, len(durations)) + 1):
        floor = max(longest, -(-total // count))
        if best is not None and floor + count * WORKER_MS >= best[0]:
            continue
        if count == 1:
            load, groups = total, [[number for _, number in durations]]
        else:
            load, groups = pack(durations, count)
            low, high = floor, load
            while low < high:
                middle = (low + high) // 2
                attempt = fits(durations, count, middle)
                if attempt is None:
                    low = middle + 1
                else:
                    load, groups = attempt
                    high = min(middle, load)
        cost = load + count * WORKER_MS
        if best is None or cost < best[0]:
            best = (cost, [group for group in groups if group])
    return best


# --------------------------------------------------------------------------- main


def prepare(root):
    """Image build step: lay out the pinned package with bytecode, so run-time imports are cheap."""
    import compileall

    maps = Maps(MAPS)
    lay_out(maps, root)
    compileall.compile_dir(root, quiet=1, workers=1)
    here = os.path.dirname(os.path.abspath(__file__))
    # The pinned import-time fingerprint with this very interpreter (bytecode differs between
    # Python releases), so that no run has to redo it.
    done = subprocess.run([sys.executable, "-I", "-S", "-B", os.path.join(here, "fingerprint.py"), root],
                          check=True, stdout=subprocess.PIPE)
    with open(os.path.join(root, "fingerprint.bin"), "wb") as handle:
        handle.write(rebased(done.stdout.decode(), os.path.join(MAPS, "fingerprint.bin")))
    script = os.path.join(here, "replay.py")
    if os.path.isdir(os.path.join(root, "tests", "fixtures")) and os.path.exists(script):
        # What the replayed tests compute on the pinned tree, with this very interpreter.
        subprocess.run([sys.executable, "-S", "-P", "-B", script, "sigs", root], check=True,
                       env=dict(os.environ, PYTHONHASHSEED="0"), stdout=subprocess.DEVNULL)
    for directory, _subdirs, files in os.walk(root):
        os.chmod(directory, 0o755)
        for name in files:
            os.chmod(os.path.join(directory, name), 0o644)


def main():
    warnings.simplefilter("ignore")  # patched code may compile with SyntaxWarnings: not ours to report
    if len(sys.argv) == 3 and sys.argv[1] == "--prepare":
        prepare(sys.argv[2])
        return
    with open(os.path.join(DATA, "tests.json")) as handle:
        tests = json.load(handle)["tests"]
    maps = Maps(MAPS)
    position_of = {test_id: position for position, test_id in enumerate(maps.ids)}
    millis = {}
    number_at = {}
    strangers = []  # tests the maps have never seen: treated as affected by any real change
    for test in tests:
        millis[test["number"]] = int(round(test["seconds"] * 1000))
        position = position_of.get(test["id"])
        if position is None:
            strangers.append(test["number"])
        else:
            number_at[position] = test["number"]

    def numbers_of(mask):
        numbers = []
        while mask:
            low = mask & -mask
            number = number_at.get(low.bit_length() - 1)
            if number is not None:
                numbers.append(number)
            mask ^= low
        return numbers

    def cost(mask):
        return split([(millis[number], number) for number in numbers_of(mask)])[0]

    tree = Tree(maps)
    replay = Replay(maps, tree)
    out = []
    commits = os.path.join(DATA, "commits")
    started = time.monotonic()
    try:
        # The run may take 3 minutes for about 40 commits and 4.5 for about 60: the time
        # allowed for analysing patches grows with the number of commits.
        global DYNAMIC_BUDGET, BATCH_BUDGET
        count = sum(1 for name in os.listdir(commits) if name.endswith(".patch"))
        DYNAMIC_BUDGET = min(100.0, max(DYNAMIC_BUDGET, 1.5 * count))
        BATCH_BUDGET = min(180.0, max(BATCH_BUDGET, 2.75 * count))
        for name in sorted(os.listdir(commits)):
            if not name.endswith(".patch"):
                continue
            commit = name[:-6]
            log(commit)
            try:
                if time.monotonic() - started > BATCH_BUDGET:
                    raise TimeoutError("batch budget spent")
                with open(os.path.join(commits, name), "rb") as handle:
                    text = handle.read().decode("utf-8", "surrogateescape")
                mask, active = select(maps, text, tree, cost, replay)
                numbers = numbers_of(mask)
                if active:
                    numbers.extend(strangers)
            except Exception as error:  # never lose the whole batch to one odd patch
                log("  failed: %r" % (error,))
                if DEBUG and not isinstance(error, TimeoutError):
                    raise
                numbers = list(millis)
            total, groups = split([(millis[number], number) for number in numbers])
            log("  -> %d tests, %d workers, %.3fs" % (len(numbers), len(groups), total / 1000))
            out.append(json.dumps({"commit": commit, "workers": groups}, separators=(",", ":")))
    finally:
        tree.close()
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()

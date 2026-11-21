"""Fingerprint of everything sqlglot defines at import time.

    python -I -S fingerprint.py <tree>                       print the full fingerprint
    python -I -S fingerprint.py <tree> --against <baseline>  print only what differs from a baseline
    python -I -S fingerprint.py <tree> --emit <baseline>     print every name with its stable digest
    ... --neutral path:first-last[,...]                      count code defined on those lines as unchanged

<tree> is a directory containing the sqlglot package. Every sqlglot module in
it is imported and each module-level name and each class attribute is reduced
to a digest of its structure: functions by bytecode, constants, defaults and
closure contents (never line numbers), containers by their items, classes by
name. Two trees give the same digest for a name exactly when the objects are
structurally the same.

Comparing a patched tree with the pinned one therefore shows which definitions
a change to import-time code really reaches: the edited attribute, tables
derived from it, subclasses that copy it, values computed by metaclasses, and
modules that stop importing. A change that reaches nothing leaves no trace.

Some tables are filled from sets of classes, whose iteration order depends on
memory addresses and differs from run to run. For those a second, order-free
digest exists; the map builder decides per name which digest is stable.

Full output, one line per name:
    module, class qualname ("" at module level), name, kind, digest, order-free digest[, entries]
kind is f (function-like), d (dict), c (class), e (enum member) or o (other).
entries lists "class=digest/order-free digest" for dictionaries keyed only by
classes, so a difference can be pinned to the entries it touches. For a class
attribute that is itself a class, it is "@" followed by that class.

Output of --against, one line per differing name:
    module, class qualname, name, kind, differing entry classes (or "-")
"""

import enum
import functools
import hashlib
import os
import re
import sys
import types
import unittest  # noqa: F401  sqlglot checks more when a test runner is loaded (expressions.core.UNITTEST), as it is for every test
import zlib

SKIP_CLASS_ATTRS = {
    "__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__",
    "__static_attributes__", "__doc__", "_abc_impl", "__parameters__", "__orig_bases__",
    "__abstractmethods__", "__match_args__", "__dataclass_fields__", "__dataclass_params__",
}
MAX_DEPTH = 40
BUILTIN_KINDS = (
    types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
    types.MethodWrapperType, types.ClassMethodDescriptorType, types.GetSetDescriptorType,
    types.MemberDescriptorType,
)
FUNCTION_LIKE = (
    types.FunctionType, types.MethodType, types.BuiltinFunctionType, staticmethod, classmethod,
    property, functools.partial, functools.cached_property,
)


def digest(text):
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


class Walker:
    """canon(value) is a short string that is equal for structurally equal values.

    With loose=True the order of items in every container is ignored.
    """


    def __init__(self, loose=False):
        self.loose = loose
        self.memo = {}
        self.active = set()
        self.keep = []  # keeps ids stable while the walk runs
        self.refs = {}

    def class_ref(self, cls):
        found = self.refs.get(cls, 0)
        if found != 0:
            return found
        module = getattr(cls, "__module__", "?")
        qualname = getattr(cls, "__qualname__", getattr(cls, "__name__", "?"))
        target = sys.modules.get(module)
        try:
            for part in qualname.split("."):
                target = getattr(target, part) if not isinstance(target, type) else target.__dict__[part]
        except Exception:
            target = None
        found = "class:%s.%s" % (module, qualname) if target is cls else None
        try:
            self.refs[cls] = found
        except TypeError:
            pass
        return found

    def canon(self, value, depth=0):
        kind = type(value)
        if value is None or kind is bool or kind is int or kind is float or kind is complex:
            return "%s:%r" % (kind.__name__, value)
        if kind is str:
            return "s:" + value if len(value) < 64 else "S:" + digest(value)
        if kind is bytes:
            return "b:" + digest(repr(value))
        ident = id(value)
        found = self.memo.get(ident)
        if found is not None:
            return found
        if ident in self.active or depth > MAX_DEPTH:
            return "cycle:" + kind.__name__
        self.active.add(ident)
        try:
            text = self.describe(value, kind, depth + 1)
        except Exception as error:
            text = "error:%s:%s" % (kind.__name__, type(error).__name__)
        self.active.discard(ident)
        if len(text) > 80:
            text = "#" + digest(text)
        self.memo[ident] = text
        self.keep.append(value)
        return text

    def sequence(self, head, values, depth, ordered):
        items = [self.canon(value, depth) for value in values]
        if self.loose or not ordered:
            items.sort()
        return head + "|" + "|".join(items)

    def describe(self, value, kind, depth):
        canon = self.canon
        if isinstance(value, enum.Enum):
            return "enum:%s.%s.%s" % (kind.__module__, kind.__qualname__, value.name)
        if isinstance(value, type):
            ref = self.class_ref(value)
            if ref is not None:
                return ref
            head = "dynclass|%s|%s" % (value.__name__, "|".join(canon(base, depth) for base in value.__bases__))
            items = [
                name + "=" + canon(attr, depth)
                for name, attr in list(vars(value).items())
                if name not in SKIP_CLASS_ATTRS
            ]
            if self.loose:
                items.sort()
            return head + "|" + "|".join(items)
        if kind is types.FunctionType:
            parts = ["fn", self.code(value.__code__, depth), canon(value.__defaults__, depth), canon(value.__kwdefaults__, depth)]
            for cell in value.__closure__ or ():
                try:
                    parts.append(canon(cell.cell_contents, depth))
                except ValueError:
                    parts.append("empty")
            return "|".join(parts)
        if kind is types.CodeType:
            return self.code(value, depth)
        if kind is types.MethodType:
            return "method|%s|%s" % (canon(value.__self__, depth), canon(value.__func__, depth))
        if kind in BUILTIN_KINDS:
            owner = getattr(value, "__objclass__", None) or getattr(value, "__self__", None)
            owner = getattr(owner, "__qualname__", None) or getattr(owner, "__name__", None) or type(owner).__name__
            return "builtin:%s.%s" % (owner, getattr(value, "__name__", "?"))
        if kind is types.ModuleType:
            return "module:" + value.__name__
        if kind in (staticmethod, classmethod):
            return "%s|%s" % (kind.__name__, canon(value.__func__, depth))
        if kind is property:
            return "property|%s|%s|%s" % (canon(value.fget, depth), canon(value.fset, depth), canon(value.fdel, depth))
        if kind is functools.partial:
            return "partial|%s|%s|%s" % (canon(value.func, depth), canon(value.args, depth), canon(value.keywords, depth))
        if kind is functools.cached_property:
            return "cached_property|" + canon(value.func, depth)
        if kind is re.Pattern:
            return "re|%s|%d" % (canon(value.pattern, depth), value.flags)
        if isinstance(value, dict):
            head = "dict:" + kind.__name__
            factory = getattr(value, "default_factory", None)
            if factory is not None:
                head += "|factory=" + canon(factory, depth)
            items = [canon(key, depth) + "=>" + canon(item, depth) for key, item in list(value.items())]
            if self.loose:
                items.sort()
            return head + "|" + "|".join(items)
        if isinstance(value, (list, tuple)):
            return self.sequence("seq:" + kind.__name__, value, depth, True)
        if isinstance(value, (set, frozenset)):
            return self.sequence("set:" + kind.__name__, value, depth, False)
        wrapped = getattr(value, "__wrapped__", None)
        if wrapped is not None and callable(value):
            return "wrapper:%s|%s" % (kind.__name__, canon(wrapped, depth))
        head = "obj|" + canon(kind, depth)
        items = []
        state = getattr(value, "__dict__", None)
        if isinstance(state, dict):
            for name, attr in list(state.items()):
                items.append(name + "=" + canon(attr, depth))
        seen = set()
        for base in kind.__mro__:
            slots = base.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for name in slots:
                if name in seen or name in ("__dict__", "__weakref__"):
                    continue
                seen.add(name)
                try:
                    items.append(name + "=" + canon(getattr(value, name), depth))
                except AttributeError:
                    pass
        if not items:
            text = repr(value)
            items.append(text if " at 0x" not in text else "opaque")
        elif self.loose:
            items.sort()
        return head + "|" + "|".join(items)

    neutral = ()  # (path suffix, first line, last line): code defined there counts as unchanged

    def code(self, code, depth):
        ident = id(code)
        found = self.memo.get(ident)
        if found is not None:
            return found
        for suffix, first, last in self.neutral:
            if first <= code.co_firstlineno <= last and code.co_filename.replace(os.sep, "/").endswith(suffix):
                # A function whose body the patch edits (or one defined inside it): what it does
                # when called is judged from line coverage; here only what it left behind at
                # import is compared.
                text = "code:edited:%s:%s" % (suffix, code.co_qualname)
                self.memo[ident] = text
                self.keep.append(code)
                return text
        consts = []
        for const in code.co_consts:
            consts.append(self.code(const, depth) if isinstance(const, types.CodeType) else self.canon(const, depth))
        text = "code:" + digest(
            "|".join(
                [
                    code.co_name,
                    code.co_code.hex(),
                    ",".join(consts),
                    ",".join(code.co_names),
                    ",".join(code.co_varnames),
                    ",".join(code.co_freevars),
                    ",".join(code.co_cellvars),
                    "%d,%d,%d,%d" % (code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount, code.co_flags),
                    code.co_exceptiontable.hex(),
                ]
            )
        )
        self.memo[ident] = text
        self.keep.append(code)
        return text


def kind_of(value):
    if isinstance(value, enum.Enum):
        return "e"
    if isinstance(value, type):
        return "c"
    if isinstance(value, FUNCTION_LIKE) or (callable(value) and hasattr(value, "__wrapped__")):
        return "f"
    if isinstance(value, dict):
        return "d"
    return "o"


def value_digest(walker, value, owner):
    text = walker.canon(value)
    if isinstance(value, enum.Enum) and owner is not None and type(value) is owner:
        text += "=" + walker.canon(value.value)
    return digest(text)


def class_keyed(walker, value):
    """[(class reference, item)] for a dictionary keyed only by nameable classes, else None."""
    if not isinstance(value, dict) or not value:
        return None
    items = []
    for key, item in list(value.items()):
        if not isinstance(key, type):
            return None
        ref = walker.class_ref(key)
        if ref is None:
            return None
        items.append((ref[6:], item))
    return items


def module_names(root):
    names = []
    base = os.path.join(root, "sqlglot")
    for directory, subdirs, files in os.walk(base):
        subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".py") or name == "__main__.py":
                continue
            path = os.path.relpath(os.path.join(directory, name), root)[:-3]
            parts = path.split(os.sep)
            if parts[-1] == "__init__":
                parts.pop()
            names.append(".".join(parts))
    return names


def names_of(root):
    """Imports every module and yields (module, class qualname, name, value, owner class)."""
    failed = {}
    names = module_names(root)
    for name in names:
        try:
            __import__(name)
        except BaseException as error:  # noqa: a patched module may fail in any way
            failed[name] = type(error).__name__
    probe = Walker()
    for name in names:
        module = sys.modules.get(name)
        if name in failed or module is None:
            yield name, "", "!import", failed.get(name, "missing"), None
            continue
        pending = []
        for attr, value in list(vars(module).items()):
            if attr.startswith("__") and attr.endswith("__") and attr != "__all__":
                continue
            yield name, "", attr, value, None
            if isinstance(value, type) and getattr(value, "__module__", None) == name:
                if probe.class_ref(value) is not None:
                    pending.append(value)
        done = set()
        while pending:
            cls = pending.pop()
            if id(cls) in done:
                continue
            done.add(id(cls))
            qualname = cls.__qualname__
            yield name, qualname, "__bases__", (cls.__mro__[1:], type(cls).__name__, cls), None
            for attr, value in list(vars(cls).items()):
                if attr in SKIP_CLASS_ATTRS:
                    continue
                yield name, qualname, attr, value, cls
                if (
                    isinstance(value, type)
                    and getattr(value, "__module__", None) == name
                    and getattr(value, "__qualname__", "").startswith(qualname + ".")
                    and probe.class_ref(value) is not None
                ):
                    pending.append(value)


def full(root, write):
    import typing

    exact, loose = Walker(False), Walker(True)
    out = ["#python\t%s" % sys.version.split()[0]]
    for module, qualname, name, value, owner in names_of(root):
        if name == "__bases__":
            cls = value[2]
            value = value[:2]
            if typing.is_typeddict(cls):
                # Calling a TypedDict class gives a plain dict: it never has instances.
                out.append("%s\t%s\t!typeddict\tx\t1\t1" % (module, qualname))
        if name == "!import":
            out.append("%s\t\t!import\tx\t%s\t%s" % (module, value, value))
            continue
        kind = "c" if name == "__bases__" else kind_of(value)
        line = "%s\t%s\t%s\t%s\t%s\t%s" % (
            module, qualname, name, kind, value_digest(exact, value, owner), value_digest(loose, value, owner)
        )
        items = class_keyed(exact, value)
        if items:
            line += "\t" + ",".join(
                sorted("%s=%s/%s" % (ref, digest(exact.canon(item)), digest(loose.canon(item))) for ref, item in items)
            )
        elif owner is not None and isinstance(value, type) and exact.class_ref(value):
            line += "\t@" + exact.class_ref(value)[6:]  # owner holds this class as an attribute
        out.append(line)
    write("\n".join(out) + "\n")


def load_baseline(path):
    """{(module, class, name): (kind, mode, digest, entries)} and the Python version it was made with."""
    with open(path, "rb") as handle:
        text = zlib.decompress(handle.read()).decode()
    table = {}
    version = None
    for line in text.split("\n"):
        parts = line.split("\t")
        if parts[0] == "#python":
            version = parts[1]
        elif len(parts) >= 6:
            table[(parts[0], parts[1], parts[2])] = (parts[3], parts[4], parts[5], parts[6] if len(parts) > 6 else "")
    return version, table


def against(root, baseline_path, write):
    version, baseline = load_baseline(baseline_path)
    if version != sys.version.split()[0]:
        write("#mismatch\t%s\n" % version)
        return 3
    exact, loose = Walker(False), Walker(True)
    out = ["#python\t%s" % version]
    seen = set()
    for module, qualname, name, value, owner in names_of(root):
        key = (module, qualname, name)
        seen.add(key)
        if name == "!import":
            out.append("%s\t\t!import\tx\t-" % module)
            continue
        if name == "__bases__":
            value = value[:2]
        kind = "c" if name == "__bases__" else kind_of(value)
        old = baseline.get(key)
        if old is None:
            out.append("%s\t%s\t%s\t%s\t-" % (module, qualname, name, kind))
            continue
        old_kind, mode, old_digest, old_entries = old
        if mode == "v":
            continue
        walker = loose if mode == "b" else exact
        if value_digest(walker, value, owner) == old_digest:
            continue
        differing = "-"
        if old_entries and old_entries[0] != "@":
            items = class_keyed(exact, value)
            if items:
                before = dict(entry.split("=") for entry in old_entries.split(","))
                after = {ref: digest(walker.canon(item)) for ref, item in items}
                differing = ",".join(sorted(ref for ref in set(before) | set(after) if before.get(ref) != after.get(ref))) or "-"
        out.append("%s\t%s\t%s\t%s\t%s" % (module, qualname, name, kind if kind == old_kind else "o", differing))
    for key, old in baseline.items():
        if key not in seen and key[2] != "!typeddict":
            out.append("%s\t%s\t%s\t%s\t-" % (key[0], key[1], key[2], old[0]))
    write("\n".join(out) + "\n")
    return 0


def emit(root, baseline_path, write):
    """One line per name with the digest the baseline says is stable for it:
    module, class qualname, name, kind, digest, entry digests (or "-"). The caller compares
    the output for two trees (used with --neutral, where no stored baseline applies)."""
    _version, baseline = load_baseline(baseline_path)
    exact, loose = Walker(False), Walker(True)
    out = []
    for module, qualname, name, value, owner in names_of(root):
        if name == "!import":
            out.append("%s\t\t!import\tx\t%s\t-" % (module, value))
            continue
        if name == "__bases__":
            value = value[:2]
        kind = "c" if name == "__bases__" else kind_of(value)
        old = baseline.get((module, qualname, name))
        mode = old[1] if old else "o"
        if mode == "v":
            continue
        walker = loose if mode == "b" else exact
        entries = "-"
        if old is None or (old[3] and old[3][0] != "@"):
            items = class_keyed(exact, value)
            if items:
                entries = ",".join(sorted("%s=%s" % (ref, digest(walker.canon(item))) for ref, item in items))
        out.append("%s\t%s\t%s\t%s\t%s\t%s" % (module, qualname, name, kind, value_digest(walker, value, owner), entries))
    write("\n".join(out) + "\n")
    return 0


def perturb(seed):
    """Shifts later memory addresses around, to expose values that depend on them."""
    import random

    rng = random.Random(seed)
    junk = []
    for index in range(rng.randrange(50, 400)):
        junk.append(type("Junk%d" % index, (), {}))
        junk.append(bytearray(rng.randrange(16, 5000)))
        junk.append(object())
    del junk[:: rng.randrange(2, 5)]
    return junk


def main():
    root = os.path.abspath(sys.argv[1])
    sys.path.insert(0, root)
    noise = os.environ.get("BR_FP_NOISE")
    junk = perturb(int(noise)) if noise else None  # noqa: F841 (kept alive on purpose)
    write = sys.stdout.write
    flush = sys.stdout.flush
    sys.stdout = sys.stderr = open(os.devnull, "w")  # modules may print while importing
    arguments = sys.argv[2:]
    if "--neutral" in arguments:
        position = arguments.index("--neutral")
        spans = []
        for item in arguments[position + 1].split(","):
            suffix, lines = item.rsplit(":", 1)
            first, last = lines.split("-")
            spans.append((suffix, int(first), int(last)))
        Walker.neutral = tuple(spans)
        del arguments[position:position + 2]
    if len(arguments) > 1 and arguments[0] == "--against":
        code = against(root, arguments[1], write)
    elif len(arguments) > 1 and arguments[0] == "--emit":
        code = emit(root, arguments[1], write)
    else:
        code = 0
        full(root, write)
    flush()
    return code


if __name__ == "__main__":
    sys.exit(main())

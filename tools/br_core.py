"""Recording hooks shared by the pytest plugin and by helper processes that tests start.

What is recorded, per context:
  run   (file, line) executed inside a function while no sqlglot module was being imported (call time)
  imp   (file, line) executed inside a function while a sqlglot module was being imported
        (metaclasses, decorators, helpers called from class bodies, ...)
  def   (file, line) executed in a module or class body (import time)
  used  classes of the repository that were instantiated (expanded to their base classes)
  data  repository files opened as data, dirs: repository directories listed

Classes are seen through builtins.__build_class__, so the hooks must be installed
before the code under test is imported.
"""

import builtins
import functools
import os
import sys
import types

mon = sys.monitoring
TOOL = 3
DISABLE = mon.DISABLE

ROOT = None
PACKAGE = "sqlglot/"  # imports of these modules count as import time
RUN = set()
IMP = set()
DEF = set()
DATA = set()
DIRS = set()
USED = set()        # class objects instantiated in this context
CLASSES = {}        # class object -> (file, first line of its body's code, qualname)
_files = {}
_depth = [0]        # imports of repository modules in progress
_sink = [None]      # file handle when this process reports to a file instead of memory
_written = set()


def rel(filename):
    found = _files.get(filename, 0)
    if found == 0:
        found = None
        if filename and filename[0] != "<":
            real = os.path.realpath(filename)
            if real.startswith(ROOT):
                found = real[len(ROOT):]
        _files[filename] = found
    return found


def _emit(kind, name, line):
    key = (kind, name, line)
    if key not in _written:
        _written.add(key)
        _sink[0].write("%s\t%s\t%d\n" % key)


def _on_line(code, line):
    name = rel(code.co_filename)
    if name is None:
        return DISABLE
    if not code.co_flags & 1:
        if _sink[0] is None:
            DEF.add((name, line))
        else:
            _emit("D", name, line)
        return DISABLE
    if _depth[0]:
        # Keep the location armed: the same line may run again at call time.
        if _sink[0] is None:
            IMP.add((name, line))
        else:
            _emit("I", name, line)
        return None
    if _sink[0] is None:
        RUN.add((name, line))
    else:
        _emit("R", name, line)
    return DISABLE


class _Loader:
    def __init__(self, inner):
        self._br_inner = inner

    def create_module(self, spec):
        return self._br_inner.create_module(spec)

    def exec_module(self, module):
        _depth[0] += 1
        try:
            return self._br_inner.exec_module(module)
        finally:
            _depth[0] -= 1

    def __getattr__(self, name):
        return getattr(self._br_inner, name)


class _Finder:
    """Wraps the loaders of repository modules so that import time can be told from call time."""

    def find_spec(self, name, path=None, target=None):
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(name, path, target)
            if spec is None:
                continue
            origin = getattr(spec, "origin", None)
            loader = spec.loader
            if (
                isinstance(origin, str)
                and loader is not None
                and hasattr(loader, "exec_module")
                and not isinstance(loader, _Loader)
                and (rel(origin) or "").startswith(PACKAGE)
            ):
                spec.loader = _Loader(loader)
            return spec
        return None


def _note_used(cls):
    if cls not in USED:
        USED.add(cls)
        if _sink[0] is not None:
            for base in cls.__mro__:
                where = CLASSES.get(base)
                if where is not None:
                    _emit("U", where[0], where[1])


def _wrap_init(init):
    @functools.wraps(init)
    def __init__(self, *args, **kwargs):
        _note_used(self.__class__)
        return init(self, *args, **kwargs)

    __init__._br_wrapped = True
    return __init__


_build_class = builtins.__build_class__


def _traced_build_class(func, name, *bases, **keywords):
    cls = _build_class(func, name, *bases, **keywords)
    try:
        where = rel(func.__code__.co_filename)
        if where is not None:
            CLASSES[cls] = (where, func.__code__.co_firstlineno, cls.__qualname__)
            init = cls.__dict__.get("__init__")
            if isinstance(init, types.FunctionType) and not getattr(init, "_br_wrapped", False):
                cls.__init__ = _wrap_init(init)
    except Exception:
        pass
    return cls


def _audit(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, bytes):
            try:
                path = path.decode()
            except Exception:
                return
        if not isinstance(path, str):
            try:
                path = os.fspath(path)
            except Exception:
                return
            if not isinstance(path, str):
                return
        if "__pycache__" in path:
            return
        real = os.path.realpath(path)
        if not real.startswith(ROOT):
            return
        name = real[len(ROOT):]
        if name.endswith(".py"):
            frame = sys._getframe(1)
            depth = 0
            while frame is not None and depth < 40:
                caller = frame.f_code.co_filename
                if caller.startswith("<frozen importlib") or caller.endswith(
                    ("linecache.py", "tokenize.py", "traceback.py")
                ):
                    return
                frame = frame.f_back
                depth += 1
        if _sink[0] is None:
            DATA.add(name)
        else:
            _emit("F", name, 0)
    elif event in ("os.listdir", "os.scandir", "glob.glob"):
        frame = sys._getframe(1)
        depth = 0
        while frame is not None and depth < 3:
            caller = frame.f_code.co_filename
            if caller.startswith("<frozen importlib") or "_pytest" in caller:
                return
            frame = frame.f_back
            depth += 1
        path = args[0]
        if path is None:
            path = "."
        if isinstance(path, bytes):
            path = path.decode(errors="replace")
        if not isinstance(path, str):
            return
        real = os.path.realpath(path)
        if (real + os.sep).startswith(ROOT):
            if _sink[0] is None:
                DIRS.add(real[len(ROOT):])
            else:
                _emit("L", real[len(ROOT):], 0)


_installed = [False]


def install(root):
    """Call once per process, before the code under test is imported."""
    global ROOT
    ROOT = root
    if _installed[0]:
        return
    _installed[0] = True
    builtins.__build_class__ = _traced_build_class
    sys.meta_path.insert(0, _Finder())
    sys.addaudithook(_audit)
    mon.use_tool_id(TOOL, "blast-radius")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.set_events(TOOL, mon.events.LINE)


def reset():
    """Start a fresh context: forget what was recorded and re-arm every line."""
    RUN.clear(); IMP.clear(); DEF.clear(); DATA.clear(); DIRS.clear(); USED.clear()
    _written.clear()
    mon.restart_events()


def to_file(directory):
    """Report to a file from now on (helper processes may leave without running exit handlers)."""
    path = os.path.join(directory, "%d.txt" % os.getpid())
    _sink[0] = open(path, "a", buffering=1)
    reset()


def used_classes():
    found = set()
    for cls in USED:
        for base in cls.__mro__:
            where = CLASSES.get(base)
            if where is not None:
                found.add(where[:2])
    return found


def class_table():
    """(file, line) -> (qualname, True when instantiation of the class is observable)."""
    table = {}
    for cls, where in list(CLASSES.items()):
        init = getattr(cls, "__init__", None)
        table[where[:2]] = (where[2], bool(getattr(init, "_br_wrapped", False)))
    return table


def snapshot():
    return {
        "run": sorted(RUN),
        "imp": sorted(IMP),
        "def": sorted(DEF),
        "data": sorted(DATA),
        "dirs": sorted(DIRS),
        "used": sorted(used_classes()),
    }

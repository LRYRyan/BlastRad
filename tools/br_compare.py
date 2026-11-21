"""Comparison profile of the pinned commit: what every comparison in sqlglot evaluated to.

Imported through sitecustomize.py when BR_CMP_DIR is set. sqlglot modules are then
compiled with every single-operator comparison `a OP b` rewritten to
`__br_cmp__(site, op, a, b)`, which evaluates `a OP b` exactly as before (same
operands, same order, same result or exception) and also notes what the operators
of the same family would have given on those operands:

    ==, is          (family E)
    !=, is not      (family N)
    <, <=, >, >=    (family O)

A site is a comparison's position in the pinned source: path:line:col:end line:end col.
For each site the profile keeps the distinct outcome strings seen, one letter per
family member in the order above: T (exactly True), F (exactly False), X (raised or
not a bool).

The selector uses this to see that, say, `x is None` -> `x == None` cannot change
anything: if at every execution of that comparison during the pinned test run both
operators gave the same bool, a test run of the patched code follows the same path.
Chained comparisons (a < b < c) and `in` / `not in` are left alone.

Output: one file per process in BR_CMP_DIR, lines "site<TAB>outcome", plus
"!<TAB>site" for sites where recording was cut short (those are never trusted).
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import operator
import os
import sys

ROOT = os.path.realpath(os.environ.get("BR_CMP_ROOT", os.getcwd())) + os.sep
PACKAGE = "sqlglot" + os.sep
OUT = os.environ["BR_CMP_DIR"]

OPS = {ast.Eq: 0, ast.Is: 1, ast.NotEq: 2, ast.IsNot: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}
FUNCS = (operator.eq, operator.is_, operator.ne, operator.is_not, operator.lt, operator.le, operator.gt, operator.ge)
FAMILY = {0: (0, 1), 1: (0, 1), 2: (2, 3), 3: (2, 3), 4: (4, 5, 6, 7), 5: (4, 5, 6, 7), 6: (4, 5, 6, 7), 7: (4, 5, 6, 7)}
PRIMITIVE = {int, str, float, bool, type(None), bytes}
RICH = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
CAP = 5000  # full evaluations per (site, operand types) before the site is given up

SITES = []          # site index -> key
_seen = set()       # cache keys already recorded
_written = set()    # (site, outcome) already written
_counts = {}
_given_up = set()
_plain = {}
_handle = [None]


def _out():
    handle = _handle[0]
    if handle is None or handle[0] != os.getpid():
        _seen.clear(); _written.clear(); _counts.clear(); _given_up.clear()
        os.makedirs(OUT, exist_ok=True)
        handle = (os.getpid(), open(os.path.join(OUT, "%d.txt" % os.getpid()), "a", buffering=1))
        _handle[0] = handle
    return handle[1]


def _is_plain(kind):
    found = _plain.get(kind)
    if found is None:
        found = all(getattr(kind, name, None) is getattr(object, name) for name in RICH)
        _plain[kind] = found
    return found


def _primitive(value, depth=0):
    kind = type(value)
    if kind in PRIMITIVE:
        return True
    if kind is tuple and depth < 3:
        return all(_primitive(item, depth + 1) for item in value)
    return False


def __br_cmp__(site, op, a, b):
    result = FUNCS[op](a, b)  # the real comparison: exceptions propagate unchanged
    if site in _given_up:
        return result
    out = _out()
    ta, tb = type(a), type(b)
    if _primitive(a) and _primitive(b):
        key = (site, ta, tb, a, b)
    elif isinstance(a, _Enum) and isinstance(b, _Enum) or (_is_plain(ta) and _is_plain(tb)):
        key = (site, ta, tb, a is b)
    elif (isinstance(a, _Enum) or _is_plain(ta)) and b is None or (isinstance(b, _Enum) or _is_plain(tb)) and a is None:
        key = (site, ta, tb, a is b)
    else:
        key = None
        count = _counts.get((site, ta, tb), 0) + 1
        _counts[(site, ta, tb)] = count
        if count > CAP:
            _given_up.add(site)
            out.write("!\t%s\n" % SITES[site])
            return result
    if key is not None:
        try:
            if key in _seen:
                return result
            _seen.add(key)
        except TypeError:
            pass
    letters = []
    for member in FAMILY[op]:
        if member == op:
            value = result
        else:
            try:
                value = FUNCS[member](a, b)
            except Exception:
                letters.append("X")
                continue
        letters.append("T" if value is True else "F" if value is False else "X")
    outcome = "".join(letters)
    if (site, outcome) not in _written:
        _written.add((site, outcome))
        out.write("%s\t%s\n" % (SITES[site], outcome))
    return result


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        key = "%s:%d:%d:%d:%d" % (self.rel, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
        SITES.append(key)
        call = ast.Call(
            func=ast.Name(id="__br_cmp__", ctx=ast.Load()),
            args=[ast.Constant(len(SITES) - 1), ast.Constant(OPS[type(node.ops[0])]), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.realpath(path)[len(ROOT):].replace(os.sep, "/")
        tree = ast.parse(data, filename=path)
        tree = ast.fix_missing_locations(_Rewriter(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def set_data(self, *args, **kwargs):  # never write rewritten bytecode
        pass

    def get_code(self, fullname):  # never read cached bytecode
        source = self.get_source(fullname)
        return self.source_to_code(source.encode("utf-8") if isinstance(source, str) else source, self.get_filename(fullname))


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.origin, str) or not spec.origin.endswith(".py"):
            return spec
        if not os.path.realpath(spec.origin).startswith(ROOT + PACKAGE):
            return spec
        spec.loader = _Loader(name, spec.origin)
        return spec


import enum as _enum_module  # noqa: E402

_Enum = _enum_module.Enum
builtins.__br_cmp__ = __br_cmp__
os.environ["BR_CMP_ROOT"] = ROOT[:-1]
sys.meta_path.insert(0, _Finder())

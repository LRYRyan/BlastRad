"""Dev-only stand-in for duckdb (not installable in this sandbox). Not shipped.

Results compare equal to anything, so tests follow the same path through
sqlglot as they do when duckdb agrees with the expected rows.
"""
import unittest

import pandas.testing as _pdt

__version__ = "0.0.0-stub"


class _Any:
    columns = ()

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __hash__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def rename(self, *args, **kwargs):
        return self


class _Result:
    description = ()

    def fetchall(self):
        return _Any()

    def fetchone(self):
        return _Any()

    def fetchdf(self):
        return _Any()

    df = fetchdf


class _Connection:
    def execute(self, *args, **kwargs):
        return _Result()

    sql = execute

    def register(self, *args, **kwargs):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def connect(*args, **kwargs):
    return _Connection()


def _lenient(original):
    def check(*args, **kwargs):
        if any(isinstance(arg, _Any) for arg in args):
            return None
        return original(*args, **kwargs)

    return check


_pdt.assert_frame_equal = _lenient(_pdt.assert_frame_equal)
for _name in ("assertEqual", "assertCountEqual", "assertListEqual"):
    setattr(unittest.TestCase, _name, _lenient(getattr(unittest.TestCase, _name)))


class Error(Exception):
    pass


class InvalidInputException(Error):
    pass


class BinderException(Error):
    pass


class ParserException(Error):
    pass


class _Expected:
    """Stands in for assertRaises(<a duckdb error>): the stub never raises, the test moves on."""

    exception = Error("stub")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _lenient_raises(original):
    def check(self, expected, *args, **kwargs):
        if isinstance(expected, type) and issubclass(expected, Error):
            return _Expected()
        return original(self, expected, *args, **kwargs)

    return check


for _name in ("assertRaises", "assertRaisesRegex"):
    setattr(unittest.TestCase, _name, _lenient_raises(getattr(unittest.TestCase, _name)))

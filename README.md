# Blast Radius entry

For every patch against sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, print the tests
that can be affected and the cheapest way to split them over at most 8 workers.

No test runs while deciding. The selector looks a patch up in maps that were recorded once from
the pinned commit, and for changes to import-time code it imports the patched package once to
see what the change really reaches.

## Layout

| | |
| --- | --- |
| `selector.py` | Reads `/data`, prints one JSON line per commit. Standard library only. |
| `fingerprint.py` | Digest of everything sqlglot defines at import time, for comparing a patched tree with the pinned one. |
| `maps/` | What every test depends on at the pinned commit. Built by `tools/`, committed as plain files. |
| `tools/` | Records the maps: a pytest plugin, a driver, the map builder, and the image they run in. Also the checks described below. |
| `Dockerfile` | The submission image. Copies the files above; downloads nothing and runs no test. |
| `sample/`, `score.py`, `SCORING.md`, `requirements.lock` | From the starter kit, unchanged. |

## How a patch is read

1. **Parse the diff** into files and hunks, and rebuild each changed Python file in memory from
   the pinned source (kept compressed in `maps/src.bin`).
2. **Compare syntax trees**, statement by statement, around the hunks. Statements whose tree is
   unchanged select nothing, so comments, blank lines and reformatting cost nothing. A file
   that no longer parses or compiles selects every test that ever touched it.
3. **Call-time changes** (inside functions and lambdas) select the tests that executed the
   changed statement's lines at the pinned commit. A statement whose only change is a
   comparison operator swapped within its family (`is`/`==`, `is not`/`!=`, `<`/`<=`/`>`/`>=`)
   selects nothing when, at every execution of that comparison in the pinned test run, both
   operators gave the same bool: the patched run then takes exactly the same path. An inserted statement selects the tests that
   reached the statement before it, or the enclosing block. A line no test executed selects
   nothing.
4. **Import-time changes** (class and module level) select the tests whose process imported
   the module. When that set is expensive, the selector writes the patched package to `/tmp`,
   imports it with `fingerprint.py` and compares its import-time state with the pinned one.
   The comparison names exactly the class attributes and module names that came out different,
   including tables derived from the changed one, subclasses that copy it, and values computed
   by metaclasses. The selection then shrinks to tests that instantiated those classes and
   executed, at call time, a line that mentions those names. If the patched package no longer
   imports, every test that used the broken module is selected.
5. **Other files** (SQL fixtures, CSV data, JSON) select the tests that opened them. New or
   removed files also select the tests that listed their directory. New pytest configuration, a
   new `conftest.py`, or a new importable top-level name selects everything.
6. **Split**: for 1 to 8 workers, longest test first onto the lightest worker, tightened by
   first-fit with a binary search on the capacity; the cheapest `largest load + 2 s x workers`
   wins.

Whenever the maps cannot explain a change, the selection widens (statement, then function, then
module, then everything) and never narrows.

## What the maps hold

Recorded by running every test of the pinned commit once, each test in its own forked process
so that lazily imported dialects and class-level setup are attributed to every test that needs
them (`tools/br_tracer.py`). Helper processes that tests start (the optimizer and executor
tests use process pools) are recorded too.

- For every source line: the tests that executed it at call time, the tests that executed it
  inside a function while sqlglot was being imported, and the tests that executed it in a
  class or module body.
- For every class: the tests that instantiated it or a subclass.
- For every data file and directory in the repository: the tests that opened or listed it.
- For every single-operator comparison: what it and the other operators of its family gave
  on the operands it actually saw (`tools/br_compare.py`, `maps/compare.json`). Recorded by
  running the suite with comparisons rewritten to a recording helper that returns exactly
  what the original comparison returns. A comparison seen with too many distinct
  non-primitive operands is marked as given up and never trusted.
- The pinned sources, and the pinned import-time fingerprint.

Everything in `maps/` is derived from the pinned commit alone. There is no table from a patch,
or from a kind of edit, to tests.

## Running it

```sh
make sample                  # build the image, run it under the scorer's limits, score the 30 sample commits
make local                   # the same without Docker
make explain                 # print, per commit, why it selected what it did (stderr)
python3 tools/evaluate.py    # per-commit table: cost, cheapest possible cost, any failing test left out
```

On the 30 sample commits the selection costs 113.0 simulated seconds with no miss. The cheapest
possible answer (exactly the failing tests) costs 94.4; running everything costs 1104.5.

## Checking that nothing is left out

`tools/mutation_check.py` mutates the pinned tree at random, asks the selector, then really
runs the tests it left out among those that ever touched the mutated file, and reports any
that fail. Mode `cmp` makes only same-family comparison swaps, to test the comparison
profile. It needs a scratch copy of the sqlglot checkout and the packages of
`requirements.lock`.

## Rebuilding the maps

```sh
make maps      # several minutes; then commit maps/
```

This traces the pinned commit inside the image the commits were labeled in, with the packages
from `requirements.lock`. Do this before submitting: the maps currently committed were recorded
in a sandbox that could not install duckdb or pytz, using the stand-ins in `tools/devstubs/`,
on a different 3.13 patch release (two passes, hash seeds 0 and 1, plus the comparison
profile). All 1620 tests passed there and the sample scores without a miss, but the
labeled environment is the one that counts.

If a test does not pass while the maps are recorded, it is marked unmapped and the selector
then includes it in every commit that changes anything.

## Limits worth knowing

- A test is assumed to read a class attribute through an instance of that class, of a class it
  is nested in, or of a class that holds it (a dialect and its parser), or on a line that names
  the attribute. Attribute names computed at run time are only handled for methods.
- Import-time side effects that leave no trace in module or class state are not seen.
- A patch that changes both import-time and call-time code in one file is compared as a whole,
  which can select more than needed, never less.

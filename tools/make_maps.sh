#!/bin/sh
# Runs inside tools/Dockerfile.maps: trace the pinned commit twice (two hash seeds, so code
# paths that depend on set ordering are both seen), profile its comparisons, record the replay units (replay.py), then write the maps to /out.
set -e
BR_HASHSEED=0 python tools/trace_all.py /src /work/tests.json /tmp/trace-0
BR_HASHSEED=1 python tools/trace_all.py /src /work/tests.json /tmp/trace-1
python tools/profile_compares.py /src /work/tests.json /tmp/compare
PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python tools/trace_units.py /src /tmp/units-0.pkl
PYTHONHASHSEED=1 PYTHONDONTWRITEBYTECODE=1 python tools/trace_units.py /src /tmp/units-1.pkl
(cd /src && BR_ALWAYS=1 BR_OUT=/tmp/always BR_FILE=tests/test_executor.py SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED=0 PYTHONPATH=/work/tools:/src python -m pytest -q -p br_tracer -p no:cacheprovider tests/test_executor.py)
BR_COMPARE_DIR=/tmp/compare BR_UNITS=/tmp/units-0.pkl:/tmp/units-1.pkl BR_ALWAYS_DIRS=/tmp/always \
  python tools/build_maps.py /src /work/tests.json /out /tmp/trace-0 /tmp/trace-1
chmod -R a+rX /out

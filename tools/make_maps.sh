#!/bin/sh
# Runs inside tools/Dockerfile.maps: trace the pinned commit twice (two hash seeds, so code
# paths that depend on set ordering are both seen), profile its comparisons, then write the
# maps to /out.
set -e
BR_HASHSEED=0 python tools/trace_all.py /src /work/tests.json /tmp/trace-0
BR_HASHSEED=1 python tools/trace_all.py /src /work/tests.json /tmp/trace-1
python tools/profile_compares.py /src /work/tests.json /tmp/compare
BR_COMPARE_DIR=/tmp/compare python tools/build_maps.py /src /work/tests.json /out /tmp/trace-0 /tmp/trace-1
chmod -R a+rX /out

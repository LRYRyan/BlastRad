"""Starts recording in processes that tests spawn (multiprocessing "spawn").

BR_SPAWN_DIR (set by br_tracer around each test): line recording, see br_core.py.
BR_CMP_DIR (set by profile_compares.py): comparison profile, see br_compare.py.
"""
import os

if os.environ.get("BR_CMP_DIR"):
    try:
        import br_compare  # noqa: F401
    except Exception:  # pragma: no cover
        pass

if os.environ.get("BR_SPAWN_DIR") and os.environ.get("BR_ROOT"):
    try:
        import br_core

        br_core.install(os.environ["BR_ROOT"])
        br_core.to_file(os.environ["BR_SPAWN_DIR"])
    except Exception:  # pragma: no cover
        pass

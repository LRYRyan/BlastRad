# The scorer builds this on linux/amd64 and runs it with no network.
#
# Nothing is downloaded and no test runs during the build: the maps in maps/
# were recorded once from sqlglot at commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa
# with the scripts in tools/ (see README.md, "Rebuilding the maps").
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY run.py selector.py fingerprint.py replay.py /app/
COPY maps/ /app/maps/

# Unpack the pinned sqlglot sources kept in maps/src.bin, with bytecode, so that
# importing a patched copy at run time (fingerprint.py, replay.py) costs as little CPU
# as possible, and record what replay.py computes on the pinned tree (about 15 seconds).
RUN python /app/selector.py --prepare /app/tree \
 && python -m compileall -q /app/selector.py /app/fingerprint.py /app/replay.py \
 && chmod -R a+rX /app

CMD ["python", "-S", "/app/run.py"]

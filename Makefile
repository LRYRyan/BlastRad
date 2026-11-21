IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check maps local explain

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs the image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Re-records maps/ from the pinned sqlglot commit inside the image the commits
# were labeled in (real duckdb, pandas and pytz from requirements.lock).
# Takes several minutes. Commit the result.
maps:
	docker build --platform linux/amd64 --file tools/Dockerfile.maps --tag blast-radius-maps .
	docker run --rm --platform linux/amd64 --volume $(CURDIR)/maps:/out blast-radius-maps

# No Docker: run the selector straight from this checkout and score it.
local:
	BR_DATA=$(SAMPLE) python3 selector.py > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# Same, but print on stderr why each commit selected what it did.
explain:
	BR_DEBUG=1 BR_DATA=$(SAMPLE) python3 selector.py > $(OUTPUT)

# codec Blast Radius selector

Selects regression tests for unified diffs against SQLGlot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The runtime matches tests by pytest node ID and emits the test numbers supplied by the scorer.

The default uses line and expression coverage for comparable edits inside a function. It widens to the whole function for insertions, deletions, and changes without direct coverage. Class configuration combines runtime class usage, static imports, and observed file access. Collection, import, and unsupported changes have conservative fallbacks. Worker assignment minimizes the largest load plus the per-worker charge using integer milliseconds.

## Build and run

```sh
docker build --platform linux/amd64 -t codec-blast-radius .
```

The Dockerfile fetches the pinned SQLGlot snapshot. It uses the included coverage and dependency map, with no test execution during image builds or runs.

Download the [official starter kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz) and extract it in a separate directory. Set `KIT` to that directory and run the official scorer locally.

```sh
docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$KIT/sample/commits:/data/commits:ro" \
  --volume "$KIT/sample/tests.json:/data/tests.json:ro" \
  codec-blast-radius > "$KIT/codec-output.jsonl"
python3 "$KIT/score.py" "$KIT/sample" "$KIT/sample/answers.json" "$KIT/codec-output.jsonl"
```

Add `--whole-function` after the image name to run the conservative variant. Add `--explain` for selection reasons on stderr.

Run the local regression checks with `python3 -m unittest discover -s tests`.

## Map provenance

`baseline.json.gz` uses the public test catalog for node identities and traces from the pinned, unmodified SQLGlot revision. It contains coverage, import dependents, file readers, and runtime class ownership. The generator accepts test identities and baseline traces.

The map can be rebuilt on Python 3.13 with the starter’s `requirements.lock`, Coverage.py 7.17.4, and the pinned SQLGlot repository at `/src`. Set `PYTHONPATH=/src`, `SKIP_INTEGRATION=0`, and `PYTHON_CPU_COUNT=3`. Run the capture command from `/src` and the map compiler from the entry tree. Use separate directories for the coverage and class-usage passes to keep peak memory down.

```sh
# From pinned SQLGlot. ENTRY is the path to this repository.
RECORD_OWNERS=0 python "$ENTRY/trace/capture.py" /coverage/baseline.json
RECORD_COVERAGE=0 python "$ENTRY/trace/capture.py" /usage/baseline.json tests \
  --deselect=tests/test_parser.py::TestParser::test_max_nodes
# From the entry tree. The coverage pass still includes the parser stress test.
python3 trace/build_map.py /src /coverage "$KIT/sample/tests.json" baseline.json.gz \
  --usage-trace /usage
```

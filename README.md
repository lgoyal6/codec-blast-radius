# codec Blast Radius selector

Selects regression tests for unified diffs against SQLGlot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The runtime matches tests by pytest node ID and emits the test numbers supplied by the scorer.

The default uses line and expression coverage for comparable edits inside a function. It widens to the whole function for insertions, deletions, and changes without direct coverage. For isolated comparison edits, baseline operand observations can show when both operators give the same result. The selector also records class attribute providers, expression schema consumers, and calls to parser registry closures, and key lookups in the tokenizer numeric escape table. It also uses function coverage for isolated literal default changes when the body is unused during collection. Uncertain edits retain the coverage and import fallbacks. Collection, import, and unsupported changes have conservative fallbacks. Worker assignment minimizes the largest load plus the per-worker charge using integer milliseconds.

## Build and run

```sh
docker build --platform linux/amd64 -t codec-blast-radius .
```

The Dockerfile fetches the pinned SQLGlot snapshot. The runtime uses the included coverage and semantic profiles. The image build and scorer run do not execute the test suite.

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

The source reader trace recognizes pytest's own assertion-rewrite reads during collection. Test ownership and import coverage account for loading those modules. Direct application and test reads still contribute to the reader mask. Use `--reader-trace /readers` with a newer capture when rebuilding the map from older coverage data.

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


### Semantic profile

`semantics.json.gz` contains observations from the same unmodified SQLGlot revision. The capture records operands, attribute providers, registry callbacks, and tokenizer table lookups during the baseline test suite. It takes test IDs from the coverage map. It does not use patches or failure labels.

From the pinned checkout at `/src`, add the entry repository's `trace/` directory to `PYTHONPATH`. Use the same locked test environment as the coverage pass.

```sh
export PYTHONPATH="$ENTRY/trace:/src"
BLAST_PROFILE=/profile/main.json python -c 'import profile_runtime; raise SystemExit(profile_runtime.main())' /profile/main.json
BLAST_PROFILE=/profile/stress.json python -c 'import profile_runtime; raise SystemExit(profile_runtime.main())' /profile/stress.json \
  tests/test_parser.py::TestParser::test_max_nodes
```

From the entry repository, combine the successful reports. The parser stress test runs separately to cap baseline capture memory.

```sh
python3 trace/build_semantics.py /src baseline.json.gz semantics.json.gz /profile/*.json
```


### Expression argument validation

The optional schema trace runs the pinned validator and records which inputs would fail a required-argument check. It preserves any test that reached an uncertain validator state. Changes that could affect collection fall back to the existing dependency selection.

Use separate output directories for the two capture modes. From the pinned checkout, run the schema capture with the same environment as the semantic profile.

```sh
BLAST_SCHEMA=1 BLAST_PROFILE=/schema/main.json python -c 'import profile_schema; raise SystemExit(profile_schema.main())' /schema/main.json
BLAST_SCHEMA=1 BLAST_PROFILE=/schema/stress.json python -c 'import profile_schema; raise SystemExit(profile_schema.main())' /schema/stress.json \
  tests/test_parser.py::TestParser::test_max_nodes
```

Add `--schema-reports /schema/*.json` to the semantic profile compiler command.

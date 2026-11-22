# codec Blast Radius selector

Selects regression tests for unified diffs against SQLGlot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The runtime matches tests by pytest node ID and emits the test numbers supplied by the scorer.

The default uses line and expression coverage inside a function to identify candidates for semantic checks. Ordinary function-body edits select whole-function coverage. When the edited function ran during collection, selection can widen to the full catalog. Insertions, deletions, and indentation changes also widen to whole-function coverage. For executable changes, the default unions that selection with baseline execution and test ownership for the edited file. For isolated comparison edits, baseline operand observations can show when both operators give the same result. The selector also records class attribute providers, expression schema consumers, and calls to parser registry closures, and key lookups in the tokenizer numeric escape table. It also uses function coverage for isolated literal default changes when the body is unused during collection. Uncertain edits retain the coverage and import fallbacks. Collection, import, and unsupported changes have conservative fallbacks. Worker assignment minimizes the largest load plus the per-worker charge using integer milliseconds.

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

`semantics.json.gz` contains observations from the same unmodified SQLGlot revision. The capture records operands, attribute providers, registry callbacks, and tokenizer table lookups during the baseline test suite. The compiler imports the pinned SQLGlot checkout to verify enum options for observed dialect configuration fields. Isolated edits between verified members of the same enum can use the recorded attribute readers. Unknown members and collection-time or indirect source reads retain the broad fallback. It takes test IDs from the coverage map. It does not use patches or failure labels.

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

The optional schema trace runs the pinned validator and records which inputs would fail a required-argument check. It preserves any test that reached an uncertain validator state. Collection-time schema reads may use the validator trace when they only derive required keys or check key membership. The selector keeps the broad fallback whenever a changed flag can affect collection-time diagnostics.

Use separate output directories for the two capture modes. From the pinned checkout, run the schema capture with the same environment as the semantic profile.

```sh
BLAST_SCHEMA=1 BLAST_PROFILE=/schema/main.json python -c 'import profile_schema; raise SystemExit(profile_schema.main())' /schema/main.json
BLAST_SCHEMA=1 BLAST_PROFILE=/schema/stress.json python -c 'import profile_schema; raise SystemExit(profile_schema.main())' /schema/stress.json \
  tests/test_parser.py::TestParser::test_max_nodes
```

Add `--schema-reports /schema/*.json` to the semantic profile compiler command.


### Lazy import requests

The import trace records explicit project imports, cached dialect lookups, and registry enumeration. The compiler combines those observations with reverse import dependencies and class coverage. The selector narrows isolated top-level import edits in dialect modules when the baseline can attribute the affected module and the changed imports target already initialized standard library leaf modules.

From the pinned checkout, use another output directory with the same capture environment.

```sh
BLAST_IMPORTS=1 BLAST_PROFILE=/imports/main.json python -c 'import profile_imports; raise SystemExit(profile_imports.main())' /imports/main.json
BLAST_IMPORTS=1 BLAST_PROFILE=/imports/stress.json python -c 'import profile_imports; raise SystemExit(profile_imports.main())' /imports/stress.json \
  tests/test_parser.py::TestParser::test_max_nodes
```

Add `--import-reports /imports/*.json` to the semantic profile compiler command.

For collection-time imports, run two separate collection-only traces from the pinned checkout. The compiler retains tests owned by each observed importing file and its dependents, unions existing runtime coverage, and keeps full selection for any unattributed collection request. The selector only narrows imports from modules initialized before collection in both baseline traces. Type-ignore directives must preserve their tags and their positions among unchanged lines.

```sh
python "$ENTRY/trace/profile_collection_imports.py" /imports/collection-forward.json "$KIT/sample/tests.json"
python "$ENTRY/trace/profile_collection_imports.py" /imports/collection-reverse.json "$KIT/sample/tests.json" --reverse
```

Pass the two collection files with `--collection-reports` and list the runtime reports separately under `--import-reports`.


The selector parses the full file and indexes the syntax around the edited lines first. Class-level fallback rebuilds the complete index when it needs definitions elsewhere in the file.

For compatible one-line comparison edits, the selector can verify a literal or operator change from the original source byte ranges. It uses the whole syntax tree whenever that exact check cannot establish the edit.

### Function defaults

The baseline capture records function entries, arguments that equal the original literal defaults, and signature readers keyed by source path and qualified name. Tests covered by the original function map without a recorded entry remain selected. Collection-time reads and callables that can outlive their creator keep the broad fallback. A private parser factory can use callback coverage when every baseline reference is a direct registry constructor, the changed default is omitted, and the returned callback is never invoked during collection. Call sites with an explicit value are unaffected by the default change. Reflection and unobserved coverage remain selected.

From the pinned checkout, use a dedicated output directory and the same capture environment.

```sh
BLAST_DEFAULTS=1 BLAST_PROFILE=/defaults/main.json python -c 'import profile_defaults; raise SystemExit(profile_defaults.main())' /defaults/main.json
BLAST_DEFAULTS=1 BLAST_PROFILE=/defaults/stress.json python -c 'import profile_defaults; raise SystemExit(profile_defaults.main())' /defaults/stress.json \
  tests/test_parser.py::TestParser::test_max_nodes
```

Add `--default-reports /defaults/*.json` to the semantic profile compiler command.

### Copies of fresh expressions

The copy trace observes direct calls to `exp.alias_` on results from the original `exp.table_` and `exp.column` builders. A test can omit an isolated literal `copy` toggle when its baseline calls pass only scalar constructor arguments and produce detached expression trees with ordinary copy implementations. Missing observations and indirect or shared inputs keep coverage-based selection.

From the pinned checkout, use a separate output directory and the same test environment as the other semantic traces.

```sh
BLAST_COPIES=1 BLAST_PROFILE=/copies/main.json python -c 'import profile_copies; raise SystemExit(profile_copies.main())' /copies/main.json
```

Add `--copy-reports /copies/*.json` to the semantic profile compiler command.

# codec Blast Radius selector

Selects regression tests for unified diffs against SQLGlot `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The runtime matches tests by pytest node ID and emits the test numbers supplied by the scorer.

The default uses line and expression coverage for comparable edits inside a function. It widens to the whole function for insertions, deletions, indentation changes, and changes without direct coverage. For isolated comparison edits, baseline operand observations can show when both operators give the same result. The selector also records class attribute providers, expression schema consumers, and calls to parser registry closures, and key lookups in the tokenizer numeric escape table. It also uses function coverage for isolated literal default changes when the body is unused during collection. Uncertain edits retain the coverage and import fallbacks. Collection, import, and unsupported changes have conservative fallbacks. Fixture reads during class, module, package, or session setup include each test that shares that scope. Worker assignment minimizes the largest load plus the per-worker charge using integer milliseconds.

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

### Shared fixture reads

The fixture scope pass runs pytest setup on the pinned checkout and records files read by shared fixtures. It expands each scope to the corresponding test IDs in the public catalog. Merge those dependencies into the direct-reader map with the commands below.

```bash
selector_dir=$(pwd)
cd /path/to/pinned-sqlglot
python3 "$selector_dir/trace/capture_fixture_scopes.py" /path/to/tests.json /tmp/scoped-reads.json tests
cd "$selector_dir"
python3 trace/merge_fixture_scopes.py direct-baseline.json.gz /tmp/scoped-reads.json baseline.json.gz
```


### Parser registry callbacks

For a private factory used only by observed parser mappings, the selector compares effective defaults at each callback construction. Explicit overrides that preserve the old effective argument keep that callback unchanged. Uncertain changes retain the existing dependency selection.

The handler pass instruments unchanged baseline dictionary lambdas at creation and records which test enters each callback. It preserves the original functions and watches Python function entry through `sys.monitoring`. It applies when the surrounding AST stays the same and the changed callback had no recorded collection use.

Use the same pinned checkout, Python 3.13, and test setup as the other baseline traces. Set `BLAST_HANDLERS=1` and invoke the existing runtime capture with the trace directory on `PYTHONPATH`. Then merge the main and worker reports into the semantic profile with `trace/build_handlers.py`.


The fixture-pair selector can narrow read-only expected-query edits when the input and metadata remain identical. It preserves the scoped setup readers for other changes.

### New package modules

The root package pass records import requests, file lookups, and package enumeration on the pinned baseline. A single newly created root module with only an optional docstring and literal assignments can use that evidence when its name has no observed lookup. Tests that may enumerate the package remain eligible.

Set `BLAST_MODULES=1` in the baseline trace environment. Combine the full-catalog main and worker reports with `trace/build_added_modules.py`.


### Class configuration dependencies

Literal class containers can use their baseline attribute readers when static analysis finds only read-only runtime access. The trace builder excludes initialization helpers and aliasing accesses.

Expression schema edits that add or remove keys can use existing schema readers when the class has no collection-time consumers of the mapping beyond deriving required keys. That guard covers optional and required keys. The selector preserves the relative order and required status of each shared key.

For read-only configuration fields, the selector can match a changed string key to its baseline lookup and membership observations. It keeps whole-container readers and tests with unresolved observations. A dictionary edit must preserve the key order and value types. A set edit must preserve a literal string set.

Set `BLAST_CONFIG_KEYS=1` with the existing `BLAST_PROFILE` capture. The tracked fields are listed in `trace/config_key_fields.json`. Merge the main and worker reports with `trace/build_config_keys.py`.

### Already initialized project imports

The warm-import trace records which leaf modules have completed initialization before each lazy module starts loading. The selector admits a project dependency only when the same leaf was ready in the pinned baseline suite and in a fresh interpreter importing the target directly. Reverse-order collection observations can narrow that evidence further. Packages, partially initialized modules, and modules with dynamic attribute hooks are excluded.

Run the baseline suite with `BLAST_WARM_MODE=full` and the trace directory on `PYTHONPATH`. Use `capture_warm_reverse.py` with the catalog at `/tests.json` for reversed collection. The cold capture imports the module names from `/trace/targets.json` in fresh interpreters. These names come from the baseline profile's `lazy_module_users` keys. Collect captures under `/reports` and rebuild the map with `trace/build_warm_imports.py`.

A dictionary profile built with `--dictionary-membership` also records membership checks, including lookups for absent keys. That profile can scope literal key additions and removals while preserving tests that consume the complete dictionary. Value-only edits continue to use the value-lookup profile. Edits must preserve the relative order and value types of shared entries.

### TPC fixture consumers

A fixture edit that preserves the pinned pair loader can use the executor methods that consume the affected fields. The TPC-DS executor uses input SQL and execution flags. Its expected SQL remains covered through the existing optimizer readers. Literal-only input edits and `execute` flag changes keep the affected executor methods and any tests sharing an input cache key. Changes outside those guards retain the shared setup readers.

### Inserted branch exits

A patch that inserts one return or raise before an unchanged statement can use that statement's baseline coverage. Structural AST checks reject edits that introduce bindings or change other statements. Missing coverage and collection execution retain the whole-function fallback.

### Parser dispatch entries

Parser dispatch changes can use observed lookups when a dialect's table had no whole-table readers across baseline execution. The selector admits literal-key edits to lambdas without default arguments. It keeps inherited table initializers in the same order and leaves other class initialization unchanged.

For capture, use the existing configuration profile with `BLAST_CONFIG_FIELDS` pointing to a JSON array containing `FUNCTION_PARSERS`. Include the isolated parser stress test as well as the regular catalog. Build the map with `trace/build_config_keys.py --parser-dispatch --pinned-source` and the pinned checkout.

For dispatch tables keyed by `TokenType`, the selector can narrow handler substitutions with the same keys and order. It accepts lambdas without defaults and references to pinned module functions already present in the table. It retains the broader selection for whole-container consumers, collection-time callbacks, unresolved providers, and changes to the dictionary structure.

To capture token lookups, set `BLAST_TOKEN_DISPATCH=1` and point `BLAST_CONFIG_FIELDS` to `trace/token_dispatch_fields.json`. Run the regular catalog and isolated parser stress test separately. Pass both reports and the worker reports to `trace/build_config_keys.py --token-dispatch --pinned-source`. This profile treats dictionary truth tests and the pinned parser's token-membership helper as independent of table values.

The optional namespace profile records the receiving class at each `__dict__` read. It preserves tests with unknown or unobserved receivers, and it widens selection when the affected class namespace was inspected during collection. Run `profile_reflection.start` in pytest and its workers through the same baseline capture entry point, then pass all reports and the pinned checkout to `trace/build_reflection.py`.


The optional call reachability profile covers literal argument changes in an existing statement when two baseline captures agree that its complete span is unexecuted. The selector verifies that the surrounding syntax, binding, and statement location are preserved. Anything outside that shape uses the ordinary selection rules, and the selector retains source and code metadata readers.

Run `trace/build_call_reach.py SOURCE BASELINE TARGETS` to generate candidate source lines. Capture the complete catalog and the isolated parser stress test with `profile_call_reach.start` in pytest and its workers, setting `BLAST_CALL_TARGETS` to `TARGETS`. Compile the resulting reports with `trace/build_call_reach.py SOURCE BASELINE OUTPUT --profile PROFILE --reports REPORTS...`.


The optional documentation profile records tracked Markdown files with no file access beyond pytest discovery's file-type checks. Run `strace -f -s 4096 -y -e trace=%file` around the baseline pytest catalog, the separate parser stress test, and collection alone. During the collection trace, record the Python callers of `os.stat` for tracked Markdown basenames in `collect-stats.json`. Provide all three trace and log pairs to `trace/build_unread_docs.py`. The selector uses the profile for a single existing Markdown file after checking the complete patch against the pinned contents.

The optional module binding profile associates literal top-level constants with their observed readers. It combines direct names from pinned source with external attribute reads during the full test catalog. It retains the broad fallback for re-exported names, uncertain module namespace access, or binding consumption during collection. The selector requires one existing literal assignment, the same value type, and unchanged surrounding syntax.

Generate module targets with `trace/build_module_bindings.py SOURCE BASELINE TARGETS`. Capture the catalog and separate parser stress test with `profile_module_bindings.start` in pytest and its workers, passing `BLAST_MODULE_TARGETS=TARGETS`. Merge the reports using the same compiler with `--profile PROFILE --reports REPORTS...`.

For the pinned dialect initializer, changing only single-character values in the existing
global escape dictionary can use observed map lookups. Tokenizer lookups see only the
changed original keys. Generator lookups can see only each old or new value that the
initializer reverses into a key. The profile also retains whole-map consumers, direct
readers, and namespace or source inspection. Collection-time observations, identifier
delimiter changes, and edits that change keys or surrounding code use the broader
fallback. Rebuild the masks with `trace/build_escape_tables.py` using the complete
baseline capture from `profile_escape_reads.start` and the module namespace capture.

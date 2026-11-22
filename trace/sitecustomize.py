"""Install the baseline import hooks before spawn workers unpickle SQLGlot objects."""

import atexit
import os
from pathlib import Path

if os.environ.get("BLAST_WARM_MODE"):
    import profile_warm_imports

if output := os.environ.get("BLAST_PROFILE"):
    if os.environ.get("BLAST_ANNOTATIONS"):
        import profile_runtime as runtime
        from profile_annotations import start

        runtime.start = start
    elif os.environ.get("BLAST_CONFIG_KEYS"):
        import profile_runtime as runtime
        from profile_config_keys import start

        runtime.start = start
    elif os.environ.get("BLAST_MODULES"):
        import profile_runtime as runtime
        from profile_added_modules import start

        runtime.start = start
    elif os.environ.get("BLAST_HANDLERS"):
        import profile_runtime as runtime
        from profile_handlers import start

        runtime.start = start
    elif os.environ.get("BLAST_COPIES"):
        from profile_copies import start
    elif os.environ.get("BLAST_DEFAULTS"):
        from profile_defaults import start
    elif os.environ.get("BLAST_IMPORTS"):
        from profile_imports import start
    elif os.environ.get("BLAST_SCHEMA"):
        from profile_schema import start
    else:
        from profile_runtime import start

    master = Path(output)
    trace = start(master.with_name(f"{master.stem}.worker-{os.getpid()}.json"))
    atexit.register(trace.save)

"""Install the baseline import hooks before spawn workers unpickle SQLGlot objects."""

import atexit
import os
from pathlib import Path

if output := os.environ.get("BLAST_PROFILE"):
    from profile_runtime import start

    master = Path(output)
    trace = start(master.with_name(f"{master.stem}.worker-{os.getpid()}.json"))
    atexit.register(trace.save)

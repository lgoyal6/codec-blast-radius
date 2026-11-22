import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path


def capture(name):
    command = [sys.executable, '-c', 'import importlib, sys; importlib.import_module(sys.argv[1])', name]
    result = subprocess.run(command, capture_output=True, text=True)
    return name, result.returncode


targets = json.loads(Path('/trace/targets.json').read_text())
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results = dict(pool.map(capture, targets))
Path('/reports/cold-status.json').write_text(json.dumps(results, sort_keys=True))
print(f'{sum(status == 0 for status in results.values())}/{len(results)} clean baseline imports')

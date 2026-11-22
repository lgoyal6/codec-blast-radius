import json
from pathlib import Path

import pytest

known=sorted({entry['file'] for entry in json.loads(Path('/tests.json').read_bytes())['tests']}, reverse=True)
raise SystemExit(pytest.main(['-q','--collect-only','-p','no:cacheprovider',*known]))

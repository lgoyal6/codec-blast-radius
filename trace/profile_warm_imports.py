"""Measure completed leaf imports at the start of lazy project imports."""
import atexit
import json
import os
import sys
import types
from pathlib import Path

FAMILIES = {'dialects', 'generators', 'parsers', 'typing'}
observed = {}


class WarmImportFinder:
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split('.')
        if len(parts) != 3 or parts[0] != 'sqlglot' or parts[1] not in FAMILIES:
            return None
        warm = set()
        for name, module in sys.modules.copy().items():
            if not (name.startswith('sqlglot.') or name in sys.stdlib_module_names) or type(module) is not types.ModuleType:
                continue
            meta = vars(module)
            spec = meta.get('__spec__')
            if spec is None or getattr(spec, '_initializing', False) or {'__getattr__', '__path__'} & meta.keys():
                continue
            warm.add(name)
        if fullname in observed:
            observed[fullname] &= warm
        else:
            observed[fullname] = warm
        return None


def report():
    variant = os.environ['BLAST_WARM_MODE']
    destination = Path(f'/reports/{variant}-{os.getpid()}.json')
    destination.write_text(json.dumps({name:sorted(items) for name,items in sorted(observed.items())}))


if 'BLAST_WARM_MODE' in os.environ:
    sys.meta_path.insert(0, WarmImportFinder())
    atexit.register(report)

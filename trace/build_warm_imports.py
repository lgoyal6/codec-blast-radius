"""Index initialized project leaves seen before each pinned import."""
import argparse
import gzip
import json
from pathlib import Path


def observed(reports):
    ready = {}
    for report in reports:
        for name, available in json.loads(report.read_bytes()).items():
            modules = set(available)
            ready[name] = ready[name] & modules if name in ready else modules
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('profile', 'captured', 'output'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    trace = args.captured
    full = observed(sorted(trace.glob('full-[0-9]*.json')))
    reverse = observed(sorted(trace.glob('reverse-[0-9]*.json')))
    cold = observed(sorted(trace.glob('cold-[0-9]*.json')))
    status = json.loads((trace / 'cold-status.json').read_bytes())
    if not (full and reverse and cold and status):
        raise ValueError('full, reverse collection and cold import observations are required')
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    imports = profile['import_profile']
    entries = {}
    for path in sorted(imports['lazy_module_users']):
        name = path.removesuffix('.py').replace('/', '.')
        if status.get(name) != 0 or name not in full or name not in cold:
            continue
        ready = full[name] & cold[name]
        if name in reverse:
            ready &= reverse[name]
        leaf_modules = sorted(module for module in ready if module.startswith('sqlglot.'))
        if leaf_modules:
            entries[path] = leaf_modules
    imports['module_warm_imports'] = entries
    with args.output.open('wb') as target:
        with gzip.GzipFile(filename='', fileobj=target, mode='wb', mtime=0) as output:
            output.write(json.dumps(profile, separators=(',', ':')).encode())
    print(f'{len(entries)} lazy source files have cold and warm import evidence')


if __name__ == '__main__':
    main()

"""Union scoped fixture readers with the pinned baseline's direct readers."""
import argparse
import gzip
import json
from pathlib import Path


def merge(document, scoped_readers):
    indices = {test: 1 << index for index, test in enumerate(document["ids"])}
    masks = document["masks"]
    positions = {value: index for index, value in enumerate(masks)}
    changed = 0
    for path, readers in sorted(scoped_readers.items()):
        data = document["files"].setdefault(path, [[], [], 0, 0, 0])
        previous = int(masks[data[3]], 16)
        merged = previous
        for test in readers:
            merged |= indices[test]
        if merged == previous:
            continue
        value = format(merged, "x")
        if value not in positions:
            positions[value] = len(masks)
            masks.append(value)
        data[3] = positions[value]
        changed += 1
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("scoped_readers", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    document = json.loads(gzip.decompress(args.baseline.read_bytes()))
    scoped = json.loads(args.scoped_readers.read_bytes())
    changed = merge(document, scoped)
    with (
        args.output.open("wb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as output,
    ):
        output.write(json.dumps(document, separators=(",", ":")).encode())
    print(f"Expanded fixture readers for {changed} files")


if __name__ == "__main__":
    main()

"""Compile baseline root package import and filesystem observations."""
import argparse
import gzip
import json
from pathlib import Path


def build(profile, reports):
    observed = set()
    enumerators = set()
    looked_up = set()
    for path in reports:
        report = json.loads(path.read_bytes())
        observed.update(report["seen"])
        enumerators.update(report["enumerated"])
        looked_up.update(report["requested"])
        looked_up.update(report["looked_up"])
    ids = profile["ids"]
    seen = sum(1 << index for index, test in enumerate(ids) if test in observed)
    if "@collection" in enumerators:
        enumeration_mask = (1 << len(ids)) - 1
    else:
        enumeration_mask = sum(1 << index for index, test in enumerate(ids) if test in enumerators)
    profile["added_module_profile"] = {
        "seen": format(seen, "x"),
        "enumerated": format(enumeration_mask, "x"),
        "looked_up": sorted(looked_up),
    }
    return seen.bit_count(), enumeration_mask.bit_count(), len(looked_up)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    profile = json.loads(gzip.decompress(args.profile.read_bytes()))
    tests, enumerators, names = build(profile, args.reports)
    with (
        args.output.open("wb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as output,
    ):
        output.write(json.dumps(profile, separators=(",", ":")).encode())
    print(f"{tests} tests, {enumerators} possible package enumerators, {names} observed root names")


if __name__ == "__main__":
    main()

"""Read ordinary unified diffs and verify text hunks against the pinned tree."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

HUNK_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?$")
NO_NEWLINE = b"\\ No newline at end of file"


def split_lines(data):
    """Split only at LF, preserving CR, embedded control characters and final LF."""
    lines = data.split(b"\n")
    return [part + b"\n" for part in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def filename(header, prefix):
    name = header[4:].rstrip(b"\n").split(b"\t", 1)[0].decode("utf-8")
    if not name.startswith(prefix):
        raise ValueError("unsupported file header")
    name = name[len(prefix):]
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError("invalid patch path")
    return name


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    rows: list[tuple[bytes, bytes]]


@dataclass
class FileChange:
    path: str
    hunks: list[Hunk]

    def apply(self, before):
        source = split_lines(before)
        result = []
        changed_old, changed_new, anchors = set(), set(), set()
        source_cursor = 0
        for hunk in self.hunks:
            start = hunk.old_start - bool(hunk.old_count)
            new_start = hunk.new_start - bool(hunk.new_count)
            if not source_cursor <= start <= len(source):
                raise ValueError("overlapping or out-of-range hunk")
            result.extend(source[source_cursor:start])
            if len(result) != new_start:
                raise ValueError("inconsistent new-file hunk offset")
            source_cursor = start
            removes_in_block = False
            for command, text in hunk.rows:
                if command == b" ":
                    removes_in_block = False
                if command in (b" ", b"-"):
                    if source_cursor >= len(source) or source[source_cursor] != text:
                        raise ValueError("patch context differs from pinned source")
                    if command == b"-":
                        changed_old.add(source_cursor + 1)
                        removes_in_block = True
                    source_cursor += 1
                if command in (b" ", b"+"):
                    if command == b"+":
                        changed_new.add(len(result) + 1)
                        if not removes_in_block:
                            anchors.add(max(1, min(source_cursor + 1, len(source))))
                    result.append(text)
        result.extend(source[source_cursor:])
        return b"".join(result), changed_old, changed_new, anchors


def parse_patch(data):
    lines = split_lines(data)
    changes = []
    seen = set()
    index = 0
    while index < len(lines):
        if lines[index].startswith(b"diff --git "):
            index += 1
            if index < len(lines) and lines[index].startswith(b"index "):
                index += 1
        if (
            index + 1 >= len(lines)
            or not lines[index].startswith(b"--- ")
            or not lines[index + 1].startswith(b"+++ ")
        ):
            raise ValueError("unsupported patch metadata")
        path = filename(lines[index], "a/")
        if filename(lines[index + 1], "b/") != path or path in seen:
            raise ValueError("renamed or repeated file")
        seen.add(path)
        index += 2
        hunks = []
        while index < len(lines) and (match := HUNK_HEADER.fullmatch(lines[index])):
            old_start, old_count, new_start, new_count = (
                int(value) if value is not None else 1 for value in match.groups()
            )
            if not (old_count or new_count):
                raise ValueError("empty hunk")
            old_left, new_left = old_count, new_count
            rows = []
            index += 1
            while old_left or new_left:
                if index >= len(lines):
                    raise ValueError("incomplete hunk")
                command, text = lines[index][:1], lines[index][1:]
                if command not in (b" ", b"-", b"+"):
                    raise ValueError("invalid hunk row")
                old_left -= command != b"+"
                new_left -= command != b"-"
                if min(old_left, new_left) < 0:
                    raise ValueError("invalid hunk length")
                index += 1
                if index < len(lines) and lines[index].rstrip(b"\n") == NO_NEWLINE:
                    text = text.removesuffix(b"\n")
                    index += 1
                rows.append((command, text))
            hunks.append(Hunk(old_start, old_count, new_start, new_count, rows))
        if not hunks:
            raise ValueError("file without text hunks")
        changes.append(FileChange(path, hunks))
    if not changes:
        raise ValueError("patch has no file changes")
    return changes

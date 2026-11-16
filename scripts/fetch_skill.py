#!/usr/bin/env python3
"""Download the published Genomic Intelligence skill zip and extract it safely.

Usage: python3 -I scripts/fetch_skill.py <empty-output-dir> [--zip <local.zip>]

--zip extracts a local file instead of downloading (used by scripts/tests/).

The zip is untrusted data. Nothing inside it is executed or imported. Every
entry must be a regular file or directory under genomic-intelligence/, with no
absolute paths, no "..", no control characters in names, no symlinks and no
special files. Anything else aborts the run before a single byte is written.
Decompression is streamed with running byte counters, so an entry that
inflates past its declared size, the per-entry cap or the total cap aborts the
run and removes whatever was extracted.

On success the skill folder is at <empty-output-dir>/genomic-intelligence.
"""
import os
import shutil
import stat
import sys
import urllib.request
import zipfile
from pathlib import PurePosixPath

ZIP_URL = os.environ.get(
    "GI_SKILL_ZIP_URL",
    "https://docs.genomicintelligence.ai/skill/genomic-intelligence.zip",
)
TOP = "genomic-intelligence"
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_ENTRY_BYTES = 5 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024
CHUNK = 64 * 1024
MAX_ENTRIES = 500


class Refused(Exception):
    pass


def fail(msg):
    raise Refused(msg)


def check_entry(info):
    name = info.filename
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        fail(f"refusing entry with a control character in its name: {name!r}")
    if "\\" in name:
        fail(f"refusing entry with a backslash: {name!r}")
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith("/"):
        fail(f"refusing absolute entry: {name!r}")
    if ".." in p.parts:
        fail(f"refusing entry with '..': {name!r}")
    if not p.parts or p.parts[0] != TOP:
        fail(f"refusing entry outside {TOP}/: {name!r}")
    mode = (info.external_attr >> 16) & 0xFFFF
    ftype = stat.S_IFMT(mode)
    if info.is_dir():
        if ftype not in (0, stat.S_IFDIR):
            fail(f"refusing odd directory entry: {name!r}")
        return "dir"
    if ftype not in (0, stat.S_IFREG):
        fail(f"refusing non-regular entry (symlink or special file): {name!r}")
    return "file"


def download(dest):
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "gi-claude-plugins-sync"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            fail(f"GET {ZIP_URL} returned {resp.status}")
        data = resp.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES:
        fail("zip is larger than the size cap")
    with open(dest, "wb") as fh:
        fh.write(data)


def copy_capped(src, dst, name, declared, total):
    """Copy one entry in chunks, counting ACTUAL decompressed bytes."""
    written = 0
    limit = min(declared, MAX_ENTRY_BYTES)
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return total
        written += len(chunk)
        total += len(chunk)
        if written > limit:
            if written > declared:
                fail(f"entry inflates past its declared size of {declared} bytes: {name!r}")
            fail(f"entry is larger than the per-entry cap: {name!r}")
        if total > MAX_TOTAL_UNCOMPRESSED:
            fail("zip expands past the total size cap")
        dst.write(chunk)


def extract(zpath, out):
    with zipfile.ZipFile(zpath) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            fail("too many entries in zip")
        if sum(i.file_size for i in infos) > MAX_TOTAL_UNCOMPRESSED:
            fail("zip declares more than the total size cap")
        kinds = [(i, check_entry(i)) for i in infos]
        if not any(i.filename == f"{TOP}/SKILL.md" for i, k in kinds if k == "file"):
            fail(f"zip has no {TOP}/SKILL.md")
        total = 0
        for info, kind in kinds:
            dest = os.path.realpath(os.path.join(out, info.filename))
            if not dest.startswith(out + os.sep):
                fail(f"entry escapes the output dir: {info.filename!r}")
            if kind == "dir":
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, "xb") as dst:
                total = copy_capped(src, dst, info.filename, info.file_size, total)
            os.chmod(dest, 0o644)


def main():
    args = sys.argv[1:]
    local_zip = None
    if len(args) == 3 and args[1] == "--zip":
        local_zip = args[2]
        args = args[:1]
    if len(args) != 1:
        print("usage: fetch_skill.py <empty-output-dir> [--zip <local.zip>]", file=sys.stderr)
        sys.exit(2)
    out = os.path.realpath(args[0])
    os.makedirs(out, exist_ok=True)
    if os.listdir(out):
        print(f"fetch_skill: output dir is not empty: {out}", file=sys.stderr)
        sys.exit(1)
    zpath = os.path.join(out, "skill.zip")
    try:
        if local_zip:
            shutil.copyfile(local_zip, zpath)
        else:
            download(zpath)
        extract(zpath, out)
    except (Refused, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        shutil.rmtree(os.path.join(out, TOP), ignore_errors=True)
        print(f"fetch_skill: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if os.path.exists(zpath):
            os.remove(zpath)
    print(os.path.join(out, TOP))


if __name__ == "__main__":
    main()

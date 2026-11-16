#!/usr/bin/env python3
"""Offline tests for scripts/fetch_skill.py. Run: python3 -I scripts/tests/test_fetch_skill.py

Each case builds a zip in a temp dir, runs fetch_skill.py --zip on it, and
checks the exit code and that a refused zip leaves no extracted files behind.
"""
import os
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FETCH = os.path.join(HERE, "..", "fetch_skill.py")
REG = stat.S_IFREG | 0o644
SKILL = ("genomic-intelligence/SKILL.md", b"---\nname: genomic-intelligence\n---\n", REG)


def build(path, entries, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression) as z:
        for name, data, mode in entries:
            zi = zipfile.ZipInfo(name)
            zi.external_attr = mode << 16
            zi.compress_type = compression
            z.writestr(zi, data)


def lie_about_size(path, entry, declared):
    """Rewrite the uncompressed-size fields of one entry (local header + central directory)."""
    raw = bytearray(open(path, "rb").read())
    name = entry.encode()
    i = 0
    while (i := raw.find(b"PK\x03\x04", i)) != -1:
        n, = struct.unpack_from("<H", raw, i + 26)
        if raw[i + 30:i + 30 + n] == name:
            struct.pack_into("<I", raw, i + 22, declared)
        i += 4
    i = 0
    while (i := raw.find(b"PK\x01\x02", i)) != -1:
        n, = struct.unpack_from("<H", raw, i + 28)
        if raw[i + 46:i + 46 + n] == name:
            struct.pack_into("<I", raw, i + 24, declared)
        i += 4
    open(path, "wb").write(raw)


def run(zpath):
    out = tempfile.mkdtemp()
    p = subprocess.run([sys.executable, "-I", FETCH, out, "--zip", zpath], capture_output=True, text=True)
    files = [os.path.join(r, f) for r, _, fs in os.walk(out) for f in fs]
    return p.returncode, p.stderr.strip(), files


CASES = []


def case(name, entries, expect_ok, patch=None):
    CASES.append((name, entries, expect_ok, patch))


case("good", [SKILL, ("genomic-intelligence/scripts/a.py", b"print(1)\n", REG)], True)
case("dotdot", [SKILL, ("genomic-intelligence/../../evil.txt", b"x", REG)], False)
case("outside", [SKILL, ("other/evil.txt", b"x", REG)], False)
case("absolute", [SKILL, ("/tmp/evil.txt", b"x", REG)], False)
case("symlink", [SKILL, ("genomic-intelligence/link", b"/etc/passwd", stat.S_IFLNK | 0o777)], False)
case("no SKILL.md", [("genomic-intelligence/README", b"x", REG)], False)
for label, ch in (("newline", "\n"), ("carriage return", "\r"), ("tab", "\t"), ("0x01", "\x01"), ("0x1f", "\x1f"), ("DEL 0x7f", "\x7f")):
    case(f"control char in name: {label}", [SKILL, (f"genomic-intelligence/bad{ch}name.txt", b"x", REG)], False)
# 200 KB of real data whose headers declare 10 bytes, placed after a good file so
# the test also proves that a refusal mid-extraction removes what was written.
case("bomb: 200 KB declares 10 bytes", [SKILL, ("genomic-intelligence/bomb.bin", b"\0" * 200_000, REG)], False,
     patch=("genomic-intelligence/bomb.bin", 10))
case("per-entry cap: honest 6 MB entry", [SKILL, ("genomic-intelligence/big.bin", b"\0" * (6 * 1024 * 1024), REG)], False)

failed = 0
with tempfile.TemporaryDirectory() as tmp:
    for i, (name, entries, expect_ok, patch) in enumerate(CASES):
        zpath = os.path.join(tmp, f"case{i}.zip")
        build(zpath, entries)
        if patch:
            lie_about_size(zpath, *patch)
        code, err, files = run(zpath)
        if expect_ok:
            good = code == 0 and len(files) == len(entries)
        else:
            good = code != 0 and not files
        print(f"{'PASS' if good else 'FAIL'}  {name}: exit={code} files_left={len(files)}  {err.splitlines()[-1] if err else ''}")
        failed += not good

# The zip-level bomb above is stopped by zipfile itself: it stops reading at the
# declared size and the CRC check fails. These cases exercise fetch_skill's own
# running counters directly, so they hold even if a zipfile version stops
# honouring the declared size.
import importlib.util
import io

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fetch_skill", FETCH)
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)


def counter_case(name, fn, expect):
    global failed
    try:
        fn()
        msg, good = "no refusal", False
    except fs.Refused as exc:
        msg, good = str(exc), expect in str(exc)
    print(f"{'PASS' if good else 'FAIL'}  {name}: {msg}")
    failed += not good
    CASES.append(name)


counter_case("counter: 200 KB stream declared as 10 bytes",
             lambda: fs.copy_capped(io.BytesIO(b"\0" * 200_000), io.BytesIO(), "bomb.bin", 10, 0),
             "declared size")
counter_case("counter: entry past the per-entry cap",
             lambda: fs.copy_capped(io.BytesIO(b"\0" * (fs.MAX_ENTRY_BYTES + 1)), io.BytesIO(), "big.bin",
                                    fs.MAX_ENTRY_BYTES * 2, 0),
             "per-entry cap")
counter_case("counter: running total past the total cap",
             lambda: fs.copy_capped(io.BytesIO(b"\0" * 1000), io.BytesIO(), "last.bin", 1000,
                                    fs.MAX_TOTAL_UNCOMPRESSED - 10),
             "total size cap")

print(f"{len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)

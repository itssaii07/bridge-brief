"""Check a dataset ZIP's central directory before committing to the download.

Why this exists: the CODEBRIM archive is 8.3 GB, its MD5 matches the one Zenodo
publishes, and it still cannot be extracted -- its own central directory records
local-header offsets up to 277 MB past the end of the file, so ``zipfile`` seeks
somewhere that is not a header and reports ``Bad magic number for file header``.
Nothing short of the complete download reveals that, and the complete download is
the expensive part.

A ZIP's index lives at the *end* of the file, so an HTTP range request can fetch
it on its own. This reads the end-of-central-directory record, follows the ZIP64
locator when there is one, pulls just the central directory, and checks that every
local-header offset lands inside the file. A few MB decides whether a multi-GB
download is worth starting.

It works on a local path too, which is how the CODEBRIM diagnosis was reached.

    python -m scripts.check_dataset_archive <url-or-path> [...]

Exit code is 0 when every archive checks out, 1 otherwise, so it can gate a
download in a shell script.
"""

from __future__ import annotations

import struct
import sys
import urllib.request
from pathlib import Path

#: ZIP record signatures.
EOCD = b"PK\x05\x06"          # end of central directory
EOCD64 = b"PK\x06\x06"        # ZIP64 end of central directory record
LOCATOR64 = b"PK\x06\x07"     # ZIP64 end of central directory locator
CENTRAL = b"PK\x01\x02"       # central directory file header

TAIL_BYTES = 65_536           # the EOCD is within 64 KB of the end by spec
TIMEOUT = 120


class Reader:
    """Random access to an archive, over HTTP or from disk, without reading it all."""

    def __init__(self, target: str):
        self.target = target
        self.is_remote = target.startswith(("http://", "https://"))
        if self.is_remote:
            request = urllib.request.Request(target, method="HEAD")
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                self.size = int(response.headers["Content-Length"])
        else:
            self.size = Path(target).stat().st_size

    def read(self, start: int, length: int) -> bytes:
        """``length`` bytes from ``start``."""
        if length <= 0:
            return b""
        if not self.is_remote:
            with open(self.target, "rb") as handle:
                handle.seek(start)
                return handle.read(length)
        end = start + length - 1        # HTTP ranges are inclusive at both ends
        request = urllib.request.Request(
            self.target, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 206:
                raise RuntimeError(
                    f"server ignored the range request (HTTP {response.status}); "
                    "this check needs a server that supports byte ranges")
            return response.read()


def _zip64_local_offset(extra: bytes) -> int | None:
    """Pull the true local-header offset out of a ZIP64 extra field."""
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack("<HH", extra[pos:pos + 4])
        if tag == 0x0001:
            field = extra[pos + 4:pos + 4 + size]
            # Only the values that overflowed 32 bits are present, in a fixed
            # order, and the local-header offset is the last of them.
            return struct.unpack("<Q", field[-8:])[0] if len(field) >= 8 else None
        pos += 4 + size
    return None


def check(target: str) -> bool:
    """Report on one archive. Returns True when it looks sound."""
    print("=" * 78)
    print(target)
    reader = Reader(target)
    print(f"  size: {reader.size:,} bytes ({reader.size / 2 ** 30:.2f} GiB)")

    tail = reader.read(max(0, reader.size - TAIL_BYTES), min(TAIL_BYTES, reader.size))
    index = tail.rfind(EOCD)
    if index < 0:
        print("  FAIL: no end-of-central-directory record near the end of the file.")
        print("        The archive is truncated, or it is not a ZIP.")
        return False

    entries, cd_size, cd_offset = struct.unpack("<HII", tail[index + 10:index + 20])
    locator = tail.rfind(LOCATOR64, 0, index)
    if locator >= 0:
        z64_at = struct.unpack("<Q", tail[locator + 8:locator + 16])[0]
        record = reader.read(z64_at, 56)
        if not record.startswith(EOCD64):
            print("  FAIL: the ZIP64 locator does not point at a ZIP64 EOCD record.")
            return False
        entries, cd_size, cd_offset = struct.unpack("<QQQ", record[32:56])
        print("  format: ZIP64")
    else:
        print("  format: classic ZIP")

    print(f"  central directory: {entries:,} entries, {cd_size:,} bytes "
          f"at offset {cd_offset:,}")
    if cd_offset + cd_size > reader.size:
        print("  FAIL: the central directory itself runs past the end of the file.")
        return False

    # A classic ZIP stores offsets in 32 bits. Written past 4 GiB without ZIP64
    # records, every offset wraps modulo 2**32 and the recorded directory lands
    # exactly 4 GiB too low. This is what is wrong with CODEBRIM, and it is worth
    # naming precisely, because it means the member data is all physically
    # present and a scanning recovery tool will get it back.
    wrapped = False
    if locator < 0 and reader.size > 2 ** 32:
        blob = reader.read(cd_offset, min(4, cd_size))
        if blob[:4] != CENTRAL:
            candidate = cd_offset + 2 ** 32
            if candidate + cd_size <= reader.size:
                probe = reader.read(candidate, min(4, cd_size))
                if probe[:4] == CENTRAL:
                    print(f"  !! the recorded offset holds no central directory, but "
                          f"{candidate:,} does")
                    print("  !! 32-bit offset overflow: this archive is larger than 4 GiB")
                    print("     and was written WITHOUT the ZIP64 records that size needs,")
                    print("     so every stored offset has wrapped modulo 2**32.")
                    cd_offset, wrapped = candidate, True

    blob = reader.read(cd_offset, cd_size)
    pos, parsed, worst, sample = 0, 0, -1, []
    while pos + 46 <= len(blob) and blob[pos:pos + 46][:4] == CENTRAL:
        name_len, extra_len, comment_len = struct.unpack("<HHH", blob[pos + 28:pos + 34])
        offset = struct.unpack("<I", blob[pos + 42:pos + 46])[0]
        name = blob[pos + 46:pos + 46 + name_len].decode("utf-8", "replace")
        if offset == 0xFFFFFFFF:
            extra = blob[pos + 46 + name_len:pos + 46 + name_len + extra_len]
            offset = _zip64_local_offset(extra) or offset
        worst = max(worst, offset)
        if len(sample) < 3 and not name.endswith("/"):
            sample.append(name)
        parsed += 1
        pos += 46 + name_len + extra_len + comment_len

    print(f"  parsed {parsed:,} of {entries:,} entries")
    print(f"  largest local-header offset: {worst:,}")
    if sample:
        print(f"  sample members: {', '.join(sample)}")

    if wrapped or worst >= reader.size:
        if worst >= reader.size:
            print(f"  *** BROKEN: {worst - reader.size:,} bytes past the end of the file.")
        else:
            print("  *** BROKEN: offsets have wrapped; they cannot be trusted as stored.")
        print("      This is the CODEBRIM failure mode: the download completes, the")
        print("      checksum matches the one the publisher lists, and extraction still")
        print("      fails with 'Bad magic number for file header'. Re-downloading gets")
        print("      the same bytes and no password is involved -- the index is wrong,")
        print("      not the data.")
        print("      The member data is physically present, so recover with a tool that")
        print("      scans for local headers instead of trusting the index:")
        print("        7z x <archive>          or        zip -FF <archive> --out fixed.zip")
        return False
    if parsed != entries:
        print("  *** SUSPECT: fewer entries parsed than the record claims.")
        return False
    print("  OK: every local-header offset lies inside the file.")
    return True


def main(argv: list[str] | None = None) -> int:
    targets = list(argv if argv is not None else sys.argv[1:])
    if not targets:
        print(__doc__)
        return 2
    results = []
    for target in targets:
        try:
            results.append(check(target))
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            results.append(False)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Copy a handful of genuine records out of each source into ``samples/``.

Run this once the real data is on disk. It exists so that record shape can be
inspected — and committed for review — without reading a 624k-row file into
context, as CLAUDE.md requires.

It is strictly read-only with respect to ``data/raw/``: it opens files, copies a
few leading records, and writes only into ``samples/``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from src.db import RAW_ROOT, DataUnavailable
from src.ingest import nbe, nbi

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def sample_nbi(year: int, *, records: int = 3, root: Path | None = None) -> Path:
    """Header row plus the first few real records from the NBI file."""
    source = nbi.find_files(year, root)[0]
    handle, encoding = nbi._open_text(source)
    try:
        lines = [next(handle) for _ in range(records + 1)]
    except StopIteration:
        raise DataUnavailable(f"{source} has fewer than {records + 1} lines")
    finally:
        handle.close()
    out = SAMPLES / f"nbi_{year}.txt"
    out.write_text("".join(lines), encoding="utf-8")
    print(f"wrote {out} ({records} records, source encoding {encoding})")
    return out


def sample_nbe(year: int, *, structures: int = 2, root: Path | None = None) -> list[Path]:
    """The first couple of real structure subtrees from each state's NBE file."""
    written: list[Path] = []
    for source in nbe.find_files(year, root):
        state = nbe._state_from_path(source) or "unknown"
        label, payload = next(nbe._iter_xml_sources(source))
        root_el = ET.fromstring(payload)
        kept = [n for n in root_el.iter() if nbe._local(n.tag) in nbe.STRUCTURE_TAGS][:structures]
        if not kept:
            print(f"  [warn] {label}: no structure-like nodes found; "
                  "writing the first 4000 bytes verbatim instead")
            out = SAMPLES / f"nbe_{year}_{state}.xml"
            out.write_bytes(payload[:4000])
            written.append(out)
            continue
        wrapper = ET.Element(root_el.tag, root_el.attrib)
        for node in kept:
            wrapper.append(node)
        out = SAMPLES / f"nbe_{year}_{state}.xml"
        out.write_bytes(ET.tostring(wrapper, encoding="utf-8"))
        print(f"wrote {out} ({len(kept)} structures from {label})")
        written.append(out)
    return written


def sample_codebrim(root: Path | None = None) -> Path | None:
    """One real CODEBRIM annotation file, copied verbatim."""
    directory = (root or RAW_ROOT) / "codebrim"
    if not directory.exists():
        print(f"  [skip] no CODEBRIM directory at {directory}")
        return None
    candidates = sorted(directory.rglob("*.xml"))
    if not candidates:
        print(f"  [skip] no .xml annotation files under {directory}")
        return None
    out = SAMPLES / "codebrim_annotation.xml"
    out.write_bytes(candidates[0].read_bytes())
    print(f"wrote {out} (copy of {candidates[0].name})")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--raw-root", default=None)
    args = parser.parse_args(argv)
    root = Path(args.raw_root) if args.raw_root else None

    SAMPLES.mkdir(exist_ok=True)
    failures = 0
    for name, fn in (("NBI", lambda: sample_nbi(args.year, root=root)),
                     ("NBE", lambda: sample_nbe(args.year, root=root)),
                     ("CODEBRIM", lambda: sample_codebrim(root=root))):
        try:
            fn()
        except DataUnavailable as exc:
            failures += 1
            print(f"  [skip] {name}: {exc}", file=sys.stderr)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

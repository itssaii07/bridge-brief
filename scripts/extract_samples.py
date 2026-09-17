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
    # newline="" so the source's own line endings survive untranslated. Without
    # it, write_text on Windows turns the file's CRLF into CRCRLF and the
    # "excerpt" is no longer byte-identical to the record it claims to show.
    with open(out, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))
    print(f"wrote {out} ({records} records, source encoding {encoding})")
    return out


def sample_nbe(year: int, *, structures: int = 2, root: Path | None = None) -> list[Path]:
    """Real element records from each state's NBE file, whole and unedited.

    The published files are flat — a ``<FHWAELEMENT>`` root of repeated
    ``<FHWAED>`` records, each carrying its own structure number (ASSUMPTIONS.md
    D6) — so "a structure" is a group of sibling records sharing a ``STRUCNUM``,
    not a subtree. Records are kept whole: a sample truncated mid-record would be
    useless as the authority on record shape, which is what samples/ is for.

    The structure-subtree path is kept for genuinely nested extracts, since other
    states may publish that way when the corpus is extended.
    """
    written: list[Path] = []
    for source in nbe.find_files(year, root):
        state = nbe._state_from_path(source) or "unknown"
        label, payload = next(nbe._iter_xml_sources(source))
        root_el = ET.fromstring(payload)

        kept = [n for n in root_el.iter() if nbe._local(n.tag) in nbe.STRUCTURE_TAGS][:structures]
        described = "structures"
        if not kept:
            # Flat extract: take every record belonging to the first N structure
            # numbers encountered, in document order.
            wanted: list[str] = []
            for node in root_el.iter():
                if nbe._local(node.tag) not in nbe.ELEMENT_TAGS or node is root_el:
                    continue
                number = nbe._pick(nbe._values(node), nbe.STRUCT_KEYS)
                if number and number not in wanted:
                    if len(wanted) == structures:
                        break
                    wanted.append(number)
            kept = [
                node for node in root_el.iter()
                if nbe._local(node.tag) in nbe.ELEMENT_TAGS and node is not root_el
                and nbe._pick(nbe._values(node), nbe.STRUCT_KEYS) in wanted
            ]
            described = f"element records for {len(wanted)} structures"

        if not kept:
            raise DataUnavailable(
                f"{label}: found neither structure subtrees nor element records. "
                "The parser's tag-candidate lists in src/ingest/nbe.py do not match "
                "this file; fix those first, then re-run."
            )

        wrapper = ET.Element(root_el.tag, root_el.attrib)
        for node in kept:
            wrapper.append(node)
        ET.indent(wrapper, space="  ")
        out = SAMPLES / f"nbe_{year}_{state}.xml"
        out.write_bytes(ET.tostring(wrapper, encoding="utf-8", xml_declaration=True))
        print(f"wrote {out} ({len(kept)} {described} from {label})")
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

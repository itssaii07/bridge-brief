"""Ingest of National Bridge Elements (NBE) element-level condition states.

NBE records, for each AASHTO element on a structure, how much of that element's
quantity sits in each of four condition states. It is the second half of the
contradiction engine's input, and the more informative half: where NBI gives one
number for a whole deck, NBE says how many square feet of it are in poor shape.

## The one thing to correct if the parser is wrong

The exact XML shape cannot be known without a real file, and none is on disk.
Everything shape-specific is deliberately confined to **one function**,
:func:`extract_elements`, and the tag-name candidate lists immediately above it.
If the real files differ, that is the only place to edit. What was assumed is
recorded in ASSUMPTIONS.md section D, and what the parser *actually matched* on a
given file is recorded in ``ingest_log.message``, so you can see which of the
alternatives fired without reading the code.

The parser is namespace-agnostic and case-insensitive, matches on local tag
names, and accepts a value whether it appears as an attribute or as the text of
a child element — because published state extracts do all of these.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from .. import ids
from ..db import DataUnavailable, RAW_ROOT, connect
from ..ids import IdError
from ..store import (
    already_ingested,
    begin_file,
    reject_row,
    sha256_file,
    upsert_element_state,
    upsert_structure,
)
from .element_map import classify_element

#: States for which NBE element data is published and expected here.
EXPECTED_STATES = ("AL", "AZ", "IA")


class NbeFormatError(RuntimeError):
    """The XML does not contain anything the parser recognises as elements.

    Fatal by design. A file we failed to parse and a structure that genuinely has
    no element data look identical in the database otherwise, and confusing the
    two would corrupt the missing-evidence metric.
    """


# ---------------------------------------------------------------------------
# Tag-name candidates — the assumption surface
# ---------------------------------------------------------------------------
# Matched against lower-cased local tag names and attribute names.

#: Tags whose subtree represents one structure.
STRUCTURE_TAGS = ("structure", "bridge", "structureunit", "str")
#: Tags whose subtree represents one element record on a structure.
# ``nbe`` is deliberately NOT in this list: it is commonly the document root
# tag, and matching it would count the whole document as one element record.
ELEMENT_TAGS = ("element", "bridgeelement", "elem", "elementdata", "elementrecord")
#: Where the structure number lives.
STRUCT_KEYS = ("strucnum", "structnum", "structurenumber", "structnumber", "brkey",
               "bridgeid", "structid", "structureid", "bid", "struct")
#: Where the AASHTO element number lives.
ELEMENT_NUM_KEYS = ("elemnum", "elementnumber", "elemno", "elementno", "en",
                    "elementid", "elemid", "aashtoelement")
#: Optional element name / description.
ELEMENT_NAME_KEYS = ("elemname", "elementname", "elemdesc", "elementdescription",
                     "description", "name")
#: Element total quantity.
TOTAL_QTY_KEYS = ("elemquantity", "elementquantity", "totalqty", "totalquantity",
                  "quantity", "qty", "elemtotalqty")
#: Units of measure.
UNITS_KEYS = ("elemscalecode", "units", "unit", "uom", "elemunits")
#: Per-condition-state quantities, in the "four named fields" layout.
CS_FIELD_KEYS = {
    1: ("elemqtystate1", "cs1", "qtystate1", "condstate1", "elemcs1", "state1qty", "cs1qty"),
    2: ("elemqtystate2", "cs2", "qtystate2", "condstate2", "elemcs2", "state2qty", "cs2qty"),
    3: ("elemqtystate3", "cs3", "qtystate3", "condstate3", "elemcs3", "state3qty", "cs3qty"),
    4: ("elemqtystate4", "cs4", "qtystate4", "condstate4", "elemcs4", "state4qty", "cs4qty"),
}
#: Tags for the "four repeated child records" layout.
CS_CHILD_TAGS = ("conditionstate", "condstate", "elementstate", "state", "cs")
#: Within a repeated condition-state record, where its number and quantity live.
CS_NUM_KEYS = ("statenum", "state", "csnum", "conditionstate", "number", "cs")
CS_QTY_KEYS = ("qty", "quantity", "stateqty", "csqty", "elemqty", "value")


def _local(tag: str) -> str:
    """Strip the XML namespace and normalise a tag or attribute name."""
    text = str(tag or "")
    if "}" in text:
        text = text.rsplit("}", 1)[1]
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _values(node: ET.Element) -> dict[str, str]:
    """Collect a node's own attributes and its direct children's texts.

    Keyed by normalised local name. This is what makes the parser indifferent to
    whether a state publishes ``<Element EN="12"/>`` or
    ``<Element><EN>12</EN></Element>`` — both are extremely common.
    """
    out: dict[str, str] = {}
    for key, value in node.attrib.items():
        text = str(value).strip()
        if text:
            out.setdefault(_local(key), text)
    for child in node:
        if len(child) == 0 and child.text and child.text.strip():
            out.setdefault(_local(child.tag), child.text.strip())
    return out


def _pick(values: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in values:
            return values[key]
    return None


def _number(raw: str | None) -> float | None:
    """Parse a quantity. Unparseable or absent returns None, never 0."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class ElementRecord:
    """One element on one structure, with its four condition-state quantities."""

    struct_raw: str
    elem_num: int
    elem_name: str | None = None
    units: str | None = None
    total_qty: float | None = None
    #: condition state -> quantity. Absent states are absent, not zero.
    cs_qty: dict[int, float] = field(default_factory=dict)
    #: Which layout produced the condition-state quantities: 'fields' or 'children'.
    layout: str = "fields"


def _iter_candidate_nodes(root: ET.Element) -> Iterator[tuple[ET.Element, list[ET.Element]]]:
    """Yield (structure_node, element_nodes) pairs, tolerating nesting depth.

    Some extracts nest elements directly under a structure; others interpose a
    unit or inspection level. Rather than assume a depth, this walks the tree and
    attaches each element node to its nearest structure-like ancestor.
    """
    parents: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent

    structures: dict[ET.Element, list[ET.Element]] = {}
    loose: list[ET.Element] = []
    for node in root.iter():
        if node is root or _local(node.tag) not in ELEMENT_TAGS:
            continue
        ancestor = parents.get(node)
        while ancestor is not None and _local(ancestor.tag) not in STRUCTURE_TAGS:
            ancestor = parents.get(ancestor)
        if ancestor is None:
            loose.append(node)
        else:
            structures.setdefault(ancestor, []).append(node)

    yield from structures.items()
    if loose:
        # Elements with no structure ancestor still carry a structure number in
        # many flat extracts; hand them back against the document root.
        yield root, loose


def extract_elements(root: ET.Element) -> tuple[list[ElementRecord], Counter]:
    """**The single assumption point.** Pull element records out of parsed NBE XML.

    Args:
        root: parsed XML document root.

    Returns:
        ``(records, stats)`` where ``stats`` counts what the parser matched on —
        which layout was used, how many element nodes were seen, how many lacked
        a structure number or element number. That counter is written to
        ``ingest_log.message`` so the operator can see what happened without
        instrumenting the code.

    If the real NBE schema differs from what is assumed here, edit this function
    and the tag-candidate lists above it. Nothing else in the project needs to
    change.
    """
    records: list[ElementRecord] = []
    stats: Counter = Counter()

    for struct_node, element_nodes in _iter_candidate_nodes(root):
        struct_values = _values(struct_node)
        struct_level = _pick(struct_values, STRUCT_KEYS)

        for node in element_nodes:
            stats["element_nodes"] += 1
            values = _values(node)

            struct_raw = _pick(values, STRUCT_KEYS) or struct_level
            if not struct_raw:
                stats["missing_struct"] += 1
                continue

            elem_raw = _pick(values, ELEMENT_NUM_KEYS)
            elem_number = _number(elem_raw)
            if elem_number is None or elem_number <= 0:
                stats["missing_elem_num"] += 1
                continue

            record = ElementRecord(
                struct_raw=struct_raw,
                elem_num=int(elem_number),
                elem_name=_pick(values, ELEMENT_NAME_KEYS),
                units=_pick(values, UNITS_KEYS),
                total_qty=_number(_pick(values, TOTAL_QTY_KEYS)),
            )

            # Layout A: four named fields on the element itself.
            for state, keys in CS_FIELD_KEYS.items():
                qty = _number(_pick(values, keys))
                if qty is not None:
                    record.cs_qty[state] = qty

            # Layout B: repeated condition-state child records.
            if not record.cs_qty:
                for child in node:
                    if _local(child.tag) not in CS_CHILD_TAGS:
                        continue
                    child_values = _values(child)
                    state_number = _number(_pick(child_values, CS_NUM_KEYS))
                    qty = _number(_pick(child_values, CS_QTY_KEYS))
                    if state_number is None or int(state_number) not in (1, 2, 3, 4):
                        stats["bad_condition_state"] += 1
                        continue
                    if qty is not None:
                        record.cs_qty[int(state_number)] = qty
                        record.layout = "children"

            if not record.cs_qty:
                stats["no_condition_states"] += 1
                continue

            # Where the total quantity is not published, sum the states. Which of
            # the two was used is recorded so the engine's denominator is never a
            # mystery (ASSUMPTIONS.md K5).
            if record.total_qty is None:
                record.total_qty = sum(record.cs_qty.values())
                stats["total_qty_summed"] += 1
            else:
                stats["total_qty_published"] += 1

            stats[f"layout_{record.layout}"] += 1
            records.append(record)

    return records, stats


# ---------------------------------------------------------------------------
# File-level ingest
# ---------------------------------------------------------------------------


def _iter_xml_sources(path: Path) -> Iterator[tuple[str, bytes]]:
    """Yield ``(label, xml_bytes)`` from an XML file or a ZIP of XML files.

    ZIPs are read in place. Nothing is ever unpacked into ``data/raw/``.
    """
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise NbeFormatError(f"{path} contains no .xml entries")
            for name in sorted(names):
                yield f"{path.name}!{name}", archive.read(name)
    else:
        yield path.name, path.read_bytes()


def ingest_file(
    conn, path: Path, year: int, *, state_abbr: str | None = None,
    force: bool = False, log=print,
) -> dict:
    """Ingest one NBE XML or ZIP file. Idempotent and resumable per file."""
    path = Path(path)
    if not path.exists():
        raise DataUnavailable(
            f"NBE file not found: {path}\n"
            f"Place the {year} NBE per-state files under "
            f"{RAW_ROOT / 'nbe' / str(year)}/<STATE>/ and re-run."
        )

    file_sha = sha256_file(path)
    if not force and already_ingested(conn, str(path), file_sha):
        log(f"  [skip] {path.name} already ingested (unchanged)")
        return {"path": str(path), "skipped": True}

    state_abbr = state_abbr or _state_from_path(path)
    run = begin_file(conn, source="nbe", source_path=str(path), file_sha=file_sha,
                     year=year, state_abbr=state_abbr)
    totals: Counter = Counter()
    structures_seen: set[str] = set()

    try:
        for label, payload in _iter_xml_sources(path):
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as exc:
                run.rows_rejected += 1
                reject_row(conn, source="nbe", source_path=f"{path}:{label}",
                           source_line=None, reason=f"XML parse error: {exc}")
                log(f"  [warn] {label}: XML parse error, skipped ({exc})")
                continue

            records, stats = extract_elements(root)
            totals.update(stats)
            run.rows_read += stats.get("element_nodes", 0)

            if not records:
                # Loud, per the module docstring: an unparsed file must never
                # masquerade as a structure with no elements.
                raise NbeFormatError(
                    f"{label}: parsed {stats.get('element_nodes', 0)} candidate element "
                    "node(s) but extracted no usable element records.\n"
                    f"Parser statistics: {dict(stats)}\n"
                    "The assumed XML shape (ASSUMPTIONS.md section D) does not match this "
                    "file. Fix src/ingest/nbe.py::extract_elements and the tag-candidate "
                    "lists above it — that is the only place that needs to change."
                )

            for record in records:
                try:
                    struct_norm = ids.normalise_struct(record.struct_raw)
                except IdError as exc:
                    run.rows_rejected += 1
                    reject_row(conn, source="nbe", source_path=f"{path}:{label}",
                               source_line=None, reason=f"structure number unusable: {exc}",
                               excerpt=str(record.struct_raw))
                    continue

                if struct_norm not in structures_seen:
                    upsert_structure(conn, struct_norm, struct_raw=record.struct_raw,
                                     state_abbr=state_abbr, year=year)
                    structures_seen.add(struct_norm)

                elem_class = classify_element(record.elem_num)
                for cs, qty in sorted(record.cs_qty.items()):
                    upsert_element_state(
                        conn,
                        struct_norm=struct_norm, year=year, elem_num=record.elem_num,
                        cs=cs, cs_qty=qty, total_qty=record.total_qty,
                        units=record.units, elem_name=record.elem_name,
                        elem_class=elem_class, state_abbr=state_abbr,
                        artifact_id=ids.nbe_id(struct_norm, year, record.elem_num, cs),
                        source_path=f"{path}:{label}",
                    )
                    run.rows_written += 1
            conn.commit()
            log(f"  [read] {label}: {len(records):,} element records, "
                f"{len(structures_seen):,} structures so far")

        conn.commit()
        run.finish("complete", f"parser stats: {dict(totals)}")
        log(f"  [done] {path.name}: {run.rows_written:,} condition-state rows across "
            f"{len(structures_seen):,} structures")
    except Exception as exc:
        conn.commit()
        run.finish("failed", f"{type(exc).__name__}: {exc}")
        raise

    return {
        "path": str(path), "skipped": False, "year": year, "state": state_abbr,
        "rows_read": run.rows_read, "rows_written": run.rows_written,
        "rows_rejected": run.rows_rejected, "structures": len(structures_seen),
        "parser_stats": dict(totals),
    }


def _state_from_path(path: Path) -> str | None:
    """Take the state from the directory name, per the documented layout."""
    for part in reversed(path.parts[:-1]):
        candidate = part.strip().upper()
        if len(candidate) == 2 and candidate.isalpha():
            return candidate
    return None


def find_files(year: int, root: Path | None = None) -> list[Path]:
    directory = (root or RAW_ROOT) / "nbe" / str(year)
    if not directory.exists():
        raise DataUnavailable(
            f"No NBE directory for {year} at {directory}.\n"
            f"Create it with one subdirectory per state ({', '.join(EXPECTED_STATES)}) "
            "and place each state's element file inside. See HANDOFF.md."
        )
    files = sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in {".xml", ".zip"}
    )
    if not files:
        raise DataUnavailable(
            f"{directory} exists but contains no .xml or .zip files.\n"
            "NBE is published as one ZIP per state per year; place the ZIPs "
            "under data/raw/nbe/<year>/<STATE>/ — they do not need unpacking."
        )
    return files


def ingest_year(conn, year: int, *, root: Path | None = None, force: bool = False, log=print) -> list[dict]:
    files = find_files(year, root)
    log(f"NBE {year}: {len(files)} file(s)")
    results = [ingest_file(conn, path, year, force=force, log=log) for path in files]
    found_states = {r.get("state") for r in results if not r.get("skipped")}
    absent = [s for s in EXPECTED_STATES if s not in found_states]
    if absent and found_states:
        log(f"  [note] no NBE data ingested for expected state(s): {', '.join(absent)}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest NBE element condition states.")
    parser.add_argument("--year", type=int, action="append", required=True)
    parser.add_argument("--db", default=None)
    parser.add_argument("--raw-root", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.raw_root) if args.raw_root else None
    # Locate the source files BEFORE opening the database, so that running this
    # with no data on disk reports the absence and leaves no empty index behind.
    try:
        for year in args.year:
            find_files(year, root)
    except DataUnavailable as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        for year in args.year:
            ingest_year(conn, year, root=root, force=args.force)
    except DataUnavailable as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    except NbeFormatError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

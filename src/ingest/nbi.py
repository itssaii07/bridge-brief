"""Ingest of the National Bridge Inventory (NBI) delimited condition records.

The NBI gives one blunt 0-9 condition rating per component per structure per
year. It is one half of the contradiction engine's input; ``nbe.py`` supplies
the other.

Two properties matter more than anything else here:

**Columns are resolved by header name, never by position.** FHWA has published
the delimited NBI with varying column order and with two spellings of several
field names across releases. Hardcoding an index would mean silently reading the
wrong column — the worst possible failure for a system whose output is supposed
to be traceable. If a required column is absent the loader stops and says which
one, listing the headers it actually saw.

**Nothing is invented.** A blank or non-numeric rating is stored as NULL with the
original characters preserved in ``rating_raw``. A row we cannot use is written
to ``rejected_rows`` with a reason. There is no default that could be mistaken
for an observation.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

from .. import ids
from ..db import DataUnavailable, RAW_ROOT, connect
from ..ids import IdError
from ..store import (
    FileRun,
    already_ingested,
    begin_file,
    reject_row,
    sha256_file,
    upsert_rating,
    upsert_structure,
)

#: The delimited NBI uses a single quote as its text qualifier, not a double
#: quote. Change point if a future release differs.
NBI_DIALECT = dict(delimiter=",", quotechar="'", skipinitialspace=True)

#: Encodings tried in order. The files are nominally ASCII, but state-supplied
#: free-text fields carry stray high bytes often enough that refusing to read a
#: 624k-row file over one byte in a facility name would be the wrong failure.
ENCODINGS = ("utf-8", "latin-1")

#: Rows are committed in batches this size, so an interrupted run loses at most
#: one batch and resumes from the file level.
BATCH_SIZE = 5000


class NbiFormatError(RuntimeError):
    """The file's header does not contain a column we must have.

    Deliberately fatal. Guessing which column holds the deck rating is exactly
    the kind of silent assumption this project exists to avoid.
    """


@dataclass(frozen=True)
class FieldSpec:
    """One logical field and the ways it might be spelled in a header row."""

    name: str
    item: str | None          # FHWA item number, e.g. "058"
    synonyms: tuple[str, ...] = ()
    required: bool = False


#: The FHWA item number is the primary signal: it is stable across releases and
#: appears as a suffix in the modern naming (``DECK_COND_058``). The synonyms
#: cover releases that publish the bare name without the item suffix.
FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("struct_raw", "008", ("STRUCTURE_NUMBER", "STRUCTURENUMBER", "BRIDGE_ID"), required=True),
    FieldSpec("state_code", "001", ("STATE_CODE", "STATECODE", "FIPS_STATE"), required=True),
    FieldSpec("county_code", "003", ("COUNTY_CODE",)),
    FieldSpec("feature_crossed", "006", ("FEATURES_DESC", "FEATURES")),
    FieldSpec("facility", "007", ("FACILITY_CARRIED",)),
    FieldSpec("latitude", "016", ("LAT",)),
    FieldSpec("longitude", "017", ("LONG", "LON")),
    FieldSpec("year_built", "027", ("YEAR_BUILT",)),
    FieldSpec("deck", "058", ("DECK_COND",), required=True),
    FieldSpec("superstructure", "059", ("SUPERSTRUCTURE_COND", "SUPERSTRUCTURE"), required=True),
    FieldSpec("substructure", "060", ("SUBSTRUCTURE_COND", "SUBSTRUCTURE"), required=True),
    # Item 62 is the culvert rating. Not marked required: a few state extracts
    # omit it, and a missing culvert column should cost us culverts, not the run.
    FieldSpec("culvert", "062", ("CULVERT_COND", "CULVERT")),
)

#: Logical field name -> NBI component name, for the four rated components.
COMPONENT_FIELDS = {
    "deck": "deck",
    "superstructure": "superstructure",
    "substructure": "substructure",
    "culvert": "culvert",
}

_NORM_HEADER = re.compile(r"[^A-Z0-9]")


def _norm_header(header: str) -> str:
    return _NORM_HEADER.sub("", str(header or "").upper())


def resolve_headers(headers: Sequence[str]) -> dict[str, int]:
    """Map logical field names to column indices using the header row.

    Matching is done on a normalised header (upper-cased, punctuation removed):
    a header matches a field if it ends with that field's FHWA item number, or
    equals one of the field's documented synonyms.

    Returns:
        ``{logical_name: column_index}``, containing only fields that were found.

    Raises:
        NbiFormatError: if any required field is missing. The message names the
            missing fields and lists the headers actually present, because the
            operator needs to see the real header to fix the mapping.
    """
    if not headers:
        raise NbiFormatError("the file has no header row; cannot map columns by name")

    normalised = [_norm_header(h) for h in headers]
    mapping: dict[str, int] = {}

    for spec in FIELDS:
        found: int | None = None
        # Pass 1: FHWA item-number suffix, the most reliable signal. A trailing
        # letter is allowed because FHWA suffixes some items (``..._006A``).
        if spec.item:
            suffix = re.compile(rf".+{spec.item}[A-Z]?$")
            for index, norm in enumerate(normalised):
                if suffix.match(norm):
                    found = index
                    break
        # Pass 2: documented synonyms. Matches the bare name, or the name with
        # an item-number suffix, so both published spellings resolve.
        if found is None:
            for synonym in spec.synonyms:
                pattern = re.compile(rf"^{_norm_header(synonym)}\d*[A-Z]?$")
                for index, norm in enumerate(normalised):
                    if pattern.match(norm):
                        found = index
                        break
                if found is not None:
                    break
        if found is not None:
            mapping[spec.name] = found

    missing = [s.name for s in FIELDS if s.required and s.name not in mapping]
    if missing:
        raise NbiFormatError(
            "NBI file is missing required column(s): " + ", ".join(missing) + ".\n"
            "Columns are mapped by header name, never by position, so this file "
            "cannot be read safely.\nHeaders present ("
            + str(len(headers)) + "): " + ", ".join(str(h) for h in headers[:60])
            + (" ..." if len(headers) > 60 else "")
        )
    return mapping


# ---------------------------------------------------------------------------
# Pure row parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedRow:
    """One NBI record, normalised. ``ratings`` maps component -> (value, raw)."""

    struct_raw: str
    struct_norm: str
    state_code: str | None = None
    state_abbr: str | None = None
    county_code: str | None = None
    facility: str | None = None
    feature_crossed: str | None = None
    year_built: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    ratings: dict[str, tuple[int | None, str]] = field(default_factory=dict)


class RowRejected(ValueError):
    """This row cannot be used. Carries the reason for ``rejected_rows``."""


def _cell(row: Sequence[str], mapping: dict[str, int], name: str) -> str | None:
    index = mapping.get(name)
    if index is None or index >= len(row):
        return None
    value = str(row[index]).strip()
    return value or None


def parse_rating(raw: str | None) -> tuple[int | None, str]:
    """Interpret an NBI condition-rating cell.

    Returns ``(value, raw_text)``. ``value`` is ``None`` for every case that is
    not a 0-9 digit — blank, ``N`` (not applicable, as on the deck rating of a
    culvert), or anything unexpected. The original characters always survive in
    ``raw_text``.

    A NULL rating is never treated as zero, and never as good. Downstream, a
    component with no rating simply does not participate in analysis.
    """
    text = "" if raw is None else str(raw).strip()
    if len(text) == 1 and text.isdigit():
        return int(text), text
    return None, text


def parse_coordinate(raw: str | None, *, is_longitude: bool) -> float | None:
    """Convert an NBI item 16/17 coordinate to decimal degrees.

    NBI publishes coordinates as packed ``DDMMSSss`` (latitude) and ``DDDMMSSss``
    (longitude) digit strings. Anything that does not parse cleanly, or lands
    outside the valid range, returns ``None`` — a wrong coordinate is worse than
    no coordinate, and no finding depends on it.
    """
    text = re.sub(r"[^0-9]", "", str(raw or ""))
    if not text or set(text) == {"0"}:
        return None
    deg_digits = 3 if is_longitude else 2
    if len(text) < deg_digits + 4:
        return None
    try:
        degrees = int(text[:deg_digits])
        minutes = int(text[deg_digits:deg_digits + 2])
        seconds = float(text[deg_digits + 2:deg_digits + 4] + "." + text[deg_digits + 4:] or "0")
    except ValueError:
        return None
    if minutes >= 60 or seconds >= 60:
        return None
    value = degrees + minutes / 60 + seconds / 3600
    limit = 180 if is_longitude else 90
    if value > limit:
        return None
    # NBI stores west longitude as a positive magnitude.
    return -value if is_longitude else value


def parse_row(row: Sequence[str], mapping: dict[str, int]) -> ParsedRow:
    """Turn one delimited record into a :class:`ParsedRow`.

    Pure: no database, no filesystem. This is the function the tests exercise.

    Raises:
        RowRejected: when the structure number is absent or unusable. That is the
            join key; without it the record cannot be attached to anything.
    """
    struct_raw = _cell(row, mapping, "struct_raw")
    if not struct_raw:
        raise RowRejected("structure number is blank")

    # The join key is state-qualified: NBI item 8 is unique only within a state
    # (ids.structure_key). Resolving the abbreviation is therefore required to
    # build the key at all, not merely to label output, so an unrecognised state
    # code rejects the row rather than producing a key that would collide with
    # another state's bridge.
    state_code = _cell(row, mapping, "state_code")
    state_abbr = STATE_CODE_TO_ABBR.get((state_code or "").lstrip("0"))
    if not state_abbr:
        raise RowRejected(f"unrecognised FHWA state code: {state_code!r}")
    try:
        struct_norm = ids.structure_key(state_abbr, struct_raw)
    except IdError as exc:
        raise RowRejected(f"structure number unusable: {exc}") from exc

    year_built_raw = _cell(row, mapping, "year_built")
    year_built: int | None = None
    if year_built_raw and year_built_raw.isdigit():
        candidate = int(year_built_raw)
        if 1700 <= candidate <= 2100:
            year_built = candidate

    parsed = ParsedRow(
        struct_raw=struct_raw,
        struct_norm=struct_norm,
        state_code=state_code,
        state_abbr=state_abbr,
        county_code=_cell(row, mapping, "county_code"),
        facility=_cell(row, mapping, "facility"),
        feature_crossed=_cell(row, mapping, "feature_crossed"),
        year_built=year_built,
        latitude=parse_coordinate(_cell(row, mapping, "latitude"), is_longitude=False),
        longitude=parse_coordinate(_cell(row, mapping, "longitude"), is_longitude=True),
    )
    for field_name, component in COMPONENT_FIELDS.items():
        if field_name not in mapping:
            continue
        parsed.ratings[component] = parse_rating(_cell(row, mapping, field_name))
    return parsed


# ---------------------------------------------------------------------------
# File-level ingest
# ---------------------------------------------------------------------------


def _open_text(path: Path):
    """Open a source file, trying each encoding. Never modifies the file."""
    last: Exception | None = None
    for encoding in ENCODINGS:
        try:
            handle = open(path, "r", encoding=encoding, newline="")
            handle.read(64 * 1024)
            handle.seek(0)
            return handle, encoding
        except UnicodeDecodeError as exc:
            last = exc
            try:
                handle.close()
            except Exception:
                pass
    raise NbiFormatError(f"cannot decode {path} with any of {ENCODINGS}: {last}")


def ingest_file(
    conn,
    path: Path,
    year: int,
    *,
    force: bool = False,
    progress_every: int = 25_000,
    log=print,
) -> dict:
    """Ingest one NBI file. Idempotent, resumable, and loud about what it drops.

    Returns a summary dict. Re-running against an unchanged file is a no-op
    unless ``force`` is set.
    """
    path = Path(path)
    if not path.exists():
        raise DataUnavailable(
            f"NBI file not found: {path}\n"
            f"Place the {year} NBI delimited file under {RAW_ROOT / 'nbi' / str(year)}/ "
            "and re-run. Nothing is downloaded automatically."
        )

    file_sha = sha256_file(path)
    if not force and already_ingested(conn, str(path), file_sha):
        log(f"  [skip] {path.name} already ingested (unchanged)")
        return {"path": str(path), "skipped": True}

    run: FileRun = begin_file(conn, source="nbi", source_path=str(path), file_sha=file_sha, year=year)
    handle, encoding = _open_text(path)
    log(f"  [read] {path.name} (encoding={encoding})")
    try:
        reader = csv.reader(handle, **NBI_DIALECT)
        try:
            headers = next(reader)
        except StopIteration:
            raise NbiFormatError(f"{path} is empty — no header row")
        mapping = resolve_headers(headers)
        optional_missing = [s.name for s in FIELDS if not s.required and s.name not in mapping]
        if optional_missing:
            log(f"  [note] optional columns absent, stored as NULL: {', '.join(optional_missing)}")

        line_no = 1
        for row in reader:
            line_no += 1
            run.rows_read += 1
            if not row or all(not str(c).strip() for c in row):
                continue
            try:
                parsed = parse_row(row, mapping)
            except RowRejected as exc:
                run.rows_rejected += 1
                reject_row(conn, source="nbi", source_path=str(path), source_line=line_no,
                           reason=str(exc), excerpt=",".join(str(c) for c in row[:6]))
                continue

            upsert_structure(
                conn, parsed.struct_norm,
                struct_raw=parsed.struct_raw,
                state_code=parsed.state_code,
                state_abbr=parsed.state_abbr,
                county_code=parsed.county_code,
                facility=parsed.facility,
                feature_crossed=parsed.feature_crossed,
                year_built=parsed.year_built,
                latitude=parsed.latitude,
                longitude=parsed.longitude,
                year=year,
            )
            for component, (value, raw_text) in parsed.ratings.items():
                upsert_rating(
                    conn,
                    struct_norm=parsed.struct_norm, year=year, component=component,
                    rating=value, rating_raw=raw_text,
                    artifact_id=ids.nbi_id(parsed.struct_norm, year, component),
                    source_path=str(path), source_line=line_no,
                )
            run.rows_written += 1

            if run.rows_read % BATCH_SIZE == 0:
                conn.commit()
            if progress_every and run.rows_read % progress_every == 0:
                log(f"  [progress] {run.rows_read:,} rows read, "
                    f"{run.rows_written:,} written, {run.rows_rejected:,} rejected")

        conn.commit()
        run.finish("complete", f"encoding={encoding}; columns mapped={len(mapping)}")
        log(f"  [done] {path.name}: {run.rows_written:,} structures, "
            f"{run.rows_rejected:,} rejected")
    except Exception as exc:
        conn.commit()
        run.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        handle.close()

    return {
        "path": str(path), "skipped": False, "year": year,
        "rows_read": run.rows_read, "rows_written": run.rows_written,
        "rows_rejected": run.rows_rejected,
    }


def find_files(year: int, root: Path | None = None) -> list[Path]:
    """Locate NBI source files for a year under ``data/raw/nbi/{year}/``."""
    directory = (root or RAW_ROOT) / "nbi" / str(year)
    if not directory.exists():
        raise DataUnavailable(
            f"No NBI directory for {year} at {directory}.\n"
            f"Create it and place the delimited NBI file(s) for {year} inside. "
            "See HANDOFF.md for where to download them."
        )
    files = sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in {".txt", ".csv"}
    )
    if not files:
        raise DataUnavailable(
            f"{directory} exists but contains no .txt or .csv files.\n"
            "The NBI delimited download is a single .txt (or .csv) per year; "
            "if you downloaded a ZIP, unpack it into this directory first."
        )
    return files


def ingest_year(conn, year: int, *, root: Path | None = None, force: bool = False, log=print) -> list[dict]:
    files = find_files(year, root)
    log(f"NBI {year}: {len(files)} file(s) under {(root or RAW_ROOT) / 'nbi' / str(year)}")
    return [ingest_file(conn, path, year, force=force, log=log) for path in files]


#: FHWA numeric state codes to postal abbreviations. Only used to label output;
#: an unknown code yields NULL rather than a guess.
STATE_CODE_TO_ABBR = {
    "1": "AL", "2": "AK", "4": "AZ", "5": "AR", "6": "CA", "8": "CO", "9": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID",
    "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA",
    "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ",
    "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK",
    "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN",
    "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR",
    "78": "VI",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest NBI condition records.")
    parser.add_argument("--year", type=int, action="append", required=True,
                        help="NBI year to ingest; repeatable (e.g. --year 2023 --year 2025)")
    parser.add_argument("--db", default=None, help="path to assets.sqlite")
    parser.add_argument("--raw-root", default=None, help="override data/raw")
    parser.add_argument("--force", action="store_true",
                        help="re-ingest files already recorded as complete")
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
    except NbiFormatError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

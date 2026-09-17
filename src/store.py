"""Evidence store — the write side of the asset index.

Shared by the ingest modules and the analysis modules. It does two jobs:

1. Upsert rows into the domain tables (structures, ratings, elements, images…).
2. Keep the ``artifacts`` table in step, so that *every* unit written here is
   immediately addressable and resolvable by its artifact ID.

Those two jobs are done in one call precisely so they cannot drift apart. There
is no code path that writes a rating without registering its artifact.

This module is persistence only. It knows nothing about analysis or generation,
and imports nothing from them.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .db import utcnow


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Hash a source file. Used for idempotency, never to modify the file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def register_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    kind: str,
    *,
    struct_norm: str | None = None,
    year: int | None = None,
    summary: str | None = None,
    source_path: str | None = None,
    source_locator: str | None = None,
) -> None:
    """Make an evidence unit addressable.

    Idempotent: re-ingesting the same file re-registers the same ID with the same
    values. ``created_at`` is preserved from the first registration so that the
    audit trail reflects when the evidence first entered the system.
    """
    conn.execute(
        """
        INSERT INTO artifacts
            (artifact_id, kind, struct_norm, year, summary, source_path, source_locator, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact_id) DO UPDATE SET
            kind = excluded.kind,
            struct_norm = excluded.struct_norm,
            year = excluded.year,
            summary = excluded.summary,
            source_path = excluded.source_path,
            source_locator = excluded.source_locator
        """,
        (artifact_id, kind, struct_norm, year, summary, source_path, source_locator, utcnow()),
    )


def resolve_artifact(conn: sqlite3.Connection, artifact_id: str) -> sqlite3.Row | None:
    """Look an artifact up. ``None`` means the citation does not resolve."""
    return conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()


def artifacts_exist(conn: sqlite3.Connection, artifact_ids: Iterable[str]) -> set[str]:
    """Return the subset of ``artifact_ids`` that resolve. Used by the gate."""
    wanted = list(dict.fromkeys(artifact_ids))
    if not wanted:
        return set()
    found: set[str] = set()
    # Chunked to stay under SQLite's variable limit on large citation sets.
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT artifact_id FROM artifacts WHERE artifact_id IN ({placeholders})", chunk
        ).fetchall()
        found.update(r["artifact_id"] for r in rows)
    return found


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


def upsert_structure(
    conn: sqlite3.Connection,
    struct_norm: str,
    *,
    struct_raw: str | None = None,
    state_code: str | None = None,
    state_abbr: str | None = None,
    county_code: str | None = None,
    facility: str | None = None,
    feature_crossed: str | None = None,
    year_built: int | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    year: int | None = None,
) -> None:
    """Insert or enrich a structure row.

    Existing non-null values are never overwritten with nulls — a later file
    missing a field must not erase what an earlier file told us. ``COALESCE``
    on the excluded value keeps the first real observation.
    """
    conn.execute(
        """
        INSERT INTO structures
            (struct_norm, struct_raw, state_code, state_abbr, county_code, facility,
             feature_crossed, year_built, latitude, longitude, first_seen_year, last_seen_year)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(struct_norm) DO UPDATE SET
            struct_raw      = COALESCE(structures.struct_raw, excluded.struct_raw),
            state_code      = COALESCE(excluded.state_code, structures.state_code),
            state_abbr      = COALESCE(excluded.state_abbr, structures.state_abbr),
            county_code     = COALESCE(excluded.county_code, structures.county_code),
            facility        = COALESCE(excluded.facility, structures.facility),
            feature_crossed = COALESCE(excluded.feature_crossed, structures.feature_crossed),
            year_built      = COALESCE(excluded.year_built, structures.year_built),
            latitude        = COALESCE(excluded.latitude, structures.latitude),
            longitude       = COALESCE(excluded.longitude, structures.longitude),
            first_seen_year = MIN(COALESCE(structures.first_seen_year, excluded.first_seen_year),
                                  COALESCE(excluded.first_seen_year, structures.first_seen_year)),
            last_seen_year  = MAX(COALESCE(structures.last_seen_year, excluded.last_seen_year),
                                  COALESCE(excluded.last_seen_year, structures.last_seen_year))
        """,
        (struct_norm, struct_raw, state_code, state_abbr, county_code, facility,
         feature_crossed, year_built, latitude, longitude, year, year),
    )


# ---------------------------------------------------------------------------
# Ratings and elements
# ---------------------------------------------------------------------------


def upsert_rating(
    conn: sqlite3.Connection,
    *,
    struct_norm: str,
    year: int,
    component: str,
    rating: int | None,
    rating_raw: str | None,
    artifact_id: str,
    source_path: str | None,
    source_line: int | None,
) -> None:
    """Store one NBI component rating and register its artifact."""
    conn.execute(
        """
        INSERT INTO ratings
            (struct_norm, year, component, rating, rating_raw, artifact_id, source_path, source_line)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(struct_norm, year, component) DO UPDATE SET
            rating = excluded.rating,
            rating_raw = excluded.rating_raw,
            artifact_id = excluded.artifact_id,
            source_path = excluded.source_path,
            source_line = excluded.source_line
        """,
        (struct_norm, year, component, rating, rating_raw, artifact_id, source_path, source_line),
    )
    shown = rating if rating is not None else f"not rated ({rating_raw!r})"
    register_artifact(
        conn, artifact_id, "NBI",
        struct_norm=struct_norm, year=year,
        summary=f"NBI {year} {component} condition rating: {shown}",
        source_path=source_path,
        source_locator=f"line {source_line}" if source_line else None,
    )


def upsert_element_state(
    conn: sqlite3.Connection,
    *,
    struct_norm: str,
    year: int,
    elem_num: int,
    cs: int,
    cs_qty: float | None,
    total_qty: float | None,
    units: str | None,
    elem_name: str | None,
    elem_class: str | None,
    state_abbr: str | None,
    artifact_id: str,
    source_path: str | None,
) -> None:
    """Store one NBE element condition-state quantity and register its artifact."""
    conn.execute(
        """
        INSERT INTO elements
            (struct_norm, year, elem_num, cs, cs_qty, total_qty, units, elem_name,
             elem_class, state_abbr, artifact_id, source_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(struct_norm, year, elem_num, cs) DO UPDATE SET
            cs_qty = excluded.cs_qty,
            total_qty = excluded.total_qty,
            units = excluded.units,
            elem_name = excluded.elem_name,
            elem_class = excluded.elem_class,
            state_abbr = excluded.state_abbr,
            artifact_id = excluded.artifact_id,
            source_path = excluded.source_path
        """,
        (struct_norm, year, elem_num, cs, cs_qty, total_qty, units, elem_name,
         elem_class, state_abbr, artifact_id, source_path),
    )
    label = elem_name or f"element {elem_num}"
    qty = "unreported quantity" if cs_qty is None else f"{cs_qty:g} {units or 'units'}"
    register_artifact(
        conn, artifact_id, "NBE",
        struct_norm=struct_norm, year=year,
        summary=f"NBE {year} {label} condition state {cs}: {qty}",
        source_path=source_path,
        source_locator=f"element {elem_num} cs{cs}",
    )


# ---------------------------------------------------------------------------
# Ingest bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class FileRun:
    """Open ingest_log entry for one source file."""

    log_id: int
    conn: sqlite3.Connection
    rows_read: int = 0
    rows_written: int = 0
    rows_rejected: int = 0

    def finish(self, status: str, message: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE ingest_log
               SET status = ?, rows_read = ?, rows_written = ?, rows_rejected = ?,
                   message = ?, finished_at = ?
             WHERE id = ?
            """,
            (status, self.rows_read, self.rows_written, self.rows_rejected,
             message, utcnow(), self.log_id),
        )
        self.conn.commit()


def already_ingested(conn: sqlite3.Connection, source_path: str, file_sha: str) -> bool:
    """True when this exact file has already been ingested to completion.

    Idempotency and resumability both rest on this: an interrupted run leaves a
    ``started`` row, which does not match, so the file is processed again.
    """
    row = conn.execute(
        "SELECT 1 FROM ingest_log WHERE source_path = ? AND file_sha256 = ? AND status = 'complete'",
        (source_path, file_sha),
    ).fetchone()
    return row is not None


def begin_file(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_path: str,
    file_sha: str | None,
    year: int | None = None,
    state_abbr: str | None = None,
) -> FileRun:
    cur = conn.execute(
        """
        INSERT INTO ingest_log
            (source, year, state_abbr, source_path, file_sha256, status, started_at)
        VALUES (?, ?, ?, ?, ?, 'started', ?)
        """,
        (source, year, state_abbr, source_path, file_sha, utcnow()),
    )
    conn.commit()
    return FileRun(log_id=int(cur.lastrowid), conn=conn)


def reject_row(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_path: str,
    source_line: int | None,
    reason: str,
    excerpt: str | None = None,
) -> None:
    """Record a row we could not use, with why. Nothing is dropped silently."""
    conn.execute(
        """
        INSERT INTO rejected_rows (source, source_path, source_line, reason, excerpt, rejected_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source, source_path, source_line, reason, (excerpt or "")[:300], utcnow()),
    )


def record_missing(
    conn: sqlite3.Connection, *, struct_norm: str, year: int, source: str, reason: str
) -> None:
    """Record that an expected source is absent for a structure and year."""
    conn.execute(
        """
        INSERT INTO missing_evidence (struct_norm, year, source, reason, detected_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(struct_norm, year, source) DO UPDATE SET
            reason = excluded.reason, detected_at = excluded.detected_at
        """,
        (struct_norm, year, source, reason, utcnow()),
    )

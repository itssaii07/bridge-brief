"""Coverage catalogue — what evidence exists, and what is missing.

Answers the milestone 1-2 gate question ("corpus size known") and feeds the
missing-evidence metric. Its most useful output is the *negative* one: how many
structures have an NBI rating but no element data, because those are the bridges
where the contradiction engine cannot run and the brief must say so rather than
imply the records agree.

Read-only with respect to source data. It writes only ``missing_evidence`` rows,
and only when explicitly asked to.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from .db import DataUnavailable, connect
from .ingest.nbe import EXPECTED_STATES
from .store import record_missing


@dataclass
class Coverage:
    """Corpus-level coverage figures for one year."""

    year: int
    structures: int = 0
    with_nbi: int = 0
    with_nbe: int = 0
    with_both: int = 0
    nbi_only: int = 0
    nbe_only: int = 0
    rated_components: int = 0
    null_ratings: int = 0
    element_rows: int = 0
    per_state: dict[str, dict[str, int]] = field(default_factory=dict)


def coverage_for_year(conn, year: int) -> Coverage:
    """Compute coverage for one year from the asset index."""
    cov = Coverage(year=year)

    cov.with_nbi = conn.execute(
        "SELECT COUNT(DISTINCT struct_norm) FROM ratings WHERE year = ?", (year,)
    ).fetchone()[0]
    cov.with_nbe = conn.execute(
        "SELECT COUNT(DISTINCT struct_norm) FROM elements WHERE year = ?", (year,)
    ).fetchone()[0]
    cov.with_both = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT struct_norm FROM ratings WHERE year = ?
            INTERSECT
            SELECT struct_norm FROM elements WHERE year = ?
        )
        """,
        (year, year),
    ).fetchone()[0]
    cov.nbi_only = cov.with_nbi - cov.with_both
    cov.nbe_only = cov.with_nbe - cov.with_both
    cov.structures = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT struct_norm FROM ratings WHERE year = ?
            UNION
            SELECT struct_norm FROM elements WHERE year = ?
        )
        """,
        (year, year),
    ).fetchone()[0]
    cov.rated_components = conn.execute(
        "SELECT COUNT(*) FROM ratings WHERE year = ? AND rating IS NOT NULL", (year,)
    ).fetchone()[0]
    cov.null_ratings = conn.execute(
        "SELECT COUNT(*) FROM ratings WHERE year = ? AND rating IS NULL", (year,)
    ).fetchone()[0]
    cov.element_rows = conn.execute(
        "SELECT COUNT(*) FROM elements WHERE year = ?", (year,)
    ).fetchone()[0]

    # Per-state breakdown, restricted to the NBE states where the join is
    # actually possible. This is the population the contradiction engine runs on.
    for state in EXPECTED_STATES:
        nbi = conn.execute(
            """
            SELECT COUNT(DISTINCT r.struct_norm)
              FROM ratings r JOIN structures s ON s.struct_norm = r.struct_norm
             WHERE r.year = ? AND s.state_abbr = ?
            """,
            (year, state),
        ).fetchone()[0]
        nbe = conn.execute(
            "SELECT COUNT(DISTINCT struct_norm) FROM elements WHERE year = ? AND state_abbr = ?",
            (year, state),
        ).fetchone()[0]
        both = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT r.struct_norm FROM ratings r
                  JOIN structures s ON s.struct_norm = r.struct_norm
                 WHERE r.year = ? AND s.state_abbr = ?
                INTERSECT
                SELECT struct_norm FROM elements WHERE year = ? AND state_abbr = ?
            )
            """,
            (year, state, year, state),
        ).fetchone()[0]
        cov.per_state[state] = {"nbi": nbi, "nbe": nbe, "both": both, "nbi_only": nbi - both}

    return cov


def ingest_summary(conn) -> list[dict]:
    """What has been ingested, per source and year, from ``ingest_log``."""
    rows = conn.execute(
        """
        SELECT source, year, status, COUNT(*) AS files,
               SUM(rows_read) AS rows_read, SUM(rows_written) AS rows_written,
               SUM(rows_rejected) AS rows_rejected
          FROM ingest_log
         GROUP BY source, year, status
         ORDER BY source, year, status
        """
    ).fetchall()
    return [dict(r) for r in rows]


def rejection_summary(conn, limit: int = 15) -> list[dict]:
    """The most common reasons rows were rejected. Never hidden."""
    rows = conn.execute(
        """
        SELECT source, reason, COUNT(*) AS n
          FROM rejected_rows GROUP BY source, reason
         ORDER BY n DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def struct_number_spelling_check(conn, limit: int = 10) -> list[dict]:
    """Structures whose normalised key came from more than one raw spelling.

    A large count here would mean the normalisation in
    :func:`src.ids.normalise_struct` is merging structures that should be
    distinct — the collision risk recorded in ASSUMPTIONS.md B3. This is how that
    risk becomes visible instead of silent.
    """
    rows = conn.execute(
        """
        SELECT a.struct_norm, COUNT(DISTINCT a.source_path) AS sources
          FROM artifacts a GROUP BY a.struct_norm
        HAVING sources > 1 ORDER BY sources DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def record_missing_nbe(conn, year: int, *, states: tuple[str, ...] = EXPECTED_STATES) -> int:
    """Record structures that have NBI ratings but no element data.

    Restricted to the states where NBE is published at all — flagging a
    Pennsylvania bridge as "missing NBE" would be meaningless, since no
    Pennsylvania bridge has it. Returns the number of rows recorded.
    """
    placeholders = ",".join("?" * len(states))
    rows = conn.execute(
        f"""
        SELECT DISTINCT r.struct_norm
          FROM ratings r JOIN structures s ON s.struct_norm = r.struct_norm
         WHERE r.year = ? AND s.state_abbr IN ({placeholders})
           AND NOT EXISTS (
               SELECT 1 FROM elements e
                WHERE e.struct_norm = r.struct_norm AND e.year = r.year
           )
        """,
        (year, *states),
    ).fetchall()
    for row in rows:
        record_missing(
            conn, struct_norm=row["struct_norm"], year=year, source="nbe",
            reason="structure is in an NBE-publishing state but has no element records for this year",
        )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    rule = "  ".join("-" * w for w in widths)
    body = ["  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) for row in rows]
    return "\n".join([line, rule, *body])


def render(conn, years: list[int]) -> str:
    """Render the full coverage report as text."""
    out: list[str] = ["=" * 72, "COVERAGE CATALOGUE", "=" * 72, ""]

    ingested = ingest_summary(conn)
    if not ingested:
        out += [
            "No source files have been ingested yet.",
            "",
            "The asset index exists but is empty. This is the expected state before",
            "the datasets are placed on disk. Nothing has been invented to fill it.",
            "",
            "Next step:  see HANDOFF.md",
            "",
        ]
        return "\n".join(out)

    out.append("Ingested files")
    out.append(_table(
        ["source", "year", "status", "files", "rows read", "written", "rejected"],
        [[r["source"], r["year"] or "-", r["status"], r["files"],
          f"{r['rows_read'] or 0:,}", f"{r['rows_written'] or 0:,}",
          f"{r['rows_rejected'] or 0:,}"] for r in ingested],
    ))
    out.append("")

    for year in years:
        cov = coverage_for_year(conn, year)
        if cov.structures == 0:
            out += [f"{year}: no structures ingested for this year.", ""]
            continue
        out.append(f"{year} corpus")
        out.append(_table(
            ["metric", "count"],
            [
                ["structures (any source)", f"{cov.structures:,}"],
                ["with NBI ratings", f"{cov.with_nbi:,}"],
                ["with NBE elements", f"{cov.with_nbe:,}"],
                ["with BOTH (engine-eligible)", f"{cov.with_both:,}"],
                ["NBI only (no element data)", f"{cov.nbi_only:,}"],
                ["NBE only (no NBI record)", f"{cov.nbe_only:,}"],
                ["component ratings present", f"{cov.rated_components:,}"],
                ["component ratings NULL / not applicable", f"{cov.null_ratings:,}"],
                ["element condition-state rows", f"{cov.element_rows:,}"],
            ],
        ))
        out.append("")
        out.append(f"{year} per-state breakdown (NBE-publishing states)")
        out.append(_table(
            ["state", "NBI structures", "NBE structures", "both", "NBI only"],
            [[s, f"{v['nbi']:,}", f"{v['nbe']:,}", f"{v['both']:,}", f"{v['nbi_only']:,}"]
             for s, v in cov.per_state.items()],
        ))
        out.append("")

    rejections = rejection_summary(conn)
    if rejections:
        out.append("Rejected rows by reason")
        out.append(_table(
            ["source", "reason", "count"],
            [[r["source"], r["reason"][:70], f"{r['n']:,}"] for r in rejections],
        ))
        out.append("")
    else:
        out += ["No rows were rejected.", ""]

    missing = conn.execute(
        "SELECT source, year, COUNT(*) AS n FROM missing_evidence GROUP BY source, year"
    ).fetchall()
    if missing:
        out.append("Recorded missing evidence")
        out.append(_table(["source", "year", "structures"],
                          [[m["source"], m["year"], f"{m['n']:,}"] for m in missing]))
        out.append("")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report evidence coverage per structure and state.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--year", type=int, action="append", default=None,
                        help="years to report; defaults to 2023 and 2025")
    parser.add_argument("--record-missing", action="store_true",
                        help="also write missing_evidence rows for NBI-only structures")
    args = parser.parse_args(argv)
    years = args.year or [2023, 2025]

    try:
        conn = connect(args.db, create=False)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        if args.record_missing:
            for year in years:
                n = record_missing_nbe(conn, year)
                print(f"recorded {n:,} structures missing NBE data for {year}")
        print(render(conn, years))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

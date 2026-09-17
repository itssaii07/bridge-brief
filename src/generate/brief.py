"""Brief assembly.

Pulls a structure's findings and imagery out of the asset index, hands them to a
drafter, runs the drafter's output through the grounding gate, and stores what
survives together with what did not.

The stored brief is always a **draft**. It becomes anything else only through a
recorded human sign-off (invariant 4); nothing in this module can set that status.
"""

from __future__ import annotations

import argparse
import sys

from .. import ids
from ..analysis.findings import load_findings
from ..db import DataUnavailable, connect, utcnow
from ..store import StructureNotFound, resolve_structure_key
from .drafters import SECTIONS, get_drafter
from .grounding import GateResult, apply_gate


def build_context(conn, struct_norm: str, year: int) -> dict:
    """Assemble everything known about a structure and year, for the drafter."""
    struct_norm = ids.normalise_struct(struct_norm)
    findings = load_findings(conn, struct_norm, year)

    # Only this structure's own imagery. Reference corpus rows have a NULL
    # struct_norm and so cannot be selected here — invariant 6 holds by the shape
    # of the query, not by a filter someone could forget.
    images = [dict(r) for r in conn.execute(
        "SELECT artifact_id, provenance, stored_path, photo_key FROM images "
        "WHERE struct_norm = ? ORDER BY artifact_id", (struct_norm,))]
    regions = [dict(r) for r in conn.execute(
        """
        SELECT r.* FROM image_regions r JOIN images i ON i.artifact_id = r.image_artifact
         WHERE i.struct_norm = ? AND r.source = 'detector'
         ORDER BY r.image_artifact, r.region_index
        """, (struct_norm,))]
    missing = [dict(r) for r in conn.execute(
        "SELECT source, year, reason FROM missing_evidence WHERE struct_norm = ? AND year = ?",
        (struct_norm, year))]

    # Artifacts a statement about an *absence* can legitimately cite: the records
    # that do exist for this structure and year.
    anchors = [r["artifact_id"] for r in conn.execute(
        "SELECT artifact_id FROM artifacts WHERE struct_norm = ? AND year = ? ORDER BY artifact_id",
        (struct_norm, year))]

    structure = conn.execute(
        "SELECT * FROM structures WHERE struct_norm = ?", (struct_norm,)).fetchone()

    return {
        "struct_norm": struct_norm, "year": year, "findings": findings,
        "images": images, "regions": regions, "missing": missing,
        "anchor_artifacts": anchors,
        "structure": dict(structure) if structure else None,
    }


def next_version(conn, struct_norm: str, year: int) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM briefs WHERE struct_norm = ? AND year = ?",
        (struct_norm, year)).fetchone()
    return (row["v"] or 0) + 1


def generate(conn, struct: str, year: int, *, drafter_name: str | None = None,
             log=print) -> dict:
    """Generate one brief version. Returns a summary including the gate counters.

    Raises:
        DataUnavailable: when there is nothing to write a brief from. An empty
            brief is not produced; the absence is reported.
    """
    struct_norm = ids.normalise_struct(struct)
    context = build_context(conn, struct_norm, year)
    if not context["findings"] and not context["images"]:
        raise DataUnavailable(
            f"No findings or imagery for structure {struct_norm} in {year}.\n"
            "Run ingest and the contradiction engine first:\n"
            f"  python -m src.ingest.nbi --year {year}\n"
            f"  python -m src.ingest.nbe --year {year}\n"
            f"  python -m src.analysis.contradictions --year {year}"
        )

    drafter = get_drafter(drafter_name)
    candidates = drafter.draft(context)
    result: GateResult = apply_gate(conn, candidates)

    version = next_version(conn, struct_norm, year)
    brief_id = ids.brief_id(struct_norm, year, version)
    conn.execute(
        """
        INSERT INTO briefs
            (brief_id, struct_norm, year, version, status, generated_at, generator,
             total_sentences, blocked_unsupported)
        VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)
        """,
        (brief_id, struct_norm, year, version, utcnow(), drafter.name,
         result.total, result.blocked_unsupported),
    )

    order = {section: index for index, section in enumerate(SECTIONS)}
    ordered = sorted(enumerate(result.passed),
                     key=lambda pair: (order.get(pair[1].section, 99), pair[0]))
    for ordinal, (_, sentence) in enumerate(ordered):
        sentence_id = ids.sentence_id(brief_id, ordinal)
        conn.execute(
            """
            INSERT INTO brief_sentences
                (sentence_id, brief_id, ordinal, section, text, finding_id, confidence, evidence_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sentence_id, brief_id, ordinal, sentence.section, sentence.text,
             sentence.finding_id, sentence.confidence, sentence.evidence_tier),
        )
        conn.executemany(
            "INSERT INTO sentence_citations (sentence_id, artifact_id) VALUES (?, ?)",
            [(sentence_id, artifact) for artifact in sentence.citations],
        )

    for blocked in result.blocked:
        conn.execute(
            "INSERT INTO blocked_sentences (brief_id, section, text, reason, blocked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (brief_id, blocked.section, blocked.text, blocked.reason, utcnow()),
        )
    conn.commit()

    summary = {
        "brief_id": brief_id, "struct_norm": struct_norm, "year": year, "version": version,
        "generator": drafter.name, "total_sentences": result.total,
        "rendered": len(result.passed), "blocked_unsupported": result.blocked_unsupported,
        "unsupported_rate": result.unsupported_rate,
        "unresolved_citations": result.unresolved_citations,
        "malformed_citations": result.malformed_citations,
        "status": "draft",
    }
    log(f"  [brief] {brief_id}: {len(result.passed)} sentence(s) rendered, "
        f"{result.blocked_unsupported} blocked as unsupported")
    return summary


def render_text(conn, brief_id: str) -> str:
    """Render a stored brief as plain text, counters included."""
    brief = conn.execute("SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)).fetchone()
    if brief is None:
        raise DataUnavailable(f"no brief with id {brief_id}")

    out = ["=" * 72,
           f"INSPECTION BRIEF (DRAFT) — structure {brief['struct_norm']}, {brief['year']}",
           f"{brief_id}  version {brief['version']}  status {brief['status']}",
           "=" * 72, "",
           "This is a draft assembled from published records. It is not a safety",
           "clearance, not a maintenance order, and carries no authority until a named",
           "human reviewer has signed it off.", ""]

    rate = (brief["blocked_unsupported"] / brief["total_sentences"]
            if brief["total_sentences"] else None)
    out += [f"Sentences generated        {brief['total_sentences']:>6}",
            f"Rendered                   {brief['total_sentences'] - brief['blocked_unsupported']:>6}",
            f"blocked_unsupported        {brief['blocked_unsupported']:>6}"
            + (f"   ({rate:.1%} of generated)" if rate is not None else ""),
            ""]

    rows = conn.execute(
        "SELECT * FROM brief_sentences WHERE brief_id = ? ORDER BY ordinal", (brief_id,)
    ).fetchall()
    if not rows:
        out += ["No sentence in this brief passed the grounding gate.", ""]

    current = None
    for row in rows:
        if row["section"] != current:
            current = row["section"]
            out += ["", current.replace("_", " ").upper(), "-" * len(current)]
        tier = row["evidence_tier"] or "-"
        confidence = f"{row['confidence']:.2f}" if row["confidence"] is not None else "-"
        out.append(f"  [{tier} | confidence {confidence}] {row['text']}")

    blocked = conn.execute(
        "SELECT section, reason, text FROM blocked_sentences WHERE brief_id = ? ORDER BY id",
        (brief_id,)).fetchall()
    if blocked:
        out += ["", "BLOCKED AS UNSUPPORTED", "-" * 22]
        for row in blocked:
            out.append(f"  ({row['reason']}) {row['text'][:110]}")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an evidence-linked inspection brief.")
    parser.add_argument("--structure", required=True,
                        help="state-qualified structure key (e.g. AL013450); a bare "
                             "structure number is accepted when only one state uses it")
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--drafter", default=None, help="'template' (default) or 'llm'")
    parser.add_argument("--db", default=None)
    parser.add_argument("--print", action="store_true", help="print the rendered brief")
    args = parser.parse_args(argv)

    try:
        conn = connect(args.db, create=False)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        structure = resolve_structure_key(conn, args.structure)
        summary = generate(conn, structure, args.year, drafter_name=args.drafter)
        if args.print:
            print(render_text(conn, summary["brief_id"]))
        else:
            print(summary)
    except (DataUnavailable, StructureNotFound) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

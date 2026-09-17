"""Human verification of what a brief actually says.

Two of the metrics the specification asks for cannot be computed by this system,
and must not be:

* **Factual fidelity** — is the statement true of the records it cites?
* **Source-link accuracy (the semantic half)** — does the cited artifact
  actually *support* the statement, or merely exist?

``source_link_resolution`` already measures the mechanical half and is 1.0 by
construction when the grounding gate works: every citation parses and resolves
to a real row. That is a necessary condition and not a sufficient one. A sentence
can cite a real artifact and still misdescribe it, and no amount of internal
checking can notice — the system would be grading its own homework against its
own claim.

So this module does what the contradiction-precision harness does: it exports a
deterministic sample with everything a human needs in order to judge, and reads
their labels back. It never labels anything itself.

    python -m src.eval.sentence_review --export reviews/sentences.csv --sample-size 40
    #  ... a human fills in `supported` and `factual` ...
    python -m src.eval.sentence_review --read reviews/sentences.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

from ..db import DataUnavailable, connect

#: Columns a reviewer fills in. Anything else in the file is context for them.
VERDICT_COLUMNS = ("supported (yes|no)", "factual (yes|no)")

HEADER = (
    "sentence_id", "brief_id", "structure", "year", "section", "evidence_tier",
    "confidence", "sentence", "cited_artifacts", "cited_summaries",
    *VERDICT_COLUMNS, "reviewer note",
)


def _sample_key(sentence_id: str) -> str:
    """Deterministic ordering key, independent of insertion order.

    A sample chosen by ``ORDER BY RANDOM()`` cannot be re-exported identically,
    so two reviewers would label different rows and the metric would not be
    reproducible. Hashing the sentence id gives a stable pseudo-random order.
    """
    return hashlib.sha256(sentence_id.encode("utf-8")).hexdigest()


def export_sample(conn, path: Path | str, *, n: int = 40,
                  brief_id: str | None = None) -> dict:
    """Write a deterministic sample of rendered sentences for a human to judge.

    Each row carries the sentence, its evidence tier and confidence, the artifact
    IDs it cites **and the summary of each of those artifacts**, so the reviewer
    can decide both questions without opening the database or the code.
    """
    where, params = "", []
    if brief_id:
        where, params = "WHERE s.brief_id = ?", [brief_id]
    rows = [dict(r) for r in conn.execute(
        f"""
        SELECT s.sentence_id, s.brief_id, s.section, s.text, s.evidence_tier,
               s.confidence, b.struct_norm, b.year
          FROM brief_sentences s
          JOIN briefs b ON b.brief_id = s.brief_id
          {where}
        """, params)]
    if not rows:
        raise DataUnavailable(
            "No rendered brief sentences to sample.\n"
            "Generate a brief first:\n"
            "  python -m src.generate.brief --structure <structure> --year 2023"
        )

    rows.sort(key=lambda r: _sample_key(r["sentence_id"]))
    chosen = rows[:n]
    for row in chosen:
        cited = [c[0] for c in conn.execute(
            "SELECT artifact_id FROM sentence_citations WHERE sentence_id = ? ORDER BY 1",
            (row["sentence_id"],))]
        row["cited"] = cited
        summaries = []
        for artifact_id in cited:
            found = conn.execute(
                "SELECT summary FROM artifacts WHERE artifact_id = ?",
                (artifact_id,)).fetchone()
            summaries.append(f"{artifact_id}: {found['summary'] if found else 'DOES NOT RESOLVE'}")
        row["summaries"] = summaries

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for row in chosen:
            writer.writerow([
                row["sentence_id"], row["brief_id"], row["struct_norm"], row["year"],
                row["section"], row["evidence_tier"],
                "" if row["confidence"] is None else f"{row['confidence']:.4f}",
                row["text"], " ".join(row["cited"]), " | ".join(row["summaries"]),
                "", "", "",
            ])
    return {"path": str(path), "sentences": len(chosen), "available": len(rows)}


def read_labels(path: Path | str) -> dict:
    """Read a labelled sample back and compute the two human-judged metrics.

    Unlabelled rows are counted and excluded, never assumed to be correct — an
    unreviewed sentence is not a passing sentence.
    """
    path = Path(path)
    if not path.exists():
        raise DataUnavailable(f"no review file at {path}")

    supported_yes = supported_no = factual_yes = factual_no = 0
    unlabelled = 0
    total = 0
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            total += 1
            supported = (row.get(VERDICT_COLUMNS[0]) or "").strip().lower()
            factual = (row.get(VERDICT_COLUMNS[1]) or "").strip().lower()
            if not supported and not factual:
                unlabelled += 1
                continue
            if supported in ("yes", "y", "true", "1"):
                supported_yes += 1
            elif supported in ("no", "n", "false", "0"):
                supported_no += 1
            if factual in ("yes", "y", "true", "1"):
                factual_yes += 1
            elif factual in ("no", "n", "false", "0"):
                factual_no += 1

    supported_total = supported_yes + supported_no
    factual_total = factual_yes + factual_no
    return {
        "rows": total,
        "unlabelled": unlabelled,
        "source_link_accuracy_semantic": (
            round(supported_yes / supported_total, 4) if supported_total else None),
        "source_link_judged": supported_total,
        "factual_fidelity": (
            round(factual_yes / factual_total, 4) if factual_total else None),
        "factual_judged": factual_total,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export brief sentences for human verification, or read labels back.")
    parser.add_argument("--export", default=None, help="write a sample to this path")
    parser.add_argument("--read", default=None, help="read labels back from this path")
    parser.add_argument("--sample-size", type=int, default=40)
    parser.add_argument("--brief", default=None, help="limit the sample to one brief")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    if not args.export and not args.read:
        parser.error("give --export or --read")

    if args.read:
        try:
            stats = read_labels(args.read)
        except DataUnavailable as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(stats, indent=2))
        if stats["unlabelled"]:
            print(f"\n{stats['unlabelled']} of {stats['rows']} row(s) are unlabelled "
                  "and excluded. An unreviewed sentence is not a passing sentence.")
        return 0

    try:
        conn = connect(args.db, create=False)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        result = export_sample(conn, args.export, n=args.sample_size,
                               brief_id=args.brief)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"wrote {result['sentences']} of {result['available']} sentence(s) to "
          f"{result['path']}")
    print("Fill in the 'supported' and 'factual' columns, then:")
    print(f"  python -m src.eval.sentence_review --read {result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

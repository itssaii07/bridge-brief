"""The evaluation harness: the full metrics table from CLAUDE.md, end to end.

Run it after ingest, the contradiction engine, the validation pass and at least
one brief. It reads the asset index and any report files you point it at, and
prints the table.

Metrics it cannot compute are printed as ``n/a`` with the reason beside them.
That is the whole design: the table is a status report on what has actually been
measured, not a grid of numbers with zeros standing in for gaps. On an empty
database every row reads ``n/a``, and that is the correct output.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ..db import DataUnavailable, connect
from .metrics import (
    Metric, contradiction_precision, corpus_metrics, correction_effort_metric,
    defect_detection, human_sentence_metrics, missing_evidence_recall,
    predictive_alignment,
    predictive_lift_by_direction,
    source_link_accuracy, unsupported_content_rate,
)


def collect(conn, *, base_year: int = 2023, check_year: int = 2025,
            review_path: Path | None = None,
            benchmark_path: Path | None = None,
            sentence_review_path: Path | None = None) -> list[Metric]:
    """Compute every metric in the table, in reporting order."""
    benchmark = None
    if benchmark_path and Path(benchmark_path).exists():
        benchmark = json.loads(Path(benchmark_path).read_text(encoding="utf-8"))

    metrics: list[Metric] = []
    metrics += corpus_metrics(conn, base_year)
    metrics.append(contradiction_precision(conn, review_path))
    metrics += predictive_alignment(conn, base_year, check_year)
    metrics += predictive_lift_by_direction(conn, base_year, check_year)
    metrics += defect_detection(conn, benchmark)
    metrics.append(source_link_accuracy(conn))
    metrics += human_sentence_metrics(conn, sentence_review_path)
    metrics.append(missing_evidence_recall(conn, base_year))
    metrics.append(unsupported_content_rate(conn))
    metrics.append(correction_effort_metric(conn))
    return metrics


def render(metrics: list[Metric], *, base_year: int, check_year: int) -> str:
    out = ["=" * 100,
           f"EVALUATION — all metrics computed from the asset index "
           f"({base_year} base, {check_year} check)",
           "=" * 100, ""]

    name_width = max(len(m.name) for m in metrics) + 2
    out.append(f"{'metric'.ljust(name_width)}{'value':>10}   {'ground truth'}")
    out.append("-" * 100)
    for metric in metrics:
        if metric.value is None:
            value = "n/a"
        elif isinstance(metric.value, int):
            value = f"{metric.value:,}"
        else:
            value = f"{metric.value:.4f}"
        out.append(f"{metric.name.ljust(name_width)}{value:>10}   {metric.ground_truth}")
        if metric.status and metric.status != "ok":
            out.append(f"{' ' * name_width}{'':>10}   -> {metric.status}")
    out.append("")

    unavailable = [m for m in metrics if not m.available]
    out.append(f"{len(metrics) - len(unavailable)} of {len(metrics)} metrics computed.")
    if unavailable:
        out += ["", "Not computed (each line says why above):"]
        out += [f"  - {m.name}" for m in unavailable]
    out += ["",
            "Notes",
            "-----",
            "* Contradiction precision and the semantic half of source-link accuracy require a",
            "  human verdict. The harness exports samples and reads labels back; it does not",
            "  score itself.",
            "* Predictive alignment is meaningless without the control base rate printed",
            "  beside it. Read predictive_lift, not predictive_alignment alone.",
            "* source_link_resolution is 1.0 by construction when the grounding gate is",
            "  working. A value below 1.0 is a bug report.",
            "* No metric here, and no output of this system, constitutes a safety clearance",
            "  or a maintenance authorisation.",
            ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Produce the full evaluation metrics table.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--base-year", type=int, default=2023)
    parser.add_argument("--check-year", type=int, default=2025)
    parser.add_argument("--review", default=None,
                        help="labelled contradiction review CSV, for precision")
    parser.add_argument("--benchmark", default=None,
                        help="JSON written by src.eval.codebrim_benchmark --json")
    parser.add_argument("--sentence-review", default=None,
                        help="labelled sentence CSV from src.eval.sentence_review, "
                             "for factual fidelity and semantic source-link accuracy")
    parser.add_argument("--json", default=None, help="also write the table as JSON")
    args = parser.parse_args(argv)

    try:
        conn = connect(args.db, create=False)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        metrics = collect(
            conn, base_year=args.base_year, check_year=args.check_year,
            review_path=Path(args.review) if args.review else None,
            benchmark_path=Path(args.benchmark) if args.benchmark else None,
            sentence_review_path=(Path(args.sentence_review)
                                  if args.sentence_review else None),
        )
        print(render(metrics, base_year=args.base_year, check_year=args.check_year))
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(
                json.dumps([asdict(m) for m in metrics], indent=2), encoding="utf-8")
            print(f"wrote {args.json}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

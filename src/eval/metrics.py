"""Metric computation for the evaluation table in CLAUDE.md.

Every metric here is computed from what is actually in the asset index. Where a
metric cannot be computed — because the data is absent, or because it requires a
human judgement nobody has made yet — the value is ``None`` and a ``status``
explains why. Nothing is defaulted to zero, and nothing is estimated.

That distinction matters: a missing metric and a metric that measured zero look
identical if you let them, and the second is a result while the first is a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..analysis.validate import read_review_labels, validate
from ..db import DataUnavailable
from ..ui.review import correction_effort


@dataclass
class Metric:
    """One row of the metrics table."""

    name: str
    value: float | int | None
    ground_truth: str
    status: str = "ok"
    detail: dict = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.value is not None


def contradiction_precision(conn, review_path: Path | None) -> Metric:
    """Precision of the contradiction flags, from human labels only."""
    name, truth = "contradiction_precision", "manual review of sampled flags"
    if review_path is None or not Path(review_path).exists():
        return Metric(name, None, truth, status=(
            "no labelled review file; export a sample with "
            "`python -m src.analysis.validate --export-review reviews/sample.csv`, "
            "label it, then pass --review to this harness"))
    stats = read_review_labels(Path(review_path))
    if stats["contradiction_precision"] is None:
        return Metric(name, None, truth, status="review file exists but nothing is labelled yet",
                      detail=stats)
    return Metric(name, stats["contradiction_precision"], truth, detail=stats)


def predictive_alignment(conn, base_year: int, check_year: int) -> list[Metric]:
    """Predictive alignment, its control base rate, and the lift between them."""
    truth = f"{check_year} NBI rating change on {base_year}-flagged bridges"
    result = validate(conn, base_year=base_year, check_year=check_year)

    if result.flagged_total == 0:
        status = (f"no contradictions stored for {base_year}; run "
                  f"`python -m src.analysis.contradictions --year {base_year}`")
        return [Metric("predictive_alignment", None, truth, status=status),
                Metric("control_base_rate_direction_matched", None, truth, status=status),
                Metric("predictive_lift", None, truth, status=status)]

    detail = {
        "flagged_total": result.flagged_total,
        "flagged_evaluable": result.flagged_evaluable,
        "flagged_confirmed": result.flagged_confirmed,
        "flagged_unevaluable_excluded": result.flagged_unevaluable,
        "control_evaluable": result.control_evaluable,
        "control_dropped": result.control_dropped,
        "control_drop_rate": result.control_drop_rate,
        "control_rose": result.control_rose,
        "control_rise_rate": result.control_rise_rate,
        "expected_confirmations": result.expected_confirmations,
        "by_direction": result.direction_stats(),
    }
    unevaluable = (f"{result.flagged_unevaluable:,} flagged component(s) had no comparable "
                   f"{check_year} rating and are excluded from the denominator")
    return [
        Metric("predictive_alignment", result.predictive_alignment, truth,
               status=unevaluable if result.flagged_unevaluable else "ok", detail=detail),
        # The base rate has to match the movement each direction predicts: an
        # NBI-optimistic flag is confirmed by a downgrade, an NBI-pessimistic one
        # by an upgrade, and on real records those rates differ by an order of
        # magnitude (5.9% vs 0.6% in 2023). Reporting the drop rate alone here
        # left a base-rate row that did not subtract to the lift beside it.
        Metric("control_base_rate_direction_matched", result.pooled_base_rate,
               f"unflagged components with both sources in {base_year}, weighted by "
               "this flag set's mix of directions",
               status=("ok" if result.control_evaluable
                       else "no evaluable control population"),
               detail=detail),
        Metric("predictive_lift", result.lift,
               "predictive alignment minus the direction-matched control base rate",
               status=("ok" if result.lift is None or result.lift > 0
                       else "AT OR BELOW ZERO: the flags carry no predictive information "
                            "on this data"),
               detail=detail),
    ]


def predictive_lift_by_direction(conn, base_year: int, check_year: int) -> list[Metric]:
    """Per-direction lift, each against the base rate for its own movement.

    The pooled figure is dominated by whichever direction has more flags. In the
    2023 data that is nbi_pessimistic at 81% of findings, which pulled the
    headline down and hid that nbi_optimistic flags were lifting 6.7 points over
    their own base rate. A significance figure accompanies each, so a small lift
    on few flags is not mistaken for a result.
    """
    result = validate(conn, base_year=base_year, check_year=check_year)
    stats = result.direction_stats()
    if not stats:
        status = (f"no contradictions stored for {base_year}; run "
                  f"`python -m src.analysis.contradictions --year {base_year}`")
        return [Metric("predictive_lift_by_direction", None,
                       "2025 rating change, split by flag direction", status=status)]

    out: list[Metric] = []
    for direction, bucket in stats.items():
        info = bucket.get("significance") or {}
        p = info.get("p_value")
        note = "ok"
        if p is not None:
            note = (f"{bucket['confirmed']:,} of {bucket['evaluable']:,} evaluable; "
                    f"{bucket['base_rate']:.1%} base rate; "
                    f"{info.get('risk_ratio')}x, z={info.get('z')}, "
                    f"p{'<1e-12' if p < 1e-12 else f'={p:.2g}'}")
            if p > 0.05:
                note = "NOT SIGNIFICANT at p=0.05 — " + note
        out.append(Metric(f"predictive_lift_{direction}", bucket["lift"],
                          f"{check_year} rating change on {base_year} {direction} flags",
                          status=note, detail=bucket))
    return out


def defect_detection(conn, benchmark: dict | None) -> list[Metric]:
    """Detector precision/recall, from a benchmark report if one has been run."""
    truth = "CODEBRIM annotations"
    if not benchmark:
        status = ("no benchmark has been run; run "
                  "`python -m src.eval.codebrim_benchmark --json reports/codebrim.json`")
        return [Metric("defect_detection_precision", None, truth, status=status),
                Metric("defect_detection_recall", None, truth, status=status),
                Metric("defect_detection_f1", None, truth, status=status)]
    agnostic = benchmark.get("class_agnostic", {})
    note = ("class-agnostic (localisation only); the shipped baseline is not a trained "
            "model and its class-aware score is zero by construction")
    return [
        Metric("defect_detection_precision", agnostic.get("precision"), truth,
               status=note, detail=benchmark.get("class_aware", {})),
        Metric("defect_detection_recall", agnostic.get("recall"), truth, status=note),
        Metric("defect_detection_f1", agnostic.get("f1"), truth, status=note),
    ]


def source_link_accuracy(conn) -> Metric:
    """Share of rendered citations that resolve to a stored artifact.

    This is the *mechanical* half of source-link accuracy and it should be 1.0 by
    construction, because the grounding gate drops anything that does not resolve.
    A value below 1.0 means the gate has been bypassed somewhere, which is a bug
    report, not a measurement. Whether a resolving citation actually *supports*
    its sentence still requires the manual verification in CLAUDE.md.
    """
    total = conn.execute("SELECT COUNT(*) FROM sentence_citations").fetchone()[0]
    if total == 0:
        return Metric("source_link_resolution", None,
                      "artifacts table (mechanical half; semantic half is manual)",
                      status="no brief has been generated yet")
    resolved = conn.execute(
        "SELECT COUNT(*) FROM sentence_citations c "
        "JOIN artifacts a ON a.artifact_id = c.artifact_id").fetchone()[0]
    value = round(resolved / total, 4)
    return Metric(
        "source_link_resolution", value,
        "artifacts table (mechanical half; semantic half is manual)",
        status=("ok" if value == 1.0 else
                "BELOW 1.0 — a citation was rendered that does not resolve; the grounding "
                "gate has been bypassed somewhere and this is a bug, not a measurement"),
        detail={"citations": total, "resolved": resolved},
    )


def missing_evidence_recall(conn, year: int) -> Metric:
    """Share of NBE-state structures genuinely lacking element data that we recorded.

    Ground truth is computable here: a structure in an NBE-publishing state with
    NBI ratings and no element rows genuinely lacks element data. The metric asks
    whether the system *noticed* — that is, whether ``missing_evidence`` has the
    row — rather than whether the gap exists.
    """
    from ..ingest.nbe import EXPECTED_STATES

    placeholders = ",".join("?" * len(EXPECTED_STATES))
    truth = "structures genuinely lacking NBE records"
    actual = conn.execute(
        f"""
        SELECT COUNT(DISTINCT r.struct_norm) FROM ratings r
          JOIN structures s ON s.struct_norm = r.struct_norm
         WHERE r.year = ? AND s.state_abbr IN ({placeholders})
           AND NOT EXISTS (SELECT 1 FROM elements e
                            WHERE e.struct_norm = r.struct_norm AND e.year = r.year)
        """,
        (year, *EXPECTED_STATES),
    ).fetchone()[0]
    if actual == 0:
        return Metric("missing_evidence_recall", None, truth,
                      status=f"no structure in {', '.join(EXPECTED_STATES)} is missing NBE "
                             f"data for {year} (or nothing has been ingested)")
    recorded = conn.execute(
        "SELECT COUNT(*) FROM missing_evidence WHERE year = ? AND source = 'nbe'", (year,)
    ).fetchone()[0]
    return Metric("missing_evidence_recall", round(min(recorded, actual) / actual, 4), truth,
                  status="ok" if recorded else
                         "no gaps recorded; run `python -m src.catalog --record-missing`",
                  detail={"gaps_present": actual, "gaps_recorded": recorded})


def unsupported_content_rate(conn) -> Metric:
    """``blocked_unsupported`` over total generated sentences, across all briefs."""
    row = conn.execute(
        "SELECT COALESCE(SUM(total_sentences), 0) AS total, "
        "COALESCE(SUM(blocked_unsupported), 0) AS blocked FROM briefs").fetchone()
    truth = "blocked_unsupported / total generated sentences"
    if not row["total"]:
        return Metric("unsupported_content_rate", None, truth,
                      status="no brief has been generated yet")
    return Metric("unsupported_content_rate", round(row["blocked"] / row["total"], 4), truth,
                  detail={"total_sentences": row["total"], "blocked": row["blocked"]})


def correction_effort_metric(conn) -> Metric:
    """Edits per reviewed finding, from the append-only review trail."""
    stats = correction_effort(conn)
    truth = "review actions recorded in the UI"
    if stats["edits_per_finding"] is None:
        return Metric("correction_effort_edits_per_finding", None, truth,
                      status="no finding has been reviewed yet", detail=stats)
    return Metric("correction_effort_edits_per_finding", stats["edits_per_finding"], truth,
                  detail=stats)


def corpus_metrics(conn, year: int) -> list[Metric]:
    """Corpus-size figures. Context for every other number in the table."""
    from ..catalog import coverage_for_year

    cov = coverage_for_year(conn, year)
    return [
        Metric(f"corpus_structures_{year}", cov.structures or None, "asset index",
               status="ok" if cov.structures else f"nothing ingested for {year}"),
        Metric(f"corpus_engine_eligible_{year}", cov.with_both or None,
               "structures with both NBI and NBE",
               status="ok" if cov.with_both else
                      f"no structure has both sources for {year}"),
    ]

"""Validation pass: did the bridges we flagged in 2023 get downgraded in 2025?

This is the project's one real predictive result, and it needs no constructed
ground truth. The engine flags, in the 2023 records, structures where the NBI
rating and the NBE element data disagree. Two years later FHWA publishes the 2025
ratings. If a flagged component's rating subsequently dropped, the element data
was seeing something the rating had not yet caught up with.

Three things make this an honest measurement rather than a flattering one:

* **A control group.** The same drop rate is computed over structures that had
  both sources in 2023 and were *not* flagged. Predictive alignment without a
  base rate is not a result: if flagged and unflagged bridges drop at the same
  rate, the engine has found nothing, and this module says so in as many words.
* **Unevaluable cases are excluded and counted, not scored.** A structure absent
  from the 2025 file, or with a NULL rating in either year, cannot be evaluated.
  Counting it as a miss would understate the result by an unknown amount;
  counting it as a hit would be worse. It is reported separately.
* **Contradiction precision is not computed here.** Whether a flag is *correct*
  is a question about the records, and the answer requires a person. This module
  exports a review sample for a human to label and reads the labels back — it
  does not score itself.

The direction of a flag matters to what counts as confirmation. For an
NBI-optimistic flag (elements worse than the rating), confirmation is a *drop*.
For an NBI-pessimistic flag (rating worse than the elements), confirmation is a
*rise*. Scoring both as "drop" would count the second kind's successes as
failures.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..db import DataUnavailable, connect

BASE_YEAR = 2023
CHECK_YEAR = 2025


@dataclass
class Outcome:
    """What happened to one flagged component between the two years."""

    struct_norm: str
    component: str
    direction: str
    severity: float | None
    confidence: float
    base_rating: int
    check_rating: int | None
    delta: int | None = None
    status: str = "unevaluable"   # confirmed | unchanged | contrary | unevaluable
    reason: str | None = None


@dataclass
class ValidationResult:
    """The full validation outcome. Every count is a real count or a None."""

    base_year: int
    check_year: int
    flagged_total: int = 0
    flagged_evaluable: int = 0
    flagged_confirmed: int = 0
    flagged_unchanged: int = 0
    flagged_contrary: int = 0
    flagged_unevaluable: int = 0
    control_total: int = 0
    control_evaluable: int = 0
    control_dropped: int = 0
    #: Unflagged components whose rating *rose*. An NBI-pessimistic flag is
    #: confirmed by a rise, so a drop rate is the wrong comparator for it.
    control_rose: int = 0
    by_direction: dict[str, dict[str, int]] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def predictive_alignment(self) -> float | None:
        """Share of evaluable flagged components whose rating moved as predicted."""
        if not self.flagged_evaluable:
            return None
        return round(self.flagged_confirmed / self.flagged_evaluable, 4)

    @property
    def control_drop_rate(self) -> float | None:
        """Base rate for a *downgrade*: the comparator for NBI-optimistic flags."""
        if not self.control_evaluable:
            return None
        return round(self.control_dropped / self.control_evaluable, 4)

    @property
    def control_rise_rate(self) -> float | None:
        """Base rate for an *upgrade*: the comparator for NBI-pessimistic flags."""
        if not self.control_evaluable:
            return None
        return round(self.control_rose / self.control_evaluable, 4)

    #: Which control movement confirms each flag direction.
    CONTROL_FOR_DIRECTION = {"nbi_optimistic": "control_drop_rate",
                             "nbi_pessimistic": "control_rise_rate"}

    def base_rate_for(self, direction: str) -> float | None:
        """The base rate for the movement this direction actually predicts.

        This distinction is not cosmetic. An NBI-optimistic flag predicts a
        downgrade and an NBI-pessimistic flag predicts an upgrade, and on real
        records those two movements have very different base rates. Scoring both
        against the drop rate — as this harness first did — compares the majority
        of flags against the base rate for the opposite movement, and the pooled
        headline lift then reports mostly that artefact.
        """
        attribute = self.CONTROL_FOR_DIRECTION.get(direction)
        return getattr(self, attribute) if attribute else None

    @staticmethod
    def two_proportion_z(confirmed: int, evaluable: int,
                         control_moved: int, control_evaluable: int) -> dict | None:
        """Is the difference between two rates larger than sampling noise?

        A lift reported without this is an uncaveated number: on a few hundred
        flags a couple of points of lift is not distinguishable from chance. The
        standard two-proportion z-test, computed with ``math`` alone so the
        project keeps its stdlib-only runtime.

        Returns the z statistic, a two-sided p-value from the normal
        approximation, and the risk ratio. ``None`` when either group is empty.
        """
        if evaluable <= 0 or control_evaluable <= 0:
            return None
        p1 = confirmed / evaluable
        p2 = control_moved / control_evaluable
        pooled = (confirmed + control_moved) / (evaluable + control_evaluable)
        if pooled in (0.0, 1.0):
            return None
        se = math.sqrt(pooled * (1 - pooled) * (1 / evaluable + 1 / control_evaluable))
        if se == 0:
            return None
        z = (p1 - p2) / se
        return {
            "z": round(z, 2),
            # erfc is the complement of the error function: this is the exact
            # two-sided tail of the normal, with no dependency on scipy.
            "p_value": math.erfc(abs(z) / math.sqrt(2)),
            "risk_ratio": round(p1 / p2, 2) if p2 else None,
        }

    def control_moved_for(self, direction: str) -> int | None:
        """The control count for the movement this direction predicts."""
        if direction == "nbi_optimistic":
            return self.control_dropped
        if direction == "nbi_pessimistic":
            return self.control_rose
        return None

    def direction_stats(self) -> dict[str, dict]:
        """Per-direction alignment and lift, each against its matching base rate."""
        out: dict[str, dict] = {}
        for direction, bucket in sorted(self.by_direction.items()):
            evaluable = bucket["total"] - bucket["unevaluable"]
            base = self.base_rate_for(direction)
            alignment = round(bucket["confirmed"] / evaluable, 4) if evaluable else None
            out[direction] = {
                **bucket,
                "evaluable": evaluable,
                "alignment": alignment,
                "base_rate": base,
                "lift": (None if alignment is None or base is None
                         else round(alignment - base, 4)),
                "significance": self.two_proportion_z(
                    bucket["confirmed"], evaluable,
                    self.control_moved_for(direction) or 0, self.control_evaluable),
            }
        return out

    def _expected_confirmations_exact(self) -> float | None:
        """How many confirmations the control rates predict for this flag mix.

        The pooled comparator. Each direction contributes its own evaluable count
        times its own base rate, so a flag set dominated by one direction is not
        scored against the other direction's base rate.

        Unrounded: :attr:`pooled_base_rate` divides by it, and rounding here
        before dividing moves the headline lift.
        """
        if not self.control_evaluable or not self.by_direction:
            return None
        total = 0.0
        for direction, bucket in self.by_direction.items():
            base = self.base_rate_for(direction)
            if base is None:
                return None
            total += (bucket["total"] - bucket["unevaluable"]) * base
        return total

    @property
    def expected_confirmations(self) -> float | None:
        """:meth:`_expected_confirmations_exact`, rounded for display."""
        exact = self._expected_confirmations_exact()
        return None if exact is None else round(exact, 1)

    @property
    def pooled_base_rate(self) -> float | None:
        """The flag-mix-weighted control rate the pooled alignment is measured against."""
        exact = self._expected_confirmations_exact()
        if exact is None or not self.flagged_evaluable:
            return None
        return round(exact / self.flagged_evaluable, 4)

    @property
    def lift(self) -> float | None:
        """Predictive alignment minus the direction-matched base rate.

        The number that actually matters. At or below zero, the engine's flags
        carry no predictive information and the correct report is that they do
        not.
        """
        alignment, base = self.predictive_alignment, self.pooled_base_rate
        if alignment is None or base is None:
            return None
        return round(alignment - base, 4)


def _ratings_for_year(conn, year: int) -> dict[tuple[str, str], int]:
    """``(struct, component) -> rating`` for every non-NULL rating in a year."""
    return {
        (row["struct_norm"], row["component"]): row["rating"]
        for row in conn.execute(
            "SELECT struct_norm, component, rating FROM ratings WHERE year = ? AND rating IS NOT NULL",
            (year,),
        )
    }


def classify_outcome(direction: str, base_rating: int, check_rating: int | None) -> tuple[str, int | None, str | None]:
    """Decide whether a flag was borne out. Pure function.

    Returns ``(status, delta, reason)``. ``delta`` is
    ``check_rating - base_rating``; a negative delta is a downgrade.
    """
    if check_rating is None:
        return "unevaluable", None, "no comparable rating in the check year"
    delta = int(check_rating) - int(base_rating)

    if direction == "nbi_optimistic":
        # We said the rating was too generous. Confirmation is a downgrade.
        if delta < 0:
            return "confirmed", delta, None
        if delta == 0:
            return "unchanged", delta, None
        return "contrary", delta, "rating rose after we flagged it as too generous"

    if direction == "nbi_pessimistic":
        # We said the rating was harsher than the elements. Confirmation is a rise.
        if delta > 0:
            return "confirmed", delta, None
        if delta == 0:
            return "unchanged", delta, None
        return "contrary", delta, "rating fell further after we flagged it as too harsh"

    return "unevaluable", delta, f"unknown flag direction {direction!r}"


def validate(conn, *, base_year: int = BASE_YEAR, check_year: int = CHECK_YEAR) -> ValidationResult:
    """Run the validation pass over stored contradictions."""
    result = ValidationResult(base_year=base_year, check_year=check_year)
    check_ratings = _ratings_for_year(conn, check_year)

    flagged: set[tuple[str, str]] = set()
    rows = conn.execute(
        """
        SELECT f.struct_norm, f.component, f.severity, f.confidence,
               json_extract(f.detail_json, '$.direction') AS direction,
               json_extract(f.detail_json, '$.nbi_rating') AS base_rating
          FROM findings f
         WHERE f.kind = 'contradiction' AND f.year = ?
         ORDER BY f.struct_norm, f.component
        """,
        (base_year,),
    ).fetchall()

    for row in rows:
        key = (row["struct_norm"], row["component"])
        flagged.add(key)
        result.flagged_total += 1
        status, delta, reason = classify_outcome(
            row["direction"], row["base_rating"], check_ratings.get(key)
        )
        outcome = Outcome(
            struct_norm=row["struct_norm"], component=row["component"],
            direction=row["direction"], severity=row["severity"], confidence=row["confidence"],
            base_rating=row["base_rating"], check_rating=check_ratings.get(key),
            delta=delta, status=status, reason=reason,
        )
        result.outcomes.append(outcome)

        bucket = result.by_direction.setdefault(
            row["direction"], {"total": 0, "confirmed": 0, "unchanged": 0,
                               "contrary": 0, "unevaluable": 0})
        bucket["total"] += 1
        bucket[status] += 1

        if status == "unevaluable":
            result.flagged_unevaluable += 1
        else:
            result.flagged_evaluable += 1
            if status == "confirmed":
                result.flagged_confirmed += 1
            elif status == "unchanged":
                result.flagged_unchanged += 1
            else:
                result.flagged_contrary += 1

    # Control group: components that had both sources in the base year — so the
    # engine could have flagged them — and did not get flagged.
    control_rows = conn.execute(
        """
        SELECT r.struct_norm, r.component, r.rating
          FROM ratings r
         WHERE r.year = ? AND r.rating IS NOT NULL
           AND EXISTS (SELECT 1 FROM elements e
                        WHERE e.struct_norm = r.struct_norm AND e.year = r.year)
        """,
        (base_year,),
    ).fetchall()

    for row in control_rows:
        key = (row["struct_norm"], row["component"])
        if key in flagged:
            continue
        result.control_total += 1
        later = check_ratings.get(key)
        if later is None:
            continue
        result.control_evaluable += 1
        if later < row["rating"]:
            result.control_dropped += 1
        elif later > row["rating"]:
            result.control_rose += 1

    return result


# ---------------------------------------------------------------------------
# Contradiction precision — the part that requires a person
# ---------------------------------------------------------------------------


def export_review_sample(conn, path: Path, *, year: int = BASE_YEAR, n: int = 50,
                         seed: int = 20230101) -> int:
    """Write a deterministic sample of flagged contradictions for human labelling.

    The sample is drawn deterministically from the finding IDs, so the same
    database yields the same sample and a precision figure can be reproduced.
    The output CSV has an empty ``verdict`` column for a reviewer to fill with
    ``correct`` or ``incorrect``; :func:`read_review_labels` reads it back.
    """
    rows = conn.execute(
        """
        SELECT finding_id, struct_norm, component, severity, confidence, detail_json
          FROM findings WHERE kind = 'contradiction' AND year = ?
         ORDER BY substr(finding_id, -8), finding_id
         LIMIT ?
        """,
        (year, n),
    ).fetchall()
    if not rows:
        raise DataUnavailable(
            f"No contradictions stored for {year}. Run the contradiction engine first:\n"
            f"  python -m src.analysis.contradictions --year {year}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "finding_id", "structure", "component", "severity", "confidence",
            "nbi_rating", "deteriorated_qty", "total_qty", "units", "direction",
            "evidence", "verdict (correct|incorrect)", "reviewer note",
        ])
        for row in rows:
            detail = json.loads(row["detail_json"])
            evidence = [
                r["artifact_id"] for r in conn.execute(
                    "SELECT artifact_id FROM finding_evidence WHERE finding_id = ? "
                    "AND role = 'contradicts' ORDER BY artifact_id", (row["finding_id"],))
            ]
            writer.writerow([
                row["finding_id"], row["struct_norm"], row["component"],
                row["severity"], row["confidence"], detail.get("nbi_rating"),
                detail.get("deteriorated_qty"), detail.get("total_qty"),
                detail.get("units"), detail.get("direction"),
                " ".join(evidence), "", "",
            ])
    return len(rows)


def read_review_labels(path: Path) -> dict:
    """Read a labelled review file back and compute contradiction precision.

    Unlabelled rows are counted as unlabelled, never assumed correct.
    """
    if not path.exists():
        raise DataUnavailable(
            f"No review file at {path}.\n"
            "Export one with:  python -m src.analysis.validate --export-review <path>\n"
            "then fill in the verdict column and re-run."
        )
    correct = incorrect = unlabelled = 0
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            verdict = (row.get("verdict (correct|incorrect)") or "").strip().lower()
            if verdict == "correct":
                correct += 1
            elif verdict == "incorrect":
                incorrect += 1
            else:
                unlabelled += 1
    labelled = correct + incorrect
    return {
        "labelled": labelled, "correct": correct, "incorrect": incorrect,
        "unlabelled": unlabelled,
        "contradiction_precision": round(correct / labelled, 4) if labelled else None,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render(result: ValidationResult) -> str:
    out = ["=" * 72,
           f"VALIDATION PASS: {result.base_year} contradictions checked against "
           f"{result.check_year} ratings",
           "=" * 72, ""]

    if result.flagged_total == 0:
        out += [
            f"No contradictions are stored for {result.base_year}, so there is nothing to validate.",
            "",
            "Run in order:",
            f"  python -m src.ingest.nbi  --year {result.base_year} --year {result.check_year}",
            f"  python -m src.ingest.nbe  --year {result.base_year}",
            f"  python -m src.analysis.contradictions --year {result.base_year}",
            "",
        ]
        return "\n".join(out)

    pct = lambda v: "n/a" if v is None else f"{v:.1%}"
    out += [
        f"Flagged components ({result.base_year})       {result.flagged_total:>10,}",
        f"  evaluable in {result.check_year}                {result.flagged_evaluable:>10,}",
        f"  confirmed (moved as predicted)  {result.flagged_confirmed:>10,}",
        f"  unchanged                       {result.flagged_unchanged:>10,}",
        f"  contrary (moved the other way)  {result.flagged_contrary:>10,}",
        f"  unevaluable (excluded)          {result.flagged_unevaluable:>10,}",
        "",
        f"Control: unflagged components with both sources in {result.base_year}",
        f"  evaluable                       {result.control_evaluable:>10,}",
        f"  dropped by {result.check_year}                  {result.control_dropped:>10,}"
        f"   ({pct(result.control_drop_rate)})",
        f"  rose by {result.check_year}                     {result.control_rose:>10,}"
        f"   ({pct(result.control_rise_rate)})",
        "",
        "An NBI-optimistic flag predicts a downgrade and an NBI-pessimistic flag predicts",
        "an upgrade, so each direction is scored against the base rate for its own",
        "movement. The pooled base rate below is those two rates weighted by this flag",
        "set's actual mix of directions.",
        "",
        f"Predictive alignment (flagged)    {pct(result.predictive_alignment):>10}"
        f"   ({result.flagged_confirmed:,} confirmed)",
        f"Base rate (direction-matched)     {pct(result.pooled_base_rate):>10}"
        f"   ({result.expected_confirmations:,} expected)"
        if result.expected_confirmations is not None else
        f"Base rate (direction-matched)     {'n/a':>10}",
        f"Lift                              {pct(result.lift):>10}",
        "",
    ]

    stats = result.direction_stats()
    if stats:
        def sig(bucket):
            info = bucket.get("significance")
            if not info:
                return f"{'n/a':>8}{'':>11}"
            p = info["p_value"]
            shown = "<1e-12" if p < 1e-12 else f"{p:.2g}"
            ratio = "n/a" if info["risk_ratio"] is None else f"{info['risk_ratio']}x"
            return f"{ratio:>8}{'z=' + str(info['z']) + ' p' + shown:>20}"

        out.append("By flag direction, each against its own base rate")
        out.append(f"{'direction':<18}{'total':>7}{'confirmed':>11}{'unchanged':>11}"
                   f"{'contrary':>10}{'uneval':>8}{'align':>8}{'base':>8}{'lift':>8}"
                   f"{'ratio':>8}{'significance':>20}")
        out.append("-" * 117)
        for direction, bucket in stats.items():
            out.append(f"{direction:<18}{bucket['total']:>7,}{bucket['confirmed']:>11,}"
                       f"{bucket['unchanged']:>11,}{bucket['contrary']:>10,}"
                       f"{bucket['unevaluable']:>8,}{pct(bucket['alignment']):>8}"
                       f"{pct(bucket['base_rate']):>8}{pct(bucket['lift']):>8}"
                       + sig(bucket))
        out.append("")
        out.append("`ratio` is how many times more often a flagged component moved as")
        out.append("predicted than an unflagged one. A two-proportion z-test is shown so a")
        out.append("small lift on few flags is not read as a result; p is two-sided.")
        out.append("")
        strongest = max(
            (b for b in stats.values() if b["lift"] is not None),
            key=lambda b: b["lift"], default=None)
        if strongest is not None and strongest["lift"] > 0:
            best = [d for d, b in stats.items() if b is strongest][0]
            out.append(
                f"Read the per-direction rows before the pooled one: {best} flags "
                f"lift {pct(strongest['lift'])} over their own base rate. A pooled "
                "figure hides that when one direction dominates the mix."
            )
            out.append("")

    lift = result.lift
    if lift is None:
        out.append("INTERPRETATION: not enough evaluable data to compare against a base rate.")
    elif lift <= 0:
        out.append(
            "INTERPRETATION: flagged components did NOT move as predicted any more often\n"
            "than unflagged ones. On this data the flags carry no predictive information.\n"
            "That is the result; it should be reported as it stands."
        )
    else:
        out.append(
            f"INTERPRETATION: flagged components moved as predicted {pct(lift)} more often\n"
            "than the unflagged control. This is an association on published records, not\n"
            "a causal claim, and not a safety judgement about any structure."
        )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate 2023 contradictions against 2025 ratings.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--base-year", type=int, default=BASE_YEAR)
    parser.add_argument("--check-year", type=int, default=CHECK_YEAR)
    parser.add_argument("--export-review", default=None,
                        help="write a sample of flagged contradictions to this CSV for human labelling")
    parser.add_argument("--read-review", default=None,
                        help="read a labelled review CSV back and report contradiction precision")
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--json", default=None, help="also write the result as JSON to this path")
    args = parser.parse_args(argv)

    try:
        conn = connect(args.db, create=False)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        if args.export_review:
            n = export_review_sample(conn, Path(args.export_review), year=args.base_year,
                                     n=args.sample_size)
            print(f"wrote {n} contradictions to {args.export_review} for review")
            return 0
        if args.read_review:
            stats = read_review_labels(Path(args.read_review))
            print(json.dumps(stats, indent=2))
            return 0

        result = validate(conn, base_year=args.base_year, check_year=args.check_year)
        print(render(result))
        if args.json:
            payload = {k: v for k, v in asdict(result).items() if k != "outcomes"}
            payload.update(predictive_alignment=result.predictive_alignment,
                           control_drop_rate=result.control_drop_rate,
                           control_rise_rate=result.control_rise_rate,
                           pooled_base_rate=result.pooled_base_rate,
                           expected_confirmations=result.expected_confirmations,
                           by_direction=result.direction_stats(),
                           lift=result.lift)
            Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"wrote {args.json}")
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

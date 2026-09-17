"""The contradiction engine.

NBI records one blunt 0-9 score per component. NBE records how much of each
element sits in condition states 1-4. On real bridges these two official records
disagree, and nobody is checking. This module is what checks.

    Structure 013450, 2023:
      NBI  deck rating = 7  ("good")          [NBI-013450-2023-deck]
      NBE  340 sq ft of deck in CS3 ("poor")  [NBE-013450-2023-12-cs3]
      -> contradiction

## How a disagreement is decided

Both records are reduced to the same three-band vocabulary — good, fair, poor —
and then compared:

* The **NBI band** comes from the published 0-9 rating via the FHWA
  good/fair/poor classification.
* The **element-implied band** comes from how much of the component's mapped
  element quantity sits in condition states 3 and 4, plus an absolute-quantity
  rule so that a materially large deteriorated area is never washed out by a
  large denominator.

A disagreement of one band or more, once the quantity gates are satisfied, is a
contradiction. Both directions are flagged — NBI optimistic relative to the
elements, and NBI pessimistic relative to them. Suppressing the second direction
would bias the engine toward the story it is looking for.

## What is deliberately not done

* Elements not mapped to a component do not drive a contradiction; they are
  counted and reported so the mapping can be extended from real data.
* Protective-system and defect elements are excluded from roll-ups: worn sealant
  is not structural deterioration.
* A NULL rating never participates. Absence of a rating is not evidence.
* Nothing here clears a structure, schedules maintenance, or asserts safety. It
  reports that two official records disagree, and by how much.

Every threshold below is a named constant, and every finding records the
threshold values that produced it, so a later re-tune against real distributions
is auditable rather than archaeological.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .. import ids
from ..db import DataUnavailable, connect
from ..ingest.element_map import classify_element, is_ambiguous
from .findings import Evidence, Finding, save_findings

# ---------------------------------------------------------------------------
# Thresholds — the whole tunable surface of the engine, in one place
# ---------------------------------------------------------------------------

#: FHWA good/fair/poor classification of the NBI 0-9 condition rating.
RATING_POOR_MAX = 4          # 0-4 poor
RATING_FAIR_MAX = 6          # 5-6 fair; 7-9 good

#: Fraction of a component's mapped quantity in CS3+CS4 implying each band.
FRAC_POOR = 0.20             # >= 20% deteriorated implies "poor"
FRAC_FAIR = 0.05             # >= 5% deteriorated implies "fair"

#: CS4 is "severe". Even a small severe fraction implies a poor component.
FRAC_CS4_POOR = 0.02

#: Absolute deteriorated quantity that implies at least "fair" regardless of
#: fraction. Without this, a large denominator hides a materially large defect
#: area — which is exactly the CLAUDE.md worked example (340 sq ft of deck in
#: CS3 against a deck rating of 7).
ABS_DETERIORATED_QTY = 250.0

#: A component whose mapped element quantity is below this does not produce a
#: contradiction: one small element in CS3 swings the fraction to a large number
#: and the finding is noise. The single threshold most in need of tuning against
#: real distributions (ASSUMPTIONS.md K4).
MIN_TOTAL_QTY = 100.0

#: For the NBI-pessimistic direction, the elements must be near-pristine before
#: we will call a poor rating a contradiction.
FRAC_PRISTINE = 0.02

#: Confidence penalties, applied multiplicatively and floored at CONFIDENCE_FLOOR.
CONFIDENCE_BASE = 0.9
PENALTY_NEAR_QTY_GATE = 0.85     # total quantity within 2x of the gate
PENALTY_AMBIGUOUS_ELEMENT = 0.9  # component roll-up includes an ambiguous element
PENALTY_NULL_QUANTITY = 0.85     # some mapped element reported no quantity
PENALTY_SUMMED_TOTAL = 0.95      # total quantity inferred, not published
CONFIDENCE_FLOOR = 0.3

BANDS = ("poor", "fair", "good")
BAND_INDEX = {band: i for i, band in enumerate(BANDS)}

#: Recorded on every finding so a re-tune is auditable.
THRESHOLDS = {
    "RATING_POOR_MAX": RATING_POOR_MAX,
    "RATING_FAIR_MAX": RATING_FAIR_MAX,
    "FRAC_POOR": FRAC_POOR,
    "FRAC_FAIR": FRAC_FAIR,
    "FRAC_CS4_POOR": FRAC_CS4_POOR,
    "ABS_DETERIORATED_QTY": ABS_DETERIORATED_QTY,
    "MIN_TOTAL_QTY": MIN_TOTAL_QTY,
    "FRAC_PRISTINE": FRAC_PRISTINE,
}


# ---------------------------------------------------------------------------
# Banding
# ---------------------------------------------------------------------------


def rating_band(rating: int | None) -> str | None:
    """Map an NBI 0-9 condition rating to good / fair / poor.

    ``None`` in, ``None`` out: an unrated component does not participate.
    """
    if rating is None:
        return None
    value = int(rating)
    if not 0 <= value <= 9:
        return None
    if value <= RATING_POOR_MAX:
        return "poor"
    if value <= RATING_FAIR_MAX:
        return "fair"
    return "good"


# ---------------------------------------------------------------------------
# Element roll-up
# ---------------------------------------------------------------------------


@dataclass
class ElementState:
    """One condition-state quantity as read from the asset index."""

    elem_num: int
    cs: int
    cs_qty: float | None
    total_qty: float | None
    units: str | None
    elem_name: str | None
    artifact_id: str


@dataclass
class ComponentRollup:
    """Element evidence for one NBI component on one structure and year."""

    component: str
    total_qty: float = 0.0
    cs3_qty: float = 0.0
    cs4_qty: float = 0.0
    units: str | None = None
    elem_nums: list[int] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    #: Artifacts for the CS3/CS4 quantities specifically — the ones a sentence
    #: about deterioration should cite, rather than the whole element set.
    deteriorated_artifact_ids: list[str] = field(default_factory=list)
    has_ambiguous_element: bool = False
    has_null_quantity: bool = False
    total_was_summed: bool = False

    @property
    def deteriorated_qty(self) -> float:
        return self.cs3_qty + self.cs4_qty

    @property
    def deteriorated_fraction(self) -> float | None:
        """CS3+CS4 as a fraction of total. ``None`` when there is no denominator."""
        if not self.total_qty:
            return None
        return self.deteriorated_qty / self.total_qty

    @property
    def cs4_fraction(self) -> float | None:
        if not self.total_qty:
            return None
        return self.cs4_qty / self.total_qty

    def implied_band(self) -> str | None:
        """The condition band the element quantities imply for this component.

        Returns ``None`` when there is no usable denominator — which is reported,
        not defaulted to "good".
        """
        fraction = self.deteriorated_fraction
        if fraction is None:
            return None
        cs4_fraction = self.cs4_fraction or 0.0
        if fraction >= FRAC_POOR or cs4_fraction >= FRAC_CS4_POOR:
            return "poor"
        if fraction >= FRAC_FAIR or self.deteriorated_qty >= ABS_DETERIORATED_QTY:
            return "fair"
        return "good"


def roll_up(component: str, states: Iterable[ElementState]) -> ComponentRollup:
    """Aggregate element condition states into one component's evidence.

    Pure function — this is what the tests exercise directly.

    The denominator is the sum of each element's total quantity, counted once per
    element rather than once per condition state (the source publishes the same
    total on all four rows). Where an element reports no total, its four state
    quantities are summed instead and the substitution is flagged, because which
    denominator was used changes the fraction and must not be invisible.
    """
    rollup = ComponentRollup(component=component)
    by_element: dict[int, list[ElementState]] = {}
    for state in states:
        by_element.setdefault(state.elem_num, []).append(state)

    for elem_num, element_states in sorted(by_element.items()):
        totals = {s.total_qty for s in element_states if s.total_qty is not None}
        if totals:
            element_total = max(totals)
        else:
            element_total = sum(s.cs_qty for s in element_states if s.cs_qty is not None)
            rollup.total_was_summed = True
        rollup.total_qty += element_total

        if any(s.cs_qty is None for s in element_states):
            rollup.has_null_quantity = True
        if is_ambiguous(elem_num):
            rollup.has_ambiguous_element = True

        rollup.elem_nums.append(elem_num)
        for state in sorted(element_states, key=lambda s: s.cs):
            rollup.artifact_ids.append(state.artifact_id)
            if state.cs == 3 and state.cs_qty:
                rollup.cs3_qty += state.cs_qty
                rollup.deteriorated_artifact_ids.append(state.artifact_id)
            elif state.cs == 4 and state.cs_qty:
                rollup.cs4_qty += state.cs_qty
                rollup.deteriorated_artifact_ids.append(state.artifact_id)
            if rollup.units is None and state.units:
                rollup.units = state.units

    return rollup


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_severity(nbi_band: str, implied_band: str, rollup: ComponentRollup) -> float:
    """How badly the two records disagree, in [0, 1].

    Two components: how many bands apart the records are (the dominant term), and
    how far past the deciding threshold the element evidence sits (a refinement,
    so that two one-band disagreements are not indistinguishable).

    Severity is not confidence. This says how big the disagreement is; confidence
    says how much we trust the observation. They are scored and stored separately.
    """
    distance = abs(BAND_INDEX[nbi_band] - BAND_INDEX[implied_band])
    base = distance / (len(BANDS) - 1)          # 1 band -> 0.5, 2 bands -> 1.0

    fraction = rollup.deteriorated_fraction or 0.0
    if BAND_INDEX[implied_band] < BAND_INDEX[nbi_band]:
        # Elements worse than the rating: scale with how deteriorated they are.
        magnitude = min(fraction / FRAC_POOR, 1.0) if FRAC_POOR else 0.0
    else:
        # Rating worse than the elements: scale with how pristine they are.
        magnitude = 1.0 - min(fraction / max(FRAC_PRISTINE, 1e-9), 1.0)

    # A one-band disagreement scores 0.4-0.6 depending on magnitude; a two-band
    # disagreement scores 0.8-1.0. Monotone in both terms, and the band distance
    # always dominates, because two bands apart is the more serious finding
    # however marginal the fraction that put it there.
    return round(min(1.0, base * (0.8 + 0.4 * magnitude)), 4)


def score_confidence(rollup: ComponentRollup) -> tuple[float, list[str]]:
    """How much the observation can be trusted, in [0, 1], with the reasons.

    Confidence is reduced — never silently — when the evidence is thin: a total
    quantity close to the gate, an element that reported no quantity, an element
    whose component assignment is ambiguous, or a denominator we had to infer.
    The reasons are returned so the UI can show *why* a finding is less certain
    rather than just showing a smaller number.
    """
    confidence = CONFIDENCE_BASE
    reasons: list[str] = []

    if rollup.total_qty < MIN_TOTAL_QTY * 2:
        confidence *= PENALTY_NEAR_QTY_GATE
        reasons.append("small total element quantity")
    if rollup.has_null_quantity:
        confidence *= PENALTY_NULL_QUANTITY
        reasons.append("an element reported no quantity for at least one condition state")
    if rollup.has_ambiguous_element:
        confidence *= PENALTY_AMBIGUOUS_ELEMENT
        reasons.append("component roll-up includes an element claimed by more than one component")
    if rollup.total_was_summed:
        confidence *= PENALTY_SUMMED_TOTAL
        reasons.append("element total quantity was inferred by summing condition states")

    return round(max(CONFIDENCE_FLOOR, confidence), 4), reasons


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def evaluate_component(
    struct_norm: str,
    year: int,
    component: str,
    rating: int | None,
    rating_artifact: str | None,
    rollup: ComponentRollup,
) -> Finding | None:
    """Compare one component's NBI rating against its element evidence.

    Returns a :class:`Finding` — ``conflicting`` when the records disagree,
    ``corroborated`` when they agree — or ``None`` when the comparison cannot be
    made at all. The three reasons for ``None`` are all honest absences: no
    rating, no element denominator, or a denominator too small to reason from.

    Pure function. No database, no I/O.
    """
    nbi_band = rating_band(rating)
    if nbi_band is None or rating_artifact is None:
        return None

    implied = rollup.implied_band()
    if implied is None:
        return None
    if rollup.total_qty < MIN_TOTAL_QTY:
        return None

    fraction = rollup.deteriorated_fraction or 0.0
    distance = abs(BAND_INDEX[nbi_band] - BAND_INDEX[implied])

    detail = {
        "component": component,
        "nbi_rating": int(rating),
        "nbi_band": nbi_band,
        "implied_band": implied,
        "band_distance": distance,
        "deteriorated_qty": round(rollup.deteriorated_qty, 3),
        "cs3_qty": round(rollup.cs3_qty, 3),
        "cs4_qty": round(rollup.cs4_qty, 3),
        "total_qty": round(rollup.total_qty, 3),
        "deteriorated_fraction": round(fraction, 5),
        "units": rollup.units,
        "elements": rollup.elem_nums,
        "total_was_summed": rollup.total_was_summed,
        "has_ambiguous_element": rollup.has_ambiguous_element,
        "thresholds": THRESHOLDS,
    }
    confidence, caveats = score_confidence(rollup)
    detail["confidence_caveats"] = caveats

    if distance == 0:
        # The records agree. Still worth emitting: a corroborated finding is what
        # lets the brief say "two independent sources agree" with citations, and
        # it is the control population for the validation pass.
        detail["direction"] = "agreement"
        return Finding(
            finding_id=ids.finding_id("condition", struct_norm, year, component),
            struct_norm=struct_norm, year=year, kind="condition", component=component,
            evidence_tier="corroborated", confidence=confidence, severity=0.0,
            detail=detail,
            evidence=[Evidence(rating_artifact, "supports")]
                     + [Evidence(a, "supports") for a in rollup.artifact_ids],
        )

    nbi_optimistic = BAND_INDEX[implied] < BAND_INDEX[nbi_band]
    if not nbi_optimistic and fraction > FRAC_PRISTINE:
        # NBI is worse than the elements suggest, but the elements are not clean
        # enough for us to call the rating wrong. Not a contradiction; the rating
        # may well reflect something the element inspection does not capture.
        detail["direction"] = "agreement_within_tolerance"
        return Finding(
            finding_id=ids.finding_id("condition", struct_norm, year, component),
            struct_norm=struct_norm, year=year, kind="condition", component=component,
            evidence_tier="corroborated", confidence=round(confidence * 0.9, 4), severity=0.0,
            detail=detail,
            evidence=[Evidence(rating_artifact, "supports")]
                     + [Evidence(a, "supports") for a in rollup.artifact_ids],
        )

    detail["direction"] = "nbi_optimistic" if nbi_optimistic else "nbi_pessimistic"
    severity = score_severity(nbi_band, implied, rollup)

    # The deteriorated-state artifacts are the ones that carry the contradiction;
    # the rest of the element set is context. Both are attached, distinguished by
    # role, so a sentence can cite precisely what it is claiming.
    contradicting = rollup.deteriorated_artifact_ids if nbi_optimistic else rollup.artifact_ids
    context = [a for a in rollup.artifact_ids if a not in set(contradicting)]

    return Finding(
        finding_id=ids.finding_id("contradiction", struct_norm, year, component),
        struct_norm=struct_norm, year=year, kind="contradiction", component=component,
        evidence_tier="conflicting", confidence=confidence, severity=severity,
        detail=detail,
        evidence=[Evidence(rating_artifact, "contradicts")]
                 + [Evidence(a, "contradicts") for a in contradicting]
                 + [Evidence(a, "context") for a in context],
    )


def single_source_finding(
    struct_norm: str, year: int, component: str, rating: int, rating_artifact: str
) -> Finding:
    """A condition finding backed by NBI alone, with no element data to check it.

    Tier ``single_source``, explicitly. This is the finding that stops the brief
    from reading as if the records had been cross-checked when they have not
    been. It is why "no NBE data for this structure" is an output rather than a
    silence.
    """
    band = rating_band(rating)
    return Finding(
        finding_id=ids.finding_id("condition", struct_norm, year, component),
        struct_norm=struct_norm, year=year, kind="condition", component=component,
        evidence_tier="single_source",
        # Capped well below a corroborated finding: one source, unchecked.
        confidence=0.6,
        severity=None,
        detail={
            "component": component, "nbi_rating": int(rating), "nbi_band": band,
            "implied_band": None, "direction": "single_source",
            "note": "no NBE element data for this structure and year; "
                    "this rating has not been cross-checked against any other source",
        },
        evidence=[Evidence(rating_artifact, "supports")],
    )


# ---------------------------------------------------------------------------
# Database-driven analysis
# ---------------------------------------------------------------------------


def _load_component_states(conn, struct_norm: str, year: int) -> dict[str, list[ElementState]]:
    """Group a structure's element condition states by NBI component."""
    grouped: dict[str, list[ElementState]] = {}
    rows = conn.execute(
        """
        SELECT elem_num, cs, cs_qty, total_qty, units, elem_name, elem_class, artifact_id
          FROM elements WHERE struct_norm = ? AND year = ?
         ORDER BY elem_num, cs
        """,
        (struct_norm, year),
    ).fetchall()
    for row in rows:
        component = row["elem_class"] or classify_element(row["elem_num"])
        if component in (None, "protective"):
            continue
        grouped.setdefault(component, []).append(ElementState(
            elem_num=row["elem_num"], cs=row["cs"], cs_qty=row["cs_qty"],
            total_qty=row["total_qty"], units=row["units"], elem_name=row["elem_name"],
            artifact_id=row["artifact_id"],
        ))
    return grouped


def analyse_structure(conn, struct_norm: str, year: int) -> list[Finding]:
    """Run the engine over one structure. Returns findings without saving them."""
    ratings = {
        row["component"]: row
        for row in conn.execute(
            "SELECT component, rating, artifact_id FROM ratings WHERE struct_norm = ? AND year = ?",
            (struct_norm, year),
        )
    }
    if not ratings:
        return []

    by_component = _load_component_states(conn, struct_norm, year)
    findings: list[Finding] = []

    for component, row in sorted(ratings.items()):
        if row["rating"] is None:
            continue    # unrated component: absence is not evidence
        states = by_component.get(component)
        if not states:
            findings.append(single_source_finding(
                struct_norm, year, component, row["rating"], row["artifact_id"]
            ))
            continue
        finding = evaluate_component(
            struct_norm, year, component, row["rating"], row["artifact_id"],
            roll_up(component, states),
        )
        if finding is not None:
            findings.append(finding)
        else:
            # The comparison could not be made (denominator too small or absent).
            # That is a single-source situation, and is reported as one.
            findings.append(single_source_finding(
                struct_norm, year, component, row["rating"], row["artifact_id"]
            ))
    return findings


def eligible_structures(conn, year: int, *, require_elements: bool = True) -> Iterator[str]:
    """Structures the engine can run on for a year."""
    if require_elements:
        sql = """
            SELECT struct_norm FROM ratings WHERE year = ?
            INTERSECT
            SELECT struct_norm FROM elements WHERE year = ?
             ORDER BY 1
        """
        params = (year, year)
    else:
        sql = "SELECT DISTINCT struct_norm FROM ratings WHERE year = ? ORDER BY 1"
        params = (year,)
    for row in conn.execute(sql, params):
        yield row["struct_norm"]


def run(conn, year: int, *, limit: int | None = None, require_elements: bool = True,
        log=print, progress_every: int = 2000) -> dict:
    """Run the engine across a year and persist the findings.

    Returns a summary dict. Re-running supersedes previous findings for the same
    structures rather than accumulating duplicates, because finding IDs are
    deterministic.
    """
    counts = {"structures": 0, "contradiction": 0, "corroborated": 0, "single_source": 0}
    severity_total = 0.0

    for index, struct_norm in enumerate(eligible_structures(conn, year, require_elements=require_elements)):
        if limit is not None and index >= limit:
            break
        findings = analyse_structure(conn, struct_norm, year)
        if not findings:
            continue
        conn.execute(
            "DELETE FROM findings WHERE struct_norm = ? AND year = ? AND kind IN ('contradiction','condition')",
            (struct_norm, year),
        )
        save_findings(conn, findings)
        counts["structures"] += 1
        for finding in findings:
            if finding.kind == "contradiction":
                counts["contradiction"] += 1
                severity_total += finding.severity or 0.0
            elif finding.evidence_tier == "single_source":
                counts["single_source"] += 1
            else:
                counts["corroborated"] += 1
        if index and index % progress_every == 0:
            conn.commit()
            log(f"  [progress] {index:,} structures analysed, "
                f"{counts['contradiction']:,} contradictions")

    conn.commit()
    counts["mean_severity"] = (
        round(severity_total / counts["contradiction"], 4) if counts["contradiction"] else None
    )
    return counts


def summarise(conn, year: int) -> str:
    """Human-readable summary of what the engine found for a year."""
    total = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE year = ? AND kind = 'contradiction'", (year,)
    ).fetchone()[0]
    if total == 0:
        eligible = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT struct_norm FROM ratings WHERE year = ?
                INTERSECT SELECT struct_norm FROM elements WHERE year = ?)
            """,
            (year, year),
        ).fetchone()[0]
        if eligible == 0:
            return (f"{year}: no structure has both NBI ratings and NBE elements, so the "
                    "engine has nothing to compare. Ingest both sources first.")
        return f"{year}: {eligible:,} eligible structures analysed, no contradictions flagged."

    lines = [f"{year}: {total:,} contradictions flagged.", ""]
    by_direction = conn.execute(
        """
        SELECT json_extract(detail_json, '$.direction') AS direction,
               json_extract(detail_json, '$.component') AS component,
               COUNT(*) AS n, ROUND(AVG(severity), 3) AS mean_severity
          FROM findings WHERE year = ? AND kind = 'contradiction'
         GROUP BY direction, component ORDER BY n DESC
        """,
        (year,),
    ).fetchall()
    lines.append(f"{'direction':<20}{'component':<18}{'count':>10}{'mean severity':>16}")
    lines.append("-" * 64)
    for row in by_direction:
        lines.append(f"{row['direction']:<20}{row['component']:<18}"
                     f"{row['n']:>10,}{row['mean_severity']:>16}")
    lines.append("")
    tiers = conn.execute(
        "SELECT evidence_tier, COUNT(*) AS n FROM findings WHERE year = ? GROUP BY evidence_tier",
        (year,),
    ).fetchall()
    lines.append("All findings by evidence tier:")
    for row in tiers:
        lines.append(f"  {row['evidence_tier']:<16}{row['n']:>10,}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find NBI-vs-NBE contradictions.")
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--db", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="analyse at most this many structures (for a quick look)")
    parser.add_argument("--all-structures", action="store_true",
                        help="also analyse structures with no element data, emitting "
                             "single-source findings for them")
    args = parser.parse_args(argv)

    try:
        conn = connect(args.db, create=False)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        counts = run(conn, args.year, limit=args.limit,
                     require_elements=not args.all_structures)
        print(f"\nanalysed {counts['structures']:,} structures")
        print(summarise(conn, args.year))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

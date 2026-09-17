"""Tests for the contradiction engine.

Every input here is constructed in the test. These are rule tests: they pin the
banding, the roll-up arithmetic, the thresholds and the direction logic, so that
the engine's behaviour is specified independently of any dataset.
"""

import pytest

from src.analysis import contradictions as C
from src.analysis.findings import Finding, UngroundedFinding, load_findings, save_findings, Evidence
from src.db import connect


def state(elem=12, cs=1, qty=0.0, total=1000.0, units="SQFT", struct="013450", year=2023):
    from src.ids import nbe_id
    return C.ElementState(
        elem_num=elem, cs=cs, cs_qty=qty, total_qty=total, units=units,
        elem_name=f"element {elem}", artifact_id=nbe_id(struct, year, elem, cs),
    )


def deck_rollup(cs3=0.0, cs4=0.0, total=1000.0, elem=12):
    good = max(total - cs3 - cs4, 0.0)
    return C.roll_up("deck", [
        state(elem=elem, cs=1, qty=good, total=total),
        state(elem=elem, cs=3, qty=cs3, total=total),
        state(elem=elem, cs=4, qty=cs4, total=total),
    ])


class TestRatingBand:
    @pytest.mark.parametrize("rating,band", [
        (9, "good"), (8, "good"), (7, "good"),
        (6, "fair"), (5, "fair"),
        (4, "poor"), (2, "poor"), (0, "poor"),
    ])
    def test_fhwa_good_fair_poor_classification(self, rating, band):
        assert C.rating_band(rating) == band

    @pytest.mark.parametrize("rating", [None, -1, 10, 99])
    def test_unrated_or_impossible_ratings_do_not_participate(self, rating):
        assert C.rating_band(rating) is None


class TestRollUp:
    def test_total_is_counted_once_per_element_not_once_per_state(self):
        """The source repeats the element total on all four condition-state rows."""
        rollup = deck_rollup(cs3=100, total=1000)
        assert rollup.total_qty == 1000
        assert rollup.deteriorated_fraction == pytest.approx(0.1)

    def test_two_elements_sum_their_totals(self):
        rollup = C.roll_up("deck", [
            state(elem=12, cs=3, qty=50, total=1000),
            state(elem=38, cs=3, qty=50, total=500),
        ])
        assert rollup.total_qty == 1500
        assert rollup.deteriorated_qty == 100
        assert rollup.elem_nums == [12, 38]

    def test_cs3_and_cs4_are_the_deteriorated_quantity_cs2_is_not(self):
        rollup = C.roll_up("deck", [
            state(cs=1, qty=600, total=1000),
            state(cs=2, qty=300, total=1000),
            state(cs=3, qty=60, total=1000),
            state(cs=4, qty=40, total=1000),
        ])
        assert rollup.deteriorated_qty == 100
        assert rollup.cs3_qty == 60 and rollup.cs4_qty == 40

    def test_missing_total_is_summed_from_states_and_flagged(self):
        rollup = C.roll_up("deck", [
            state(cs=1, qty=900, total=None),
            state(cs=3, qty=100, total=None),
        ])
        assert rollup.total_qty == 1000
        assert rollup.total_was_summed is True

    def test_null_quantity_is_flagged_not_treated_as_zero(self):
        rollup = C.roll_up("deck", [state(cs=3, qty=None, total=1000)])
        assert rollup.has_null_quantity is True
        assert rollup.cs3_qty == 0.0     # nothing invented for the numerator

    def test_ambiguous_element_is_flagged(self):
        rollup = C.roll_up("superstructure", [state(elem=155, cs=3, qty=10, total=1000)])
        assert rollup.has_ambiguous_element is True

    def test_no_denominator_means_no_implied_band_rather_than_good(self):
        rollup = C.roll_up("deck", [state(cs=3, qty=None, total=None)])
        assert rollup.deteriorated_fraction is None
        assert rollup.implied_band() is None

    def test_deteriorated_artifacts_are_separable_from_context(self):
        rollup = deck_rollup(cs3=300, total=1000)
        assert rollup.deteriorated_artifact_ids == ["NBE-013450-2023-12-cs3"]
        assert "NBE-013450-2023-12-cs1" in rollup.artifact_ids


class TestImpliedBand:
    def test_twenty_percent_deteriorated_implies_poor(self):
        assert deck_rollup(cs3=200, total=1000).implied_band() == "poor"

    def test_any_material_cs4_fraction_implies_poor(self):
        assert deck_rollup(cs4=25, total=1000).implied_band() == "poor"

    def test_five_percent_implies_fair(self):
        assert deck_rollup(cs3=50, total=1000).implied_band() == "fair"

    def test_large_absolute_quantity_implies_fair_despite_a_small_fraction(self):
        """The rule that reproduces the CLAUDE.md worked example."""
        assert deck_rollup(cs3=340, total=8000).implied_band() == "fair"

    def test_clean_elements_imply_good(self):
        assert deck_rollup(cs3=5, total=1000).implied_band() == "good"


class TestTheDocumentedExample:
    """Structure 013450, 2023: NBI deck = 7, 340 sq ft of deck in CS3."""

    def build(self):
        rollup = C.roll_up("deck", [
            state(cs=1, qty=7660, total=8000),
            state(cs=3, qty=340, total=8000),
        ])
        return C.evaluate_component("013450", 2023, "deck", 7, "NBI-013450-2023-deck", rollup)

    def test_it_is_flagged_as_a_contradiction(self):
        finding = self.build()
        assert finding.kind == "contradiction"
        assert finding.evidence_tier == "conflicting"
        assert finding.detail["direction"] == "nbi_optimistic"

    def test_it_cites_exactly_the_artifacts_from_the_specification(self):
        finding = self.build()
        contradicting = finding.artifact_ids(roles=["contradicts"])
        assert "NBI-013450-2023-deck" in contradicting
        assert "NBE-013450-2023-12-cs3" in contradicting

    def test_its_finding_id_is_deterministic(self):
        assert self.build().finding_id == self.build().finding_id


class TestDirections:
    def test_nbi_optimistic_when_elements_are_worse_than_the_rating(self):
        finding = C.evaluate_component(
            "013450", 2023, "deck", 8, "NBI-013450-2023-deck", deck_rollup(cs3=300, total=1000))
        assert finding.detail["direction"] == "nbi_optimistic"
        assert finding.detail["band_distance"] == 2
        assert finding.severity >= 0.8

    def test_nbi_pessimistic_when_the_rating_is_worse_than_pristine_elements(self):
        """The inverse case is flagged too — suppressing it would bias the engine."""
        finding = C.evaluate_component(
            "013450", 2023, "deck", 3, "NBI-013450-2023-deck", deck_rollup(cs3=0, total=1000))
        assert finding.kind == "contradiction"
        assert finding.detail["direction"] == "nbi_pessimistic"

    def test_a_poor_rating_with_merely_okay_elements_is_not_a_contradiction(self):
        finding = C.evaluate_component(
            "013450", 2023, "deck", 4, "NBI-013450-2023-deck", deck_rollup(cs3=40, total=1000))
        assert finding.kind == "condition"
        assert finding.evidence_tier == "corroborated"

    def test_agreement_is_emitted_as_a_corroborated_finding(self):
        finding = C.evaluate_component(
            "013450", 2023, "deck", 8, "NBI-013450-2023-deck", deck_rollup(cs3=5, total=1000))
        assert finding.kind == "condition"
        assert finding.evidence_tier == "corroborated"
        assert finding.severity == 0.0
        assert finding.detail["band_distance"] == 0


class TestGatesAndAbsences:
    def test_a_null_rating_produces_nothing(self):
        assert C.evaluate_component(
            "013450", 2023, "deck", None, "NBI-013450-2023-deck", deck_rollup(cs3=300)) is None

    def test_a_tiny_element_quantity_does_not_produce_a_contradiction(self):
        """One small element in CS3 swings the fraction; that is noise, not a finding."""
        rollup = deck_rollup(cs3=30, total=50)
        assert rollup.implied_band() == "poor"
        assert C.evaluate_component(
            "013450", 2023, "deck", 8, "NBI-013450-2023-deck", rollup) is None

    def test_no_denominator_produces_nothing(self):
        rollup = C.roll_up("deck", [state(cs=3, qty=None, total=None)])
        assert C.evaluate_component(
            "013450", 2023, "deck", 8, "NBI-013450-2023-deck", rollup) is None


class TestConfidenceIsNotSeverity:
    def test_thin_evidence_lowers_confidence_and_says_why(self):
        rollup = C.roll_up("deck", [
            state(elem=155, cs=1, qty=100, total=None),
            state(elem=155, cs=3, qty=None, total=None),
        ])
        confidence, reasons = C.score_confidence(rollup)
        assert confidence < C.CONFIDENCE_BASE
        assert any("no quantity" in r for r in reasons)
        assert any("more than one component" in r for r in reasons)

    def test_confidence_never_falls_below_the_floor_or_above_one(self):
        rollup = C.roll_up("superstructure", [state(elem=155, cs=3, qty=None, total=None)])
        confidence, _ = C.score_confidence(rollup)
        assert C.CONFIDENCE_FLOOR <= confidence <= 1.0

    def test_severity_and_confidence_are_independent(self):
        """A severe disagreement observed on thin evidence keeps both facts."""
        rollup = C.roll_up("deck", [
            state(elem=12, cs=1, qty=100, total=None),
            state(elem=12, cs=4, qty=100, total=None),
        ])
        finding = C.evaluate_component("013450", 2023, "deck", 9, "NBI-013450-2023-deck", rollup)
        assert finding.severity >= 0.8
        assert finding.confidence < C.CONFIDENCE_BASE
        assert finding.detail["confidence_caveats"]

    def test_every_finding_records_the_thresholds_that_produced_it(self):
        finding = C.evaluate_component(
            "013450", 2023, "deck", 8, "NBI-013450-2023-deck", deck_rollup(cs3=300))
        assert finding.detail["thresholds"]["FRAC_POOR"] == C.FRAC_POOR


class TestSingleSource:
    def test_nbi_alone_is_never_presented_as_corroborated(self):
        finding = C.single_source_finding("013450", 2023, "deck", 7, "NBI-013450-2023-deck")
        assert finding.evidence_tier == "single_source"
        assert finding.confidence < 0.9
        assert finding.detail["implied_band"] is None
        assert "not been cross-checked" in finding.detail["note"]


class TestPersistence:
    def make_conn(self):
        conn = connect(":memory:")
        conn.execute("INSERT INTO structures (struct_norm, state_abbr) VALUES ('013450','AL')")
        for artifact, kind in [("NBI-013450-2023-deck", "NBI"), ("NBE-013450-2023-12-cs3", "NBE")]:
            conn.execute(
                "INSERT INTO artifacts (artifact_id, kind, struct_norm, year, created_at) "
                "VALUES (?,?, '013450', 2023, '2020-01-01T00:00:00+00:00')", (artifact, kind))
        return conn

    def test_a_finding_without_evidence_cannot_be_stored(self):
        conn = self.make_conn()
        naked = Finding(finding_id="F-x", struct_norm="013450", year=2023, kind="contradiction",
                        evidence_tier="conflicting", confidence=0.5, detail={})
        with pytest.raises(UngroundedFinding):
            save_findings(conn, [naked])

    def test_findings_round_trip_with_their_evidence_and_roles(self):
        conn = self.make_conn()
        rollup = C.roll_up("deck", [state(cs=1, qty=7660, total=8000), state(cs=3, qty=340, total=8000)])
        finding = C.evaluate_component("013450", 2023, "deck", 7, "NBI-013450-2023-deck", rollup)
        save_findings(conn, [finding])
        loaded = load_findings(conn, "013450", 2023)
        assert len(loaded) == 1
        assert loaded[0].evidence_tier == "conflicting"
        assert set(loaded[0].artifact_ids(roles=["contradicts"])) == \
               set(finding.artifact_ids(roles=["contradicts"]))
        assert loaded[0].detail["nbi_rating"] == 7

    def test_re_running_supersedes_rather_than_duplicates(self):
        conn = self.make_conn()
        rollup = deck_rollup(cs3=340, total=8000)
        for _ in range(3):
            save_findings(conn, [C.evaluate_component(
                "013450", 2023, "deck", 7, "NBI-013450-2023-deck", rollup)])
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM finding_evidence").fetchone()[0] == \
               len(rollup.artifact_ids) + 1

    def test_confidence_outside_the_unit_interval_is_refused(self):
        with pytest.raises(ValueError):
            Finding(finding_id="F-x", struct_norm="013450", year=2023, kind="condition",
                    evidence_tier="single_source", confidence=1.5, detail={})

    def test_unknown_evidence_tier_is_refused(self):
        with pytest.raises(ValueError):
            Finding(finding_id="F-x", struct_norm="013450", year=2023, kind="condition",
                    evidence_tier="probably_fine", confidence=0.5, detail={},
                    evidence=[Evidence("NBI-013450-2023-deck")])


class TestEndToEndOverTheIndex:
    """The engine reading from a populated index — built entirely in the test."""

    def populate(self):
        from src import ids
        from src.store import upsert_element_state, upsert_rating, upsert_structure

        conn = connect(":memory:")
        upsert_structure(conn, "013450", struct_raw="013450", state_abbr="AL", year=2023)
        upsert_rating(conn, struct_norm="013450", year=2023, component="deck", rating=7,
                      rating_raw="7", artifact_id=ids.nbi_id("013450", 2023, "deck"),
                      source_path="test", source_line=2)
        upsert_rating(conn, struct_norm="013450", year=2023, component="substructure", rating=6,
                      rating_raw="6", artifact_id=ids.nbi_id("013450", 2023, "substructure"),
                      source_path="test", source_line=2)
        for cs, qty in ((1, 7660.0), (3, 340.0)):
            upsert_element_state(
                conn, struct_norm="013450", year=2023, elem_num=12, cs=cs, cs_qty=qty,
                total_qty=8000.0, units="SQFT", elem_name="RC Deck", elem_class="deck",
                state_abbr="AL", artifact_id=ids.nbe_id("013450", 2023, 12, cs),
                source_path="test")
        return conn

    def test_run_flags_the_deck_and_marks_the_unchecked_component_single_source(self):
        conn = self.populate()
        counts = C.run(conn, 2023, log=lambda *a: None)
        assert counts["structures"] == 1
        assert counts["contradiction"] == 1

        findings = {f.component: f for f in load_findings(conn, "013450", 2023)}
        assert findings["deck"].kind == "contradiction"
        # No element data for the substructure: reported as single-source, not as
        # agreement, and not silently omitted.
        assert findings["substructure"].evidence_tier == "single_source"

    def test_every_cited_artifact_resolves(self):
        from src.store import resolve_artifact

        conn = self.populate()
        C.run(conn, 2023, log=lambda *a: None)
        for finding in load_findings(conn, "013450", 2023):
            assert finding.evidence
            for artifact_id in finding.artifact_ids():
                assert resolve_artifact(conn, artifact_id) is not None

    def test_summary_is_honest_when_nothing_is_eligible(self):
        conn = connect(":memory:")
        assert "nothing to compare" in C.summarise(conn, 2023)

    def test_run_is_idempotent(self):
        conn = self.populate()
        first = C.run(conn, 2023, log=lambda *a: None)
        rows_first = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        second = C.run(conn, 2023, log=lambda *a: None)
        assert first == second
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == rows_first

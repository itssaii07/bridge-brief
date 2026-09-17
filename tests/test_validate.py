"""Tests for the 2023 -> 2025 validation pass.

All ratings and findings below are inserted by the test.
"""

import pytest

from src import ids
from src.analysis import contradictions as C
from src.analysis.validate import (
    classify_outcome, export_review_sample, read_review_labels, render, validate,
)
from src.analysis.findings import Evidence, Finding, save_findings
from src.db import DataUnavailable, connect
from src.store import upsert_rating, upsert_structure


def make_conn():
    return connect(":memory:")


def add_rating(conn, struct, year, component, rating):
    upsert_structure(conn, struct, struct_raw=struct, state_abbr="AL", year=year)
    upsert_rating(conn, struct_norm=struct, year=year, component=component, rating=rating,
                  rating_raw=str(rating) if rating is not None else "N",
                  artifact_id=ids.nbi_id(struct, year, component),
                  source_path="test", source_line=1)


def add_flag(conn, struct, component, base_rating, direction="nbi_optimistic", severity=0.5):
    save_findings(conn, [Finding(
        finding_id=ids.finding_id("contradiction", struct, 2023, component),
        struct_norm=struct, year=2023, kind="contradiction", component=component,
        evidence_tier="conflicting", confidence=0.9, severity=severity,
        detail={"direction": direction, "nbi_rating": base_rating, "component": component,
                "deteriorated_qty": 340, "total_qty": 8000, "units": "SQFT"},
        evidence=[Evidence(ids.nbi_id(struct, 2023, component), "contradicts")],
    )])


def add_element(conn, struct, year, elem=12, cs=3, qty=10.0, total=1000.0):
    from src.store import upsert_element_state
    upsert_element_state(conn, struct_norm=struct, year=year, elem_num=elem, cs=cs,
                         cs_qty=qty, total_qty=total, units="SQFT", elem_name="deck",
                         elem_class="deck", state_abbr="AL",
                         artifact_id=ids.nbe_id(struct, year, elem, cs), source_path="test")


class TestClassifyOutcome:
    def test_optimistic_flag_is_confirmed_by_a_downgrade(self):
        assert classify_outcome("nbi_optimistic", 7, 5)[:2] == ("confirmed", -2)

    def test_optimistic_flag_with_no_change(self):
        assert classify_outcome("nbi_optimistic", 7, 7)[0] == "unchanged"

    def test_optimistic_flag_contradicted_by_an_upgrade(self):
        status, delta, reason = classify_outcome("nbi_optimistic", 7, 8)
        assert status == "contrary" and delta == 1 and "rose" in reason

    def test_pessimistic_flag_is_confirmed_by_an_upgrade_not_a_downgrade(self):
        """Scoring both directions as 'drop' would count these successes as failures."""
        assert classify_outcome("nbi_pessimistic", 4, 6)[0] == "confirmed"
        assert classify_outcome("nbi_pessimistic", 4, 2)[0] == "contrary"

    def test_absent_check_year_rating_is_unevaluable_not_a_miss(self):
        status, delta, reason = classify_outcome("nbi_optimistic", 7, None)
        assert status == "unevaluable" and delta is None
        assert "no comparable rating" in reason


class TestValidate:
    def test_confirmed_unchanged_contrary_and_unevaluable_are_separated(self):
        conn = make_conn()
        cases = [("000001", 7, 5), ("000002", 7, 7), ("000003", 7, 8), ("000004", 7, None)]
        for struct, base, later in cases:
            add_rating(conn, struct, 2023, "deck", base)
            if later is not None:
                add_rating(conn, struct, 2025, "deck", later)
            add_flag(conn, struct, "deck", base)

        result = validate(conn)
        assert result.flagged_total == 4
        assert result.flagged_evaluable == 3          # the absent one is excluded
        assert (result.flagged_confirmed, result.flagged_unchanged,
                result.flagged_contrary, result.flagged_unevaluable) == (1, 1, 1, 1)
        assert result.predictive_alignment == pytest.approx(1 / 3, abs=1e-4)

    def test_unevaluable_structures_are_excluded_from_the_denominator(self):
        conn = make_conn()
        add_rating(conn, "000001", 2023, "deck", 7)
        add_rating(conn, "000001", 2025, "deck", 5)
        add_flag(conn, "000001", "deck", 7)
        for i in range(2, 6):                       # four flagged, none in 2025
            struct = f"00000{i}"
            add_rating(conn, struct, 2023, "deck", 7)
            add_flag(conn, struct, "deck", 7)
        result = validate(conn)
        assert result.flagged_evaluable == 1
        assert result.predictive_alignment == 1.0    # not 0.2
        assert result.flagged_unevaluable == 4

    def test_control_group_gives_a_base_rate(self):
        conn = make_conn()
        # One flagged component that dropped.
        add_rating(conn, "000001", 2023, "deck", 7)
        add_rating(conn, "000001", 2025, "deck", 5)
        add_element(conn, "000001", 2023)
        add_flag(conn, "000001", "deck", 7)
        # Four unflagged components with both sources; one of them dropped.
        for i, later in enumerate([6, 7, 7, 7], start=2):
            struct = f"00000{i}"
            add_rating(conn, struct, 2023, "deck", 7)
            add_rating(conn, struct, 2025, "deck", later)
            add_element(conn, struct, 2023)

        result = validate(conn)
        assert result.control_evaluable == 4
        assert result.control_dropped == 1
        assert result.control_drop_rate == 0.25
        assert result.lift == pytest.approx(0.75)

    def test_a_flag_with_no_predictive_power_reports_zero_or_negative_lift(self):
        conn = make_conn()
        add_rating(conn, "000001", 2023, "deck", 7)
        add_rating(conn, "000001", 2025, "deck", 7)     # flagged, did not move
        add_element(conn, "000001", 2023)
        add_flag(conn, "000001", "deck", 7)
        add_rating(conn, "000002", 2023, "deck", 7)
        add_rating(conn, "000002", 2025, "deck", 5)     # unflagged, dropped
        add_element(conn, "000002", 2023)

        result = validate(conn)
        assert result.lift is not None and result.lift < 0
        assert "no predictive information" in render(result)

    def test_control_excludes_structures_without_element_data(self):
        """A structure the engine could never have flagged is not a fair control."""
        conn = make_conn()
        add_rating(conn, "000009", 2023, "deck", 7)
        add_rating(conn, "000009", 2025, "deck", 5)     # no elements at all
        result = validate(conn)
        assert result.control_total == 0

    def test_per_direction_breakdown(self):
        conn = make_conn()
        add_rating(conn, "000001", 2023, "deck", 7)
        add_rating(conn, "000001", 2025, "deck", 5)
        add_flag(conn, "000001", "deck", 7, direction="nbi_optimistic")
        add_rating(conn, "000002", 2023, "substructure", 3)
        add_rating(conn, "000002", 2025, "substructure", 5)
        add_flag(conn, "000002", "substructure", 3, direction="nbi_pessimistic")

        result = validate(conn)
        assert result.by_direction["nbi_optimistic"]["confirmed"] == 1
        assert result.by_direction["nbi_pessimistic"]["confirmed"] == 1
        assert result.predictive_alignment == 1.0

    def test_empty_database_reports_what_to_run_rather_than_a_number(self):
        text = render(validate(make_conn()))
        assert "nothing to validate" in text
        assert "python -m src.ingest.nbi" in text

    def test_rates_are_none_not_zero_when_nothing_is_evaluable(self):
        result = validate(make_conn())
        assert result.predictive_alignment is None
        assert result.control_drop_rate is None
        assert result.lift is None


class TestReviewSample:
    def test_export_requires_findings_and_says_what_to_run(self, tmp_path):
        with pytest.raises(DataUnavailable) as exc:
            export_review_sample(make_conn(), tmp_path / "review.csv")
        assert "contradictions" in str(exc.value)

    def test_export_is_deterministic_and_carries_the_evidence(self, tmp_path):
        conn = make_conn()
        for i in range(1, 6):
            struct = f"00000{i}"
            add_rating(conn, struct, 2023, "deck", 7)
            add_flag(conn, struct, "deck", 7)
        path = tmp_path / "review.csv"
        assert export_review_sample(conn, path, n=3) == 3
        first = path.read_text(encoding="utf-8")
        export_review_sample(conn, path, n=3)
        assert path.read_text(encoding="utf-8") == first            # same database, same sample
        assert "NBI-000001-2023-deck" in first or "NBI-00000" in first
        assert "verdict" in first.splitlines()[0]

    def test_precision_comes_from_human_labels_and_unlabelled_rows_are_not_assumed(self, tmp_path):
        path = tmp_path / "review.csv"
        path.write_text(
            "finding_id,verdict (correct|incorrect)\n"
            "F-a,correct\nF-b,correct\nF-c,incorrect\nF-d,\n",
            encoding="utf-8",
        )
        stats = read_review_labels(path)
        assert stats == {"labelled": 3, "correct": 2, "incorrect": 1, "unlabelled": 1,
                         "contradiction_precision": 0.6667}

    def test_precision_is_none_when_nothing_has_been_labelled(self, tmp_path):
        path = tmp_path / "review.csv"
        path.write_text("finding_id,verdict (correct|incorrect)\nF-a,\n", encoding="utf-8")
        assert read_review_labels(path)["contradiction_precision"] is None

    def test_missing_review_file_explains_how_to_make_one(self, tmp_path):
        with pytest.raises(DataUnavailable) as exc:
            read_review_labels(tmp_path / "nope.csv")
        assert "--export-review" in str(exc.value)


class TestEndToEnd:
    def test_engine_output_flows_into_validation_without_hand_wiring(self):
        conn = make_conn()
        add_rating(conn, "013450", 2023, "deck", 7)
        add_rating(conn, "013450", 2025, "deck", 5)
        for cs, qty in ((1, 7660.0), (3, 340.0)):
            add_element(conn, "013450", 2023, cs=cs, qty=qty, total=8000.0)

        C.run(conn, 2023, log=lambda *a: None)
        result = validate(conn)
        assert result.flagged_total == 1
        assert result.flagged_confirmed == 1
        assert result.predictive_alignment == 1.0


class TestDirectionMatchedBaseRate:
    """Each flag direction must be scored against the movement it predicts.

    An NBI-optimistic flag is confirmed by a downgrade; an NBI-pessimistic flag
    by an upgrade. Scoring both against the drop rate — which this harness did
    at first — compares most flags against the base rate for the opposite
    movement, and on the real 2023 data that artefact was the pooled headline:
    5,714 of 7,018 flags were pessimistic, so a −1.8% pooled lift was reported
    while the optimistic flags were in fact lifting well above their own base
    rate.
    """

    def _control(self, conn, later_ratings, first=100):
        """Unflagged components with both sources, moving as specified."""
        for i, later in enumerate(later_ratings, start=first):
            struct = f"C{i:05d}"
            add_rating(conn, struct, 2023, "deck", 6)
            add_rating(conn, struct, 2025, "deck", later)
            add_element(conn, struct, 2023)

    def test_the_two_control_rates_are_measured_separately(self):
        conn = make_conn()
        # 10 controls: 2 dropped, 3 rose, 5 unchanged.
        self._control(conn, [5, 5, 7, 7, 7, 6, 6, 6, 6, 6])
        result = validate(conn)
        assert result.control_evaluable == 10
        assert result.control_dropped == 2
        assert result.control_rose == 3
        assert result.control_drop_rate == 0.2
        assert result.control_rise_rate == 0.3

    def test_a_pessimistic_flag_is_scored_against_the_rise_rate(self):
        conn = make_conn()
        self._control(conn, [5, 5, 7, 7, 7, 6, 6, 6, 6, 6])   # drop 20%, rise 30%
        add_rating(conn, "P00001", 2023, "deck", 4)
        add_rating(conn, "P00001", 2025, "deck", 6)            # rose: confirmed
        add_element(conn, "P00001", 2023)
        add_flag(conn, "P00001", "deck", 4, direction="nbi_pessimistic")

        result = validate(conn)
        stats = result.direction_stats()["nbi_pessimistic"]
        assert stats["confirmed"] == 1
        assert stats["alignment"] == 1.0
        assert stats["base_rate"] == 0.3        # the rise rate, not the drop rate
        assert stats["lift"] == pytest.approx(0.7)

    def test_an_optimistic_flag_is_scored_against_the_drop_rate(self):
        conn = make_conn()
        self._control(conn, [5, 5, 7, 7, 7, 6, 6, 6, 6, 6])
        add_rating(conn, "O00001", 2023, "deck", 7)
        add_rating(conn, "O00001", 2025, "deck", 5)            # dropped: confirmed
        add_element(conn, "O00001", 2023)
        add_flag(conn, "O00001", "deck", 7, direction="nbi_optimistic")

        result = validate(conn)
        stats = result.direction_stats()["nbi_optimistic"]
        assert stats["base_rate"] == 0.2        # the drop rate
        assert stats["lift"] == pytest.approx(0.8)

    def test_the_pooled_base_rate_follows_the_flag_mix(self):
        """A pooled rate must weight each direction by how many flags it has."""
        conn = make_conn()
        self._control(conn, [5, 5, 7, 7, 7, 6, 6, 6, 6, 6])   # drop 20%, rise 30%
        # Three pessimistic flags, one optimistic. None move, so alignment is 0
        # and the lift is exactly the negative of the pooled base rate.
        for i in range(3):
            struct = f"P{i:05d}"
            add_rating(conn, struct, 2023, "deck", 4)
            add_rating(conn, struct, 2025, "deck", 4)
            add_element(conn, struct, 2023)
            add_flag(conn, struct, "deck", 4, direction="nbi_pessimistic")
        add_rating(conn, "O00001", 2023, "deck", 7)
        add_rating(conn, "O00001", 2025, "deck", 7)
        add_element(conn, "O00001", 2023)
        add_flag(conn, "O00001", "deck", 7, direction="nbi_optimistic")

        result = validate(conn)
        # 3 x 0.30 + 1 x 0.20 = 1.1 expected confirmations over 4 evaluable.
        assert result.expected_confirmations == pytest.approx(1.1)
        assert result.pooled_base_rate == pytest.approx(0.275)
        assert result.predictive_alignment == 0.0
        assert result.lift == pytest.approx(-0.275)

    def test_expected_confirmations_is_not_rounded_before_dividing(self):
        """Rounding the expected count for display must not move the lift."""
        conn = make_conn()
        self._control(conn, [5, 6, 6, 6, 6, 6, 6, 6, 6, 6])   # drop 10%, rise 0%
        add_rating(conn, "O00001", 2023, "deck", 7)
        add_rating(conn, "O00001", 2025, "deck", 5)
        add_element(conn, "O00001", 2023)
        add_flag(conn, "O00001", "deck", 7, direction="nbi_optimistic")

        result = validate(conn)
        # One evaluable flag x a 10% base rate = 0.1 expected, which rounds to
        # 0.1 for display but must stay 0.1 (not 0.0) in the rate.
        assert result.pooled_base_rate == pytest.approx(0.1)
        assert result.lift == pytest.approx(0.9)

    def test_the_report_shows_both_control_rates_and_per_direction_lift(self):
        conn = make_conn()
        self._control(conn, [5, 5, 7, 7, 7, 6, 6, 6, 6, 6])
        add_rating(conn, "P00001", 2023, "deck", 4)
        add_rating(conn, "P00001", 2025, "deck", 6)
        add_element(conn, "P00001", 2023)
        add_flag(conn, "P00001", "deck", 4, direction="nbi_pessimistic")

        text = render(validate(conn))
        assert "rose by 2025" in text
        assert "dropped by 2025" in text
        assert "each against its own base rate" in text


class TestSignificance:
    """A lift with no significance figure beside it is an uncaveated number."""

    def test_a_large_clear_difference_is_significant(self):
        z = validate.__globals__["ValidationResult"].two_proportion_z(160, 1268, 1042, 17652)
        assert z["z"] > 5
        assert z["p_value"] < 1e-6
        assert z["risk_ratio"] == pytest.approx(2.14, abs=0.01)

    def test_the_same_rate_in_both_groups_is_not_significant(self):
        z = validate.__globals__["ValidationResult"].two_proportion_z(10, 100, 100, 1000)
        assert abs(z["z"]) < 0.001
        assert z["p_value"] > 0.9
        assert z["risk_ratio"] == 1.0

    def test_a_tiny_sample_does_not_reach_significance(self):
        """Two flags out of three looks like a huge lift and means nothing."""
        z = validate.__globals__["ValidationResult"].two_proportion_z(2, 3, 100, 1000)
        assert z["p_value"] > 0.001

    def test_empty_groups_return_none_rather_than_a_number(self):
        cls = validate.__globals__["ValidationResult"]
        assert cls.two_proportion_z(0, 0, 10, 100) is None
        assert cls.two_proportion_z(1, 10, 0, 0) is None
        # No movement anywhere: there is no rate to compare, not a zero one.
        assert cls.two_proportion_z(0, 10, 0, 100) is None

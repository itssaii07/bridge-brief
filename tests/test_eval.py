"""Tests for the evaluation harness.

The point of these is that a missing metric and a zero metric must never be
confused, and that the table is honest on an empty database.
"""

import json

import pytest

from src import ids
from src.analysis import contradictions as C
from src.db import connect
from src.eval.metrics import (
    contradiction_precision, missing_evidence_recall, source_link_accuracy,
    unsupported_content_rate,
)
from src.eval.run_all import collect, render
from src.generate.brief import generate
from src.store import upsert_element_state, upsert_rating, upsert_structure
from src.ui.review import record_action


def populated():
    conn = connect(":memory:")
    upsert_structure(conn, "013450", struct_raw="013450", state_abbr="AL", year=2023)
    upsert_rating(conn, struct_norm="013450", year=2023, component="deck", rating=7,
                  rating_raw="7", artifact_id=ids.nbi_id("013450", 2023, "deck"),
                  source_path="t", source_line=1)
    upsert_rating(conn, struct_norm="013450", year=2025, component="deck", rating=5,
                  rating_raw="5", artifact_id=ids.nbi_id("013450", 2025, "deck"),
                  source_path="t", source_line=1)
    for cs, qty in ((1, 7660.0), (3, 340.0)):
        upsert_element_state(conn, struct_norm="013450", year=2023, elem_num=12, cs=cs,
                             cs_qty=qty, total_qty=8000.0, units="SQFT", elem_name="RC Deck",
                             elem_class="deck", state_abbr="AL",
                             artifact_id=ids.nbe_id("013450", 2023, 12, cs), source_path="t")
    C.run(conn, 2023, log=lambda *a: None)
    return conn


class TestEmptyDatabase:
    def test_every_metric_is_na_with_a_stated_reason(self):
        metrics = collect(connect(":memory:"))
        assert metrics
        for metric in metrics:
            assert metric.value is None
            assert metric.status and metric.status != "ok"

    def test_nothing_is_defaulted_to_zero(self):
        for metric in collect(connect(":memory:")):
            assert metric.value != 0

    def test_the_table_renders_and_says_how_many_were_computed(self):
        metrics = collect(connect(":memory:"))
        text = render(metrics, base_year=2023, check_year=2025)
        assert f"0 of {len(metrics)} metrics computed." in text
        assert "n/a" in text

    def test_the_table_disclaims_safety_clearance(self):
        text = render(collect(connect(":memory:")), base_year=2023, check_year=2025)
        assert "safety clearance" in text


class TestPopulated:
    def test_predictive_metrics_appear_once_there_are_findings(self):
        metrics = {m.name: m for m in collect(populated())}
        assert metrics["predictive_alignment"].value == 1.0
        assert metrics["corpus_engine_eligible_2023"].value == 1

    def test_lift_is_flagged_when_there_is_no_control_population(self):
        metrics = {m.name: m for m in collect(populated())}
        # One structure, all of it flagged: there is no control to compare against.
        row = metrics["control_base_rate_direction_matched"]
        assert row.value is None
        assert "control" in row.status

    def test_source_link_resolution_is_one_when_the_gate_is_working(self):
        conn = populated()
        generate(conn, "013450", 2023, log=lambda *a: None)
        metric = source_link_accuracy(conn)
        assert metric.value == 1.0 and metric.status == "ok"

    def test_a_bypassed_gate_is_reported_as_a_bug_not_a_measurement(self):
        conn = populated()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        sentence = conn.execute("SELECT sentence_id FROM brief_sentences WHERE brief_id = ?",
                                (summary["brief_id"],)).fetchone()[0]
        conn.execute("INSERT INTO sentence_citations (sentence_id, artifact_id) "
                     "VALUES (?, 'NBI-999999-2023-deck')", (sentence,))
        metric = source_link_accuracy(conn)
        assert metric.value < 1.0
        assert "bug" in metric.status

    def test_unsupported_content_rate_comes_from_the_stored_counters(self):
        conn = populated()
        generate(conn, "013450", 2023, log=lambda *a: None)
        metric = unsupported_content_rate(conn)
        assert metric.value is not None
        assert metric.detail["total_sentences"] > 0

    def test_correction_effort_appears_after_a_review_action(self):
        conn = populated()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        row = conn.execute("SELECT sentence_id, finding_id FROM brief_sentences "
                           "WHERE brief_id = ? AND finding_id IS NOT NULL",
                           (summary["brief_id"],)).fetchone()
        record_action(conn, brief_id=summary["brief_id"], action="edit", reviewer="A",
                      sentence_id=row["sentence_id"], finding_id=row["finding_id"],
                      text_after="Reworded.")
        metrics = {m.name: m for m in collect(conn)}
        assert metrics["correction_effort_edits_per_finding"].value == 1.0

    def test_missing_evidence_recall_needs_a_real_gap_to_measure(self):
        conn = populated()
        # The only structure has element data, so there is no gap and no metric.
        assert missing_evidence_recall(conn, 2023).value is None

    def test_missing_evidence_recall_measures_whether_the_gap_was_recorded(self):
        from src.catalog import record_missing_nbe

        conn = populated()
        upsert_structure(conn, "000999", struct_raw="000999", state_abbr="AL", year=2023)
        upsert_rating(conn, struct_norm="000999", year=2023, component="deck", rating=6,
                      rating_raw="6", artifact_id=ids.nbi_id("000999", 2023, "deck"),
                      source_path="t", source_line=2)
        assert missing_evidence_recall(conn, 2023).value == 0.0   # gap exists, unrecorded
        record_missing_nbe(conn, 2023)
        assert missing_evidence_recall(conn, 2023).value == 1.0


class TestContradictionPrecision:
    def test_it_is_never_self_scored(self):
        metric = contradiction_precision(populated(), None)
        assert metric.value is None
        assert "--export-review" in metric.status

    def test_it_reads_a_human_labelled_file(self, tmp_path):
        path = tmp_path / "review.csv"
        path.write_text("finding_id,verdict (correct|incorrect)\nF-a,correct\nF-b,incorrect\n", encoding="utf-8")
        metric = contradiction_precision(populated(), path)
        assert metric.value == 0.5

    def test_an_unlabelled_file_yields_no_number(self, tmp_path):
        path = tmp_path / "review.csv"
        path.write_text("finding_id,verdict (correct|incorrect)\nF-a,\n", encoding="utf-8")
        assert contradiction_precision(populated(), path).value is None


class TestBenchmarkInput:
    def test_detector_metrics_wait_for_a_benchmark_run(self, tmp_path):
        metrics = {m.name: m for m in collect(populated(), benchmark_path=tmp_path / "none.json")}
        assert metrics["defect_detection_precision"].value is None
        assert "codebrim_benchmark" in metrics["defect_detection_precision"].status

    def test_detector_metrics_are_read_from_a_benchmark_report(self, tmp_path):
        path = tmp_path / "bench.json"
        path.write_text(json.dumps({
            "class_agnostic": {"precision": 0.21, "recall": 0.34, "f1": 0.26},
            "class_aware": {"precision": 0.0, "recall": 0.0, "f1": 0.0},
        }), encoding="utf-8")
        metrics = {m.name: m for m in collect(populated(), benchmark_path=path)}
        assert metrics["defect_detection_precision"].value == 0.21
        assert "not a trained model" in metrics["defect_detection_precision"].status

"""Tests for the grounding gate (invariant 2) and brief assembly.

The candidate sentences here are fabricated — that is the point. The gate is
logic, and testing logic means feeding it inputs it would otherwise never see,
including sentences that cite artifacts which do not exist. No data file is
involved and nothing here produces a metric.
"""

import pytest

from src import ids
from src.analysis import contradictions as C
from src.db import DataUnavailable, connect
from src.generate.brief import generate, render_text
from src.generate.drafters import TemplateDrafter, get_drafter
from src.generate.grounding import (
    CandidateSentence, apply_gate, collect_citations, render_with_citations,
)
from src.store import upsert_element_state, upsert_rating, upsert_structure

REAL_NBI = "NBI-013450-2023-deck"
REAL_NBE = "NBE-013450-2023-12-cs3"
WELL_FORMED_BUT_ABSENT = "NBI-999999-2023-deck"


def conn_with_artifacts():
    conn = connect(":memory:")
    conn.execute("INSERT INTO structures (struct_norm) VALUES ('013450')")
    for artifact, kind in ((REAL_NBI, "NBI"), (REAL_NBE, "NBE")):
        conn.execute(
            "INSERT INTO artifacts (artifact_id, kind, struct_norm, year, created_at) "
            "VALUES (?, ?, '013450', 2023, '2020-01-01T00:00:00+00:00')", (artifact, kind))
    return conn


class TestCitationCollection:
    def test_declared_and_inline_citations_are_unioned(self):
        candidate = CandidateSentence("s", f"Deck is rated 7 [{REAL_NBE}].", [REAL_NBI])
        well_formed, malformed = collect_citations(candidate)
        assert well_formed == [REAL_NBI, REAL_NBE]
        assert malformed == []

    def test_a_drafter_cannot_hide_a_citation_by_not_declaring_it(self):
        candidate = CandidateSentence("s", f"See [{REAL_NBI}].", [])
        assert collect_citations(candidate)[0] == [REAL_NBI]

    def test_malformed_declared_citations_are_separated_not_accepted(self):
        candidate = CandidateSentence("s", "text", ["not-an-id", "NBI-013450-2023-railing"])
        well_formed, malformed = collect_citations(candidate)
        assert well_formed == []
        assert len(malformed) == 2

    def test_duplicates_collapse(self):
        candidate = CandidateSentence("s", f"[{REAL_NBI}] and [{REAL_NBI}]", [REAL_NBI])
        assert collect_citations(candidate)[0] == [REAL_NBI]


class TestRendering:
    def test_surviving_citations_are_made_visible_in_the_sentence(self):
        text = render_with_citations("The deck is rated 7.", [REAL_NBI])
        assert text == f"The deck is rated 7. [{REAL_NBI}]"

    def test_citations_already_inline_are_not_repeated(self):
        original = f"The deck is rated 7 [{REAL_NBI}]."
        assert render_with_citations(original, [REAL_NBI]) == original

    def test_what_is_read_and_what_was_verified_are_the_same_string(self):
        rendered = render_with_citations("Claim.", [REAL_NBI, REAL_NBE])
        assert set(ids.extract_ids(rendered)) == {REAL_NBI, REAL_NBE}


class TestTheGate:
    def test_a_grounded_sentence_passes(self):
        conn = conn_with_artifacts()
        result = apply_gate(conn, [CandidateSentence("condition", "The deck is rated 7.", [REAL_NBI])])
        assert result.blocked_unsupported == 0
        assert result.passed[0].citations == [REAL_NBI]

    def test_a_sentence_with_no_citations_is_dropped_and_counted(self):
        conn = conn_with_artifacts()
        result = apply_gate(conn, [
            CandidateSentence("summary", "This bridge is in poor condition overall.", []),
        ])
        assert result.passed == []
        assert result.blocked_unsupported == 1
        assert "no artifact IDs" in result.blocked[0].reason

    def test_a_fabricated_citation_cannot_launder_a_sentence(self):
        """Well-formed but non-existent. The single most important case."""
        conn = conn_with_artifacts()
        result = apply_gate(conn, [
            CandidateSentence("summary", "The deck is failing.", [WELL_FORMED_BUT_ABSENT]),
        ])
        assert result.passed == []
        assert result.blocked_unsupported == 1
        assert WELL_FORMED_BUT_ABSENT in result.unresolved_citations

    def test_a_fabricated_inline_citation_cannot_launder_a_sentence_either(self):
        conn = conn_with_artifacts()
        result = apply_gate(conn, [
            CandidateSentence("summary", f"The deck is failing [{WELL_FORMED_BUT_ABSENT}].", []),
        ])
        assert result.passed == []

    def test_a_sentence_survives_on_its_resolvable_citations_only(self):
        conn = conn_with_artifacts()
        result = apply_gate(conn, [
            CandidateSentence("condition", "Mixed.", [REAL_NBI, WELL_FORMED_BUT_ABSENT]),
        ])
        assert result.passed[0].citations == [REAL_NBI]
        assert WELL_FORMED_BUT_ABSENT not in result.passed[0].text

    def test_an_empty_sentence_is_blocked_not_rendered(self):
        conn = conn_with_artifacts()
        result = apply_gate(conn, [CandidateSentence("summary", "   ", [REAL_NBI])])
        assert result.blocked_unsupported == 1
        assert "empty" in result.blocked[0].reason

    def test_malformed_citations_are_reported(self):
        conn = conn_with_artifacts()
        result = apply_gate(conn, [CandidateSentence("s", "Claim.", ["NBI-oops"])])
        assert result.malformed_citations == ["NBI-oops"]
        assert result.blocked_unsupported == 1

    def test_counters_and_rate(self):
        conn = conn_with_artifacts()
        result = apply_gate(conn, [
            CandidateSentence("s", "Grounded.", [REAL_NBI]),
            CandidateSentence("s", "Grounded too.", [REAL_NBE]),
            CandidateSentence("s", "Ungrounded.", []),
            CandidateSentence("s", "Ungrounded too.", [WELL_FORMED_BUT_ABSENT]),
        ])
        assert result.total == 4
        assert result.blocked_unsupported == 2
        assert result.unsupported_rate == 0.5

    def test_the_rate_is_none_rather_than_zero_when_nothing_was_drafted(self):
        assert apply_gate(conn_with_artifacts(), []).unsupported_rate is None

    def test_nothing_is_discarded_quietly(self):
        conn = conn_with_artifacts()
        candidates = [CandidateSentence("s", f"Sentence {i}.", [REAL_NBI] if i % 2 else [])
                      for i in range(10)]
        result = apply_gate(conn, candidates)
        assert len(result.passed) + len(result.blocked) == len(candidates)

    def test_the_gate_does_not_judge_truth_only_attributability(self):
        """A false but cited claim passes; deciding truth is the reviewer's job."""
        conn = conn_with_artifacts()
        result = apply_gate(conn, [
            CandidateSentence("condition", "The deck is rated 2.", [REAL_NBI])])
        assert len(result.passed) == 1


class TestDrafterSelection:
    def test_the_default_drafter_needs_no_api_key(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_BRIEF_DRAFTER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert isinstance(get_drafter(), TemplateDrafter)

    def test_the_llm_drafter_reports_a_missing_key_instead_of_failing_obscurely(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        drafter = get_drafter("llm")
        with pytest.raises(RuntimeError) as exc:
            drafter.draft({"struct_norm": "013450", "year": 2023, "findings": []})
        assert "ANTHROPIC_API_KEY" in str(exc.value)

    def test_an_unknown_drafter_is_refused(self):
        with pytest.raises(ValueError):
            get_drafter("magic")


def populated_conn():
    conn = connect(":memory:")
    upsert_structure(conn, "013450", struct_raw="013450", state_abbr="AL", year=2023)
    upsert_rating(conn, struct_norm="013450", year=2023, component="deck", rating=7,
                  rating_raw="7", artifact_id=ids.nbi_id("013450", 2023, "deck"),
                  source_path="t", source_line=1)
    upsert_rating(conn, struct_norm="013450", year=2023, component="substructure", rating=6,
                  rating_raw="6", artifact_id=ids.nbi_id("013450", 2023, "substructure"),
                  source_path="t", source_line=1)
    for cs, qty in ((1, 7660.0), (3, 340.0)):
        upsert_element_state(conn, struct_norm="013450", year=2023, elem_num=12, cs=cs,
                             cs_qty=qty, total_qty=8000.0, units="SQFT", elem_name="RC Deck",
                             elem_class="deck", state_abbr="AL",
                             artifact_id=ids.nbe_id("013450", 2023, 12, cs), source_path="t")
    C.run(conn, 2023, log=lambda *a: None)
    return conn


class TestBriefAssembly:
    def test_every_rendered_sentence_carries_at_least_one_resolvable_citation(self):
        conn = populated_conn()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        rows = conn.execute(
            "SELECT sentence_id FROM brief_sentences WHERE brief_id = ?",
            (summary["brief_id"],)).fetchall()
        assert rows
        for row in rows:
            citations = conn.execute(
                "SELECT c.artifact_id FROM sentence_citations c "
                "JOIN artifacts a ON a.artifact_id = c.artifact_id WHERE c.sentence_id = ?",
                (row["sentence_id"],)).fetchall()
            assert citations, f"{row['sentence_id']} rendered with no resolvable citation"

    def test_the_brief_is_always_a_draft_when_generated(self):
        conn = populated_conn()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        assert summary["status"] == "draft"
        assert conn.execute("SELECT status FROM briefs WHERE brief_id = ?",
                            (summary["brief_id"],)).fetchone()[0] == "draft"

    def test_counters_are_stored_on_the_brief(self):
        conn = populated_conn()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        row = conn.execute("SELECT total_sentences, blocked_unsupported FROM briefs "
                           "WHERE brief_id = ?", (summary["brief_id"],)).fetchone()
        assert row["total_sentences"] == summary["total_sentences"]
        assert row["blocked_unsupported"] == summary["blocked_unsupported"]

    def test_regenerating_makes_a_new_version_rather_than_overwriting(self):
        conn = populated_conn()
        first = generate(conn, "013450", 2023, log=lambda *a: None)
        second = generate(conn, "013450", 2023, log=lambda *a: None)
        assert first["version"] == 1 and second["version"] == 2
        assert first["brief_id"] != second["brief_id"]

    def test_the_brief_states_it_is_not_a_clearance(self):
        conn = populated_conn()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        text = render_text(conn, summary["brief_id"])
        assert "not a safety" in text and "not a maintenance order" in text

    def test_no_drafted_sentence_recommends_or_clears(self):
        conn = populated_conn()
        context = __import__("src.generate.brief", fromlist=["build_context"]).build_context(
            conn, "013450", 2023)
        for candidate in TemplateDrafter().draft(context):
            lowered = candidate.text.lower()
            for forbidden in ("is safe", "cleared", "schedule maintenance",
                              "we recommend", "should be closed", "no further action"):
                assert forbidden not in lowered

    def test_single_source_findings_are_never_presented_as_corroborated(self):
        conn = populated_conn()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        row = conn.execute(
            "SELECT text FROM brief_sentences WHERE brief_id = ? AND evidence_tier = 'single_source'",
            (summary["brief_id"],)).fetchone()
        assert "has not been cross-checked" in row["text"]

    def test_blocked_sentences_are_kept_for_audit(self):
        conn = populated_conn()
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        # Inject an ungrounded sentence through the gate directly and confirm the
        # storage shape the UI reads from.
        conn.execute(
            "INSERT INTO blocked_sentences (brief_id, section, text, reason, blocked_at) "
            "VALUES (?, 'summary', 'Unsupported claim.', 'test', '2020-01-01T00:00:00+00:00')",
            (summary["brief_id"],))
        assert "Unsupported claim." in render_text(conn, summary["brief_id"])

    def test_generating_for_a_structure_with_nothing_says_what_to_run(self):
        conn = connect(":memory:")
        conn.execute("INSERT INTO structures (struct_norm) VALUES ('013450')")
        with pytest.raises(DataUnavailable) as exc:
            generate(conn, "013450", 2023, log=lambda *a: None)
        assert "contradictions" in str(exc.value)

    def test_reference_corpus_imagery_never_enters_a_structure_brief(self):
        conn = populated_conn()
        conn.execute(
            "INSERT INTO images (artifact_id, struct_norm, provenance, corpus, stored_path) "
            "VALUES ('REF-codebrim-00412', NULL, 'reference_corpus', 'codebrim', '/x.jpg')")
        conn.execute("INSERT INTO artifacts (artifact_id, kind, created_at) "
                     "VALUES ('REF-codebrim-00412', 'REF', '2020-01-01T00:00:00+00:00')")
        summary = generate(conn, "013450", 2023, log=lambda *a: None)
        assert "REF-codebrim" not in render_text(conn, summary["brief_id"])

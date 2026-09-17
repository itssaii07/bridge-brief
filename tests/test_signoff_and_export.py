"""The sign-off queue, the publication gate, and human sentence verification.

These cover the three clauses of the problem statement that were unimplemented
or only satisfied by absence:

* "a human sign-off queue" — a named deliverable that did not exist
* "require human review before publication, external sharing, or high-stakes
  use" — previously satisfied because there was no publication path at all,
  which is a missing feature rather than a safeguard
* "factual fidelity" and the semantic half of source-link accuracy — neither
  had a mechanism for a human to record a judgement
"""

import csv

import pytest

from src import ids
from src.analysis import contradictions as C
from src.db import DataUnavailable, connect
from src.eval import sentence_review
from src.generate.brief import generate
from src.generate.export import (
    DISCLAIMER, ReviewRequired, assert_publishable, export_brief, signoff_record,
)
from src.store import upsert_element_state, upsert_rating, upsert_structure
from src.ui.review import (
    PENDING_STATUSES, queue_totals, record_action, sign_off, signoff_queue,
)


def build(tmp_path, struct="AL013450"):
    """One structure with a real contradiction, and a brief generated from it."""
    conn = connect(tmp_path / "assets.sqlite")
    upsert_structure(conn, struct, struct_raw=struct, state_abbr="AL", year=2023)
    upsert_rating(conn, struct_norm=struct, year=2023, component="deck", rating=7,
                  rating_raw="7", artifact_id=ids.nbi_id(struct, 2023, "deck"),
                  source_path="t", source_line=1)
    for cs, qty in ((1, 7660.0), (3, 340.0)):
        upsert_element_state(conn, struct_norm=struct, year=2023, elem_num=12, cs=cs,
                             cs_qty=qty, total_qty=8000.0, units="SQFT",
                             elem_name="RC Deck", elem_class="deck", state_abbr="AL",
                             artifact_id=ids.nbe_id(struct, 2023, 12, cs),
                             source_path="t")
    C.run(conn, 2023, log=lambda *a: None)
    summary = generate(conn, struct, 2023, log=lambda *a: None)
    conn.commit()
    return conn, summary["brief_id"]


class TestSignoffQueue:
    def test_a_fresh_brief_is_in_the_queue(self, tmp_path):
        conn, brief_id = build(tmp_path)
        rows = signoff_queue(conn)
        assert [r["brief_id"] for r in rows] == [brief_id]
        assert rows[0]["status"] == "draft"
        assert rows[0]["findings_reviewed"] == 0
        assert rows[0]["findings_total"] >= 1

    def test_a_signed_off_brief_leaves_the_queue(self, tmp_path):
        conn, brief_id = build(tmp_path)
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector")
        assert signoff_queue(conn) == []
        # ...but is still findable when asked for explicitly.
        assert [r["brief_id"] for r in signoff_queue(conn, include_decided=True)] == [brief_id]

    def test_a_rejected_brief_also_leaves_the_queue(self, tmp_path):
        conn, brief_id = build(tmp_path)
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector", decision="rejected")
        assert signoff_queue(conn) == []

    def test_in_review_sorts_before_draft(self, tmp_path):
        """Finish what is started before starting more."""
        conn, first = build(tmp_path, struct="AL000001")
        upsert_structure(conn, "AL000002", struct_raw="000002", state_abbr="AL", year=2023)
        upsert_rating(conn, struct_norm="AL000002", year=2023, component="deck",
                      rating=7, rating_raw="7",
                      artifact_id=ids.nbi_id("AL000002", 2023, "deck"),
                      source_path="t", source_line=1)
        for cs, qty in ((1, 7660.0), (3, 340.0)):
            upsert_element_state(conn, struct_norm="AL000002", year=2023, elem_num=12,
                                 cs=cs, cs_qty=qty, total_qty=8000.0, units="SQFT",
                                 elem_name="RC Deck", elem_class="deck",
                                 state_abbr="AL",
                                 artifact_id=ids.nbe_id("AL000002", 2023, 12, cs),
                                 source_path="t")
        C.run(conn, 2023, log=lambda *a: None)
        second = generate(conn, "AL000002", 2023, log=lambda *a: None)["brief_id"]

        # Touching the second brief puts it in review, so it must come first.
        states = conn.execute(
            "SELECT finding_id FROM findings WHERE struct_norm = 'AL000002' LIMIT 1"
        ).fetchone()
        record_action(conn, brief_id=second, action="approve", reviewer="A",
                      finding_id=states["finding_id"])
        order = [r["brief_id"] for r in signoff_queue(conn)]
        assert order.index(second) < order.index(first)

    def test_the_order_is_deterministic(self, tmp_path):
        conn, _ = build(tmp_path)
        assert ([r["brief_id"] for r in signoff_queue(conn)]
                == [r["brief_id"] for r in signoff_queue(conn)])

    def test_totals_account_for_every_brief(self, tmp_path):
        conn, brief_id = build(tmp_path)
        totals = queue_totals(conn)
        assert totals["awaiting review"] == 1
        assert totals["draft"] == 1
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector")
        after = queue_totals(conn)
        assert after["awaiting review"] == 0 and after["signed off"] == 1

    def test_the_queue_page_states_its_ordering_and_the_no_authority_rule(self, tmp_path):
        from src.ui import views

        conn, _ = build(tmp_path)
        html = views.signoff_queue_view(signoff_queue(conn), queue_totals(conn),
                                        include_decided=False)
        assert "Awaiting human sign-off" in html
        assert "deterministic" in html            # the reviewer can predict the order
        assert "no authority" in html             # invariant 4, on the page
        assert "cannot be exported" in html or "exported" in html

    def test_pending_statuses_are_exactly_the_undecided_ones(self):
        assert set(PENDING_STATUSES) == {"draft", "in_review"}


class TestPublicationGate:
    """"Require human review before publication, external sharing, or
    high-stakes use." Previously satisfied because no publication path existed."""

    def test_an_unsigned_brief_cannot_be_published(self, tmp_path):
        conn, brief_id = build(tmp_path)
        with pytest.raises(ReviewRequired) as exc:
            export_brief(conn, brief_id, tmp_path / "out.md")
        message = str(exc.value)
        assert "has not been signed off" in message
        assert "no flag to bypass" in message
        assert not (tmp_path / "out.md").exists()

    def test_a_rejected_brief_cannot_be_published(self, tmp_path):
        conn, brief_id = build(tmp_path)
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector",
                 decision="rejected", note="element roll-up looks wrong")
        with pytest.raises(ReviewRequired) as exc:
            export_brief(conn, brief_id, tmp_path / "out.md")
        assert "'rejected'" in str(exc.value)
        assert "element roll-up looks wrong" in str(exc.value)

    def test_a_signed_off_brief_publishes_and_carries_its_signoff(self, tmp_path):
        conn, brief_id = build(tmp_path)
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector", note="checked")
        result = export_brief(conn, brief_id, tmp_path / "out.md")
        text = (tmp_path / "out.md").read_text(encoding="utf-8")
        assert result["reviewer"] == "A. Inspector"
        assert "A. Inspector" in text and "checked" in text
        # The disclaimer travels with the file, which outlives the UI.
        assert "NOT a safety clearance" in text
        assert "blocked_unsupported" in text

    def test_every_published_sentence_still_carries_its_citations(self, tmp_path):
        conn, brief_id = build(tmp_path)
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector")
        export_brief(conn, brief_id, tmp_path / "out.md")
        text = (tmp_path / "out.md").read_text(encoding="utf-8")
        assert "NBI-AL013450-2023-deck" in text
        # And the evidence table resolves each one back to its source.
        assert "## Evidence" in text

    def test_json_export_is_also_gated(self, tmp_path):
        conn, brief_id = build(tmp_path)
        with pytest.raises(ReviewRequired):
            export_brief(conn, brief_id, tmp_path / "out.json", fmt="json")
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector")
        export_brief(conn, brief_id, tmp_path / "out.json", fmt="json")
        import json as _json
        payload = _json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        assert payload["signoff"]["reviewer"] == "A. Inspector"
        assert payload["disclaimer"] == DISCLAIMER

    def test_a_signoff_does_not_carry_over_to_a_regenerated_brief(self, tmp_path):
        """The reviewer approved a specific document, not the structure."""
        conn, brief_id = build(tmp_path)
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector")
        export_brief(conn, brief_id, tmp_path / "v1.md")      # fine

        # Regenerating makes a NEW version; v1's sign-off must not cover it.
        second = generate(conn, "AL013450", 2023, log=lambda *a: None)["brief_id"]
        assert second != brief_id
        with pytest.raises(ReviewRequired):
            export_brief(conn, second, tmp_path / "v2.md")

    def test_the_gate_reads_the_append_only_trail_not_the_status_column(self, tmp_path):
        """A hand-edited status column must not open the gate."""
        conn, brief_id = build(tmp_path)
        conn.execute("UPDATE briefs SET status = 'signed_off' WHERE brief_id = ?",
                     (brief_id,))
        conn.commit()
        assert signoff_record(conn, brief_id) is None
        with pytest.raises(ReviewRequired):
            assert_publishable(conn, brief_id)

    def test_an_unknown_format_is_refused_before_anything_is_written(self, tmp_path):
        conn, brief_id = build(tmp_path)
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector")
        with pytest.raises(ValueError):
            export_brief(conn, brief_id, tmp_path / "out.txt", fmt="pdf")

    def test_there_is_no_force_or_bypass_option(self):
        """A flag to publish an unreviewed brief would be the hole this closes.

        Walks the AST rather than the source text, so the module's own docstring
        explaining that it has no ``--force`` is not mistaken for one.
        """
        import ast
        import inspect

        from src.generate import export

        tree = ast.parse(inspect.getsource(export))
        banned = ("force", "allow_unsigned", "skip_review", "bypass", "no_review")

        # No CLI flag.
        flags = [
            arg.value for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        assert not [f for f in flags if any(b in f.lower() for b in banned)], flags

        # No keyword parameter on any function either.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [a.arg for a in node.args.args + node.args.kwonlyargs]
                assert not [n for n in names
                            if any(b in n.lower() for b in banned)], (node.name, names)


class TestHumanSentenceVerification:
    """Factual fidelity and the semantic half of source-link accuracy."""

    def test_the_sample_carries_what_a_reviewer_needs_to_judge(self, tmp_path):
        conn, brief_id = build(tmp_path)
        path = tmp_path / "sentences.csv"
        result = sentence_review.export_sample(conn, path, n=10)
        assert result["sentences"] >= 1
        rows = list(csv.DictReader(open(path, encoding="utf-8", newline="")))
        first = rows[0]
        # The sentence, its cited IDs, and the summary of each cited artifact —
        # so the judgement needs neither the database nor the code.
        assert first["sentence"]
        assert first["cited_artifacts"]
        assert "NBI-" in first["cited_summaries"] or "NBE-" in first["cited_summaries"]
        assert first["evidence_tier"]

    def test_the_sample_is_deterministic(self, tmp_path):
        conn, _ = build(tmp_path)
        a, b = tmp_path / "a.csv", tmp_path / "b.csv"
        sentence_review.export_sample(conn, a, n=5)
        sentence_review.export_sample(conn, b, n=5)
        assert a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")

    def test_an_unlabelled_sample_yields_none_not_a_pass(self, tmp_path):
        conn, _ = build(tmp_path)
        path = tmp_path / "sentences.csv"
        sentence_review.export_sample(conn, path, n=5)
        stats = sentence_review.read_labels(path)
        assert stats["factual_fidelity"] is None
        assert stats["source_link_accuracy_semantic"] is None
        assert stats["unlabelled"] == stats["rows"]

    def test_labels_are_read_back_into_the_two_metrics(self, tmp_path):
        conn, _ = build(tmp_path)
        path = tmp_path / "sentences.csv"
        sentence_review.export_sample(conn, path, n=10)

        rows = list(csv.DictReader(open(path, encoding="utf-8", newline="")))
        header = list(rows[0].keys())
        verdicts = [("yes", "yes"), ("yes", "no"), ("no", "no")]
        for row, (supported, factual) in zip(rows, verdicts):
            row[sentence_review.VERDICT_COLUMNS[0]] = supported
            row[sentence_review.VERDICT_COLUMNS[1]] = factual
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)

        stats = sentence_review.read_labels(path)
        # 2 of 3 supported, 1 of 3 factual, among the labelled rows only.
        assert stats["source_link_judged"] == 3
        assert stats["source_link_accuracy_semantic"] == pytest.approx(2 / 3, abs=1e-4)
        assert stats["factual_judged"] == 3
        assert stats["factual_fidelity"] == pytest.approx(1 / 3, abs=1e-4)

    def test_a_missing_review_file_is_reported(self, tmp_path):
        with pytest.raises(DataUnavailable):
            sentence_review.read_labels(tmp_path / "nope.csv")

    def test_no_brief_sentences_is_reported_not_scored_as_perfect(self, tmp_path):
        conn = connect(tmp_path / "empty.sqlite")
        with pytest.raises(DataUnavailable) as exc:
            sentence_review.export_sample(conn, tmp_path / "s.csv")
        assert "src.generate.brief" in str(exc.value)

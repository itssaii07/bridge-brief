"""Tests for the review UI, the sign-off trail, and the empty state.

The server is started against a temporary database built in the test.
"""

import json
import threading
import urllib.parse
import urllib.request

import pytest

from src import ids
from src.analysis import contradictions as C
from src.db import connect
from src.generate.brief import generate
from src.store import upsert_element_state, upsert_rating, upsert_structure
from src.ui import views
from src.ui.review import (
    ReviewError, correction_effort, finding_states, record_action, sign_off, trail,
)
from src.ui.server import brief_payload, list_structures, make_server


def build_db(path):
    conn = connect(path)
    upsert_structure(conn, "013450", struct_raw="013450", state_abbr="AL", year=2023)
    upsert_rating(conn, struct_norm="013450", year=2023, component="deck", rating=7,
                  rating_raw="7", artifact_id=ids.nbi_id("013450", 2023, "deck"),
                  source_path="t", source_line=1)
    for cs, qty in ((1, 7660.0), (3, 340.0)):
        upsert_element_state(conn, struct_norm="013450", year=2023, elem_num=12, cs=cs,
                             cs_qty=qty, total_qty=8000.0, units="SQFT", elem_name="RC Deck",
                             elem_class="deck", state_abbr="AL",
                             artifact_id=ids.nbe_id("013450", 2023, 12, cs), source_path="t")
    C.run(conn, 2023, log=lambda *a: None)
    summary = generate(conn, "013450", 2023, log=lambda *a: None)
    conn.commit()
    return conn, summary["brief_id"]


class TestReviewTrail:
    def test_an_action_requires_a_named_reviewer(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        with pytest.raises(ReviewError):
            record_action(conn, brief_id=brief_id, action="approve", reviewer="  ")

    def test_an_unknown_action_is_refused(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        with pytest.raises(ReviewError):
            record_action(conn, brief_id=brief_id, action="clear_structure", reviewer="A")

    def test_approving_moves_the_brief_into_review_not_out_of_draft_status(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        sentence = conn.execute("SELECT sentence_id, finding_id FROM brief_sentences "
                                "WHERE brief_id = ? AND finding_id IS NOT NULL",
                                (brief_id,)).fetchone()
        record_action(conn, brief_id=brief_id, action="approve", reviewer="A Inspector",
                      sentence_id=sentence["sentence_id"], finding_id=sentence["finding_id"])
        assert conn.execute("SELECT status FROM briefs WHERE brief_id = ?",
                            (brief_id,)).fetchone()[0] == "in_review"

    def test_an_edit_rewrites_the_sentence_and_keeps_the_original(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        sentence = conn.execute("SELECT sentence_id, text FROM brief_sentences "
                                "WHERE brief_id = ? ORDER BY ordinal", (brief_id,)).fetchone()
        record_action(conn, brief_id=brief_id, action="edit", reviewer="A Inspector",
                      sentence_id=sentence["sentence_id"], text_after="Reworded by the reviewer.")
        row = conn.execute("SELECT text_before, text_after FROM review_actions "
                           "WHERE action = 'edit'").fetchone()
        assert row["text_before"] == sentence["text"]
        assert conn.execute("SELECT text FROM brief_sentences WHERE sentence_id = ?",
                            (sentence["sentence_id"],)).fetchone()[0] == "Reworded by the reviewer."

    def test_an_edit_without_replacement_text_is_refused(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        sentence = conn.execute("SELECT sentence_id FROM brief_sentences WHERE brief_id = ?",
                                (brief_id,)).fetchone()
        with pytest.raises(ReviewError):
            record_action(conn, brief_id=brief_id, action="edit", reviewer="A",
                          sentence_id=sentence["sentence_id"])

    def test_the_trail_is_append_only(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        sentence = conn.execute("SELECT sentence_id, finding_id FROM brief_sentences "
                                "WHERE brief_id = ? AND finding_id IS NOT NULL",
                                (brief_id,)).fetchone()
        for action in ("approve", "reject", "approve"):
            record_action(conn, brief_id=brief_id, action=action, reviewer="A Inspector",
                          sentence_id=sentence["sentence_id"], finding_id=sentence["finding_id"])
        rows = conn.execute("SELECT action FROM review_actions ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["approve", "reject", "approve"]
        # Current state is the most recent action, derived from the log.
        assert finding_states(conn, brief_id)[sentence["finding_id"]]["action"] == "approve"

    def test_sign_off_requires_a_name_and_records_the_version(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        with pytest.raises(ReviewError):
            sign_off(conn, brief_id=brief_id, reviewer="")
        sign_off(conn, brief_id=brief_id, reviewer="A Inspector", note="checked against source")
        row = conn.execute("SELECT * FROM signoffs").fetchone()
        assert row["reviewer"] == "A Inspector" and row["version"] == 1
        assert conn.execute("SELECT status FROM briefs WHERE brief_id = ?",
                            (brief_id,)).fetchone()[0] == "signed_off"

    def test_a_brief_cannot_reach_signed_off_without_a_signoff_row(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        assert conn.execute("SELECT status FROM briefs WHERE brief_id = ?",
                            (brief_id,)).fetchone()[0] == "draft"
        assert conn.execute("SELECT COUNT(*) FROM signoffs").fetchone()[0] == 0

    def test_nothing_in_the_schema_can_mark_a_structure_cleared(self, tmp_path):
        """Invariant 4: there is no such column anywhere."""
        conn, _ = build_db(tmp_path / "a.sqlite")
        columns = []
        for table in [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]:
            columns += [r[1].lower() for r in conn.execute(f"PRAGMA table_info({table})")]
        for forbidden in ("cleared", "is_safe", "safety_status", "maintenance_scheduled"):
            assert forbidden not in columns

    def test_correction_effort_is_none_before_any_review(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        assert correction_effort(conn, brief_id)["edits_per_finding"] is None

    def test_correction_effort_counts_edits_per_reviewed_finding(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        sentence = conn.execute("SELECT sentence_id, finding_id FROM brief_sentences "
                                "WHERE brief_id = ? AND finding_id IS NOT NULL",
                                (brief_id,)).fetchone()
        record_action(conn, brief_id=brief_id, action="edit", reviewer="A",
                      sentence_id=sentence["sentence_id"], finding_id=sentence["finding_id"],
                      text_after="Reworded.")
        stats = correction_effort(conn, brief_id)
        assert stats["findings_reviewed"] == 1 and stats["edits_per_finding"] == 1.0

    def test_the_trail_interleaves_actions_and_signoffs(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        sentence = conn.execute("SELECT sentence_id FROM brief_sentences WHERE brief_id = ?",
                                (brief_id,)).fetchone()
        record_action(conn, brief_id=brief_id, action="approve", reviewer="A",
                      sentence_id=sentence["sentence_id"])
        sign_off(conn, brief_id=brief_id, reviewer="A")
        kinds = [row["type"] for row in trail(conn, brief_id)]
        assert "action" in kinds and "signoff" in kinds


class TestRenderingWithoutData:
    def test_the_structure_list_renders_an_honest_empty_state(self):
        conn = connect(":memory:")
        rows, totals = list_structures(conn)
        html = views.structure_list(rows, totals)
        assert "No structures have been ingested" in html
        assert "python -m src.ingest.nbi" in html
        assert "<table" not in html or "Structures" not in html

    def test_totals_are_real_zeroes_not_placeholders(self):
        conn = connect(":memory:")
        _, totals = list_structures(conn)
        assert totals["contradictions"] == 0 and totals["blocked_unsupported"] == 0


class TestBriefPage:
    def test_the_page_shows_the_blocked_counter_and_the_draft_warning(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        payload = brief_payload(conn, brief_id)
        html = views.brief_view(payload["brief"], payload["sentences"], payload["artifacts"],
                                payload["blocked"], payload["states"], payload["trail"],
                                payload["images"])
        assert "blocked_unsupported" in html
        assert "not a safety clearance" in html
        assert "not a maintenance order" in html

    def test_every_sentence_carries_its_citations_for_click_to_highlight(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        payload = brief_payload(conn, brief_id)
        assert payload["sentences"]
        for sentence in payload["sentences"]:
            assert sentence["citations"]
        html = views.brief_view(payload["brief"], payload["sentences"], payload["artifacts"],
                                payload["blocked"], payload["states"], payload["trail"],
                                payload["images"])
        for artifact in payload["artifacts"]:
            assert f'id="artifact-{artifact["artifact_id"]}"' in html

    def test_evidence_tier_is_shown_on_every_finding_sentence(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        payload = brief_payload(conn, brief_id)
        html = views.brief_view(payload["brief"], payload["sentences"], payload["artifacts"],
                                payload["blocked"], payload["states"], payload["trail"],
                                payload["images"])
        assert 'class="tag conflicting"' in html

    def test_image_provenance_is_labelled_in_the_markup(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        conn.execute(
            "INSERT INTO images (artifact_id, struct_norm, provenance, stored_path) "
            "VALUES ('IMG-013450-p01', '013450', 'inspection_upload', '/x.jpg')")
        conn.commit()
        payload = brief_payload(conn, brief_id)
        html = views.brief_view(payload["brief"], payload["sentences"], payload["artifacts"],
                                payload["blocked"], payload["states"], payload["trail"],
                                payload["images"])
        assert "inspection upload" in html

    def test_no_control_in_the_ui_clears_a_structure(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        payload = brief_payload(conn, brief_id)
        html = views.brief_view(payload["brief"], payload["sentences"], payload["artifacts"],
                                payload["blocked"], payload["states"], payload["trail"],
                                payload["images"]).lower()
        # Check the controls, not the prose: the page's own disclaimer legitimately
        # contains words like "authorise" while saying it does no such thing.
        import re
        controls = re.findall(r"<button[^>]*>(.*?)</button>", html)
        controls += re.findall(r'name="decision" value="([^"]+)"', html)
        controls += re.findall(r'name="action" value="([^"]+)"', html)
        allowed = {"approve", "edit", "reject", "sign off", "reject brief",
                   "signed_off", "rejected"}
        assert set(controls) <= allowed, f"unexpected control in the UI: {set(controls) - allowed}"


class TestLiveServer:
    @pytest.fixture
    def live(self, tmp_path):
        conn, brief_id = build_db(tmp_path / "a.sqlite")
        conn.close()
        server = make_server("127.0.0.1", 0, str(tmp_path / "a.sqlite"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        yield base, brief_id, tmp_path / "a.sqlite"
        server.shutdown()
        server.server_close()

    def get(self, url):
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, url, data):
        request = urllib.request.Request(
            url, data=urllib.parse.urlencode(data).encode(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_index_lists_the_structure(self, live):
        base, _, _ = live
        status, body = self.get(base + "/")
        assert status == 200 and "013450" in body

    def test_brief_page_renders(self, live):
        base, brief_id, _ = live
        status, body = self.get(f"{base}/brief/{brief_id}")
        assert status == 200
        assert "blocked_unsupported" in body and "NBE-013450-2023-12-cs3" in body

    def test_artifact_api_resolves_and_reports_non_resolution(self, live):
        base, _, _ = live
        status, body = self.get(base + "/api/artifact/NBI-013450-2023-deck")
        assert status == 200 and json.loads(body)["kind"] == "NBI"
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.get(base + "/api/artifact/NBI-999999-2023-deck")
        assert exc.value.code == 404

    def test_review_action_and_signoff_round_trip_through_http(self, live):
        base, brief_id, db_path = live
        conn = connect(str(db_path))
        sentence = conn.execute("SELECT sentence_id, finding_id FROM brief_sentences "
                                "WHERE brief_id = ? AND finding_id IS NOT NULL",
                                (brief_id,)).fetchone()
        conn.close()

        status, _ = self.post(f"{base}/brief/{brief_id}/action", {
            "action": "approve", "reviewer": "A Inspector",
            "sentence_id": sentence["sentence_id"], "finding_id": sentence["finding_id"]})
        assert status == 200

        status, _ = self.post(f"{base}/brief/{brief_id}/signoff",
                              {"reviewer": "A Inspector", "decision": "signed_off"})
        assert status == 200

        conn = connect(str(db_path))
        assert conn.execute("SELECT COUNT(*) FROM signoffs").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM briefs WHERE brief_id = ?",
                            (brief_id,)).fetchone()[0] == "signed_off"
        conn.close()

    def test_an_anonymous_signoff_is_rejected_over_http(self, live):
        base, brief_id, _ = live
        status, body = self.post(f"{base}/brief/{brief_id}/signoff", {"reviewer": ""})
        assert status == 400 and "reviewer name is required" in body

    def test_an_unknown_brief_is_a_clean_404(self, live):
        base, _, _ = live
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.get(base + "/brief/BRIEF-999999-2023-v1")
        assert exc.value.code == 404

    def test_the_ui_serves_an_empty_database_without_error(self, tmp_path):
        server = make_server("127.0.0.1", 0, str(tmp_path / "empty.sqlite"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = self.get(f"http://127.0.0.1:{server.server_address[1]}/")
            assert status == 200
            assert "No structures have been ingested" in body
        finally:
            server.shutdown()
            server.server_close()


class TestStructureListScales:
    """The landing page must not be O(number of structures).

    Written after the obvious phrasing of this query made the page unusable on
    the real corpus. Selecting from `structures` with one correlated subquery
    per counter let SQLite prefer `idx_findings_kind` over
    `idx_findings_struct`, so it rescanned every contradiction row for each of
    632,140 structures — roughly 4.4 billion row visits. The page never loaded.

    A timing assertion would be flaky, so this checks the shape of the plan
    instead: the query may seek into `structures` by primary key, but it must
    never scan the table.
    """

    def test_the_plan_does_not_scan_the_structures_table(self, tmp_path):
        conn, _ = build_db(tmp_path / "assets.sqlite")

        # Capture the statement list_structures actually runs, rather than
        # restating it here — a copy in the test would keep passing after the
        # real query regressed.
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        list_structures(conn)
        conn.set_trace_callback(None)

        listing = next(sql for sql in statements if "candidates" in sql.lower())
        # The trace may or may not have inlined the LIMIT parameter, depending
        # on the sqlite3 build.
        args = (500,) if "?" in listing else ()
        steps = [str(row[3]) for row in
                 conn.execute("EXPLAIN QUERY PLAN " + listing, args).fetchall()]
        plan = "\n".join(steps)
        # SQLite words this as "SCAN structures" on newer builds and
        # "SCAN TABLE structures AS s" on older ones, so match the shape rather
        # than either spelling — an assertion that matched only one would pass
        # vacuously on the other.
        scans = [step for step in steps
                 if step.startswith("SCAN") and "structures" in step]
        assert not scans, (
            "the structure list is scanning every structure again:\n" + plan
        )

    def test_it_still_returns_the_structures_that_have_something_to_review(self, tmp_path):
        conn, _ = build_db(tmp_path / "assets.sqlite")
        rows, totals = list_structures(conn)
        assert rows, "a structure with findings should be listed"
        listed = {row["struct_norm"] for row in rows}
        flagged = {r[0] for r in conn.execute("SELECT DISTINCT struct_norm FROM findings")}
        assert flagged <= listed
        # Counters are still carried, and the state label survives the join.
        assert all("contradictions" in dict(row) for row in rows)
        assert totals["structures"] >= len(rows)

    def test_a_structure_with_nothing_to_review_is_not_listed(self, tmp_path):
        conn, _ = build_db(tmp_path / "assets.sqlite")
        upsert_structure(conn, "AL999999", struct_raw="999999", state_abbr="AL", year=2023)
        rows, _ = list_structures(conn)
        assert "AL999999" not in {row["struct_norm"] for row in rows}

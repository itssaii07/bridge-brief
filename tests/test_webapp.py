"""The web app: upload parsing, the inspection workflow, the vision proposer, the routes.

Everything here runs against a temporary database and a temporary upload root.
Nothing is written under data/. Test photographs are generated in the test and
never reach the real index (ASSUMPTIONS.md L9). The Anthropic client is replaced
with a stub, so these tests need no key and make no network call; the stub
returns the response shape the real API returns, which is what is being tested.
"""

import io
import json
import threading
import types
import urllib.error
import urllib.request
import uuid

import pytest

from src import ids
from src.analysis import contradictions as C
from src.db import connect
from src.detect.base import DetectorUnavailable
from src.store import upsert_element_state, upsert_rating, upsert_structure
from src.ui import multipart, workflow
from src.ui.review import ReviewError, record_action, sign_off

PIL = pytest.importorskip("PIL", reason="Pillow is an optional extra")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

STRUCT = "AL013450"


def photo_bytes(fmt="JPEG", size=(480, 320), textured=True, exif_orientation=None) -> bytes:
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, (150, 150, 150))
    if textured:
        draw = ImageDraw.Draw(image)
        for offset in range(0, 60, 4):
            draw.line([(60 + offset, 60), (60 + offset, 180)], fill=(10, 10, 10), width=2)
    buffer = io.BytesIO()
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        image.save(buffer, format=fmt, exif=exif.tobytes())
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


def seed(path) -> "sqlite3.Connection":
    """One structure with a rating and element data that disagree."""
    conn = connect(path)
    upsert_structure(conn, STRUCT, struct_raw="013450", state_abbr="AL", year=2023,
                     facility="SR 1", feature_crossed="TEST CREEK")
    upsert_rating(conn, struct_norm=STRUCT, year=2023, component="deck", rating=7,
                  rating_raw="7", artifact_id=ids.nbi_id(STRUCT, 2023, "deck"),
                  source_path="t", source_line=1)
    for cs, qty in ((1, 7660.0), (3, 340.0)):
        upsert_element_state(conn, struct_norm=STRUCT, year=2023, elem_num=12, cs=cs,
                             cs_qty=qty, total_qty=8000.0, units="SQFT", elem_name="RC Deck",
                             elem_class="deck", state_abbr="AL",
                             artifact_id=ids.nbe_id(STRUCT, 2023, 12, cs), source_path="t")
    conn.commit()
    return conn


@pytest.fixture
def uploads_dir(tmp_path, monkeypatch):
    """Point the upload store at a temp folder, so no test writes under data/."""
    from src.ingest import uploads
    target = tmp_path / "uploads"
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", target)
    return target


def multipart_body(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[str, bytes]:
    boundary = "----bb" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                  f"{value}\r\n".encode())
    for name, filename, data in files:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                  f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                  .encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", out.getvalue()


# ---------------------------------------------------------------------------
# multipart
# ---------------------------------------------------------------------------

class TestMultipart:
    def test_fields_and_files_are_separated(self):
        ctype, body = multipart_body({"structure": STRUCT, "detector": "baseline"},
                                     [("photos", "a.jpg", b"\xff\xd8\xffAAA"),
                                      ("photos", "b.png", b"\x89PNG\r\n\x1a\nBBB")])
        fields, files = multipart.parse(ctype, body)
        assert fields == {"structure": STRUCT, "detector": "baseline"}
        assert [(f.field, f.filename, f.data) for f in files] == [
            ("photos", "a.jpg", b"\xff\xd8\xffAAA"), ("photos", "b.png", b"\x89PNG\r\n\x1a\nBBB")]

    def test_binary_payloads_survive_byte_for_byte(self):
        data = bytes(range(256)) * 40
        ctype, body = multipart_body({}, [("photos", "raw.bin", data)])
        _, files = multipart.parse(ctype, body)
        assert files[0].data == data

    def test_an_empty_file_input_is_not_a_file(self):
        ctype, body = multipart_body({"structure": STRUCT}, [("photos", "", b"")])
        _, files = multipart.parse(ctype, body)
        assert files == []

    @pytest.mark.parametrize("ctype", [None, "application/json", "multipart/form-data"])
    def test_non_multipart_or_boundaryless_bodies_are_refused(self, ctype):
        with pytest.raises(multipart.MultipartError):
            multipart.parse(ctype, b"irrelevant")

    def test_the_size_ceiling_is_enforced(self, monkeypatch):
        monkeypatch.setattr(multipart, "MAX_BODY_BYTES", 10)
        ctype, body = multipart_body({}, [("photos", "a.jpg", b"x" * 100)])
        with pytest.raises(multipart.MultipartError):
            multipart.parse(ctype, body)


# ---------------------------------------------------------------------------
# workflow
# ---------------------------------------------------------------------------

class TestWorkflowHelpers:
    @pytest.mark.parametrize("data, ext", [
        (b"\xff\xd8\xff\xe0rest", ".jpg"), (b"\x89PNG\r\n\x1a\nrest", ".png"),
        (b"RIFF\x00\x00\x00\x00WEBPrest", ".webp"), (b"II*\x00rest", ".tif"),
        (b"BMrest", ".bmp"), (b"RIFF\x00\x00\x00\x00WAVErest", None),
        (b"%PDF-1.7", None), (b"", None)])
    def test_the_format_is_read_from_the_bytes_not_the_name(self, data, ext):
        assert workflow.sniff_extension(data) == ext

    def test_stored_names_are_content_addressed(self):
        a, b = photo_bytes(), photo_bytes(size=(481, 320))
        assert workflow.stored_name("IMG 0042.JPG", a) == workflow.stored_name("IMG 0042.JPG", a)
        # Same filename, different photograph: must never overwrite the first.
        assert workflow.stored_name("IMG 0042.JPG", a) != workflow.stored_name("IMG 0042.JPG", b)
        name = workflow.stored_name("Pier 3 face (north).jpeg", a)
        assert name.startswith("pier_3_face_north_") and name.endswith(".jpg")

    def test_a_path_in_the_filename_keeps_only_its_last_segment(self):
        """A browser-supplied name must never steer where the file is written."""
        for hostile in ("../../etc/passwd.jpg", "..\\..\\windows\\x.jpg", "/abs/path/ok.jpg"):
            name = workflow.stored_name(hostile, photo_bytes())
            assert "/" not in name and "\\" not in name and ".." not in name

    def test_stored_names_always_make_a_valid_photo_key(self):
        for filename in ("r3.jpg", "....jpg", "", "日本.png", "a" * 300 + ".jpg"):
            ids.photo_key_from_filename(workflow.stored_name(filename, photo_bytes()))

    def test_auto_falls_back_to_the_baseline_and_says_why(self, monkeypatch):
        monkeypatch.setattr("src.detect.vision.credentials_configured",
                            lambda: (False, "no key"))
        name, reason = workflow.resolve_detector("auto")
        assert name == "baseline" and "no key" in reason

    def test_explicitly_requested_vision_without_credentials_is_refused(self, monkeypatch):
        monkeypatch.setattr("src.detect.vision.credentials_configured",
                            lambda: (False, "no key"))
        with pytest.raises(workflow.InspectionError):
            workflow.resolve_detector("vision")

    def test_an_unknown_detector_choice_is_refused(self):
        with pytest.raises(workflow.InspectionError):
            workflow.resolve_detector("magic")


class TestRunInspection:
    def run(self, conn, photos, **kw):
        return list(workflow.run_inspection(conn, STRUCT, photos, detector="baseline", **kw))

    def test_end_to_end_produces_a_draft_with_regions_and_preserved_originals(
            self, tmp_path, uploads_dir):
        conn = seed(tmp_path / "a.sqlite")
        original = photo_bytes()
        events = self.run(conn, [workflow.Photo("deck.jpg", original)])

        stages = [e["stage"] for e in events if e["status"] == "done"]
        assert stages == ["anchor", "preserve", "detect", "records", "draft", "complete"]
        final = events[-1]["data"]
        assert final["brief_id"].startswith(f"BRIEF-{STRUCT}-")

        row = conn.execute("SELECT stored_path, sha256 FROM images WHERE struct_norm = ?",
                           (STRUCT,)).fetchone()
        from pathlib import Path
        stored = Path(row["stored_path"])
        assert stored.read_bytes() == original                     # untouched
        assert str(stored).startswith(str(uploads_dir))            # never under data/
        assert conn.execute("SELECT COUNT(*) FROM image_regions").fetchone()[0] == final["regions"]
        status = conn.execute("SELECT status FROM briefs WHERE brief_id = ?",
                              (final["brief_id"],)).fetchone()[0]
        assert status == "draft"                                   # nothing signed

    def test_the_detect_stage_finishes_only_after_every_photo(self, tmp_path, uploads_dir):
        conn = seed(tmp_path / "a.sqlite")
        events = self.run(conn, [workflow.Photo("a.jpg", photo_bytes()),
                                 workflow.Photo("b.jpg", photo_bytes(size=(500, 300)))])
        detect = [e for e in events if e["stage"] == "detect"]
        assert [e["status"] for e in detect] == ["running", "info", "info", "done"]

    def test_the_same_photo_uploaded_twice_is_stored_once(self, tmp_path, uploads_dir):
        conn = seed(tmp_path / "a.sqlite")
        data = photo_bytes()
        self.run(conn, [workflow.Photo("x.jpg", data)])
        self.run(conn, [workflow.Photo("x.jpg", data)])
        assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1

    def test_an_unknown_structure_writes_nothing(self, tmp_path, uploads_dir):
        """A photo is never attached to a bridge the inventory does not hold."""
        conn = seed(tmp_path / "a.sqlite")
        with pytest.raises(workflow.InspectionError):
            list(workflow.run_inspection(conn, "ZZ999999", [workflow.Photo("a.jpg", photo_bytes())],
                                         detector="baseline"))
        assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM structures WHERE struct_norm = 'ZZ999999'"
                            ).fetchone()[0] == 0
        assert not uploads_dir.exists()

    def test_a_non_image_is_refused_before_anything_is_written(self, tmp_path, uploads_dir):
        conn = seed(tmp_path / "a.sqlite")
        with pytest.raises(workflow.InspectionError) as exc:
            self.run(conn, [workflow.Photo("good.jpg", photo_bytes()),
                            workflow.Photo("notes.jpg", b"%PDF-1.7 not a photo")])
        assert "contents are checked" in str(exc.value)
        assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0

    def test_no_photographs_is_refused(self, tmp_path, uploads_dir):
        conn = seed(tmp_path / "a.sqlite")
        with pytest.raises(workflow.InspectionError):
            self.run(conn, [])

    def test_a_structure_without_a_rating_cannot_anchor_a_brief(self, tmp_path, uploads_dir):
        conn = connect(tmp_path / "a.sqlite")
        upsert_structure(conn, "AL000001", struct_raw="1", state_abbr="AL")
        conn.commit()
        with pytest.raises(workflow.InspectionError):
            list(workflow.run_inspection(conn, "AL000001", [workflow.Photo("a.jpg", photo_bytes())],
                                         detector="baseline"))

    def test_a_vision_failure_falls_back_to_the_baseline_and_says_so(
            self, tmp_path, uploads_dir, monkeypatch):
        conn = seed(tmp_path / "a.sqlite")
        monkeypatch.setattr("src.detect.vision.credentials_configured", lambda: (True, "stub"))

        def broken(self, path):
            raise DetectorUnavailable("stub outage")
        monkeypatch.setattr("src.detect.vision.VisionDetector.detect", broken)
        events = list(workflow.run_inspection(conn, STRUCT, [workflow.Photo("a.jpg", photo_bytes())],
                                              detector="vision"))
        warnings = [e for e in events if e["status"] == "warning"]
        assert warnings and "stub outage" in warnings[0]["message"]
        assert events[-1]["stage"] == "complete"
        names = {r[0] for r in conn.execute("SELECT detector_name FROM image_regions")}
        assert names <= {"baseline_edge_energy"}


# ---------------------------------------------------------------------------
# vision proposer
# ---------------------------------------------------------------------------

class TestVisionParsing:
    from src.detect import vision as V

    def reply(self, observations, visible=True):
        return json.dumps({"observations": observations, "structure_visible": visible,
                           "image_notes": "Close view of a deck soffit."})

    def test_fractional_boxes_map_onto_original_pixels(self):
        text = self.reply([{"defect_class": "crack", "description": "Diagonal hairline crack.",
                            "x_min": 0.1, "y_min": 0.2, "x_max": 0.5, "y_max": 0.6,
                            "confidence": 0.9}])
        detections, notes = self.V.parse_response(text, 1000, 500)
        (d,) = detections
        assert (d.x, d.y, d.w, d.h) == (100, 100, 400, 200)
        assert d.defect_class == "crack" and d.description == "Diagonal hairline crack."
        assert notes["image_notes"] == "Close view of a deck soffit."

    def test_model_confidence_is_capped(self):
        """Self-reported certainty is uncalibrated; it never reads as certain."""
        text = self.reply([{"defect_class": "spallation", "description": "d", "x_min": 0, "y_min": 0,
                            "x_max": 0.5, "y_max": 0.5, "confidence": 0.99}])
        (d,), _ = self.V.parse_response(text, 100, 100)
        assert d.confidence == self.V.CONFIDENCE_CAP

    def test_out_of_range_and_inverted_boxes_are_clamped_and_ordered(self):
        text = self.reply([{"defect_class": "crack", "description": "d", "x_min": 1.4, "y_min": 0.9,
                            "x_max": -0.2, "y_max": 0.1, "confidence": 0.5}])
        (d,), _ = self.V.parse_response(text, 100, 100)
        assert (d.x, d.y, d.w, d.h) == (0, 10, 100, 80)

    def test_degenerate_or_zero_confidence_observations_are_dropped_and_counted(self):
        text = self.reply([
            {"defect_class": "crack", "description": "d", "x_min": 0.5, "y_min": 0.5,
             "x_max": 0.5, "y_max": 0.9, "confidence": 0.5},
            {"defect_class": "crack", "description": "d", "x_min": 0.1, "y_min": 0.1,
             "x_max": 0.4, "y_max": 0.4, "confidence": 0.0}])
        detections, notes = self.V.parse_response(text, 100, 100)
        assert detections == [] and notes["observations_dropped"] == 2

    def test_an_unknown_class_becomes_other_rather_than_a_wrong_class(self):
        text = self.reply([{"defect_class": "graffiti", "description": "d", "x_min": 0, "y_min": 0,
                            "x_max": 0.3, "y_max": 0.3, "confidence": 0.4}])
        (d,), _ = self.V.parse_response(text, 100, 100)
        assert d.defect_class == "other"

    @pytest.mark.parametrize("text", ["not json", "[]", '{"observations": "no"}'])
    def test_a_malformed_reply_is_reported_not_half_used(self, text):
        with pytest.raises(DetectorUnavailable):
            self.V.parse_response(text, 100, 100)

    def test_without_credentials_detection_is_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.V, "credentials_configured", lambda: (False, "no key"))
        path = tmp_path / "p.jpg"
        path.write_bytes(photo_bytes())
        with pytest.raises(DetectorUnavailable):
            self.V.VisionDetector().detect(path)

    @pytest.mark.parametrize("variable", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
    def test_credentials_are_detected_from_the_environment(self, monkeypatch, variable, tmp_path):
        pytest.importorskip("anthropic")
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert self.V.credentials_configured()[0] is False
        monkeypatch.setenv(variable, "x")
        assert self.V.credentials_configured()[0] is True


class TestVisionRequest:
    """The request sent to the API, checked against a stub client."""

    from src.detect import vision as V

    def stub(self, monkeypatch, reply_text, stop_reason="end_turn"):
        anthropic = pytest.importorskip("anthropic")
        captured = {}

        class Messages:
            def create(self, **kwargs):
                captured.update(kwargs)
                block = types.SimpleNamespace(type="text", text=reply_text)
                return types.SimpleNamespace(content=[block], stop_reason=stop_reason,
                                             model=kwargs["model"])

        class Client:
            def __init__(self, *a, **kw):
                self.beta = types.SimpleNamespace(messages=Messages())

        monkeypatch.setattr(anthropic, "Anthropic", Client)
        monkeypatch.setattr(self.V, "credentials_configured", lambda: (True, "stub"))
        return captured

    def test_the_request_carries_the_image_the_schema_and_the_rules(self, tmp_path, monkeypatch):
        reply = json.dumps({"observations": [{"defect_class": "crack", "description": "d",
                                              "x_min": 0.1, "y_min": 0.1, "x_max": 0.3,
                                              "y_max": 0.3, "confidence": 0.6}],
                            "structure_visible": True, "image_notes": "n"})
        captured = self.stub(monkeypatch, reply)
        path = tmp_path / "p.jpg"
        path.write_bytes(photo_bytes(size=(3000, 2000)))
        detections = self.V.VisionDetector().detect(path)

        assert len(detections) == 1 and detections[0].x == 300       # original pixels
        assert captured["model"] == self.V.DEFAULT_MODEL
        assert captured["output_config"]["format"]["schema"] == self.V.SCHEMA
        image = captured["messages"][0]["content"][0]
        assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
        # The model is told not to judge safety or recommend work.
        assert "do not say whether the structure is safe" in captured["system"]
        assert "do not recommend repairs" in captured["system"]

    def test_the_original_is_never_modified_by_preparing_the_request(self, tmp_path, monkeypatch):
        self.stub(monkeypatch, json.dumps({"observations": [], "structure_visible": True,
                                           "image_notes": ""}))
        path = tmp_path / "p.jpg"
        original = photo_bytes(size=(3000, 2000))
        path.write_bytes(original)
        self.V.VisionDetector().detect(path)
        assert path.read_bytes() == original

    def test_a_photo_with_no_structure_in_it_yields_no_regions(self, tmp_path, monkeypatch):
        reply = json.dumps({"observations": [{"defect_class": "crack", "description": "d",
                                              "x_min": 0.1, "y_min": 0.1, "x_max": 0.3,
                                              "y_max": 0.3, "confidence": 0.6}],
                            "structure_visible": False, "image_notes": "A cat."})
        self.stub(monkeypatch, reply)
        path = tmp_path / "p.jpg"
        path.write_bytes(photo_bytes())
        detector = self.V.VisionDetector()
        assert detector.detect(path) == []
        assert detector.last_notes["structure_visible"] is False

    @pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
    def test_a_refused_or_truncated_reply_is_unavailable(self, tmp_path, monkeypatch, stop_reason):
        self.stub(monkeypatch, "{}", stop_reason=stop_reason)
        path = tmp_path / "p.jpg"
        path.write_bytes(photo_bytes())
        with pytest.raises(DetectorUnavailable):
            self.V.VisionDetector().detect(path)


# ---------------------------------------------------------------------------
# drafting and review rules the web app relies on
# ---------------------------------------------------------------------------

class TestDraftingAndReview:
    def test_region_sentences_name_the_proposer_and_carry_the_description(
            self, tmp_path, uploads_dir):
        from src.detect.base import Detection
        from src.generate.brief import generate
        from src.ingest.uploads import ingest_upload, store_detections

        conn = seed(tmp_path / "a.sqlite")
        C.run(conn, 2023, log=lambda *a: None)
        path = tmp_path / "p.jpg"
        path.write_bytes(photo_bytes())
        image = ingest_upload(conn, STRUCT, path, log=lambda *a: None)
        store_detections(conn, image, [Detection(10, 10, 50, 40, "spallation", 0.6,
                                                 "Concrete broken away at the bearing seat.")],
                         detector_name="claude_vision", detector_version="stub")
        brief = generate(conn, STRUCT, 2023, log=lambda *a: None)
        texts = [r[0] for r in conn.execute("SELECT text FROM brief_sentences WHERE brief_id = ?",
                                            (brief["brief_id"],))]
        region = next(t for t in texts if "candidate spallation region" in t)
        assert "a vision model" in region and "bearing seat" in region
        assert "has not been confirmed by an inspector" in region
        # The evidence streams nobody supplied are named, not assumed.
        assert any("No sensor readings" in t and "no field notes" in t for t in texts)

    def test_a_signed_off_brief_is_frozen(self, tmp_path):
        """Otherwise the published text could differ from what was signed."""
        from src.generate.brief import generate
        conn = seed(tmp_path / "a.sqlite")
        C.run(conn, 2023, log=lambda *a: None)
        brief_id = generate(conn, STRUCT, 2023, log=lambda *a: None)["brief_id"]
        sentence = conn.execute("SELECT sentence_id FROM brief_sentences WHERE brief_id = ?",
                                (brief_id,)).fetchone()[0]
        sign_off(conn, brief_id=brief_id, reviewer="A. Inspector")
        with pytest.raises(ReviewError) as exc:
            record_action(conn, brief_id=brief_id, action="edit", reviewer="B",
                          sentence_id=sentence, text_after="Changed after signing.")
        assert "frozen" in str(exc.value)

    def test_stored_dimensions_follow_exif_orientation(self, tmp_path):
        """Boxes are drawn in the upright frame, so the stored size must be too."""
        from src.ingest.uploads import _image_size
        path = tmp_path / "rotated.jpg"
        path.write_bytes(photo_bytes(size=(400, 300), exif_orientation=6))
        assert _image_size(path) == (300, 400)
        plain = tmp_path / "plain.jpg"
        plain.write_bytes(photo_bytes(size=(400, 300)))
        assert _image_size(plain) == (400, 300)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

class TestRoutes:
    @pytest.fixture
    def server(self, tmp_path, uploads_dir):
        from src.ui.server import make_server
        db = tmp_path / "assets.sqlite"
        conn = seed(db)
        C.run(conn, 2023, log=lambda *a: None)
        conn.close()
        srv = make_server("127.0.0.1", 0, str(db))
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{srv.server_address[1]}", db
        srv.shutdown()
        srv.server_close()

    def get(self, url):
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.headers.get("Content-Type"), response.read()

    def post(self, url, body: bytes, ctype: str):
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": ctype})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()

    def inspect(self, base, data=None):
        ctype, body = multipart_body({"structure": STRUCT, "detector": "baseline"},
                                     [("photos", "deck.jpg", data or photo_bytes())])
        _, raw = self.post(base + "/api/inspect", body, ctype)
        return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]

    @pytest.mark.parametrize("path", ["/", "/inspect", "/queue", "/metrics", "/structures",
                                      f"/inspect?structure={STRUCT}"])
    def test_every_page_renders(self, server, path):
        base, _ = server
        status, ctype, body = self.get(base + path)
        assert status == 200 and ctype.startswith("text/html")
        assert b"Bridge Brief" in body and b"No autonomous clearance" in body

    def test_the_upload_streams_real_progress_and_ends_in_a_brief(self, server):
        base, _ = server
        events = self.inspect(base)
        assert events[-1]["stage"] == "complete"
        brief_id = events[-1]["data"]["brief_id"]
        status, _, body = self.get(f"{base}/report/{brief_id}")
        assert status == 200 and b"blocked_unsupported" in body
        report = json.loads(self.get(f"{base}/api/report/{brief_id}")[2])
        assert report["stats"]["photos"] == 1
        assert report["stats"]["resolved"] == report["stats"]["citations"]

    def test_an_upload_error_arrives_as_an_event_not_a_crash(self, server):
        base, _ = server
        ctype, body = multipart_body({"structure": "ZZ000000", "detector": "baseline"},
                                     [("photos", "a.jpg", photo_bytes())])
        _, raw = self.post(base + "/api/inspect", body, ctype)
        events = [json.loads(l) for l in raw.decode().splitlines() if l.strip()]
        assert events[-1]["stage"] == "error"

    def test_publication_is_refused_until_sign_off_and_then_allowed(self, server):
        base, _ = server
        brief_id = self.inspect(base)[-1]["data"]["brief_id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.get(f"{base}/api/report/{brief_id}/export?format=markdown")
        assert exc.value.code == 409
        assert "Publication refused" in json.loads(exc.value.read())["error"]

        status, _ = self.post(f"{base}/api/report/{brief_id}/signoff",
                              json.dumps({"reviewer": "A. Inspector"}).encode(), "application/json")
        assert status == 200
        status, ctype, body = self.get(f"{base}/api/report/{brief_id}/export?format=markdown")
        assert status == 200 and ctype.startswith("text/markdown")
        assert b"NOT a safety clearance" in body and b"A. Inspector" in body

    def test_an_anonymous_review_action_is_refused(self, server):
        base, _ = server
        brief_id = self.inspect(base)[-1]["data"]["brief_id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.post(f"{base}/api/report/{brief_id}/action",
                      json.dumps({"action": "approve", "reviewer": " "}).encode(),
                      "application/json")
        assert exc.value.code == 400

    def test_originals_are_served_byte_for_byte(self, server):
        base, db = server
        data = photo_bytes()
        self.inspect(base, data)
        conn = connect(db)
        artifact = conn.execute("SELECT artifact_id FROM images").fetchone()[0]
        conn.close()
        status, ctype, body = self.get(f"{base}/original/{artifact}")
        assert status == 200 and ctype == "image/jpeg" and body == data

    def test_the_original_route_never_serves_corpus_imagery(self, server):
        base, db = server
        conn = connect(db)
        conn.execute("INSERT INTO images (artifact_id, struct_norm, provenance, corpus, "
                     "stored_path) VALUES ('REF-dacl10k-x', NULL, 'reference_corpus', "
                     "'dacl10k', ?)", (str(db),))
        conn.commit()
        conn.close()
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.get(f"{base}/original/REF-dacl10k-x")
        assert exc.value.code == 404

    @pytest.mark.parametrize("path", ["/static/app.css", "/static/app.js"])
    def test_static_assets_are_served(self, server, path):
        base, _ = server
        status, _, body = self.get(base + path)
        assert status == 200 and len(body) > 1000

    @pytest.mark.parametrize("path", ["/static/../server.py", "/static/%2e%2e/server.py",
                                      "/static/", "/static/nope.js"])
    def test_the_static_route_is_not_a_file_browser(self, server, path):
        base, _ = server
        with pytest.raises(urllib.error.HTTPError) as exc:
            self.get(base + path)
        assert exc.value.code == 404

    def test_structure_search_and_detail(self, server):
        base, _ = server
        results = json.loads(self.get(base + "/api/structures?q=13450")[2])["results"]
        assert [r["struct_norm"] for r in results] == [STRUCT]
        assert results[0]["has_elements"] is True
        detail = json.loads(self.get(f"{base}/api/structure/{STRUCT}")[2])
        assert detail["structure"]["struct_norm"] == STRUCT and detail["ratings"]

    def test_capabilities_never_claim_vision_without_credentials(self, server, monkeypatch):
        base, _ = server
        monkeypatch.setattr("src.detect.vision.credentials_configured", lambda: (False, "no key"))
        caps = json.loads(self.get(base + "/api/capabilities")[2])
        assert caps["vision"]["available"] is False

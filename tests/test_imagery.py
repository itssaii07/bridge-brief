"""Tests for tiling, the detector interface, upload provenance and the benchmark.

Images used here are generated in the test's own tmp_path — never under data/ —
and exist only to exercise geometry and bookkeeping.
"""

import pytest

from src.db import DataUnavailable, connect
from src.detect.base import Detection, DetectorUnavailable, get_detector, iou
from src.detect.tiling import Tile, plan_tiles
from src.eval import codebrim_benchmark as B
from src.ingest import uploads

PIL = pytest.importorskip("PIL", reason="Pillow is an optional extra")


def make_image(path, size=(600, 400), colour=128):
    from PIL import Image
    Image.new("RGB", size, (colour, colour, colour)).save(path)
    return path


def make_textured_image(path, size=(600, 400)):
    """A flat image with one high-contrast patch, so a response exists to find."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", size, (150, 150, 150))
    draw = ImageDraw.Draw(image)
    for offset in range(0, 60, 4):
        draw.line([(60 + offset, 60), (60 + offset, 180)], fill=(10, 10, 10), width=2)
    image.save(path)
    return path


class TestTiling:
    def test_tiles_cover_the_image_and_stay_inside_it(self):
        tiles = plan_tiles(1000, 800, tile=256, overlap=0.25)
        assert tiles
        for tile in tiles:
            assert tile.x >= 0 and tile.y >= 0
            assert tile.x + tile.w <= 1000 and tile.y + tile.h <= 800

    def test_the_last_tile_is_flush_with_the_edge_not_padded(self):
        tiles = plan_tiles(1000, 800, tile=256, overlap=0.25)
        assert max(t.x + t.w for t in tiles) == 1000
        assert max(t.y + t.h for t in tiles) == 800

    def test_tiles_overlap_so_a_boundary_crack_is_not_split(self):
        tiles = [t for t in plan_tiles(1000, 256, tile=256, overlap=0.25) if t.y == 0]
        xs = sorted(t.x for t in tiles)
        assert xs[1] - xs[0] < 256

    def test_an_image_smaller_than_a_tile_yields_one_tile_covering_it(self):
        assert plan_tiles(100, 50, tile=256) == [Tile(0, 0, 0, 100, 50)]

    def test_tiling_is_deterministic(self):
        assert plan_tiles(999, 777) == plan_tiles(999, 777)

    @pytest.mark.parametrize("args", [(0, 100), (100, 0), (-5, 10)])
    def test_impossible_sizes_are_refused(self, args):
        with pytest.raises(ValueError):
            plan_tiles(*args)

    @pytest.mark.parametrize("overlap", [-0.1, 1.0, 1.5])
    def test_impossible_overlap_is_refused(self, overlap):
        with pytest.raises(ValueError):
            plan_tiles(100, 100, overlap=overlap)


class TestGeometry:
    def test_identical_boxes_have_iou_one(self):
        a = Detection(0, 0, 10, 10, "crack", 1.0)
        assert iou(a, a) == 1.0

    def test_disjoint_boxes_have_iou_zero(self):
        a = Detection(0, 0, 10, 10, "crack", 1.0)
        b = Detection(100, 100, 10, 10, "crack", 1.0)
        assert iou(a, b) == 0.0

    def test_half_overlap(self):
        a = Detection(0, 0, 10, 10, "crack", 1.0)
        b = Detection(5, 0, 10, 10, "crack", 1.0)
        assert iou(a, b) == pytest.approx(50 / 150)

    def test_ordering_is_geometric_not_emission_order(self):
        boxes = [Detection(50, 10, 5, 5, "crack", 0.1), Detection(10, 10, 5, 5, "crack", 0.9)]
        assert [d.x for d in sorted(boxes, key=lambda d: d.sort_key())] == [10, 50]


class TestDetector:
    def test_unknown_detector_names_what_is_registered(self):
        with pytest.raises(DetectorUnavailable) as exc:
            get_detector("magic")
        assert "baseline" in str(exc.value)

    def test_a_missing_file_is_reported_not_returned_as_no_defects(self, tmp_path):
        with pytest.raises(DetectorUnavailable):
            get_detector("baseline").detect(tmp_path / "nope.jpg")

    def test_a_flat_image_yields_no_detections(self, tmp_path):
        """Nothing stands out, so nothing is proposed — not a random region."""
        path = make_image(tmp_path / "flat.png")
        assert get_detector("baseline").detect(path) == []

    def test_a_textured_image_yields_detections_inside_its_bounds(self, tmp_path):
        path = make_textured_image(tmp_path / "textured.png")
        detections = get_detector("baseline").detect(path)
        assert detections
        for d in detections:
            assert 0 <= d.x and d.x + d.w <= 600
            assert 0 <= d.y and d.y + d.h <= 400
            assert 0.0 < d.confidence <= 0.75   # never certain, and never looks it

    def test_detection_is_deterministic(self, tmp_path):
        path = make_textured_image(tmp_path / "textured.png")
        detector = get_detector("baseline")
        assert detector.detect(path) == detector.detect(path)

    def test_the_baseline_does_not_claim_a_defect_class(self, tmp_path):
        from src.detect.base import UNCLASSIFIED
        path = make_textured_image(tmp_path / "textured.png")
        assert {d.defect_class for d in get_detector("baseline").detect(path)} == {UNCLASSIFIED}


class TestUploadProvenance:
    def test_an_upload_is_labelled_inspection_upload_and_bound_to_its_structure(self, tmp_path):
        conn = connect(":memory:")
        source = make_image(tmp_path / "p03.jpg")
        artifact_id = uploads.ingest_upload(
            conn, "13450", source, upload_root=tmp_path / "uploads", log=lambda *a: None)
        assert artifact_id == "IMG-013450-p03"
        row = conn.execute("SELECT * FROM images WHERE artifact_id = ?", (artifact_id,)).fetchone()
        assert row["provenance"] == "inspection_upload"
        assert row["struct_norm"] == "013450"

    def test_the_original_is_copied_byte_for_byte_and_never_re_encoded(self, tmp_path):
        conn = connect(":memory:")
        source = make_image(tmp_path / "p03.jpg")
        original = source.read_bytes()
        artifact_id = uploads.ingest_upload(
            conn, "13450", source, upload_root=tmp_path / "uploads", log=lambda *a: None)
        stored = conn.execute(
            "SELECT stored_path FROM images WHERE artifact_id = ?", (artifact_id,)).fetchone()[0]
        from pathlib import Path
        assert Path(stored).read_bytes() == original

    def test_re_uploading_the_same_photo_is_idempotent(self, tmp_path):
        conn = connect(":memory:")
        source = make_image(tmp_path / "p03.jpg")
        for _ in range(3):
            uploads.ingest_upload(conn, "13450", source, upload_root=tmp_path / "u",
                                  log=lambda *a: None)
        assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1

    def test_a_non_image_is_refused(self, tmp_path):
        conn = connect(":memory:")
        path = tmp_path / "notes.txt"
        path.write_text("not a photo")
        with pytest.raises(ValueError):
            uploads.ingest_upload(conn, "13450", path, upload_root=tmp_path / "u",
                                  log=lambda *a: None)

    def test_corpus_imagery_cannot_be_attached_to_a_structure(self):
        """Invariant 6 is enforced by the schema, not by convention."""
        import sqlite3
        conn = connect(":memory:")
        conn.execute("INSERT INTO structures (struct_norm) VALUES ('013450')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO images (artifact_id, struct_norm, provenance, stored_path) "
                "VALUES ('REF-codebrim-1', '013450', 'not_a_real_provenance', '/x')")

    def test_detecting_for_a_structure_with_no_photos_says_how_to_upload(self):
        conn = connect(":memory:")
        with pytest.raises(DataUnavailable) as exc:
            uploads.detect_for_structure(conn, "13450", log=lambda *a: None)
        assert "--dir" in str(exc.value)

    def test_regions_get_stable_ids_and_resolve_as_artifacts(self, tmp_path):
        from src.store import resolve_artifact
        conn = connect(":memory:")
        source = make_textured_image(tmp_path / "p03.png")
        uploads.ingest_upload(conn, "13450", source, upload_root=tmp_path / "u",
                              log=lambda *a: None)
        first = uploads.detect_for_structure(conn, "13450", log=lambda *a: None)
        ids_first = [r[0] for r in conn.execute(
            "SELECT artifact_id FROM image_regions ORDER BY region_index")]
        uploads.detect_for_structure(conn, "13450", log=lambda *a: None)
        ids_second = [r[0] for r in conn.execute(
            "SELECT artifact_id FROM image_regions ORDER BY region_index")]

        assert first["regions"] > 0
        assert ids_first == ids_second           # re-running does not renumber
        assert ids_first[0].startswith("IMG-013450-p03-r")
        assert resolve_artifact(conn, ids_first[0]) is not None

    def test_detector_output_and_annotations_are_stored_separately(self, tmp_path):
        conn = connect(":memory:")
        source = make_textured_image(tmp_path / "p03.png")
        uploads.ingest_upload(conn, "13450", source, upload_root=tmp_path / "u",
                              log=lambda *a: None)
        uploads.detect_for_structure(conn, "13450", log=lambda *a: None)
        sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM image_regions")}
        assert sources == {"detector"}


class TestAnnotationParsing:
    VOC = """<annotation>
      <filename>image_0001.jpg</filename>
      <object><name>crack</name>
        <bndbox><xmin>10</xmin><ymin>20</ymin><xmax>110</xmax><ymax>140</ymax></bndbox>
      </object>
      <object><name>Spalling</name>
        <bndbox><xmin>200</xmin><ymin>50</ymin><xmax>260</xmax><ymax>120</ymax></bndbox>
      </object>
    </annotation>"""

    def test_voc_boxes_parse_with_classes_normalised(self, tmp_path):
        path = tmp_path / "a.xml"
        path.write_text(self.VOC)
        boxes = B.parse_annotation_file(path)
        assert [(b.x, b.y, b.w, b.h) for b in boxes] == [(10, 20, 100, 120), (200, 50, 60, 70)]
        assert [b.defect_class for b in boxes] == ["crack", "spallation"]

    def test_xywh_layout_also_parses(self, tmp_path):
        path = tmp_path / "a.xml"
        path.write_text("<annotation><object><name>crack</name>"
                        "<x>5</x><y>6</y><w>7</w><h>8</h></object></annotation>")
        box = B.parse_annotation_file(path)[0]
        assert (box.x, box.y, box.w, box.h) == (5, 6, 7, 8)

    def test_an_unparseable_file_points_at_the_one_change_point(self, tmp_path):
        path = tmp_path / "a.xml"
        path.write_text("<annotation><something/></annotation>")
        with pytest.raises(DataUnavailable) as exc:
            B.parse_annotation_file(path)
        assert "parse_annotation_file" in str(exc.value)

    def test_an_unknown_class_is_kept_verbatim_not_folded_into_a_known_one(self):
        assert B.normalise_class("scaling") == "scaling"
        assert B.normalise_class("Exposed Bars") == "exposed_bars"


class TestMatching:
    def box(self, x, cls="crack", conf=0.5):
        return Detection(x, 0, 100, 100, cls, conf)

    def test_a_good_overlap_is_a_true_positive(self):
        scores = B.match([self.box(0)], [self.box(10)], class_aware=False)
        assert (scores.tp, scores.fp, scores.fn) == (1, 0, 0)

    def test_a_poor_overlap_is_a_false_positive_and_a_false_negative(self):
        scores = B.match([self.box(0)], [self.box(90)], class_aware=False)
        assert (scores.tp, scores.fp, scores.fn) == (0, 1, 1)

    def test_each_annotation_is_claimed_at_most_once(self):
        scores = B.match([self.box(0), self.box(5)], [self.box(2)], class_aware=False)
        assert (scores.tp, scores.fp) == (1, 1)

    def test_class_aware_scoring_penalises_the_wrong_class(self):
        detections = [self.box(0, cls="defect")]
        annotations = [self.box(5, cls="crack")]
        assert B.match(detections, annotations, class_aware=True).tp == 0
        assert B.match(detections, annotations, class_aware=False).tp == 1

    def test_metrics_are_none_rather_than_zero_when_undefined(self):
        empty = B.Scores()
        assert empty.precision is None and empty.recall is None and empty.f1 is None

    def test_precision_recall_f1(self):
        scores = B.Scores(tp=3, fp=1, fn=1)
        assert scores.precision == 0.75 and scores.recall == 0.75 and scores.f1 == 0.75


class TestBenchmarkWithoutData:
    def test_missing_corpus_reports_where_it_goes(self, tmp_path):
        with pytest.raises(DataUnavailable) as exc:
            B.run(root=tmp_path, log=lambda *a: None)
        assert "CODEBRIM" in str(exc.value)

    def test_report_states_that_the_baseline_is_not_a_trained_model(self):
        report = B.BenchmarkReport(detector="baseline@1", iou_threshold=0.5)
        assert "not a trained model" in B.render(report)

"""Tests for tiling, the detector interface, upload provenance and the benchmark.

Images used here are generated in the test's own tmp_path — never under data/ —
and exist only to exercise geometry and bookkeeping.
"""

import json

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
        path.write_text("not a photo", encoding="utf-8")
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


class TestArchiveJunk:
    """The published CODEBRIM zip was made on macOS and carries a __MACOSX tree.

    Those AppleDouble stubs keep the original file's extension, so a suffix test
    alone reads `__MACOSX/dataset/._DSC_0042.jpg` as a JPEG. It is a few hundred
    bytes of resource fork, and the detector cannot decode it.
    """

    from src.ingest.codebrim import is_archive_junk

    @pytest.mark.parametrize("name", [
        "__MACOSX/original_dataset/._DSC_0042.jpg",
        "original_dataset/._DSC_0042.jpg",
        "original_dataset/.DS_Store",
        "__macosx/x.png",
    ])
    def test_archive_junk_is_recognised(self, name):
        from pathlib import Path as P
        from src.ingest.codebrim import is_archive_junk
        assert is_archive_junk(P(name)) is True

    @pytest.mark.parametrize("name", [
        "original_dataset/DSC_0042.jpg",
        "original_dataset/sub/image_0001.png",
    ])
    def test_real_images_are_not_junk(self, name):
        from pathlib import Path as P
        from src.ingest.codebrim import is_archive_junk
        assert is_archive_junk(P(name)) is False

    def test_discovery_skips_junk_but_finds_real_images(self, tmp_path):
        from src.ingest.codebrim import find_images

        corpus = tmp_path / "codebrim"
        (corpus / "original_dataset").mkdir(parents=True)
        (corpus / "__MACOSX" / "original_dataset").mkdir(parents=True)
        make_image(corpus / "original_dataset" / "DSC_0042.jpg")
        # An AppleDouble stub: .jpg suffix, not a JPEG.
        (corpus / "__MACOSX" / "original_dataset" / "._DSC_0042.jpg").write_bytes(b"\x00\x05\x16\x07junk")
        (corpus / "original_dataset" / "._DSC_0043.jpg").write_bytes(b"\x00\x05\x16\x07junk")

        _, images = find_images(root=tmp_path)
        assert [p.name for p in images] == ["DSC_0042.jpg"]

    def test_one_undecodable_image_does_not_abort_the_benchmark(self, tmp_path):
        """A single bad file in 1,590 must not destroy a long run."""
        corpus = tmp_path / "codebrim"
        corpus.mkdir(parents=True)
        good = make_textured_image(corpus / "good.png")
        good.with_suffix(".xml").write_text(
            "<annotation><object><name>crack</name><bndbox><xmin>10</xmin>"
            "<ymin>10</ymin><xmax>200</xmax><ymax>200</ymax></bndbox></object></annotation>",
            encoding="utf-8")
        bad = corpus / "truncated.png"
        bad.write_bytes(b"\x89PNG\r\n\x1a\n truncated")
        bad.with_suffix(".xml").write_text(
            "<annotation><object><name>crack</name><bndbox><xmin>1</xmin>"
            "<ymin>1</ymin><xmax>9</xmax><ymax>9</ymax></bndbox></object></annotation>",
            encoding="utf-8")

        report = B.run(root=tmp_path, corpus="codebrim", log=lambda *a: None)
        assert report.images_scored == 1
        assert report.images_skipped == 1
        assert report.skipped_reasons["image could not be decoded"] == 1
        # The failure is named in the rendered report, never silently zeroed.
        assert "could not be decoded" in B.render(report)


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
        path.write_text(self.VOC, encoding="utf-8")
        boxes = B.parse_annotation_file(path)
        assert [(b.x, b.y, b.w, b.h) for b in boxes] == [(10, 20, 100, 120), (200, 50, 60, 70)]
        assert [b.defect_class for b in boxes] == ["crack", "spallation"]

    def test_xywh_layout_also_parses(self, tmp_path):
        path = tmp_path / "a.xml"
        path.write_text("<annotation><object><name>crack</name>"
                        "<x>5</x><y>6</y><w>7</w><h>8</h></object></annotation>", encoding="utf-8")
        box = B.parse_annotation_file(path)[0]
        assert (box.x, box.y, box.w, box.h) == (5, 6, 7, 8)

    def test_an_unparseable_file_points_at_the_one_change_point(self, tmp_path):
        path = tmp_path / "a.xml"
        path.write_text("<annotation><something/></annotation>", encoding="utf-8")
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
            B.run(root=tmp_path, corpus="codebrim", log=lambda *a: None)
        assert "CODEBRIM" in str(exc.value)

    def test_missing_dacl10k_reports_where_it_goes(self, tmp_path):
        with pytest.raises(DataUnavailable) as exc:
            B.run(root=tmp_path, corpus="dacl10k", log=lambda *a: None)
        message = str(exc.value)
        assert "dacl10k" in message
        # The hint has to name the pre-flight check, because the whole reason
        # this corpus is in use is that the specified one downloaded broken.
        assert "check_dataset_archive" in message

    def test_an_unknown_corpus_names_the_ones_that_exist(self, tmp_path):
        with pytest.raises(DataUnavailable) as exc:
            B.run(root=tmp_path, corpus="not_a_corpus", log=lambda *a: None)
        assert "dacl10k" in str(exc.value) and "codebrim" in str(exc.value)

    def test_report_states_that_the_baseline_is_not_a_trained_model(self):
        report = B.BenchmarkReport(detector="baseline@1", iou_threshold=0.5)
        assert "not a trained model" in B.render(report)

class TestLabelmeAnnotationParsing:
    """dacl10k publishes labelme-style JSON polygons, not Pascal-VOC boxes.

    This project scores localisation by IoU over boxes, so each polygon is
    reduced to its bounding box. That is the standard way to use a segmentation
    set for detection, and it is recorded as a deviation in ASSUMPTIONS.md H6b.
    """

    def _write(self, path, shapes):
        path.write_text(json.dumps({
            "imageName": path.stem + ".jpg",
            "imageWidth": 500, "imageHeight": 400,
            "split": "train", "dacl10k_version": "v2",
            "shapes": shapes,
        }), encoding="utf-8")
        return path

    def test_a_polygon_becomes_its_bounding_box(self, tmp_path):
        path = self._write(tmp_path / "a.json", [{
            "label": "Crack", "shape_type": "polygon",
            "points": [[10, 20], [100, 25], [60, 90], [12, 70]],
        }])
        boxes = B.parse_annotation_file(path)
        assert len(boxes) == 1
        box = boxes[0]
        assert (box.x, box.y) == (10, 20)
        assert (box.w, box.h) == (90, 70)      # 100-10, 90-20
        assert box.defect_class == "crack"
        assert box.confidence == 1.0

    def test_dacl10k_class_names_fold_onto_the_shared_vocabulary(self, tmp_path):
        """A run over either corpus must report the same class names."""
        path = self._write(tmp_path / "b.json", [
            {"label": lbl, "shape_type": "polygon",
             "points": [[0, 0], [50, 0], [50, 50], [0, 50]]}
            for lbl in ("Spalling", "Rust", "ExposedRebars", "Efflorescence")
        ])
        assert {b.defect_class for b in B.parse_annotation_file(path)} == {
            "spallation", "corrosion_stain", "exposed_bars", "efflorescence"}

    def test_a_dacl10k_only_class_keeps_its_own_name(self, tmp_path):
        # It has no CODEBRIM counterpart, so it must not be folded onto one.
        path = self._write(tmp_path / "c.json", [{
            "label": "Wetspot", "shape_type": "polygon",
            "points": [[0, 0], [30, 0], [30, 30], [0, 30]]}])
        assert B.parse_annotation_file(path)[0].defect_class == "wetspot"

    def test_component_classes_are_not_scored_as_damage(self, tmp_path):
        """A bearing is what the bridge is made of, not what is wrong with it.

        Counting one as a defect the detector missed would put non-defects into
        the false-negative column — the reasoning behind ASSUMPTIONS.md E3.
        """
        square = [[0, 0], [40, 0], [40, 40], [0, 40]]
        path = self._write(tmp_path / "d.json", [
            {"label": "Bearing", "shape_type": "polygon", "points": square},
            {"label": "Drainage", "shape_type": "polygon", "points": square},
            {"label": "JTape", "shape_type": "polygon", "points": square},
            {"label": "PEquipment", "shape_type": "polygon", "points": square},
            {"label": "EJoint", "shape_type": "polygon", "points": square},
            {"label": "Crack", "shape_type": "polygon", "points": square},
        ])
        boxes = B.parse_annotation_file(path)
        assert [b.defect_class for b in boxes] == ["crack"]

    @pytest.mark.parametrize("label", ["Bearing", "EJoint", "Drainage",
                                       "PEquipment", "JTape", "WConccor"])
    def test_every_component_class_is_recognised_as_such(self, label):
        assert B.is_non_damage(label)

    @pytest.mark.parametrize("label", ["Crack", "Spalling", "Rust", "Cavity",
                                       "ExposedRebars", "Weathering"])
    def test_damage_classes_are_not_treated_as_components(self, label):
        assert not B.is_non_damage(label)

    def test_a_degenerate_polygon_is_dropped_not_scored_as_a_zero_box(self, tmp_path):
        path = self._write(tmp_path / "e.json", [
            {"label": "Crack", "shape_type": "point", "points": [[5, 5]]},
            {"label": "Crack", "shape_type": "polygon", "points": [[7, 7], [7, 7]]},
            {"label": "Crack", "shape_type": "polygon",
             "points": [[0, 0], [20, 0], [20, 20], [0, 20]]},
        ])
        assert len(B.parse_annotation_file(path)) == 1

    def test_an_annotation_of_components_only_is_reported_not_scored_as_clean(self, tmp_path):
        """Otherwise an image full of bearings scores as "no defects present"."""
        path = self._write(tmp_path / "f.json", [{
            "label": "Bearing", "shape_type": "polygon",
            "points": [[0, 0], [40, 0], [40, 40], [0, 40]]}])
        with pytest.raises(DataUnavailable) as exc:
            B.parse_annotation_file(path)
        assert "parse_labelme_json" in str(exc.value)

    def test_malformed_json_points_at_the_one_change_point(self, tmp_path):
        path = tmp_path / "g.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(DataUnavailable):
            B.parse_annotation_file(path)


class TestAnnotationIndex:
    """Annotations are found through one walk, not one walk per image.

    dacl10k keeps annotations in a parallel annotations/{split}/ tree rather than
    beside the images, so the sibling lookup misses and the fallback ran an
    rglob per image -- O(images x files). On 7,910 images over ~17,000 files that
    is the difference between a benchmark that finishes and one that hangs.
    """

    def test_a_parallel_annotation_tree_is_found(self, tmp_path):
        (tmp_path / "images" / "train").mkdir(parents=True)
        (tmp_path / "annotations" / "train").mkdir(parents=True)
        image = tmp_path / "images" / "train" / "img_0001.jpg"
        image.write_bytes(b"")
        annotation = tmp_path / "annotations" / "train" / "img_0001.json"
        annotation.write_text("{}", encoding="utf-8")

        index = B.build_annotation_index(tmp_path)
        assert index == {"img_0001": annotation}
        assert B.find_annotation(image, tmp_path, index) == annotation

    def test_a_sibling_annotation_still_wins(self, tmp_path):
        image = tmp_path / "shot.png"
        image.write_bytes(b"")
        sibling = tmp_path / "shot.xml"
        sibling.write_text("<annotation/>", encoding="utf-8")
        assert B.find_annotation(image, tmp_path, {}) == sibling

    def test_an_unannotated_image_resolves_to_none(self, tmp_path):
        image = tmp_path / "lonely.jpg"
        image.write_bytes(b"")
        assert B.find_annotation(image, tmp_path, {}) is None

    def test_the_index_is_built_once_and_is_deterministic(self, tmp_path):
        (tmp_path / "annotations").mkdir()
        for name in ("b", "a", "c"):
            (tmp_path / "annotations" / f"{name}.json").write_text("{}", encoding="utf-8")
        assert B.build_annotation_index(tmp_path) == B.build_annotation_index(tmp_path)

    def test_xml_is_preferred_over_json_for_the_same_stem(self, tmp_path):
        """Deterministic when a corpus carries both, rather than walk-order."""
        (tmp_path / "x.json").write_text("{}", encoding="utf-8")
        (tmp_path / "x.xml").write_text("<annotation/>", encoding="utf-8")
        assert B.build_annotation_index(tmp_path)["x"].suffix == ".xml"


class TestDacl10kDiscovery:
    def test_the_archive_wrapper_directory_is_tolerated(self, tmp_path):
        from src.ingest import dacl10k

        base = tmp_path / "dacl10k" / "dacl10k_v2_devphase"
        for split in ("train", "validation"):
            (base / "images" / split).mkdir(parents=True)
            (base / "annotations" / split).mkdir(parents=True)
            make_image(base / "images" / split / f"{split}_0001.jpg")

        found_base, images = dacl10k.find_images(root=tmp_path)
        assert found_base == base
        assert len(images) == 2

    def test_it_also_works_unpacked_without_the_wrapper(self, tmp_path):
        from src.ingest import dacl10k

        base = tmp_path / "dacl10k"
        for split in ("train", "validation"):
            (base / "images" / split).mkdir(parents=True)
            (base / "annotations" / split).mkdir(parents=True)
            make_image(base / "images" / split / f"{split}_0001.jpg")

        found_base, images = dacl10k.find_images(root=tmp_path)
        assert found_base == base
        assert len(images) == 2

    def test_the_unannotated_challenge_splits_are_not_registered(self, tmp_path):
        """testdev and testchallenge have no public annotations.

        Registering them would add images the benchmark could only skip, and a
        skipped image is indistinguishable in a report from one the detector
        failed on.
        """
        from src.ingest import dacl10k

        base = tmp_path / "dacl10k"
        for split in ("train", "validation", "testdev", "testchallenge"):
            (base / "images" / split).mkdir(parents=True)
            make_image(base / "images" / split / f"{split}_0001.jpg")
        (base / "annotations" / "train").mkdir(parents=True)
        (base / "annotations" / "validation").mkdir(parents=True)

        _, images = dacl10k.find_images(root=tmp_path)
        names = {p.parent.name for p in images}
        assert names == {"train", "validation"}

    def test_a_layout_without_annotations_is_refused_with_the_expected_shape(self, tmp_path):
        from src.ingest import dacl10k

        (tmp_path / "dacl10k" / "images" / "train").mkdir(parents=True)
        with pytest.raises(DataUnavailable) as exc:
            dacl10k.find_images(root=tmp_path)
        assert "annotations" in str(exc.value)

    def test_corpus_imagery_is_still_structurally_unattachable(self, tmp_path):
        """Invariant 6 must hold for a substituted corpus exactly as before."""
        from src.db import connect
        from src.ingest import dacl10k

        base = tmp_path / "dacl10k"
        for split in ("train", "validation"):
            (base / "images" / split).mkdir(parents=True)
            (base / "annotations" / split).mkdir(parents=True)
            make_image(base / "images" / split / f"{split}_0001.jpg")

        conn = connect(":memory:")
        result = dacl10k.ingest(conn, root=tmp_path, log=lambda *a: None)
        assert result["images"] == 2
        rows = conn.execute(
            "SELECT provenance, struct_norm, corpus FROM images").fetchall()
        assert rows
        for row in rows:
            assert row["provenance"] == "reference_corpus"
            assert row["struct_norm"] is None
            assert row["corpus"] == "dacl10k"

    def test_item_ids_are_stable_and_derived_from_the_path(self, tmp_path):
        from src.db import connect
        from src.ingest import dacl10k

        base = tmp_path / "dacl10k" / "dacl10k_v2_devphase"
        for split in ("train", "validation"):
            (base / "images" / split).mkdir(parents=True)
            (base / "annotations" / split).mkdir(parents=True)
        make_image(base / "images" / "train" / "dacl10k_v2_train_0042.jpg")

        conn = connect(":memory:")
        dacl10k.ingest(conn, root=tmp_path, log=lambda *a: None)
        first = [r[0] for r in conn.execute("SELECT artifact_id FROM images ORDER BY 1")]
        dacl10k.ingest(conn, root=tmp_path, log=lambda *a: None)
        second = [r[0] for r in conn.execute("SELECT artifact_id FROM images ORDER BY 1")]
        assert first == second
        # Relative to the base, so the wrapper directory does not leak into the ID.
        assert first == ["REF-dacl10k-images_train_dacl10k_v2_train_0042"]

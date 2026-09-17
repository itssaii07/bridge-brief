"""Detector benchmark against the CODEBRIM annotations.

Reports precision, recall and F1 for the configured detector over the reference
corpus. Ready to run; unrun, because the corpus is not on disk.

Two scores are reported for every run:

* **class-aware** — a detection counts only if its class matches the annotation's
* **class-agnostic** — localisation only

Both are shown because the shipped baseline localises without classifying
(it emits the single class ``defect``), so its class-aware score will be zero by
construction. Reporting only the class-agnostic number would flatter it;
reporting only the class-aware one would hide that it localises at all.

Matching rule: greedy by descending detection confidence, IoU >= 0.5, each
annotation claimed at most once. Unmatched detections are false positives,
unmatched annotations false negatives. These are the conventional PASCAL VOC
settings and the threshold is a named constant.

## The format assumption

``parse_annotation_file`` is the single change point for the annotation layout
(ASSUMPTIONS.md H4). It expects Pascal-VOC-style per-image XML, is namespace- and
case-insensitive, and accepts either ``xmin/ymin/xmax/ymax`` or ``x/y/w/h``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from ..db import DataUnavailable, RAW_ROOT, connect
from ..detect.base import DEFECT_CLASSES, Detection, DetectorUnavailable, get_detector, iou

#: IoU at or above which a detection is considered to have found an annotation.
IOU_THRESHOLD = 0.5

#: Annotation class names as published, mapped to our vocabulary. Unknown class
#: names are kept verbatim and counted, never silently folded into a known class.
CLASS_ALIASES = {
    "crack": "crack",
    "cracks": "crack",
    "spallation": "spallation",
    "spalling": "spallation",
    "spall": "spallation",
    "efflorescence": "efflorescence",
    "exposedbars": "exposed_bars",
    "exposed_bars": "exposed_bars",
    "exposedreinforcement": "exposed_bars",
    "corrosionstain": "corrosion_stain",
    "corrosion_stain": "corrosion_stain",
    "rust": "corrosion_stain",
}


def normalise_class(name: str | None) -> str:
    """Map a published annotation class name to our vocabulary, or keep it as-is."""
    key = re.sub(r"[^a-z]", "", str(name or "").lower())
    return CLASS_ALIASES.get(key, str(name or "").strip().lower() or "unlabelled")


def _local(tag: str) -> str:
    text = str(tag or "")
    if "}" in text:
        text = text.rsplit("}", 1)[1]
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _child_text(node: ET.Element, names: tuple[str, ...]) -> str | None:
    for child in node.iter():
        if _local(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    for key, value in node.attrib.items():
        if _local(key) in names and str(value).strip():
            return str(value).strip()
    return None


def parse_annotation_file(path: Path) -> list[Detection]:
    """**The single annotation-format change point.** Parse one annotation file.

    Ground-truth boxes are returned as :class:`Detection` objects with
    ``confidence = 1.0``, purely so that one geometry type is used on both sides
    of the comparison. They are never mixed with detector output: they are stored
    with ``source = 'annotation'`` and scored only as ground truth.

    Raises:
        DataUnavailable: if the file contains no parseable object boxes, so that a
            format mismatch is reported rather than scored as "no defects".
    """
    try:
        root = ET.fromstring(Path(path).read_bytes())
    except ET.ParseError as exc:
        raise DataUnavailable(f"cannot parse annotation file {path}: {exc}") from exc

    boxes: list[Detection] = []
    for node in root.iter():
        if _local(node.tag) not in ("object", "defect", "annotationobject"):
            continue
        name = _child_text(node, ("name", "class", "label", "defect", "type"))
        xmin = _child_text(node, ("xmin", "x1", "left"))
        ymin = _child_text(node, ("ymin", "y1", "top"))
        xmax = _child_text(node, ("xmax", "x2", "right"))
        ymax = _child_text(node, ("ymax", "y2", "bottom"))
        if xmin is not None and xmax is not None and ymin is not None and ymax is not None:
            x, y = int(float(xmin)), int(float(ymin))
            w, h = int(float(xmax)) - x, int(float(ymax)) - y
        else:
            x_raw = _child_text(node, ("x",))
            y_raw = _child_text(node, ("y",))
            w_raw = _child_text(node, ("w", "width"))
            h_raw = _child_text(node, ("h", "height"))
            if None in (x_raw, y_raw, w_raw, h_raw):
                continue
            x, y = int(float(x_raw)), int(float(y_raw))
            w, h = int(float(w_raw)), int(float(h_raw))
        if w <= 0 or h <= 0:
            continue
        boxes.append(Detection(x=x, y=y, w=w, h=h,
                               defect_class=normalise_class(name), confidence=1.0))

    if not boxes:
        raise DataUnavailable(
            f"{path} contained no parseable object boxes.\n"
            "The assumed Pascal-VOC-style layout (ASSUMPTIONS.md H4) may not match this "
            "corpus. Fix src/eval/codebrim_benchmark.py::parse_annotation_file — that is "
            "the only place that needs to change."
        )
    return boxes


@dataclass
class Scores:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return round(self.tp / (self.tp + self.fp), 4) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return round(self.tp / (self.tp + self.fn), 4) if (self.tp + self.fn) else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r:
            return 0.0 if (p is not None and r is not None) else None
        return round(2 * p * r / (p + r), 4)


@dataclass
class BenchmarkReport:
    detector: str
    iou_threshold: float
    images_scored: int = 0
    images_skipped: int = 0
    detections: int = 0
    annotations: int = 0
    class_aware: Scores = field(default_factory=Scores)
    class_agnostic: Scores = field(default_factory=Scores)
    per_class: dict[str, dict] = field(default_factory=dict)
    skipped_reasons: dict[str, int] = field(default_factory=dict)


def match(detections: list[Detection], annotations: list[Detection], *,
          class_aware: bool, threshold: float = IOU_THRESHOLD) -> Scores:
    """Greedy matching by descending detection confidence. Pure function."""
    scores = Scores()
    claimed: set[int] = set()
    for detection in sorted(detections, key=lambda d: (-d.confidence, d.sort_key())):
        best_index, best_iou = None, 0.0
        for index, annotation in enumerate(annotations):
            if index in claimed:
                continue
            if class_aware and detection.defect_class != annotation.defect_class:
                continue
            overlap = iou(detection, annotation)
            if overlap > best_iou:
                best_index, best_iou = index, overlap
        if best_index is not None and best_iou >= threshold:
            claimed.add(best_index)
            scores.tp += 1
        else:
            scores.fp += 1
    scores.fn = len(annotations) - len(claimed)
    return scores


def find_annotation(image_path: Path, root: Path) -> Path | None:
    """Locate the annotation file for an image.

    Tries the image's own stem with an ``.xml`` suffix beside it, then the same
    stem anywhere under the corpus root.
    """
    sibling = image_path.with_suffix(".xml")
    if sibling.exists():
        return sibling
    matches = list(root.rglob(f"{image_path.stem}.xml"))
    return matches[0] if matches else None


def run(*, root: Path | None = None, detector_name: str = "baseline",
        limit: int | None = None, log=print) -> BenchmarkReport:
    """Run the detector over the corpus and score it. Requires the corpus on disk."""
    from ..ingest.codebrim import find_images

    directory, images = find_images(root)
    if limit is not None:
        images = images[:limit]

    detector = get_detector(detector_name)
    report = BenchmarkReport(detector=f"{detector.name}@{detector.version}",
                             iou_threshold=IOU_THRESHOLD)
    per_class: dict[str, Scores] = {}

    log(f"benchmarking {detector.name} over {len(images):,} CODEBRIM image(s)")
    for index, image_path in enumerate(images, start=1):
        annotation_path = find_annotation(image_path, directory)
        if annotation_path is None:
            report.images_skipped += 1
            report.skipped_reasons["no annotation file found"] = \
                report.skipped_reasons.get("no annotation file found", 0) + 1
            continue
        try:
            annotations = parse_annotation_file(annotation_path)
        except DataUnavailable as exc:
            report.images_skipped += 1
            key = "annotation file did not parse"
            report.skipped_reasons[key] = report.skipped_reasons.get(key, 0) + 1
            log(f"  [warn] {exc}")
            continue

        detections = detector.detect(image_path)
        report.images_scored += 1
        report.detections += len(detections)
        report.annotations += len(annotations)

        aware = match(detections, annotations, class_aware=True)
        agnostic = match(detections, annotations, class_aware=False)
        for target, scores in ((report.class_aware, aware), (report.class_agnostic, agnostic)):
            target.tp += scores.tp
            target.fp += scores.fp
            target.fn += scores.fn

        for defect_class in {a.defect_class for a in annotations}:
            bucket = per_class.setdefault(defect_class, Scores())
            class_scores = match(
                [d for d in detections if d.defect_class == defect_class],
                [a for a in annotations if a.defect_class == defect_class],
                class_aware=False,
            )
            bucket.tp += class_scores.tp
            bucket.fp += class_scores.fp
            bucket.fn += class_scores.fn

        if index % 100 == 0:
            log(f"  [progress] {index:,}/{len(images):,} scored")

    report.per_class = {
        name: {"tp": s.tp, "fp": s.fp, "fn": s.fn,
               "precision": s.precision, "recall": s.recall, "f1": s.f1}
        for name, s in sorted(per_class.items())
    }
    return report


def render(report: BenchmarkReport) -> str:
    fmt = lambda v: "n/a" if v is None else f"{v:.4f}"
    out = ["=" * 72, f"CODEBRIM DETECTOR BENCHMARK — {report.detector}", "=" * 72, "",
           f"images scored      {report.images_scored:>8,}",
           f"images skipped     {report.images_skipped:>8,}",
           f"detections         {report.detections:>8,}",
           f"annotations        {report.annotations:>8,}",
           f"IoU threshold      {report.iou_threshold:>8}", ""]
    out.append(f"{'scoring':<18}{'TP':>8}{'FP':>8}{'FN':>8}{'precision':>12}{'recall':>10}{'F1':>10}")
    out.append("-" * 74)
    for label, scores in (("class-aware", report.class_aware),
                          ("class-agnostic", report.class_agnostic)):
        out.append(f"{label:<18}{scores.tp:>8,}{scores.fp:>8,}{scores.fn:>8,}"
                   f"{fmt(scores.precision):>12}{fmt(scores.recall):>10}{fmt(scores.f1):>10}")
    out.append("")
    if report.per_class:
        out.append("Per annotation class (localisation only)")
        out.append(f"{'class':<20}{'TP':>8}{'FP':>8}{'FN':>8}{'precision':>12}{'recall':>10}")
        out.append("-" * 66)
        for name, scores in report.per_class.items():
            out.append(f"{name:<20}{scores['tp']:>8,}{scores['fp']:>8,}{scores['fn']:>8,}"
                       f"{fmt(scores['precision']):>12}{fmt(scores['recall']):>10}")
        out.append("")
    if report.skipped_reasons:
        out.append("Skipped images by reason")
        for reason, count in report.skipped_reasons.items():
            out.append(f"  {reason:<40}{count:>8,}")
        out.append("")
    out.append(
        "NOTE: the detector shipped with this repository is a classical edge-energy\n"
        "baseline, not a trained model. Its class-aware score is zero by construction\n"
        "because it localises without classifying. These numbers are a floor for the\n"
        "pipeline, not a claim about achievable detection performance."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the detector against CODEBRIM.")
    parser.add_argument("--raw-root", default=None)
    parser.add_argument("--detector", default="baseline")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    try:
        report = run(root=Path(args.raw_root) if args.raw_root else None,
                     detector_name=args.detector, limit=args.limit)
    except (DataUnavailable, DetectorUnavailable) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(render(report))
    if args.json:
        payload = asdict(report)
        for key in ("class_aware", "class_agnostic"):
            scores = getattr(report, key)
            payload[key] = {"tp": scores.tp, "fp": scores.fp, "fn": scores.fn,
                            "precision": scores.precision, "recall": scores.recall,
                            "f1": scores.f1}
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

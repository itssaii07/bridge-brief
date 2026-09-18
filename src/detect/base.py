"""Detector interface.

The pipeline talks to a defect detector only through :class:`DefectDetector`, so
the model can be swapped — for a trained one, a hosted one, or a different
baseline — without touching upload, tiling, brief generation or the benchmark.

What ships in this repository is a classical baseline (``src/detect/baseline.py``),
not a trained model. No model was downloaded and none was trained: the data is not
on disk and downloads are out of scope. A benchmark number from an untrained
baseline will be poor, and reporting it as poor is the correct outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Defect vocabulary, aligned with the CODEBRIM annotation classes.
DEFECT_CLASSES: tuple[str, ...] = (
    "crack",
    "spallation",
    "efflorescence",
    "exposed_bars",
    "corrosion_stain",
)

#: What a detector that localises but does not classify emits. Scored only under
#: the class-agnostic metrics; see ASSUMPTIONS.md H1.
UNCLASSIFIED = "defect"


class DetectorUnavailable(RuntimeError):
    """The detector cannot run, and why — a missing dependency or a missing model.

    Raised instead of returning zero detections, because "found nothing" and
    "could not look" must never be the same answer.
    """


@dataclass(frozen=True)
class Detection:
    """One detected region, in pixel coordinates of the original image.

    ``confidence`` is the detector's own score in [0, 1]. It is carried into the
    finding and displayed; it is never rounded up or hidden.
    """

    x: int
    y: int
    w: int
    h: int
    defect_class: str
    confidence: float
    #: What the proposer says it saw. Only a describing proposer (the vision
    #: model) fills this in; geometry-only detectors leave it None.
    description: str | None = None

    @property
    def box(self) -> tuple[int, int, int, int]:
        """``(x1, y1, x2, y2)``."""
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def sort_key(self) -> tuple:
        """Deterministic ordering key.

        Region artifact IDs are derived from a detection's index in this order,
        which is a function of geometry alone — never of the order the detector
        happened to emit results in. That is what keeps region IDs stable across
        runs, as the ID system requires.
        """
        return (self.y, self.x, self.h, self.w, self.defect_class)


def iou(a: Detection, b: Detection) -> float:
    """Intersection over union of two boxes. 0.0 when they do not overlap."""
    ax1, ay1, ax2, ay2 = a.box
    bx1, by1, bx2, by2 = b.box
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    union = a.area + b.area - intersection
    return intersection / union if union > 0 else 0.0


@runtime_checkable
class DefectDetector(Protocol):
    """A defect detector. Implement this to swap the model."""

    name: str
    version: str

    def detect(self, image_path: Path) -> list[Detection]:
        """Return detections for one image, in pixel coordinates.

        Must raise :class:`DetectorUnavailable` if it cannot run, rather than
        returning an empty list.
        """
        ...


_REGISTRY: dict[str, type] = {}


def register(name: str, cls: type) -> None:
    _REGISTRY[name] = cls


def get_detector(name: str = "baseline", **kwargs) -> DefectDetector:
    """Construct a registered detector by name.

    Raises:
        DetectorUnavailable: if the name is unknown, listing what is registered.
    """
    from . import baseline  # noqa: F401  (registers the built-in on import)
    from . import vision  # noqa: F401  (registers the Claude vision proposer)

    if name not in _REGISTRY:
        raise DetectorUnavailable(
            f"unknown detector {name!r}; registered detectors: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name](**kwargs)

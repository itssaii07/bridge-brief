"""A classical, deterministic baseline defect detector.

**This is not a trained model.** It measures local edge energy and local darkness
over overlapping tiles and proposes the tiles that stand out from the rest of the
same image. Cracks and spalls are high-contrast, locally dark, high-gradient
features, so this finds *something*; it will also find shadows, vegetation,
lettering and lens dirt.

It exists so that the pipeline — upload, tiling, region artifact IDs, regions
cited in a brief, and the benchmark harness — is complete and runnable end to
end, and so that the benchmark produces a real, if low, number. Swap it for a
trained model through :class:`src.detect.base.DefectDetector` when one is
available; nothing else in the project changes.

Deterministic by construction: the same image always yields the same detections
in the same order, which is what makes region artifact IDs stable.
"""

from __future__ import annotations

import statistics
from pathlib import Path

from .base import UNCLASSIFIED, Detection, DetectorUnavailable, register
from .tiling import DEFAULT_OVERLAP, DEFAULT_TILE, plan_tiles

#: A tile is proposed when its response exceeds the image's own mean by this many
#: standard deviations. Relative to the image rather than absolute, because
#: exposure varies enormously between inspection photographs.
DEFAULT_Z_THRESHOLD = 1.5

#: Never propose more than this many regions for one photograph. A detector that
#: flags eighty regions has told the inspector nothing.
MAX_REGIONS = 12

#: Images are analysed at this longest edge to keep the scan bounded on the large
#: files a drone produces. Detections are scaled back to original coordinates.
WORKING_EDGE = 1600


def _require_pillow():
    try:
        from PIL import Image, ImageFilter, ImageOps, ImageStat  # noqa: F401
    except ImportError as exc:
        raise DetectorUnavailable(
            "Pillow is required for image analysis but is not installed.\n"
            "Install it with:  pip install -e \".[imagery]\"\n"
            "Everything that does not involve imagery runs without it."
        ) from exc
    return Image, ImageFilter, ImageOps, ImageStat


class BaselineDetector:
    """Edge-energy and darkness baseline. See the module docstring."""

    name = "baseline_edge_energy"
    version = "1"

    def __init__(self, *, tile: int = DEFAULT_TILE, overlap: float = DEFAULT_OVERLAP,
                 z_threshold: float = DEFAULT_Z_THRESHOLD, max_regions: int = MAX_REGIONS):
        self.tile = tile
        self.overlap = overlap
        self.z_threshold = z_threshold
        self.max_regions = max_regions

    def detect(self, image_path: Path) -> list[Detection]:
        """Propose defect regions for one image.

        Raises:
            DetectorUnavailable: if Pillow is missing or the file cannot be
                decoded. Never returns ``[]`` to mean "could not look".
        """
        Image, ImageFilter, ImageOps, ImageStat = _require_pillow()
        path = Path(image_path)
        if not path.exists():
            raise DetectorUnavailable(f"image not found: {path}")

        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("L")
        except OSError as exc:
            raise DetectorUnavailable(f"cannot decode image {path}: {exc}") from exc

        original_w, original_h = image.size
        scale = 1.0
        if max(original_w, original_h) > WORKING_EDGE:
            scale = WORKING_EDGE / max(original_w, original_h)
            image = image.resize((max(1, int(original_w * scale)),
                                  max(1, int(original_h * scale))))
        width, height = image.size
        edges = image.filter(ImageFilter.FIND_EDGES)

        responses: list[tuple[float, object]] = []
        for tile in plan_tiles(width, height, tile=self.tile, overlap=self.overlap):
            edge_energy = ImageStat.Stat(edges.crop(tile.box)).mean[0]
            darkness = 255.0 - ImageStat.Stat(image.crop(tile.box)).mean[0]
            # Edge energy dominates; darkness is a secondary cue for spalls and
            # shadowed cracks. Both are image-relative, so the weights only set
            # their balance, not an absolute sensitivity.
            responses.append((edge_energy + 0.25 * darkness, tile))

        if len(responses) < 2:
            return []

        values = [r for r, _ in responses]
        mean = statistics.fmean(values)
        deviation = statistics.pstdev(values)
        if deviation == 0:
            return []   # a flat image: nothing stands out, and we say nothing

        detections: list[Detection] = []
        inverse = 1.0 / scale
        for response, tile in responses:
            z = (response - mean) / deviation
            if z < self.z_threshold:
                continue
            # Map the z-score into a confidence that saturates rather than
            # reaching 1.0: this detector is never certain, and should not look it.
            confidence = round(min(0.75, 0.25 + 0.15 * (z - self.z_threshold + 1)), 4)
            detections.append(Detection(
                x=int(tile.x * inverse), y=int(tile.y * inverse),
                w=int(tile.w * inverse), h=int(tile.h * inverse),
                defect_class=UNCLASSIFIED, confidence=confidence,
            ))

        detections.sort(key=lambda d: (-d.confidence, d.sort_key()))
        detections = detections[:self.max_regions]
        # Final ordering is geometric, so region indices — and therefore region
        # artifact IDs — depend only on where the regions are.
        detections.sort(key=lambda d: d.sort_key())
        return detections


register("baseline", BaselineDetector)

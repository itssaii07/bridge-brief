"""A generative vision proposer: Claude reads an inspection photograph.

This is the "Generative AI" in the brief generator. Given an uploaded photograph,
a Claude vision model returns the defects it can see, each with a box, a class, a
one-sentence description and a confidence. Those become ordinary region
artifacts (``IMG-{struct}-{photo}-r{n}``) through the same
:class:`~src.detect.base.DefectDetector` interface as the classical baseline, so
the grounding gate, the citation chain and the sign-off queue treat them exactly
alike. Nothing downstream trusts this module more than it trusts the baseline.

What it is, stated plainly, because the output will be read by people:

* **A proposal, not an inspection.** The model looks at pixels. It has not seen
  the structure, cannot judge depth, load path or history, and is told never to
  assess safety or recommend work. Every region it returns is ``single_source``
  and is labelled an automated proposal in the brief.
* **Its confidence is self-reported and uncalibrated.** A language model's stated
  certainty is not a measured probability. It is capped at
  :data:`CONFIDENCE_CAP`, the same ceiling the baseline uses, so a model
  proposal can never read as more certain than a human-confirmed finding.
* **Its boxes are approximate.** Coordinates come back as fractions of the image
  and are mapped onto the original pixels. They locate a defect; they do not
  measure it.

The original photograph is never modified. The model is sent a downscaled JPEG
built in memory for the request and discarded afterwards, which keeps the
request under the API's image limits without touching the stored original
(invariant 3).

Configuration, all by environment:

    ANTHROPIC_API_KEY           credentials (or any source the SDK resolves)
    BRIDGE_BRIEF_VISION_MODEL   model id, default ``claude-opus-5``
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path

from .base import DEFECT_CLASSES, Detection, DetectorUnavailable, register

DEFAULT_MODEL = "claude-opus-5"

#: Self-reported model confidence is uncalibrated, so it is capped at the same
#: ceiling as the classical baseline. A proposal never looks certain.
CONFIDENCE_CAP = 0.75

#: The long edge the model is sent. Larger images are downscaled by the API
#: anyway; doing it here keeps the payload small and the coordinate mapping ours.
MAX_EDGE = 1568

#: Classes the model may use. ``other`` exists so a real defect outside the
#: vocabulary is reported with a description rather than forced into a wrong class.
VISION_CLASSES: tuple[str, ...] = DEFECT_CLASSES + ("other",)

SYSTEM = (
    "You examine photographs supplied for a bridge or civil-infrastructure "
    "inspection and report visible surface defects so a human inspector can review "
    "them.\n\n"
    "For each defect you can actually see, give its class, a tight bounding box, a "
    "one-sentence factual description of what is visible (location on the member, "
    "extent, appearance), and your confidence from 0 to 1 that the defect is real. "
    "Classes: crack, spallation (concrete broken away), efflorescence (white salt "
    "deposits), exposed_bars (visible reinforcement), corrosion_stain (rust "
    "staining), or other.\n\n"
    "Boxes are fractions of the image: x_min and x_max of its width, y_min and y_max "
    "of its height, from the top-left corner, each between 0 and 1.\n\n"
    "Report only what is visible in this photograph. Do not infer hidden damage, do "
    "not estimate load capacity, do not say whether the structure is safe or unsafe, "
    "and do not recommend repairs or maintenance: a human inspector makes those "
    "judgements. If you see no defects, return an empty list. If the photograph does "
    "not show a structure at all, set structure_visible to false."
)

INSTRUCTION = (
    "List the visible defects in this inspection photograph. In image_notes, say in "
    "one or two sentences what the photograph shows and anything that limits what "
    "can be seen (distance, lighting, blur, obstruction)."
)

#: The response shape, enforced by structured outputs rather than hoped for.
SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "defect_class": {"type": "string", "enum": list(VISION_CLASSES)},
                    "description": {"type": "string"},
                    "x_min": {"type": "number"},
                    "y_min": {"type": "number"},
                    "x_max": {"type": "number"},
                    "y_max": {"type": "number"},
                    "confidence": {"type": "number"},
                },
                "required": ["defect_class", "description", "x_min", "y_min",
                             "x_max", "y_max", "confidence"],
                "additionalProperties": False,
            },
        },
        "structure_visible": {"type": "boolean"},
        "image_notes": {"type": "string"},
    },
    "required": ["observations", "structure_visible", "image_notes"],
    "additionalProperties": False,
}


def credentials_configured() -> tuple[bool, str]:
    """Whether a credential source the SDK will read appears to be present.

    A cheap local check, used to tell the operator up front whether the vision
    proposer can run. It does not prove the credentials are valid; a request that
    is rejected is still reported as unavailable rather than crashing a run.
    """
    # find_spec rather than import: importing the SDK takes seconds, and this
    # check runs on page loads that may never call the model.
    import importlib.util

    if importlib.util.find_spec("anthropic") is None:
        return False, "the anthropic package is not installed (pip install -e \".[llm]\")"
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        if os.environ.get(variable):
            return True, f"credentials from {variable}"
    profile_dir = Path.home() / ".config" / "anthropic"
    if profile_dir.is_dir() and any(profile_dir.iterdir()):
        return True, "credentials from an ant auth login profile"
    return False, "no Anthropic credentials found; set ANTHROPIC_API_KEY"


def _encode_for_request(image_path: Path) -> tuple[str, int, int]:
    """A downscaled JPEG of the photograph, base64-encoded, plus its original size.

    Built in memory and discarded after the request. The stored original is only
    ever read.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise DetectorUnavailable(
            "The vision proposer needs Pillow to prepare images: "
            "pip install -e \".[imagery]\"") from exc
    try:
        with Image.open(image_path) as source:
            # Honour the camera's orientation flag, so a portrait photo is not
            # described sideways and its boxes land where the viewer sees them.
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            image = image.convert("RGB")
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88)
    except (OSError, ValueError) as exc:
        raise DetectorUnavailable(f"cannot read {image_path.name}: {exc}") from exc
    return base64.standard_b64encode(buffer.getvalue()).decode("ascii"), width, height


def _to_pixels(observation: dict, width: int, height: int) -> Detection | None:
    """Map one fractional box onto the original image, or drop it if degenerate."""
    def clamp(value) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    x0, x1 = sorted((clamp(observation.get("x_min")), clamp(observation.get("x_max"))))
    y0, y1 = sorted((clamp(observation.get("y_min")), clamp(observation.get("y_max"))))
    x, y = int(round(x0 * width)), int(round(y0 * height))
    w, h = int(round((x1 - x0) * width)), int(round((y1 - y0) * height))
    if w < 2 or h < 2:
        return None

    defect_class = observation.get("defect_class")
    if defect_class not in VISION_CLASSES:
        defect_class = "other"
    description = " ".join(str(observation.get("description") or "").split())[:400]
    try:
        confidence = float(observation.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = round(min(CONFIDENCE_CAP, max(0.0, confidence)), 4)
    if confidence <= 0:
        return None
    return Detection(x=x, y=y, w=w, h=h, defect_class=defect_class,
                     confidence=confidence, description=description or None)


def parse_response(text: str, width: int, height: int) -> tuple[list[Detection], dict]:
    """Turn the model's JSON into detections plus the notes that came with it.

    Separated from the network call so the conversion can be tested without an
    API key, and so a malformed reply is reported rather than half-used.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DetectorUnavailable(f"the vision model returned malformed JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        raise DetectorUnavailable("the vision model's reply did not match the schema")

    detections = []
    dropped = 0
    for observation in payload["observations"]:
        detection = _to_pixels(observation if isinstance(observation, dict) else {},
                               width, height)
        if detection is None:
            dropped += 1
        else:
            detections.append(detection)
    notes = {
        "structure_visible": bool(payload.get("structure_visible", True)),
        "image_notes": " ".join(str(payload.get("image_notes") or "").split())[:600],
        "observations_returned": len(payload["observations"]),
        "observations_dropped": dropped,
    }
    return detections, notes


class VisionDetector:
    """Claude vision as a :class:`~src.detect.base.DefectDetector`."""

    name = "claude_vision"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("BRIDGE_BRIEF_VISION_MODEL", DEFAULT_MODEL)
        self.version = self.model
        #: What the model said about the last photograph it looked at, beyond the
        #: boxes: whether a structure was visible at all, and what limited the view.
        self.last_notes: dict = {}

    def detect(self, image_path: Path) -> list[Detection]:
        image_path = Path(image_path)
        if not image_path.is_file():
            raise DetectorUnavailable(f"photo not found: {image_path}")
        configured, reason = credentials_configured()
        if not configured:
            raise DetectorUnavailable(f"vision proposer unavailable: {reason}")

        import anthropic

        data, width, height = _encode_for_request(image_path)
        client = anthropic.Anthropic()
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                # If the primary model declines, let the API re-run the request on
                # a fallback model rather than silently returning nothing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64",
                                                     "media_type": "image/jpeg",
                                                     "data": data}},
                        {"type": "text", "text": INSTRUCTION},
                    ],
                }],
            )
        except anthropic.AuthenticationError as exc:
            raise DetectorUnavailable(
                "the Anthropic API rejected the credentials; check ANTHROPIC_API_KEY"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise DetectorUnavailable(f"the API key lacks permission: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise DetectorUnavailable("the Anthropic API is rate limiting; retry shortly") from exc
        except anthropic.BadRequestError as exc:
            raise DetectorUnavailable(f"the vision request was rejected: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise DetectorUnavailable(
                f"the Anthropic API returned {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise DetectorUnavailable("could not reach the Anthropic API") from exc

        if response.stop_reason == "refusal":
            raise DetectorUnavailable("the vision model declined to analyse this photograph")
        if response.stop_reason == "max_tokens":
            raise DetectorUnavailable("the vision model's reply was cut off before it finished")

        text = next((block.text for block in response.content
                     if getattr(block, "type", "") == "text"), "")
        detections, notes = parse_response(text, width, height)
        notes["model"] = getattr(response, "model", self.model)
        self.last_notes = notes
        if not notes["structure_visible"]:
            # The model says there is no structure here. Proposing defects on a
            # photograph of something else would be worse than proposing none.
            return []
        return detections


register("vision", VisionDetector)

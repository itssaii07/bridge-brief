"""One inspection, end to end: photographs in, a draft brief out.

This is what the web app's upload button runs. It strings together the modules
that already exist, in the order an inspector would, and reports each stage as it
finishes so the browser can show real progress rather than an animation that
claims work is happening:

1. **Anchor** the inspection to a structure that exists in the federal inventory.
   A photograph is never attached to a bridge that is not on record: the upload
   store would otherwise invent an inventory entry, which is the one kind of
   fabrication this project exists to prevent.
2. **Preserve** each photograph byte-for-byte, named by its own content hash, so
   the original stays available and a second photo with the same filename can
   never overwrite the first.
3. **Propose** defect regions on each new photograph, with the Claude vision
   model when credentials are configured and the classical baseline otherwise.
   A vision failure on one photo falls back to the baseline for that photo and
   says so; it never silently produces nothing.
4. **Cross-check** the federal records for the structure's latest inspection year.
5. **Draft** the brief through the grounding gate, which drops and counts any
   sentence without a resolvable citation.

Nothing here clears a structure, schedules work, or signs anything. The result is
a draft in the sign-off queue.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .. import ids
from ..analysis import contradictions
from ..analysis.findings import save_findings
from ..db import DataUnavailable
from ..detect.base import DetectorUnavailable, get_detector
from ..generate.brief import generate
from ..ingest.uploads import IMAGE_SUFFIXES, ingest_upload, store_detections
from ..store import StructureNotFound, resolve_structure_key

#: Largest single photograph accepted. Drone stills are typically 5-15 MB.
MAX_PHOTO_BYTES = 40 * 1024 * 1024
#: Most photographs in one inspection upload.
MAX_PHOTOS = 24

#: Leading bytes of each accepted format. The extension is a claim; these are
#: what the file actually is.
SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"RIFF", ".webp"),          # confirmed by the WEBP tag below
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"BM", ".bmp"),
)

DETECTOR_CHOICES = ("auto", "vision", "baseline")


class InspectionError(ValueError):
    """The inspection cannot run as requested. The message says why and what to do."""


@dataclass(frozen=True)
class Photo:
    filename: str
    data: bytes


def sniff_extension(data: bytes) -> str | None:
    """The image format the bytes actually are, or None."""
    for signature, extension in SIGNATURES:
        if data.startswith(signature):
            if extension == ".webp" and data[8:12] != b"WEBP":
                continue
            return extension
    return None


def stored_name(filename: str, data: bytes) -> str:
    """A filename that is safe on disk and unique to the photograph's content.

    The inspector's own stem is kept so the file is recognisable, and the first
    ten hex digits of its SHA-256 are appended. Identical bytes give the identical
    name, so re-uploading is idempotent; different bytes under the same name get
    different names, so no original is ever overwritten.
    """
    extension = sniff_extension(data) or ""
    stem = Path(filename or "photo").stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")[:40] or "photo"
    digest = hashlib.sha256(data).hexdigest()[:10]
    return f"{stem}_{digest}{extension}"


def resolve_detector(choice: str) -> tuple[str, str]:
    """Which proposer to run, and why. ``auto`` prefers vision when it can run."""
    from ..detect.vision import credentials_configured

    choice = (choice or "auto").strip().lower()
    if choice not in DETECTOR_CHOICES:
        raise InspectionError(
            f"unknown detector {choice!r}; choose one of {', '.join(DETECTOR_CHOICES)}")
    configured, reason = credentials_configured()
    if choice == "baseline":
        return "baseline", "classical edge-energy baseline, as requested"
    if choice == "vision":
        if not configured:
            raise InspectionError(f"Claude vision was requested but {reason}.")
        return "vision", f"Claude vision ({reason})"
    if configured:
        return "vision", f"Claude vision ({reason})"
    return "baseline", f"classical baseline, because {reason}"


def latest_year(conn, struct_norm: str) -> int | None:
    row = conn.execute("SELECT MAX(year) FROM ratings WHERE struct_norm = ?",
                       (struct_norm,)).fetchone()
    return row[0] if row else None


def run_inspection(conn, structure: str, photos: list[Photo], *,
                   detector: str = "auto", upload_root: Path | None = None
                   ) -> Iterator[dict]:
    """Run one inspection, yielding a progress event as each stage completes.

    Every event is a dict with ``stage``, ``status`` (``running``, ``done``,
    ``warning`` or ``error``) and a human ``message``. The final event has stage
    ``complete`` and carries the brief id.

    Raises:
        InspectionError: for any input problem, before anything is written.
    """
    started = time.monotonic()

    # ---- validate everything before writing anything -----------------------
    if not photos:
        raise InspectionError("Add at least one photograph of the structure.")
    if len(photos) > MAX_PHOTOS:
        raise InspectionError(f"Upload at most {MAX_PHOTOS} photographs at a time.")
    for photo in photos:
        if len(photo.data) > MAX_PHOTO_BYTES:
            raise InspectionError(
                f"{photo.filename} is {len(photo.data) / 2**20:.0f} MB; the limit per "
                f"photograph is {MAX_PHOTO_BYTES / 2**20:.0f} MB.")
        extension = sniff_extension(photo.data)
        if extension is None or extension not in IMAGE_SUFFIXES:
            raise InspectionError(
                f"{photo.filename} is not a JPEG, PNG, WebP, TIFF or BMP image. "
                "The file's contents are checked, not just its name.")

    try:
        struct_norm = resolve_structure_key(conn, structure)
    except StructureNotFound as exc:
        raise InspectionError(str(exc)) from exc
    year = latest_year(conn, struct_norm)
    if year is None:
        raise InspectionError(
            f"{struct_norm} has no National Bridge Inventory rating on record, so "
            "there is nothing to anchor an inspection brief to.")
    detector_name, detector_reason = resolve_detector(detector)

    record = conn.execute(
        "SELECT state_abbr, facility, feature_crossed, year_built FROM structures "
        "WHERE struct_norm = ?", (struct_norm,)).fetchone()
    where = " over ".join(p for p in (record["facility"], record["feature_crossed"]) if p)
    yield {"stage": "anchor", "status": "done",
           "message": f"Anchored to {struct_norm}"
                      + (f", {where}" if where else "")
                      + f". Latest federal record: {year}.",
           "data": {"structure": struct_norm, "year": year}}

    # ---- preserve the originals --------------------------------------------
    yield {"stage": "preserve", "status": "running",
           "message": f"Storing {len(photos)} original photograph(s) unmodified."}
    new_images: list[tuple[str, Path, str]] = []
    with tempfile.TemporaryDirectory(prefix="bridge-brief-upload-") as scratch:
        for photo in photos:
            name = stored_name(photo.filename, photo.data)
            staged = Path(scratch) / name
            staged.write_bytes(photo.data)
            artifact_id = ingest_upload(conn, struct_norm, staged,
                                        upload_root=upload_root, log=lambda *a: None)
            stored = conn.execute("SELECT stored_path, sha256 FROM images WHERE artifact_id = ?",
                                  (artifact_id,)).fetchone()
            new_images.append((artifact_id, Path(stored["stored_path"]), photo.filename))
    yield {"stage": "preserve", "status": "done",
           "message": f"{len(new_images)} original(s) preserved byte-for-byte, each "
                      "addressable by its own artifact ID.",
           "data": {"images": [a for a, _, _ in new_images]}}

    # ---- propose defect regions --------------------------------------------
    yield {"stage": "detect", "status": "running",
           "message": f"Proposing defect regions with {detector_reason}."}
    proposer = get_detector(detector_name)
    fallback = None
    total_regions = 0
    for artifact_id, path, original_name in new_images:
        active = proposer
        try:
            detections = active.detect(path)
        except DetectorUnavailable as exc:
            if detector_name != "vision":
                raise
            # One photograph failing in the vision model must not lose the whole
            # inspection. The baseline runs instead, and the swap is reported.
            fallback = fallback or get_detector("baseline")
            active = fallback
            detections = active.detect(path)
            yield {"stage": "detect", "status": "warning",
                   "message": f"{original_name}: vision unavailable ({exc}); the baseline "
                              "detector was used for this photograph instead."}
        store_detections(conn, artifact_id, detections,
                         detector_name=active.name, detector_version=active.version)
        total_regions += len(detections)
        notes = getattr(active, "last_notes", {}) or {}
        if notes and not notes.get("structure_visible", True):
            yield {"stage": "detect", "status": "warning",
                   "message": f"{original_name}: the vision model did not see a structure "
                              "in this photograph, so no regions were proposed on it."}
        # Per-photo results are informational; the stage is only done once every
        # photograph has been read, so the progress display never runs ahead.
        yield {"stage": "detect", "status": "info",
               "message": f"{original_name}: {len(detections)} candidate region(s)."
                          + (f" {notes['image_notes']}" if notes.get("image_notes") else ""),
               "data": {"image": artifact_id, "regions": len(detections),
                        "notes": notes.get("image_notes")}}
    yield {"stage": "detect", "status": "done",
           "message": f"{total_regions} candidate region(s) across {len(new_images)} "
                      "photograph(s). Each is an automated proposal awaiting an inspector.",
           "data": {"regions": total_regions}}

    # ---- cross-check the federal records -----------------------------------
    yield {"stage": "records", "status": "running",
           "message": f"Cross-checking the {year} inventory rating against element data."}
    findings = contradictions.analyse_structure(conn, struct_norm, year)
    conn.execute("DELETE FROM findings WHERE struct_norm = ? AND year = ? "
                 "AND kind IN ('contradiction','condition')", (struct_norm, year))
    if findings:
        save_findings(conn, findings)
    conn.commit()
    tiers: dict[str, int] = {}
    for finding in findings:
        tiers[finding.evidence_tier] = tiers.get(finding.evidence_tier, 0) + 1
    yield {"stage": "records", "status": "done",
           "message": (f"{tiers.get('conflicting', 0)} conflicting, "
                       f"{tiers.get('corroborated', 0)} corroborated, "
                       f"{tiers.get('single_source', 0)} single-source finding(s)."),
           "data": {"tiers": tiers}}

    # ---- draft through the grounding gate -----------------------------------
    yield {"stage": "draft", "status": "running",
           "message": "Drafting the brief. Every sentence must cite a resolvable record."}
    try:
        summary = generate(conn, struct_norm, year, log=lambda *a: None)
    except DataUnavailable as exc:
        raise InspectionError(str(exc)) from exc
    yield {"stage": "draft", "status": "done",
           "message": (f"{summary['rendered']} sentence(s) rendered, "
                       f"{summary['blocked_unsupported']} blocked as unsupported."),
           "data": {"brief_id": summary["brief_id"]}}

    yield {"stage": "complete", "status": "done",
           "message": "Draft brief ready for human review. It carries no authority "
                      "until a named inspector signs it off.",
           "data": {"brief_id": summary["brief_id"], "structure": struct_norm,
                    "year": year, "regions": total_regions,
                    "seconds": round(time.monotonic() - started, 1)}}

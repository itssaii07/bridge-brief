"""Inspection photo upload, and running a detector over uploaded photos.

Two invariants meet here.

**Originals are immutable (invariant 3).** An uploaded photo is copied byte for
byte into ``data/uploads/{structure}/`` and never re-encoded, rotated, resized or
stripped of metadata. Everything derived — tiles, overlays — goes under
``data/derived/``. The stored file's SHA-256 is recorded so the original can be
shown to be unmodified.

**Imagery provenance is always labelled (invariant 6).** Every image row is
written with ``provenance = 'inspection_upload'`` here, and reference corpus
imagery is written with ``provenance = 'reference_corpus'`` and a NULL structure
by ``codebrim.py``. The column is NOT NULL with a CHECK constraint, so there is
no way to store an image whose provenance is unstated.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .. import ids
from ..db import DataUnavailable, UPLOAD_ROOT, connect, utcnow
from ..detect.base import DetectorUnavailable, Detection, get_detector
from ..store import (
    StructureNotFound,
    register_artifact,
    resolve_structure_key,
    sha256_file,
    upsert_structure,
)

#: File types accepted as inspection photographs.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def _image_size(path: Path) -> tuple[int | None, int | None]:
    """Read pixel dimensions if Pillow is available; otherwise report unknown.

    Unknown dimensions are stored as NULL. The upload still succeeds — refusing a
    photograph because an optional dependency is missing would be the wrong
    failure, and a NULL is honest.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, None
    try:
        with Image.open(path) as image:
            return image.size
    except OSError:
        return None, None


def ingest_upload(conn, struct: str, source_path: Path, *, captured_at: str | None = None,
                  upload_root: Path | None = None, log=print) -> str:
    """Copy one inspection photograph into the upload store and register it.

    Returns the image's artifact ID. Idempotent: re-uploading the same file under
    the same name produces the same artifact ID and the same stored bytes.

    Raises:
        DataUnavailable: if the source file does not exist.
        ValueError: if the file is not an accepted image type, or its name does
            not yield a usable photo key.
    """
    source = Path(source_path)
    if not source.exists():
        raise DataUnavailable(f"photo not found: {source}")
    if source.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(
            f"{source.name} is not an accepted image type "
            f"({', '.join(sorted(IMAGE_SUFFIXES))})"
        )

    struct_norm = ids.normalise_struct(struct)
    photo_key = ids.photo_key_from_filename(source.name)
    artifact_id = ids.img_id(struct_norm, photo_key)

    destination_dir = (upload_root or UPLOAD_ROOT) / struct_norm
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.resolve() != source.resolve():
        # copy2 preserves mtime; the bytes are untouched.
        shutil.copy2(source, destination)

    width, height = _image_size(destination)
    digest = sha256_file(destination)

    upsert_structure(conn, struct_norm, struct_raw=str(struct))
    conn.execute(
        """
        INSERT INTO images
            (artifact_id, struct_norm, provenance, corpus, photo_key, stored_path,
             sha256, width, height, captured_at, uploaded_at)
        VALUES (?, ?, 'inspection_upload', NULL, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact_id) DO UPDATE SET
            stored_path = excluded.stored_path, sha256 = excluded.sha256,
            width = excluded.width, height = excluded.height,
            captured_at = COALESCE(excluded.captured_at, images.captured_at)
        """,
        (artifact_id, struct_norm, photo_key, str(destination), digest,
         width, height, captured_at, utcnow()),
    )
    register_artifact(
        conn, artifact_id, "IMG", struct_norm=struct_norm,
        summary=f"Inspection photograph {source.name} supplied for structure {struct_norm}",
        source_path=str(destination), source_locator="whole image",
    )
    conn.commit()
    log(f"  [upload] {source.name} -> {artifact_id}")
    return artifact_id


def ingest_directory(conn, struct: str, directory: Path, *, upload_root: Path | None = None,
                     log=print) -> list[str]:
    """Upload every accepted image in a directory, in name order."""
    directory = Path(directory)
    if not directory.is_dir():
        raise DataUnavailable(f"not a directory: {directory}")
    photos = sorted(p for p in directory.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    if not photos:
        raise DataUnavailable(
            f"{directory} contains no images in {', '.join(sorted(IMAGE_SUFFIXES))}"
        )
    return [ingest_upload(conn, struct, p, upload_root=upload_root, log=log) for p in photos]


def store_detections(conn, image_artifact: str, detections: list[Detection],
                     *, detector_name: str, detector_version: str,
                     source: str = "detector") -> list[str]:
    """Persist detections as region artifacts under their parent image.

    Region indices follow the geometric ordering defined in
    :meth:`src.detect.base.Detection.sort_key`, so the same image and detector
    always produce the same region artifact IDs.
    """
    conn.execute(
        "DELETE FROM image_regions WHERE image_artifact = ? AND source = ?",
        (image_artifact, source),
    )
    ordered = sorted(detections, key=lambda d: d.sort_key())
    region_ids: list[str] = []
    for index, detection in enumerate(ordered):
        region_id = ids.region_id(image_artifact, index)
        conn.execute(
            """
            INSERT INTO image_regions
                (artifact_id, image_artifact, region_index, x, y, w, h,
                 defect_class, confidence, source, detector_name, detector_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                x = excluded.x, y = excluded.y, w = excluded.w, h = excluded.h,
                defect_class = excluded.defect_class, confidence = excluded.confidence,
                detector_name = excluded.detector_name,
                detector_version = excluded.detector_version
            """,
            (region_id, image_artifact, index, detection.x, detection.y,
             detection.w, detection.h, detection.defect_class, detection.confidence,
             source, detector_name, detector_version),
        )
        parent = conn.execute(
            "SELECT struct_norm, stored_path FROM images WHERE artifact_id = ?",
            (image_artifact,),
        ).fetchone()
        register_artifact(
            conn, region_id, "REGION",
            struct_norm=parent["struct_norm"] if parent else None,
            summary=(f"{detection.defect_class} candidate region in "
                     f"{image_artifact} at ({detection.x}, {detection.y}) "
                     f"{detection.w}x{detection.h}px, detector confidence "
                     f"{detection.confidence:.2f}"),
            source_path=parent["stored_path"] if parent else None,
            source_locator=f"x={detection.x} y={detection.y} w={detection.w} h={detection.h}",
        )
        region_ids.append(region_id)
    conn.commit()
    return region_ids


def detect_for_structure(conn, struct: str, *, detector_name: str = "baseline", log=print) -> dict:
    """Run the detector over every uploaded photo for a structure.

    Only ``inspection_upload`` images are considered. Reference corpus imagery is
    never attached to a structure, so it cannot be swept in here.
    """
    struct_norm = ids.normalise_struct(struct)
    rows = conn.execute(
        "SELECT artifact_id, stored_path FROM images "
        "WHERE struct_norm = ? AND provenance = 'inspection_upload' ORDER BY artifact_id",
        (struct_norm,),
    ).fetchall()
    if not rows:
        raise DataUnavailable(
            f"No inspection photographs have been uploaded for structure {struct_norm}.\n"
            f"Upload them with:  python -m src.ingest.uploads --structure {struct_norm} "
            "--dir <folder of photos>"
        )

    detector = get_detector(detector_name)
    summary = {"structure": struct_norm, "images": 0, "regions": 0,
               "detector": f"{detector.name}@{detector.version}"}
    for row in rows:
        detections = detector.detect(Path(row["stored_path"]))
        region_ids = store_detections(
            conn, row["artifact_id"], detections,
            detector_name=detector.name, detector_version=detector.version)
        summary["images"] += 1
        summary["regions"] += len(region_ids)
        log(f"  [detect] {row['artifact_id']}: {len(region_ids)} region(s)")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload inspection photographs and run the defect detector.")
    parser.add_argument("--structure", required=True,
                        help="state-qualified structure key (e.g. AL013450); a bare "
                             "structure number is accepted when only one state uses it")
    parser.add_argument("--dir", default=None, help="directory of photos to upload")
    parser.add_argument("--file", action="append", default=None, help="a single photo; repeatable")
    parser.add_argument("--detect", action="store_true", help="run the detector after uploading")
    parser.add_argument("--detector", default="baseline")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    if not args.dir and not args.file and not args.detect:
        parser.error("give --dir, --file, or --detect")

    conn = connect(args.db)
    try:
        # Resolve once, so photos and detections land under the same key and a
        # bare number cannot create an unqualified structure that joins to
        # nothing.
        structure = resolve_structure_key(conn, args.structure)
        if args.dir:
            ingest_directory(conn, structure, Path(args.dir))
        for path in args.file or []:
            ingest_upload(conn, structure, Path(path))
        if args.detect:
            summary = detect_for_structure(conn, structure, detector_name=args.detector)
            print(f"\n{summary['images']} image(s), {summary['regions']} region(s), "
                  f"detector {summary['detector']}")
    except (DataUnavailable, DetectorUnavailable, StructureNotFound) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

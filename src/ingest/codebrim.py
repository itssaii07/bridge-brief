"""Ingest of the CODEBRIM reference corpus.

CODEBRIM is a public annotated defect-imagery benchmark. It is used here for
exactly one thing: measuring how well the detector detects. It is **never**
evidence about a named bridge.

Invariant 6 is enforced structurally rather than by convention: every row written
here carries ``provenance = 'reference_corpus'`` and ``struct_norm = NULL``, and
the ``images`` table makes ``provenance`` NOT NULL with a CHECK constraint. A
corpus image therefore cannot be attached to a structure — there is no column to
put the structure in.

Annotation parsing is confined to
``src/eval/codebrim_benchmark.py :: parse_annotation_file`` so that the format
assumption (ASSUMPTIONS.md H4) has one change point.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import ids
from ..db import DataUnavailable, RAW_ROOT, connect, utcnow
from ..store import register_artifact, sha256_file
from .uploads import IMAGE_SUFFIXES, _image_size

CORPUS = "codebrim"


def corpus_item_id(path: Path, root: Path) -> str:
    """Stable corpus item ID from the image's path relative to the corpus root.

    Derived from the path, so it is the same on every run and on every machine
    that has the same corpus — never from enumeration order.
    """
    relative = path.relative_to(root).with_suffix("")
    return "_".join(relative.parts)


def find_images(root: Path | None = None) -> tuple[Path, list[Path]]:
    """Locate CODEBRIM images under ``data/raw/codebrim/``."""
    directory = (root or RAW_ROOT) / CORPUS
    if not directory.exists():
        raise DataUnavailable(
            f"No CODEBRIM directory at {directory}.\n"
            "Download the CODEBRIM original images and annotations (see README.md) "
            "and unpack them there. Nothing is downloaded automatically."
        )
    images = sorted(p for p in directory.rglob("*")
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        raise DataUnavailable(
            f"{directory} exists but contains no image files "
            f"({', '.join(sorted(IMAGE_SUFFIXES))})."
        )
    return directory, images


def ingest(conn, *, root: Path | None = None, limit: int | None = None, log=print) -> dict:
    """Register the corpus as reference imagery. Originals are not copied or touched."""
    directory, images = find_images(root)
    if limit is not None:
        images = images[:limit]
    log(f"CODEBRIM: registering {len(images):,} image(s) from {directory}")

    registered = 0
    for index, path in enumerate(images, start=1):
        artifact_id = ids.ref_id(CORPUS, corpus_item_id(path, directory))
        width, height = _image_size(path)
        conn.execute(
            """
            INSERT INTO images
                (artifact_id, struct_norm, provenance, corpus, photo_key, stored_path,
                 sha256, width, height, captured_at, uploaded_at)
            VALUES (?, NULL, 'reference_corpus', ?, NULL, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                stored_path = excluded.stored_path, sha256 = excluded.sha256,
                width = excluded.width, height = excluded.height
            """,
            (artifact_id, CORPUS, str(path), sha256_file(path), width, height, utcnow()),
        )
        register_artifact(
            conn, artifact_id, "REF", struct_norm=None,
            summary=(f"CODEBRIM benchmark image {path.name} — public reference corpus, "
                     "NOT imagery of any structure in this inventory"),
            source_path=str(path), source_locator="whole image",
        )
        registered += 1
        if index % 200 == 0:
            conn.commit()
            log(f"  [progress] {index:,}/{len(images):,}")
    conn.commit()
    log(f"  [done] {registered:,} reference images registered")
    return {"corpus": CORPUS, "images": registered, "root": str(directory)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the CODEBRIM reference corpus.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--raw-root", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    root = Path(args.raw_root) if args.raw_root else None
    # Confirm the corpus is present before creating anything under data/derived.
    try:
        find_images(root)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        ingest(conn, root=root, limit=args.limit)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

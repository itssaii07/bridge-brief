"""Shared machinery for reference-corpus imagery.

A reference corpus is public annotated defect imagery used for exactly one
thing: measuring how well the detector detects. It is **never** evidence about a
named bridge.

Invariant 6 is enforced structurally rather than by convention. Every row written
through here carries ``provenance = 'reference_corpus'`` and ``struct_norm =
NULL``, and the ``images`` table makes ``provenance`` NOT NULL with a CHECK
constraint. A corpus image therefore cannot be attached to a structure — there is
no column to put the structure in.

This module holds what every corpus shares: stable item IDs, archive-junk
filtering, discovery, and registration. A specific corpus adds only a
:class:`CorpusSpec` — its directory, how it words its own provenance note, and
anything unusual about its layout. ``codebrim.py`` and ``dacl10k.py`` are both
thin wrappers over this.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import ids
from ..db import DataUnavailable, RAW_ROOT, connect, utcnow
from ..store import register_artifact, sha256_file
from .uploads import IMAGE_SUFFIXES, _image_size

#: Directories that archives carry but that hold no real content.
JUNK_DIRS = frozenset({"__macosx", ".git", ".ipynb_checkpoints"})


def is_archive_junk(path: Path) -> bool:
    """True for files an archiver added that are not real content.

    macOS zips carry a ``__MACOSX`` tree of AppleDouble stubs named ``._original``.
    Those stubs keep the original file's extension, so a suffix test alone reads
    ``__MACOSX/dataset/._DSC_0042.jpg`` as a JPEG. It is not one: it is a few
    hundred bytes of resource fork, and the detector would fail to decode it.

    Filtering them at discovery is right rather than tolerant: they were never
    evidence, so counting them as unreadable images would overstate how much of
    the corpus we failed on.
    """
    if any(part.lower() in JUNK_DIRS for part in path.parts):
        return True
    return path.name.startswith("._") or path.name == ".DS_Store"


def corpus_item_id(path: Path, root: Path) -> str:
    """Stable corpus item ID from the image's path relative to the corpus root.

    Derived from the path, so it is the same on every run and on every machine
    that has the same corpus — never from enumeration order.
    """
    relative = path.relative_to(root).with_suffix("")
    return "_".join(relative.parts)


@dataclass(frozen=True)
class CorpusSpec:
    """Everything that distinguishes one reference corpus from another."""

    #: Short slug. Becomes part of every artifact ID: ``REF-{name}-{item}``.
    name: str
    #: Human label for logs and provenance notes.
    label: str
    #: Where to find it, and what to say when it is absent.
    download_hint: str
    #: Subdirectories under the corpus root that hold images. Empty means
    #: "search the whole tree", which is what a flat corpus needs.
    image_subdirs: tuple[str, ...] = ()

    def root(self, raw_root: Path | None = None) -> Path:
        return (raw_root or RAW_ROOT) / self.name

    def summary(self, path: Path) -> str:
        return (f"{self.label} benchmark image {path.name} — public reference corpus, "
                "NOT imagery of any structure in this inventory")


def find_images(spec: CorpusSpec, root: Path | None = None) -> tuple[Path, list[Path]]:
    """Locate a corpus's images. Raises rather than returning an empty list."""
    directory = spec.root(root)
    if not directory.exists():
        raise DataUnavailable(
            f"No {spec.label} directory at {directory}.\n{spec.download_hint}\n"
            "Nothing is downloaded automatically."
        )

    searched = [directory / sub for sub in spec.image_subdirs] or [directory]
    present = [path for path in searched if path.exists()]
    if not present:
        raise DataUnavailable(
            f"{directory} exists but none of its expected image directories do "
            f"({', '.join(spec.image_subdirs)}).\n"
            "The archive may have unpacked with a different layout than assumed."
        )

    images = sorted(
        path
        for base in present
        for path in base.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        and not is_archive_junk(path)
    )
    if not images:
        raise DataUnavailable(
            f"{directory} exists but contains no image files "
            f"({', '.join(sorted(IMAGE_SUFFIXES))})."
        )
    return directory, images


def register(conn, spec: CorpusSpec, *, root: Path | None = None,
             limit: int | None = None, log=print, find=None) -> dict:
    """Register a corpus as reference imagery. Originals are not copied or touched.

    ``find`` overrides discovery for a corpus whose layout needs more than a
    subdirectory list — it takes the raw root and returns ``(base, images)``,
    where item IDs are taken relative to ``base``.
    """
    directory, images = (find or (lambda r: find_images(spec, r)))(root)
    if limit is not None:
        images = images[:limit]
    log(f"{spec.label}: registering {len(images):,} image(s) from {directory}")

    registered = 0
    for index, path in enumerate(images, start=1):
        artifact_id = ids.ref_id(spec.name, corpus_item_id(path, directory))
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
            (artifact_id, spec.name, str(path), sha256_file(path), width, height, utcnow()),
        )
        register_artifact(
            conn, artifact_id, "REF", struct_norm=None,
            summary=spec.summary(path),
            source_path=str(path), source_locator="whole image",
        )
        registered += 1
        if index % 200 == 0:
            conn.commit()
            log(f"  [progress] {index:,}/{len(images):,}")
    conn.commit()
    log(f"  [done] {registered:,} reference images registered")
    return {"corpus": spec.name, "images": registered, "root": str(directory)}


def cli(spec: CorpusSpec, argv: list[str] | None = None, *, find=None) -> int:
    """Shared command line for a corpus ingest module."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description=f"Register the {spec.label} reference corpus.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--raw-root", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    root = Path(args.raw_root) if args.raw_root else None
    discover = find or (lambda r: find_images(spec, r))
    # Confirm the corpus is present before creating anything under data/derived.
    try:
        discover(root)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        register(conn, spec, root=root, limit=args.limit, find=discover)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0

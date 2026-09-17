"""Ingest of the dacl10k reference corpus.

dacl10k is public annotated imagery from real bridge inspections, used here for
exactly one thing: measuring how well the detector detects. Like any reference
corpus it is **never** evidence about a named bridge — :mod:`src.ingest.corpus`
enforces that structurally.

**Why this corpus and not CODEBRIM.** `CLAUDE.md` specifies CODEBRIM, but the
published CODEBRIM archive is a classic ZIP written past the 4 GiB limit without
ZIP64 records, so its stored offsets have wrapped and roughly half its images
cannot be extracted (ASSUMPTIONS.md H6). dacl10k is the substitute: same domain
(real bridge inspections), overlapping damage vocabulary, and its archive was
verified sound before download with ``scripts/check_dataset_archive.py``. This is
a deviation from the specified dataset and is recorded as one in ASSUMPTIONS.md
H6b, with its licence.

**Licence: CC BY-NC 4.0.** Non-commercial, attribution required. Nothing from it
is ever committed; ``data/`` is gitignored in full. Cite Flotzinger, Rösch and
Braml, "dacl10k: Benchmark for Semantic Bridge Damage Segmentation", WACV 2024
(arXiv:2309.00460).

**Only the annotated splits are registered.** The archive also carries ``testdev``
and ``testchallenge`` images, which have no public annotations — they are the
challenge's held-out sets. Registering them would add 2,010 images that the
benchmark could only skip, and a skipped image is indistinguishable in a report
from one the detector failed on.
"""

from __future__ import annotations

from pathlib import Path

from ..db import DataUnavailable
from .corpus import CorpusSpec, cli, corpus_item_id, is_archive_junk, register
from .uploads import IMAGE_SUFFIXES

CORPUS = "dacl10k"

#: Splits that ship with annotations. testdev/testchallenge do not.
ANNOTATED_SPLITS = ("train", "validation")

SPEC = CorpusSpec(
    name=CORPUS,
    label="dacl10k",
    download_hint=(
        "Download dacl10k_v2_devphase.zip and unpack it there:\n"
        "  https://dacl10k.s3.eu-central-1.amazonaws.com/dacl10k-challenge/"
        "dacl10k_v2_devphase.zip\n"
        "Check it first with `python -m scripts.check_dataset_archive <url>` — that "
        "reads the archive index over HTTP and costs a few MB instead of 4.76 GiB.\n"
        "Licence CC BY-NC 4.0; see src/ingest/dacl10k.py for the citation."
    ),
)

__all__ = ["CORPUS", "SPEC", "ANNOTATED_SPLITS", "dataset_base", "find_images",
           "ingest", "main"]


def dataset_base(root: Path) -> Path:
    """Find the directory holding ``images/`` and ``annotations/``.

    The archive unpacks into a ``dacl10k_v2_devphase/`` top-level directory, but
    operators unpack with and without that wrapper, so look for the real base
    rather than assume a depth. Checked to one level down, which covers both.
    """
    candidates = [root, *sorted(p for p in root.iterdir() if p.is_dir())]
    for candidate in candidates:
        if (candidate / "images").is_dir() and (candidate / "annotations").is_dir():
            return candidate
    raise DataUnavailable(
        f"{root} has no images/ + annotations/ pair, at its top level or one below.\n"
        "Expected the layout dacl10k_v2_devphase.zip unpacks to:\n"
        "  <root>/images/{train,validation}/*.jpg\n"
        "  <root>/annotations/{train,validation}/*.json"
    )


def find_images(root: Path | None = None) -> tuple[Path, list[Path]]:
    """Locate dacl10k images from the annotated splits only.

    Returns ``(base, images)`` where ``base`` is the directory that holds both
    ``images/`` and ``annotations/`` — corpus item IDs are relative to it, so an
    ID stays the same whether or not the operator kept the archive's wrapper
    directory.
    """
    directory = SPEC.root(root)
    if not directory.exists():
        raise DataUnavailable(
            f"No dacl10k directory at {directory}.\n{SPEC.download_hint}"
        )
    base = dataset_base(directory)

    present = [base / "images" / split for split in ANNOTATED_SPLITS]
    missing = [p for p in present if not p.is_dir()]
    if missing:
        raise DataUnavailable(
            f"{base} is missing annotated image split(s): "
            f"{', '.join(str(p.relative_to(base)) for p in missing)}.\n"
            "The devphase archive carries train/ and validation/; the testdev and "
            "testchallenge splits have no public annotations and are not used."
        )

    images = sorted(
        path
        for split_dir in present
        for path in split_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        and not is_archive_junk(path)
    )
    if not images:
        raise DataUnavailable(
            f"{base}/images/{{{','.join(ANNOTATED_SPLITS)}}} contain no image files "
            f"({', '.join(sorted(IMAGE_SUFFIXES))})."
        )
    return base, images


def ingest(conn, *, root: Path | None = None, limit: int | None = None, log=print) -> dict:
    """Register the annotated splits as reference imagery. Originals are untouched."""
    return register(conn, SPEC, root=root, limit=limit, log=log, find=find_images)


def main(argv: list[str] | None = None) -> int:
    return cli(SPEC, argv, find=lambda root: find_images(root))


if __name__ == "__main__":
    raise SystemExit(main())

"""Ingest of the CODEBRIM reference corpus.

CODEBRIM is a public annotated defect-imagery benchmark. It is used here for
exactly one thing: measuring how well the detector detects. It is **never**
evidence about a named bridge. The mechanics live in
:mod:`src.ingest.corpus`, which enforces that structurally.

**This corpus is currently unusable as published** — the archive is a classic ZIP
written past the 4 GiB limit without ZIP64 records, so its stored offsets have
wrapped and roughly half its images cannot be extracted. See ASSUMPTIONS.md H6,
and :mod:`src.ingest.dacl10k` for the substitute actually in use.

Annotation parsing is confined to
``src/eval/codebrim_benchmark.py :: parse_annotation_file`` so that the format
assumption (ASSUMPTIONS.md H4/H7) has one change point.
"""

from __future__ import annotations

from pathlib import Path

from .corpus import CorpusSpec, cli, corpus_item_id, find_images as _find, is_archive_junk
from .corpus import register

CORPUS = "codebrim"

SPEC = CorpusSpec(
    name=CORPUS,
    label="CODEBRIM",
    download_hint=(
        "Download the CODEBRIM original images and annotations (see README.md) and "
        "unpack them there. Note that the published archive is malformed: check it "
        "first with `python -m scripts.check_dataset_archive <path>`, and recover it "
        "with `7z x` or `zip -FF` if needed (ASSUMPTIONS.md H6)."
    ),
)

# Re-exported so existing callers and tests keep working unchanged.
__all__ = ["CORPUS", "SPEC", "corpus_item_id", "is_archive_junk", "find_images",
           "ingest", "main"]


def find_images(root: Path | None = None) -> tuple[Path, list[Path]]:
    """Locate CODEBRIM images under ``data/raw/codebrim/``."""
    return _find(SPEC, root)


def ingest(conn, *, root: Path | None = None, limit: int | None = None, log=print) -> dict:
    """Register the corpus as reference imagery. Originals are not copied or touched."""
    return register(conn, SPEC, root=root, limit=limit, log=log)


def main(argv: list[str] | None = None) -> int:
    return cli(SPEC, argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""SQLite access for the working asset index (``data/derived/assets.sqlite``).

The database is derived data. It can always be rebuilt from the immutable
originals under ``data/raw/``; nothing here ever writes outside ``data/derived/``.

Schema changes go in ``src/schema.sql`` first and then into a numbered migration
under ``src/migrations/``. ``connect()`` applies the schema to a fresh file and
runs any pending migration, but never alters an existing table ad hoc.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

#: Current schema version. Bump together with a new migration file.
SCHEMA_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("BRIDGE_BRIEF_DATA", REPO_ROOT / "data"))
RAW_ROOT = DATA_ROOT / "raw"
UPLOAD_ROOT = DATA_ROOT / "uploads"
DERIVED_ROOT = DATA_ROOT / "derived"
DEFAULT_DB_PATH = DERIVED_ROOT / "assets.sqlite"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class DataUnavailable(RuntimeError):
    """A required input is not on disk.

    Raised instead of failing with a stack trace or, worse, continuing with a
    default value. The message always names the path we looked for and what the
    operator should do about it.
    """


def utcnow() -> str:
    """Timestamp for audit columns, ISO-8601 UTC to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | os.PathLike[str] | None = None, *, create: bool = True) -> sqlite3.Connection:
    """Open the asset index, creating and migrating it if needed.

    Args:
        db_path: database file, or ``":memory:"``. Defaults to
            ``data/derived/assets.sqlite``.
        create: when False, refuse to create a missing database and raise
            :class:`DataUnavailable` instead. Read-only callers (the UI, the
            eval harness) pass False so that "no database yet" is reported
            honestly rather than silently producing an empty one.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    in_memory = str(path) == ":memory:"

    if not in_memory and not path.exists():
        if not create:
            raise DataUnavailable(
                f"No asset index at {path}.\n"
                "Nothing has been ingested yet. Place the source files under "
                f"{RAW_ROOT} and run:  python -m src.ingest.nbi --year 2023"
            )
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL" if not in_memory else "PRAGMA journal_mode = MEMORY")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the base schema to a fresh database, then any pending migrations."""
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if existing is None:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow()),
        )
        conn.commit()
        return
    _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run numbered migrations newer than the database's recorded version.

    Migration files are named ``NNN_description.sql`` and are applied in order.
    A database ahead of this code is an error, not something to work around.
    """
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this code expects "
            f"({SCHEMA_VERSION}). Update the code rather than downgrading the database."
        )
    if current == SCHEMA_VERSION or not MIGRATIONS_DIR.exists():
        return
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        try:
            version = int(sql_file.name.split("_", 1)[0])
        except ValueError:
            continue
        if version <= current:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, utcnow()),
        )
        conn.commit()


def require_path(path: Path, *, what: str, hint: str) -> Path:
    """Return ``path`` if it exists, otherwise raise a clear, actionable error."""
    if not path.exists():
        raise DataUnavailable(f"{what} not found at {path}.\n{hint}")
    return path

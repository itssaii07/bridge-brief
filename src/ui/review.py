"""Human review actions and the sign-off trail (invariant 4).

Append-only. Nothing in this module updates or deletes a review row: the current
state of a finding is its most recent action, and the history stays intact. That
is what makes the trail an audit trail rather than a status field.

A brief becomes ``signed_off`` only through :func:`sign_off`, which requires a
named reviewer. No analysis, generation or ingest code path can reach it. There
is deliberately no function anywhere in this project that marks a *structure* as
safe, cleared or scheduled — only a brief as reviewed.
"""

from __future__ import annotations

import sqlite3

from ..db import DataUnavailable, utcnow

ACTIONS = ("approve", "reject", "edit", "comment")


class ReviewError(ValueError):
    """The requested review action cannot be recorded, and why."""


def record_action(conn: sqlite3.Connection, *, brief_id: str, action: str, reviewer: str,
                  finding_id: str | None = None, sentence_id: str | None = None,
                  note: str | None = None, text_after: str | None = None) -> int:
    """Record one review action. Returns its row id.

    An ``edit`` also rewrites the sentence text in place — the reviewer's wording
    is what the brief should say — but the original is preserved in
    ``text_before`` on this row, which is what the correction-effort metric reads.
    """
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ReviewError("a reviewer name is required; review actions are not anonymous")
    if action not in ACTIONS:
        raise ReviewError(f"unknown action {action!r}; expected one of {ACTIONS}")

    brief = conn.execute("SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)).fetchone()
    if brief is None:
        raise ReviewError(f"no brief with id {brief_id}")

    text_before = None
    if sentence_id:
        row = conn.execute("SELECT text, brief_id FROM brief_sentences WHERE sentence_id = ?",
                           (sentence_id,)).fetchone()
        if row is None:
            raise ReviewError(f"no sentence with id {sentence_id}")
        if row["brief_id"] != brief_id:
            raise ReviewError(f"sentence {sentence_id} does not belong to brief {brief_id}")
        text_before = row["text"]

    if action == "edit":
        if not sentence_id:
            raise ReviewError("an edit must name the sentence it edits")
        if text_after is None or not text_after.strip():
            raise ReviewError("an edit must supply replacement text")
        conn.execute("UPDATE brief_sentences SET text = ? WHERE sentence_id = ?",
                     (text_after.strip(), sentence_id))

    cursor = conn.execute(
        """
        INSERT INTO review_actions
            (brief_id, finding_id, sentence_id, action, reviewer, note,
             text_before, text_after, acted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (brief_id, finding_id, sentence_id, action, reviewer, note,
         text_before, text_after, utcnow()),
    )
    # A brief under review is no longer a untouched draft.
    if brief["status"] == "draft":
        conn.execute("UPDATE briefs SET status = 'in_review' WHERE brief_id = ?", (brief_id,))
    conn.commit()
    return int(cursor.lastrowid)


def sign_off(conn: sqlite3.Connection, *, brief_id: str, reviewer: str,
             decision: str = "signed_off", note: str | None = None) -> int:
    """Record a human sign-off (or rejection) of a brief version.

    This is the only way a brief leaves draft/in-review status. It records *who*,
    against *which version*, and *when*. It does not, and cannot, say anything
    about the structure's safety or its maintenance schedule.
    """
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ReviewError("a reviewer name is required; a sign-off is not anonymous")
    if decision not in ("signed_off", "rejected"):
        raise ReviewError(f"decision must be 'signed_off' or 'rejected', got {decision!r}")

    brief = conn.execute("SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)).fetchone()
    if brief is None:
        raise ReviewError(f"no brief with id {brief_id}")

    cursor = conn.execute(
        "INSERT INTO signoffs (brief_id, version, reviewer, decision, note, signed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (brief_id, brief["version"], reviewer, decision, note, utcnow()),
    )
    conn.execute("UPDATE briefs SET status = ? WHERE brief_id = ?", (decision, brief_id))
    conn.commit()
    return int(cursor.lastrowid)


def finding_states(conn: sqlite3.Connection, brief_id: str) -> dict[str, dict]:
    """Current review state per finding: the most recent action for each.

    Derived from the append-only log rather than stored, so the log and the state
    cannot disagree.
    """
    rows = conn.execute(
        """
        SELECT finding_id, action, reviewer, note, acted_at
          FROM review_actions
         WHERE brief_id = ? AND finding_id IS NOT NULL
         ORDER BY id
        """,
        (brief_id,),
    ).fetchall()
    states: dict[str, dict] = {}
    for row in rows:
        if row["action"] == "comment":
            continue
        states[row["finding_id"]] = {
            "action": row["action"], "reviewer": row["reviewer"],
            "note": row["note"], "acted_at": row["acted_at"],
        }
    return states


def trail(conn: sqlite3.Connection, brief_id: str) -> list[dict]:
    """The full, ordered audit trail for a brief: actions then sign-offs."""
    actions = [dict(r) | {"type": "action"} for r in conn.execute(
        "SELECT * FROM review_actions WHERE brief_id = ? ORDER BY id", (brief_id,))]
    signoffs = [dict(r) | {"type": "signoff"} for r in conn.execute(
        "SELECT * FROM signoffs WHERE brief_id = ? ORDER BY id", (brief_id,))]
    combined = actions + signoffs
    combined.sort(key=lambda r: r.get("acted_at") or r.get("signed_at") or "")
    return combined


def correction_effort(conn: sqlite3.Connection, brief_id: str | None = None) -> dict:
    """Correction effort: how much editing a reviewer had to do.

    Reported as edits and rejections per reviewed finding. A brief nobody has
    reviewed yields ``None`` rather than a flattering zero.
    """
    clause = "WHERE brief_id = ?" if brief_id else ""
    params: tuple = (brief_id,) if brief_id else ()

    counts = {
        row["action"]: row["n"]
        for row in conn.execute(
            f"SELECT action, COUNT(*) AS n FROM review_actions {clause} GROUP BY action", params
        )
    }
    finding_clause = ("WHERE brief_id = ? AND finding_id IS NOT NULL"
                      if brief_id else "WHERE finding_id IS NOT NULL")
    reviewed = conn.execute(
        f"SELECT COUNT(DISTINCT finding_id) AS n FROM review_actions {finding_clause}", params
    ).fetchone()["n"]

    return {
        "findings_reviewed": reviewed,
        "approve": counts.get("approve", 0),
        "reject": counts.get("reject", 0),
        "edit": counts.get("edit", 0),
        "comment": counts.get("comment", 0),
        "edits_per_finding": round(counts.get("edit", 0) / reviewed, 4) if reviewed else None,
        "rejections_per_finding": round(counts.get("reject", 0) / reviewed, 4) if reviewed else None,
        "total_actions": sum(counts.values()),
    }


def require_brief(conn: sqlite3.Connection, brief_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)).fetchone()
    if row is None:
        raise DataUnavailable(f"no brief with id {brief_id}")
    return row

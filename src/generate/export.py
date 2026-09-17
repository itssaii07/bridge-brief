"""Publishing a brief — and refusing to, until a human has signed it off.

The specification requires human review **before publication, external sharing,
or high-stakes use**. Until now that constraint was satisfied by absence: there
was no way to publish a brief at all, so none could escape review. That is not
the same as enforcing it, and an audit that accepted it would be accepting a
missing feature as a safeguard.

This module is the publication path, and the gate on it. :func:`export_brief`
refuses any brief whose recorded status is not ``signed_off``, and the refusal
names the reviewer who would have to sign it. The gate reads
``signoffs`` — the append-only table — rather than trusting a status column
someone could set by hand.

Two things this deliberately does **not** do:

* It does not sign off on anything, and it has no ``--force``. A flag to publish
  an unreviewed brief would be exactly the hole the constraint exists to close.
* It does not clear, certify or schedule. Every exported format repeats, in its
  own text, that the brief is a draft assembled from published records and
  carries no authority beyond the named reviewer's sign-off.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..db import DataUnavailable, connect
from ..ui.review import require_brief

#: Formats the exporter can write.
FORMATS = ("markdown", "json")

#: The banner every export carries, in every format. It is not decoration: an
#: exported file outlives the UI it came from, and whoever opens it next has no
#: other way to know what it is and is not.
DISCLAIMER = (
    "This is a human-reviewed inspection brief assembled from published federal "
    "records. Every statement in it cites the source artifact it came from. It is "
    "NOT a safety clearance, NOT a maintenance order, and NOT an authorisation to "
    "act. It records what two official sources say and where they disagree; the "
    "engineering judgement remains with the named reviewer and the responsible "
    "authority."
)


class ReviewRequired(PermissionError):
    """Raised when a brief that has not been signed off is asked to be published.

    A distinct type rather than a generic error, so a caller cannot conflate
    "not reviewed yet" with "no such brief" and retry its way past the gate.
    """


def signoff_record(conn, brief_id: str) -> dict | None:
    """The most recent sign-off decision for a brief, or None.

    Read from ``signoffs``, which is append-only, rather than from
    ``briefs.status``. The status column is a cache of this; the trail is the
    authority, and the gate should ask the authority.
    """
    row = conn.execute(
        """
        SELECT reviewer, decision, note, signed_at, version
          FROM signoffs WHERE brief_id = ?
         ORDER BY id DESC LIMIT 1
        """,
        (brief_id,),
    ).fetchone()
    return dict(row) if row else None


def assert_publishable(conn, brief_id: str) -> dict:
    """Return the sign-off record, or refuse to publish.

    Raises:
        DataUnavailable: no such brief.
        ReviewRequired: the brief exists but no human has signed it off, or a
            human signed it off and then a newer version was generated, or the
            human rejected it.
    """
    brief = require_brief(conn, brief_id)
    record = signoff_record(conn, brief_id)

    if record is None:
        raise ReviewRequired(
            f"{brief_id} has not been signed off, so it cannot be published, shared "
            f"externally, or used for any high-stakes purpose.\n"
            f"Its status is '{brief['status']}'.\n"
            "A named human must review it first:\n"
            "  python -m src.ui.server     # then open http://127.0.0.1:8765/queue\n"
            "There is no flag to bypass this."
        )
    if record["decision"] != "signed_off":
        raise ReviewRequired(
            f"{brief_id} was reviewed by {record['reviewer']} on "
            f"{record['signed_at']} and the decision was "
            f"'{record['decision']}', not 'signed_off'. It must not be published.\n"
            f"Reviewer's note: {record['note'] or '(none)'}"
        )
    # A sign-off applies to the version that was in front of the reviewer. A
    # later regeneration is a different document (ASSUMPTIONS.md L2).
    if record["version"] != brief["version"]:
        raise ReviewRequired(
            f"{brief_id} was signed off at version {record['version']} but is now "
            f"version {brief['version']}. The reviewer approved a different "
            "document; the current one needs its own review."
        )
    return record


def brief_payload(conn, brief_id: str) -> dict:
    """Everything an export needs, gathered once."""
    brief = dict(require_brief(conn, brief_id))
    sentences = [dict(r) for r in conn.execute(
        """
        SELECT s.sentence_id, s.ordinal, s.section, s.text, s.evidence_tier,
               s.confidence, s.finding_id
          FROM brief_sentences s WHERE s.brief_id = ? ORDER BY s.ordinal
        """, (brief_id,))]
    for sentence in sentences:
        sentence["citations"] = [r[0] for r in conn.execute(
            "SELECT artifact_id FROM sentence_citations WHERE sentence_id = ? ORDER BY 1",
            (sentence["sentence_id"],))]
    citations = sorted({c for s in sentences for c in s["citations"]})
    artifacts = []
    for artifact_id in citations:
        row = conn.execute(
            "SELECT artifact_id, kind, summary, source_path, source_locator "
            "FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row:
            artifacts.append(dict(row))
    blocked = [dict(r) for r in conn.execute(
        "SELECT reason, text FROM blocked_sentences WHERE brief_id = ? ORDER BY id",
        (brief_id,))]
    return {"brief": brief, "sentences": sentences, "artifacts": artifacts,
            "blocked": blocked}


def render_markdown(payload: dict, signoff: dict) -> str:
    """A brief as a shareable document, with its evidence and its sign-off."""
    brief = payload["brief"]
    total = brief["total_sentences"] or 0
    blocked = brief["blocked_unsupported"] or 0
    rate = (blocked / total) if total else 0.0

    out = [
        f"# Inspection brief — structure {brief['struct_norm']}, {brief['year']}",
        "",
        f"> {DISCLAIMER}",
        "",
        "## Sign-off",
        "",
        f"- **Reviewer:** {signoff['reviewer']}",
        f"- **Decision:** {signoff['decision']}",
        f"- **Signed at:** {signoff['signed_at']}",
        f"- **Reviewer's note:** {signoff['note'] or '(none)'}",
        "",
        "## Provenance",
        "",
        f"- Brief: `{brief['brief_id']}` version {brief['version']}",
        f"- Generated: {brief['generated_at']} by `{brief['generator']}`",
        f"- Sentences generated: {total}",
        f"- Rendered: {len(payload['sentences'])}",
        f"- **Dropped for having no supporting artifact (`blocked_unsupported`): "
        f"{blocked}** ({rate:.1%} of generated)",
        "",
    ]
    if payload["blocked"]:
        out += ["Statements the grounding gate refused to publish, and why:", ""]
        out += [f"- _{b['reason']}_: {b['text'][:200]}" for b in payload["blocked"]]
        out.append("")

    section = None
    for sentence in payload["sentences"]:
        if sentence["section"] != section:
            section = sentence["section"]
            out += [f"## {str(section).replace('_', ' ').title()}", ""]
        confidence = ("" if sentence["confidence"] is None
                      else f", confidence {sentence['confidence']:.2f}")
        out.append(f"- [{sentence['evidence_tier'] or 'n/a'}{confidence}] "
                   f"{sentence['text']}")
    out.append("")

    out += ["## Evidence", "",
            "Every statement above cites one or more of these. Each resolves to the "
            "original published record, which is unmodified and still on disk.", "",
            "| artifact | kind | summary | source |", "|---|---|---|---|"]
    for artifact in payload["artifacts"]:
        locator = artifact["source_locator"] or ""
        out.append(f"| `{artifact['artifact_id']}` | {artifact['kind']} | "
                   f"{artifact['summary']} | `{artifact['source_path']}` {locator} |")
    out.append("")
    return "\n".join(out)


def export_brief(conn, brief_id: str, out_path: Path | str, *,
                 fmt: str = "markdown") -> dict:
    """Publish a signed-off brief. Refuses anything else.

    Raises:
        ReviewRequired: the brief has not been signed off by a named human.
        ValueError: unknown format.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    signoff = assert_publishable(conn, brief_id)
    payload = brief_payload(conn, brief_id)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "markdown":
        body = render_markdown(payload, signoff)
    else:
        body = json.dumps({"disclaimer": DISCLAIMER, "signoff": signoff, **payload},
                          indent=2, sort_keys=True)
    out_path.write_text(body, encoding="utf-8")
    return {"brief_id": brief_id, "format": fmt, "path": str(out_path),
            "bytes": len(body), "reviewer": signoff["reviewer"],
            "sentences": len(payload["sentences"]),
            "blocked_unsupported": payload["brief"]["blocked_unsupported"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a signed-off inspection brief. Refuses unreviewed briefs.")
    parser.add_argument("--brief", required=True, help="brief id, e.g. BRIEF-AL012757-2023-v1")
    parser.add_argument("--out", required=True, help="file to write")
    parser.add_argument("--format", default="markdown", choices=FORMATS)
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    try:
        conn = connect(args.db, create=False)
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        result = export_brief(conn, args.brief, args.out, fmt=args.format)
    except ReviewRequired as exc:
        # Exit 3, distinct from "missing data", so a script can tell "needs a
        # human" apart from "nothing to do".
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    except DataUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    finally:
        conn.close()
    print(f"exported {result['brief_id']} ({result['sentences']} sentence(s), "
          f"signed off by {result['reviewer']}) -> {result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

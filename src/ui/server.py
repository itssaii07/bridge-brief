"""The review UI server.

A small ``http.server`` application: structure list, brief view with
click-to-highlight evidence, per-finding approve/edit/reject, and a versioned
sign-off trail. No framework, no build step, no network dependencies.

It binds to 127.0.0.1 by default and has no authentication — it is a
single-operator research tool, and the reviewer name recorded on every action is
typed, not authenticated (ASSUMPTIONS.md I1).

**It renders correctly against an empty database.** That is not incidental: the
data is not on disk, so the empty state is the state this UI will actually be
run in first, and it says honestly that nothing is loaded and what to run.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from ..db import DataUnavailable, connect
from . import review, views
from .review import (
    ReviewError, finding_states, record_action, require_brief, sign_off, trail,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_structures(conn, limit: int = 500) -> tuple[list[dict], dict]:
    """Structures that have something to review, most contradictions first.

    Driven by the tables that have rows, not by ``structures``. The obvious
    phrasing — select from ``structures`` with a correlated subquery per counter
    — is O(structures) and collapsed on the real corpus: SQLite preferred
    ``idx_findings_kind`` over ``idx_findings_struct``, so it rescanned all 7,018
    contradiction rows for each of 632,140 structures, about 4.4 billion row
    visits, and the page never loaded. Aggregating the small tables first and
    joining ``structures`` by primary key is O(findings) and returns immediately.
    """
    rows = conn.execute(
        """
        WITH finding_counts AS (
            SELECT struct_norm,
                   SUM(CASE WHEN kind = 'contradiction' THEN 1 ELSE 0 END) AS contradictions,
                   SUM(CASE WHEN evidence_tier = 'single_source' THEN 1 ELSE 0 END) AS single_source
              FROM findings
             GROUP BY struct_norm
        ),
        photo_counts AS (
            SELECT struct_norm, COUNT(*) AS photos
              FROM images
             WHERE struct_norm IS NOT NULL
             GROUP BY struct_norm
        ),
        brief_counts AS (
            SELECT struct_norm, COUNT(*) AS briefs FROM briefs GROUP BY struct_norm
        ),
        candidates AS (
            SELECT struct_norm FROM finding_counts
            UNION
            SELECT struct_norm FROM photo_counts
            UNION
            SELECT struct_norm FROM brief_counts
        )
        SELECT c.struct_norm,
               s.state_abbr,
               COALESCE(f.contradictions, 0) AS contradictions,
               COALESCE(f.single_source, 0)  AS single_source,
               COALESCE(p.photos, 0)         AS photos,
               COALESCE(b.briefs, 0)         AS briefs
          FROM candidates c
          LEFT JOIN structures     s ON s.struct_norm = c.struct_norm
          LEFT JOIN finding_counts f ON f.struct_norm = c.struct_norm
          LEFT JOIN photo_counts   p ON p.struct_norm = c.struct_norm
          LEFT JOIN brief_counts   b ON b.struct_norm = c.struct_norm
         ORDER BY contradictions DESC, c.struct_norm
         LIMIT ?
        """,
        (limit,),
    ).fetchall()

    totals = {
        "structures": conn.execute("SELECT COUNT(*) FROM structures").fetchone()[0],
        "contradictions": conn.execute(
            "SELECT COUNT(*) FROM findings WHERE kind = 'contradiction'").fetchone()[0],
        "briefs": conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0],
        "blocked_unsupported": conn.execute(
            "SELECT COALESCE(SUM(blocked_unsupported), 0) FROM briefs").fetchone()[0],
        "signoffs": conn.execute("SELECT COUNT(*) FROM signoffs").fetchone()[0],
    }
    return [dict(r) for r in rows], totals


def structure_payload(conn, struct_norm: str) -> dict | None:
    structure = conn.execute(
        "SELECT * FROM structures WHERE struct_norm = ?", (struct_norm,)).fetchone()
    if structure is None:
        return None
    findings = [dict(r) for r in conn.execute(
        "SELECT * FROM findings WHERE struct_norm = ? ORDER BY year DESC, "
        "severity DESC NULLS LAST, component", (struct_norm,))]
    briefs = [dict(r) for r in conn.execute(
        "SELECT * FROM briefs WHERE struct_norm = ? ORDER BY year DESC, version DESC",
        (struct_norm,))]
    images = [dict(r) for r in conn.execute(
        "SELECT * FROM images WHERE struct_norm = ? ORDER BY artifact_id", (struct_norm,))]
    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT year FROM findings WHERE struct_norm = ? ORDER BY year DESC",
        (struct_norm,))]
    return {"structure": dict(structure), "findings": findings, "briefs": briefs,
            "images": images, "years": years}


def brief_payload(conn, brief_id: str) -> dict:
    brief = dict(require_brief(conn, brief_id))
    sentences = []
    for row in conn.execute(
        "SELECT * FROM brief_sentences WHERE brief_id = ? ORDER BY ordinal", (brief_id,)
    ):
        citations = [r[0] for r in conn.execute(
            "SELECT artifact_id FROM sentence_citations WHERE sentence_id = ? ORDER BY artifact_id",
            (row["sentence_id"],))]
        sentences.append(dict(row) | {"citations": citations})

    artifacts = [dict(r) for r in conn.execute(
        """
        SELECT a.* FROM artifacts a
         WHERE a.artifact_id IN (
            SELECT c.artifact_id FROM sentence_citations c
              JOIN brief_sentences s ON s.sentence_id = c.sentence_id
             WHERE s.brief_id = ?)
         ORDER BY a.kind, a.artifact_id
        """, (brief_id,))]
    blocked = [dict(r) for r in conn.execute(
        "SELECT * FROM blocked_sentences WHERE brief_id = ? ORDER BY id", (brief_id,))]
    images = [dict(r) for r in conn.execute(
        "SELECT * FROM images WHERE struct_norm = ? ORDER BY artifact_id",
        (brief["struct_norm"],))]

    return {"brief": brief, "sentences": sentences, "artifacts": artifacts,
            "blocked": blocked, "states": finding_states(conn, brief_id),
            "trail": trail(conn, brief_id), "images": images}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "bridge-brief/0.1"
    db_path: str | None = None

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  [ui] {self.address_string()} {fmt % args}\n")

    # -- helpers --------------------------------------------------------
    def _send(self, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, message: str, status: int = 400):
        self._send(views.page("Error", f'<div class="panel"><h2>Error</h2>'
                                       f'<div class="empty">{views.E(message)}</div></div>'),
                   status=status)

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _conn(self):
        # Opened per request: the UI is read-mostly and this keeps the handler
        # free of cross-thread SQLite connection sharing.
        return connect(self.db_path)

    # -- routes ---------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            conn = self._conn()
        except DataUnavailable as exc:
            self._error(str(exc), status=503)
            return
        try:
            if path == "/":
                rows, totals = list_structures(conn)
                self._send(views.structure_list(
                    rows, totals, pending=review.queue_totals(conn)["awaiting review"]))
            elif path == "/queue":
                # The human sign-off queue, required by the problem statement.
                include_decided = parse_qs(urlparse(self.path).query).get("all", ["0"])[0] == "1"
                self._send(views.signoff_queue_view(
                    review.signoff_queue(conn, include_decided=include_decided),
                    review.queue_totals(conn),
                    include_decided=include_decided))
            elif path.startswith("/structure/"):
                struct = unquote(path.split("/", 2)[2]).strip("/")
                payload = structure_payload(conn, struct)
                if payload is None:
                    self._error(f"No structure {struct} in the index.", status=404)
                    return
                self._send(views.structure_view(
                    payload["structure"], payload["findings"], payload["briefs"],
                    payload["images"], payload["years"]))
            elif path.startswith("/brief/"):
                brief_id = unquote(path.split("/", 2)[2]).strip("/")
                payload = brief_payload(conn, brief_id)
                self._send(views.brief_view(
                    payload["brief"], payload["sentences"], payload["artifacts"],
                    payload["blocked"], payload["states"], payload["trail"], payload["images"]))
            elif path.startswith("/api/artifact/"):
                artifact_id = unquote(path.split("/", 3)[3])
                row = conn.execute("SELECT * FROM artifacts WHERE artifact_id = ?",
                                   (artifact_id,)).fetchone()
                if row is None:
                    self._send(json.dumps({"error": "does not resolve",
                                           "artifact_id": artifact_id}),
                               status=404, content_type="application/json")
                else:
                    self._send(json.dumps(dict(row), indent=2),
                               content_type="application/json")
            else:
                self._error("Not found.", status=404)
        except DataUnavailable as exc:
            self._error(str(exc), status=404)
        except Exception:
            traceback.print_exc()
            self._error("Internal error; see the server log.", status=500)
        finally:
            conn.close()

    def do_POST(self):
        path = urlparse(self.path).path
        form = self._form()
        try:
            conn = self._conn()
        except DataUnavailable as exc:
            self._error(str(exc), status=503)
            return
        try:
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "brief" and parts[2] == "action":
                brief_id = unquote(parts[1])
                record_action(
                    conn, brief_id=brief_id, action=form.get("action", ""),
                    reviewer=form.get("reviewer", ""),
                    finding_id=form.get("finding_id") or None,
                    sentence_id=form.get("sentence_id") or None,
                    note=form.get("note") or None,
                    text_after=form.get("text_after") or None,
                )
                self._redirect(f"/brief/{brief_id}")
            elif len(parts) == 3 and parts[0] == "brief" and parts[2] == "signoff":
                brief_id = unquote(parts[1])
                sign_off(conn, brief_id=brief_id, reviewer=form.get("reviewer", ""),
                         decision=form.get("decision", "signed_off"),
                         note=form.get("note") or None)
                self._redirect(f"/brief/{brief_id}")
            elif len(parts) == 3 and parts[0] == "structure" and parts[2] == "generate":
                from ..generate.brief import generate

                struct = unquote(parts[1])
                year = int(form.get("year") or 2023)
                summary = generate(conn, struct, year, log=lambda *a: None)
                self._redirect(f"/brief/{summary['brief_id']}")
            else:
                self._error("Not found.", status=404)
        except (ReviewError, ValueError) as exc:
            self._error(str(exc), status=400)
        except DataUnavailable as exc:
            self._error(str(exc), status=409)
        except Exception:
            traceback.print_exc()
            self._error("Internal error; see the server log.", status=500)
        finally:
            conn.close()


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, db_path: str | None = None):
    handler = type("BoundHandler", (Handler,), {"db_path": db_path})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the inspection brief review UI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port, args.db)
    print(f"Review UI on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    print("Drafts only. No endpoint in this server clears a structure or authorises work.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

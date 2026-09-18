"""The web application server.

A ``http.server`` application, no framework and no build step:

* ``/``            overview, with every GAI40 deliverable and its live figure
* ``/inspect``     upload photographs of a structure and draft a brief
* ``/report/<id>`` the interactive brief: photographs with region overlays,
                   sentences traced to their sources, review and sign-off
* ``/queue``       the human sign-off queue, ordered by urgency
* ``/metrics``     the evaluation table
* ``/structures``  the record explorer

It binds to 127.0.0.1 by default and has no authentication: the reviewer name
recorded on every action is typed, not authenticated (ASSUMPTIONS.md I1). Do not
expose it.

No route clears a structure or authorises work. Publication is refused until a
named human has signed the brief off.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..db import DataUnavailable, connect
from ..generate import export
from ..store import StructureNotFound, resolve_structure_key
from . import app_data, multipart, review, views, webapp, workflow
from .review import (
    ReviewError, finding_states, record_action, require_brief, sign_off, trail,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

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
    db_path: str | None = None
    server_version = "BridgeBrief/2"

    #: Static assets this server will hand out, and their media types. Anything
    #: not listed is a 404: the static route never becomes a file browser.
    STATIC = {"app.css": "text/css; charset=utf-8",
              "app.js": "application/javascript; charset=utf-8"}

    #: Image media types, by extension, for originals and corpus images.
    IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                   ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff",
                   ".bmp": "image/bmp"}

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

    def _json(self, data, status: int = 200):
        self._send(json.dumps(data, default=str), status=status,
                   content_type="application/json; charset=utf-8")

    def _bytes(self, payload: bytes, content_type: str, *, cache: str = "no-cache",
               filename: str | None = None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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

    def _body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError(f"request body is {length:,} bytes; the limit is {limit:,}")
        return self.rfile.read(length) if length else b""

    def _form(self) -> dict[str, str]:
        raw = self._body(1 << 20).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _json_body(self) -> dict:
        raw = self._body(1 << 20)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"request body is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _conn(self):
        # Opened per request: the UI is read-mostly and this keeps the handler
        # free of cross-thread SQLite connection sharing.
        return connect(self.db_path)

    def _pending(self, conn) -> int:
        return review.queue_totals(conn)["awaiting review"]

    def _send_image_file(self, path: Path, *, cache: str):
        media_type = self.IMAGE_TYPES.get(path.suffix.lower())
        if media_type is None or not path.is_file():
            self._json({"error": "that image is no longer readable on disk"}, status=404)
            return
        self._bytes(path.read_bytes(), media_type, cache=cache)

    def _send_corpus_image(self, conn, artifact_id: str):
        """Serve one reference-corpus image. Never an inspection upload.

        The path is read out of the index rather than taken from the request, and
        the query filters on provenance, so a benchmark route can never hand out
        a photograph of a real structure even if its id is guessed.
        """
        row = conn.execute(
            "SELECT stored_path FROM images WHERE artifact_id = ? "
            "AND provenance = 'reference_corpus' AND struct_norm IS NULL",
            (artifact_id,)).fetchone()
        if row is None:
            self._error("No reference-corpus image with that id.", status=404)
            return
        self._send_image_file(Path(row["stored_path"]), cache="public, max-age=86400")

    def _send_original(self, conn, artifact_id: str):
        """Serve an inspection photograph exactly as it was uploaded.

        "Keep original sources available": these are the stored bytes, unmodified,
        resolved from the index by artifact id. Only inspection uploads are served
        here; the corpus has its own route with its own label.
        """
        row = conn.execute(
            "SELECT stored_path FROM images WHERE artifact_id = ? "
            "AND provenance = 'inspection_upload'", (artifact_id,)).fetchone()
        if row is None:
            self._json({"error": f"no inspection photograph {artifact_id}"}, status=404)
            return
        self._send_image_file(Path(row["stored_path"]), cache="private, max-age=3600")

    def _send_static(self, name: str):
        media_type = self.STATIC.get(name)
        path = STATIC_DIR / name
        if media_type is None or not path.is_file():
            self._send("not found", status=404, content_type="text/plain; charset=utf-8")
            return
        self._bytes(path.read_bytes(), media_type, cache="no-cache")

    # -- routes ---------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return
        try:
            conn = self._conn()
        except DataUnavailable as exc:
            self._error(str(exc), status=503)
            return
        try:
            if path == "/":
                self._send(webapp.home(app_data.home(conn)))
            elif path == "/inspect":
                self._send(webapp.inspect({
                    "capabilities": app_data.capabilities(),
                    "suggestions": app_data.suggestions(conn),
                    "pending": self._pending(conn),
                    "preselect": (query.get("structure") or [None])[0],
                }))
            elif path.startswith("/report/") or path.startswith("/brief/"):
                brief_id = unquote(path.split("/", 2)[2]).strip("/")
                self._send(webapp.report(app_data.report(conn, brief_id),
                                         pending=self._pending(conn)))
            elif path == "/queue":
                include_decided = (query.get("all") or ["0"])[0] == "1"
                self._send(webapp.queue(
                    review.signoff_queue(conn, include_decided=include_decided),
                    review.queue_totals(conn), include_decided=include_decided))
            elif path == "/metrics":
                rows, computed_at = metrics_rows(conn)
                self._send(webapp.metrics(rows, computed_at=computed_at,
                                          pending=self._pending(conn),
                                          running=METRICS["running"]))
            elif path in ("/structures", "/index"):
                rows, totals = list_structures(conn)
                self._send(views.structure_list(rows, totals, pending=self._pending(conn)))
            elif path.startswith("/structure/"):
                struct = unquote(path.split("/", 2)[2]).strip("/")
                payload = structure_payload(conn, struct)
                if payload is None:
                    self._error(f"No structure {struct} in the index.", status=404)
                    return
                self._send(views.structure_view(
                    payload["structure"], payload["findings"], payload["briefs"],
                    payload["images"], payload["years"]))
            elif path.startswith("/original/"):
                self._send_original(conn, unquote(path.split("/", 2)[2]))
            elif path.startswith("/corpus-image/"):
                self._send_corpus_image(conn, unquote(path.split("/", 2)[2]))

            # ---- JSON API ----
            elif path == "/api/capabilities":
                self._json(app_data.capabilities())
            elif path == "/api/structures":
                self._json({"results": app_data.search_structures(
                    conn, (query.get("q") or [""])[0])})
            elif path.startswith("/api/structure/"):
                key = unquote(path.split("/", 3)[3])
                try:
                    struct_norm = resolve_structure_key(conn, key)
                except StructureNotFound as exc:
                    self._json({"error": str(exc)}, status=404)
                    return
                self._json(app_data.structure_detail(conn, struct_norm))
            elif path == "/api/metrics/status":
                self._json({"running": METRICS["running"], "error": METRICS["error"],
                            "computed_at": METRICS["computed_at"]})
            elif path.startswith("/api/report/") and path.endswith("/export"):
                self._export(conn, unquote(path.split("/")[3]),
                             (query.get("format") or ["markdown"])[0])
            elif path.startswith("/api/report/"):
                self._json(app_data.report(conn, unquote(path.split("/")[3])))
            elif path.startswith("/api/artifact/"):
                artifact_id = unquote(path.split("/", 3)[3])
                row = conn.execute("SELECT * FROM artifacts WHERE artifact_id = ?",
                                   (artifact_id,)).fetchone()
                if row is None:
                    self._json({"error": "does not resolve", "artifact_id": artifact_id},
                               status=404)
                else:
                    self._json(dict(row))
            else:
                self._error("Not found.", status=404)
        except (DataUnavailable, ReviewError) as exc:
            if path.startswith("/api/"):
                self._json({"error": str(exc)}, status=404)
            else:
                self._error(str(exc), status=404)
        except Exception:
            traceback.print_exc()
            self._error("Internal error; see the server log.", status=500)
        finally:
            conn.close()

    def _export(self, conn, brief_id: str, fmt: str):
        """Publish a brief, or refuse. The refusal is the point of this route."""
        if fmt not in export.FORMATS:
            self._json({"error": f"unknown format {fmt!r}"}, status=400)
            return
        try:
            signoff = export.assert_publishable(conn, brief_id)
        except export.ReviewRequired as exc:
            # The exception text is written for the command line. On the page the
            # reader is one panel away from the fix, so say that instead.
            reason = str(exc).splitlines()[0]
            self._json({"error": f"Publication refused. {reason} Sign it off in the "
                                 "panel on this page first; there is no way around this.",
                        "refused": True}, status=409)
            return
        payload = export.brief_payload(conn, brief_id)
        if fmt == "markdown":
            body = export.render_markdown(payload, signoff)
            self._bytes(body.encode("utf-8"), "text/markdown; charset=utf-8",
                        filename=f"{brief_id}.md")
        else:
            body = json.dumps({"disclaimer": export.DISCLAIMER, "signoff": signoff, **payload},
                              indent=2, sort_keys=True, default=str)
            self._bytes(body.encode("utf-8"), "application/json; charset=utf-8",
                        filename=f"{brief_id}.json")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/inspect":
            self._inspect()
            return
        try:
            conn = self._conn()
        except DataUnavailable as exc:
            self._error(str(exc), status=503)
            return
        try:
            parts = [p for p in path.split("/") if p]
            # ---- JSON API ----
            if len(parts) == 4 and parts[:2] == ["api", "report"] and parts[3] in ("action", "signoff"):
                brief_id = unquote(parts[2])
                body = self._json_body()
                try:
                    if parts[3] == "action":
                        record_action(
                            conn, brief_id=brief_id, action=str(body.get("action") or ""),
                            reviewer=str(body.get("reviewer") or ""),
                            finding_id=body.get("finding_id") or None,
                            sentence_id=body.get("sentence_id") or None,
                            note=body.get("note") or None,
                            text_after=body.get("text_after") or None)
                    else:
                        sign_off(conn, brief_id=brief_id, reviewer=str(body.get("reviewer") or ""),
                                 decision=str(body.get("decision") or "signed_off"),
                                 note=body.get("note") or None)
                except (ReviewError, ValueError) as exc:
                    self._json({"error": str(exc)}, status=400)
                    return
                self._json(app_data.report(conn, brief_id))
                return
            if path == "/api/metrics/recompute":
                started = start_metrics_recompute(self.db_path)
                self._json({"started": started, "running": METRICS["running"]},
                           status=202 if started else 200)
                return

            # ---- form routes kept for the classic review page ----
            form = self._form()
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

    def _inspect(self):
        """Run one inspection and stream its progress as newline-delimited JSON.

        The response is streamed so the browser shows each stage as it really
        finishes. Input problems are reported as a single error event; nothing is
        written until every photograph has been validated.
        """
        def emit(event: dict):
            self.wfile.write((json.dumps(event, default=str) + "\n").encode("utf-8"))
            self.wfile.flush()

        try:
            fields, files = multipart.parse(self.headers.get("Content-Type"),
                                            self._body(multipart.MAX_BODY_BYTES))
        except (multipart.MultipartError, ValueError) as exc:
            self._json({"stage": "error", "status": "error", "message": str(exc)}, status=400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            conn = self._conn()
        except DataUnavailable as exc:
            emit({"stage": "error", "status": "error", "message": str(exc)})
            return
        try:
            photos = [workflow.Photo(filename=f.filename, data=f.data)
                      for f in files if f.field == "photos"]
            for event in workflow.run_inspection(
                    conn, fields.get("structure", ""), photos,
                    detector=fields.get("detector", "auto")):
                emit(event)
        except workflow.InspectionError as exc:
            emit({"stage": "error", "status": "error", "message": str(exc)})
        except Exception as exc:  # reported to the browser, logged in full here
            traceback.print_exc()
            emit({"stage": "error", "status": "error",
                  "message": f"The inspection stopped unexpectedly: {exc}"})
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Evaluation cache
# ---------------------------------------------------------------------------
#
# The full harness takes about a minute on the real corpus (corpus-wide
# aggregates and two validation passes), which is fine for a report and wrong for
# a page load. So the page renders from the last computed table, stamped with
# when it was computed, overlays the handful of metrics that are cheap to read
# live, and offers a recompute that runs in the background.

METRICS: dict = {"rows": None, "computed_at": None, "running": False, "error": None}
_METRICS_LOCK = threading.Lock()
METRICS_FILE = Path(__file__).resolve().parent.parent.parent / "reports" / "metrics.json"


def _load_metrics_file() -> tuple[list[dict] | None, str | None]:
    try:
        rows = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
        stamp = datetime.fromtimestamp(METRICS_FILE.stat().st_mtime, tz=timezone.utc)
        return (rows if isinstance(rows, list) else None,
                stamp.strftime("%Y-%m-%d %H:%M UTC"))
    except (OSError, json.JSONDecodeError):
        return None, None


def metrics_rows(conn) -> tuple[list[dict], str | None]:
    """The evaluation table for the page: cached, with live figures overlaid."""
    with _METRICS_LOCK:
        if METRICS["rows"] is None:
            METRICS["rows"], METRICS["computed_at"] = _load_metrics_file()
        rows = [dict(r) for r in (METRICS["rows"] or [])]
        computed_at = METRICS["computed_at"]
    live = app_data.metrics_summary(conn)
    overlay = {
        "source_link_resolution": live["source_link_resolution"],
        "unsupported_content_rate": live["unsupported_rate"],
        "correction_effort_edits_per_finding": live["edits_per_finding"],
    }
    names = {r["name"] for r in rows}
    for row in rows:
        if row["name"] in overlay and overlay[row["name"]] is not None:
            row["value"] = overlay[row["name"]]
            row["status"] = "ok (live)"
    for name, value in overlay.items():
        if name not in names:
            rows.append({"name": name, "value": value, "ground_truth": "live from the index",
                         "status": "ok (live)" if value is not None else "not yet measurable"})
    return rows, computed_at


def start_metrics_recompute(db_path: str | None) -> bool:
    """Run the full harness in the background. False if one is already running."""
    with _METRICS_LOCK:
        if METRICS["running"]:
            return False
        METRICS["running"], METRICS["error"] = True, None

    def work():
        conn = None
        try:
            conn = connect(db_path)
            rows = app_data.metrics_table(conn)
            METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
            METRICS_FILE.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
            with _METRICS_LOCK:
                METRICS["rows"] = rows
                METRICS["computed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception as exc:
            traceback.print_exc()
            with _METRICS_LOCK:
                METRICS["error"] = str(exc)
        finally:
            if conn is not None:
                conn.close()
            with _METRICS_LOCK:
                METRICS["running"] = False

    threading.Thread(target=work, name="metrics-recompute", daemon=True).start()
    return True


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

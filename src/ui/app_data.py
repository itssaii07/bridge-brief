"""JSON the web app renders from. Queries only; no HTML, no writes.

Every value here is read out of the asset index or a harness report. Where
something has not happened (no photographs, no element data, a metric nobody has
labelled yet) the payload says so explicitly instead of carrying a plausible
default, because the page will render whatever it is given.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..detect.vision import credentials_configured
from ..generate.export import signoff_record
from .review import correction_effort, finding_states, queue_totals, require_brief, trail

REPO = Path(__file__).resolve().parent.parent.parent
REPORTS = REPO / "reports"
REVIEWS = REPO / "reviews"

#: Evidence streams the problem statement says inspectors combine, and whether
#: this system can take each one. A stream that cannot be supplied is still
#: listed, so its absence is visible rather than assumed away.
STREAMS = (
    ("images", "Inspection photographs", "drone or hand-held images of the structure"),
    ("nbi", "Federal condition ratings", "National Bridge Inventory, one 0-9 rating per component"),
    ("nbe", "Element condition states", "National Bridge Elements, quantities in CS1-CS4"),
    ("sensor", "Sensor readings", "non-destructive evaluation such as GPR or resistivity"),
    ("notes", "Field notes", "the inspector's handwritten or typed notes"),
)


def _rows(conn, sql: str, *params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def capabilities() -> dict:
    configured, reason = credentials_configured()
    try:
        import PIL  # noqa: F401
        pillow = True
    except ImportError:
        pillow = False
    return {
        "vision": {"available": configured and pillow, "reason": reason if pillow
                   else "Pillow is not installed"},
        "baseline": {"available": pillow, "reason": "classical edge-energy detector"
                     if pillow else "Pillow is not installed"},
    }


def search_structures(conn, query: str, limit: int = 12) -> list[dict]:
    """Structures matching a key, a bare number, a road or a feature crossed.

    Contradiction counts come from stored findings so the operator can pick a
    bridge where the two federal records disagree.
    """
    text = " ".join((query or "").split())[:80]
    if len(text) < 2:
        return []
    key = "".join(ch for ch in text.upper() if ch.isalnum())
    like = f"%{text.upper()}%"
    rows = _rows(conn, """
        SELECT s.struct_norm, s.state_abbr, s.facility, s.feature_crossed, s.year_built
          FROM structures s
         WHERE s.struct_norm = ?
            OR s.struct_norm LIKE ?
            OR UPPER(s.facility) LIKE ?
            OR UPPER(s.feature_crossed) LIKE ?
         ORDER BY CASE WHEN s.struct_norm = ? THEN 0
                       WHEN s.struct_norm LIKE ? THEN 1 ELSE 2 END,
                  s.struct_norm
         LIMIT ?
    """, key, f"%{key}%", like, like, key, f"{key}%", limit)
    for row in rows:
        row.update(structure_flags(conn, row["struct_norm"]))
    return rows


def structure_flags(conn, struct_norm: str) -> dict:
    counts = conn.execute("""
        SELECT
          (SELECT MAX(year) FROM ratings WHERE struct_norm = :s)                     AS latest_year,
          (SELECT COUNT(*) FROM elements WHERE struct_norm = :s)                     AS element_rows,
          (SELECT COUNT(*) FROM findings WHERE struct_norm = :s
                                           AND evidence_tier = 'conflicting')        AS conflicts,
          (SELECT COUNT(*) FROM images WHERE struct_norm = :s
                                         AND provenance = 'inspection_upload')       AS photos
    """, {"s": struct_norm}).fetchone()
    return {"latest_year": counts["latest_year"], "has_elements": counts["element_rows"] > 0,
            "conflicts": counts["conflicts"], "photos": counts["photos"]}


def structure_detail(conn, struct_norm: str) -> dict | None:
    row = conn.execute("SELECT * FROM structures WHERE struct_norm = ?", (struct_norm,)).fetchone()
    if row is None:
        return None
    flags = structure_flags(conn, struct_norm)
    ratings = _rows(conn, """
        SELECT year, component, rating, rating_raw, artifact_id FROM ratings
         WHERE struct_norm = ? ORDER BY year DESC, component
    """, struct_norm)
    return {"structure": dict(row), **flags, "ratings": ratings}


def suggestions(conn, limit: int = 4) -> list[dict]:
    """Structures where the federal records disagree most sharply.

    Offered on the upload page as starting points, because a brief is most
    useful where the official record is internally inconsistent.
    """
    rows = _rows(conn, """
        SELECT f.struct_norm, MAX(f.severity) AS severity, COUNT(*) AS conflicts,
               s.state_abbr, s.facility, s.feature_crossed, s.year_built
          FROM findings f JOIN structures s ON s.struct_norm = f.struct_norm
         WHERE f.evidence_tier = 'conflicting'
         GROUP BY f.struct_norm
         ORDER BY severity DESC, conflicts DESC, f.struct_norm
         LIMIT ?
    """, limit)
    return rows


def recent_briefs(conn, limit: int = 6) -> list[dict]:
    return _rows(conn, """
        SELECT b.brief_id, b.struct_norm, b.year, b.status, b.generated_at,
               b.total_sentences, b.blocked_unsupported, s.facility, s.feature_crossed,
               s.state_abbr,
               (SELECT COUNT(*) FROM images i WHERE i.struct_norm = b.struct_norm
                  AND i.provenance = 'inspection_upload') AS photos
          FROM briefs b LEFT JOIN structures s ON s.struct_norm = b.struct_norm
         ORDER BY b.generated_at DESC, b.brief_id DESC
         LIMIT ?
    """, limit)


def home(conn) -> dict:
    totals = queue_totals(conn)
    counts = conn.execute("""
        SELECT (SELECT COUNT(*) FROM structures)                                 AS structures,
               (SELECT COUNT(*) FROM artifacts)                                  AS artifacts,
               (SELECT COUNT(*) FROM findings WHERE evidence_tier='conflicting') AS conflicts,
               (SELECT COUNT(*) FROM images WHERE provenance='inspection_upload') AS photos,
               (SELECT COUNT(*) FROM image_regions WHERE source='detector')      AS regions,
               (SELECT COUNT(*) FROM briefs)                                     AS briefs
    """).fetchone()
    return {"counts": dict(counts), "queue": totals, "recent": recent_briefs(conn),
            "suggestions": suggestions(conn), "metrics": metrics_summary(conn),
            "vision": capabilities()["vision"]["available"]}


def _image_payload(conn, struct_norm: str) -> list[dict]:
    images = _rows(conn, """
        SELECT artifact_id, photo_key, stored_path, sha256, width, height, uploaded_at
          FROM images
         WHERE struct_norm = ? AND provenance = 'inspection_upload'
         ORDER BY uploaded_at, artifact_id
    """, struct_norm)
    for image in images:
        image["filename"] = Path(image.pop("stored_path")).name
        image["provenance"] = "inspection_upload"
        image["regions"] = _rows(conn, """
            SELECT artifact_id, region_index, x, y, w, h, defect_class, confidence,
                   description, detector_name, detector_version
              FROM image_regions
             WHERE image_artifact = ? AND source = 'detector'
             ORDER BY region_index
        """, image["artifact_id"])
    return images


def _streams(conn, struct_norm: str, year: int, images: list[dict]) -> list[dict]:
    has_nbi = conn.execute("SELECT 1 FROM ratings WHERE struct_norm = ? AND year = ? LIMIT 1",
                           (struct_norm, year)).fetchone() is not None
    has_nbe = conn.execute("SELECT 1 FROM elements WHERE struct_norm = ? AND year = ? LIMIT 1",
                           (struct_norm, year)).fetchone() is not None
    state = {
        "images": (bool(images), f"{len(images)} photograph(s) supplied" if images
                   else "no photographs supplied"),
        "nbi": (has_nbi, f"{year} ratings on record" if has_nbi else f"no {year} rating"),
        "nbe": (has_nbe, f"{year} element data on record" if has_nbe else
                "not published for this structure (NBE covers AL, AZ and IA here)"),
        "sensor": (False, "not supplied; the evidence model reserves an NDE stream"),
        "notes": (False, "not supplied"),
    }
    return [{"key": key, "label": label, "what": what, "present": state[key][0],
             "status": state[key][1]} for key, label, what in STREAMS]


def report(conn, brief_id: str) -> dict:
    """Everything the report page shows for one brief."""
    brief = dict(require_brief(conn, brief_id))
    struct_norm, year = brief["struct_norm"], brief["year"]

    sentences = _rows(conn, "SELECT * FROM brief_sentences WHERE brief_id = ? ORDER BY ordinal",
                      brief_id)
    for sentence in sentences:
        sentence["citations"] = [r[0] for r in conn.execute(
            "SELECT artifact_id FROM sentence_citations WHERE sentence_id = ? ORDER BY artifact_id",
            (sentence["sentence_id"],))]

    artifacts = {row["artifact_id"]: row for row in _rows(conn, """
        SELECT a.artifact_id, a.kind, a.summary, a.source_path, a.source_locator, a.year
          FROM artifacts a
         WHERE a.artifact_id IN (
            SELECT c.artifact_id FROM sentence_citations c
              JOIN brief_sentences s ON s.sentence_id = c.sentence_id
             WHERE s.brief_id = ?)
    """, brief_id)}

    findings = _rows(conn, """
        SELECT finding_id, kind, component, severity, confidence, evidence_tier, detail_json
          FROM findings WHERE struct_norm = ? AND year = ?
         ORDER BY CASE evidence_tier WHEN 'conflicting' THEN 0
                                     WHEN 'single_source' THEN 1 ELSE 2 END,
                  severity DESC
    """, struct_norm, year)
    for finding in findings:
        finding["detail"] = json.loads(finding.pop("detail_json") or "{}")

    images = _image_payload(conn, struct_norm)
    regions = [r for image in images for r in image["regions"]]
    confidences = [s["confidence"] for s in sentences if s["confidence"] is not None]
    structure = conn.execute("SELECT * FROM structures WHERE struct_norm = ?",
                             (struct_norm,)).fetchone()
    ratings = _rows(conn, "SELECT component, rating, artifact_id FROM ratings "
                          "WHERE struct_norm = ? AND year = ? ORDER BY component",
                    struct_norm, year)
    blocked = _rows(conn, "SELECT reason, text FROM blocked_sentences WHERE brief_id = ? "
                          "ORDER BY id", brief_id)
    signoff = signoff_record(conn, brief_id)
    publishable = bool(signoff and signoff["decision"] == "signed_off"
                       and signoff["version"] == brief["version"])
    streams = _streams(conn, struct_norm, year, images)

    return {
        "brief": brief,
        "structure": dict(structure) if structure else {"struct_norm": struct_norm},
        "ratings": ratings,
        "sentences": sentences,
        "artifacts": artifacts,
        "findings": findings,
        "images": images,
        "streams": streams,
        "blocked": blocked,
        "states": finding_states(conn, brief_id),
        "trail": trail(conn, brief_id),
        "signoff": signoff,
        "publishable": publishable,
        "stats": {
            "sentences": len(sentences),
            "generated": brief["total_sentences"],
            "blocked": brief["blocked_unsupported"],
            "citations": sum(len(s["citations"]) for s in sentences),
            "resolved": sum(1 for s in sentences for c in s["citations"] if c in artifacts),
            "conflicting": sum(1 for f in findings if f["evidence_tier"] == "conflicting"),
            "corroborated": sum(1 for f in findings if f["evidence_tier"] == "corroborated"),
            "single_source": sum(1 for f in findings if f["evidence_tier"] == "single_source"),
            "photos": len(images),
            "regions": len(regions),
            "mean_confidence": (round(sum(confidences) / len(confidences), 3)
                                if confidences else None),
            "missing_streams": sum(1 for s in streams if not s["present"]),
        },
    }


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

def metrics_table(conn) -> list[dict]:
    """The full evaluation table, computed now from the index and reports."""
    from ..eval.run_all import collect

    def existing(path: Path) -> Path | None:
        return path if path.exists() else None

    metrics = collect(
        conn,
        review_path=existing(REVIEWS / "sample.csv"),
        benchmark_path=existing(REPORTS / "detector_dacl10k.json"),
        sentence_review_path=existing(REVIEWS / "sentences.csv"),
    )
    return [{"name": m.name, "value": m.value, "ground_truth": m.ground_truth,
             "status": m.status} for m in metrics]


def metrics_summary(conn) -> dict:
    """The handful of figures the home page shows, without the full harness run."""
    live = conn.execute("""
        SELECT COALESCE(SUM(total_sentences), 0) AS generated,
               COALESCE(SUM(blocked_unsupported), 0) AS blocked FROM briefs
    """).fetchone()
    unresolved = conn.execute("""
        SELECT COUNT(*) FROM sentence_citations c
         WHERE NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.artifact_id = c.artifact_id)
    """).fetchone()[0]
    total_citations = conn.execute("SELECT COUNT(*) FROM sentence_citations").fetchone()[0]
    detector = None
    path = REPORTS / "detector_dacl10k.json"
    if path.exists():
        try:
            report_json = json.loads(path.read_text(encoding="utf-8"))
            recall = (report_json.get("class_agnostic") or {}).get("recall")
            detector = {"recall": recall,
                        "missed": None if recall is None else round(1 - recall, 4),
                        "corpus": report_json.get("corpus")}
        except (OSError, json.JSONDecodeError):
            detector = None
    validation = None
    path = REPORTS / "validation.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            optimistic = (data.get("by_direction") or {}).get("nbi_optimistic") or {}
            validation = {"lift": data.get("lift"),
                          "ratio": (optimistic.get("significance") or {}).get("risk_ratio")}
        except (OSError, json.JSONDecodeError):
            validation = None
    effort = correction_effort(conn)
    return {
        "source_link_resolution": (round(1 - unresolved / total_citations, 4)
                                   if total_citations else None),
        "unsupported_rate": (round(live["blocked"] / live["generated"], 4)
                             if live["generated"] else None),
        "edits_per_finding": effort.get("edits_per_finding"),
        "detector": detector,
        "validation": validation,
    }

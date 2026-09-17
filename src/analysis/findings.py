"""The Finding: the unit of analytical output, and its persistence.

A finding is a claim about a structure that the system is prepared to put in
front of an inspector. Two properties are structural, not decorative:

* **It carries its evidence.** ``evidence`` is a list of artifact IDs with the
  role each plays. A finding with no evidence cannot be stored — the check is in
  :func:`save_findings`, not in a convention.
* **It carries its uncertainty.** ``confidence`` and ``evidence_tier`` are
  required fields. There is no way to construct a finding that presents itself as
  more certain than it is, and no default that hides a single source.

Findings are regenerated from scratch each time the analysis runs, so their IDs
are deterministic functions of what they are about (see :func:`src.ids.finding_id`)
and re-running produces the same IDs rather than duplicates.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from ..db import utcnow

EvidenceTier = Literal["corroborated", "single_source", "conflicting"]
EvidenceRole = Literal["supports", "contradicts", "context"]

#: Every tier the schema allows, in the order the UI presents them.
TIERS: tuple[str, ...] = ("conflicting", "single_source", "corroborated")


class UngroundedFinding(ValueError):
    """A finding was constructed with no supporting artifact.

    Raised at save time. Invariant 2 says a generated sentence must carry artifact
    IDs; a finding with no artifacts could only ever produce such a sentence, so
    it is refused one layer earlier.
    """


@dataclass
class Evidence:
    """One artifact and the role it plays in a finding."""

    artifact_id: str
    role: EvidenceRole = "supports"


@dataclass
class Finding:
    """An analytical claim, with its evidence and its uncertainty."""

    finding_id: str
    struct_norm: str
    year: int
    kind: str                       # contradiction | condition | defect | missing_evidence
    evidence_tier: EvidenceTier
    confidence: float
    detail: dict[str, Any]
    component: str | None = None
    severity: float | None = None
    evidence: list[Evidence] = field(default_factory=list)

    def artifact_ids(self, *, roles: Iterable[str] | None = None) -> list[str]:
        wanted = set(roles) if roles else None
        return [e.artifact_id for e in self.evidence if wanted is None or e.role in wanted]

    def __post_init__(self) -> None:
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence!r}")
        if self.severity is not None and not 0.0 <= float(self.severity) <= 1.0:
            raise ValueError(f"severity must be in [0, 1], got {self.severity!r}")
        if self.evidence_tier not in TIERS:
            raise ValueError(f"unknown evidence tier {self.evidence_tier!r}")


def save_findings(conn: sqlite3.Connection, findings: Iterable[Finding], *, replace_kind: str | None = None,
                  struct_norm: str | None = None, year: int | None = None) -> int:
    """Persist findings and their evidence links.

    Args:
        replace_kind: when given, findings of that kind for the same structure and
            year are deleted first, so a re-run supersedes rather than accumulates.

    Raises:
        UngroundedFinding: if any finding has no evidence. Nothing is written.
    """
    findings = list(findings)
    for finding in findings:
        if not finding.evidence:
            raise UngroundedFinding(
                f"finding {finding.finding_id} ({finding.kind}) carries no artifact IDs; "
                "a finding without evidence cannot be stored"
            )

    if replace_kind and struct_norm is not None and year is not None:
        conn.execute(
            "DELETE FROM findings WHERE kind = ? AND struct_norm = ? AND year = ?",
            (replace_kind, struct_norm, year),
        )

    for finding in findings:
        conn.execute(
            """
            INSERT INTO findings
                (finding_id, struct_norm, year, kind, component, severity, confidence,
                 evidence_tier, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(finding_id) DO UPDATE SET
                severity = excluded.severity,
                confidence = excluded.confidence,
                evidence_tier = excluded.evidence_tier,
                detail_json = excluded.detail_json
            """,
            (finding.finding_id, finding.struct_norm, finding.year, finding.kind,
             finding.component, finding.severity, finding.confidence,
             finding.evidence_tier, json.dumps(finding.detail, sort_keys=True), utcnow()),
        )
        conn.execute("DELETE FROM finding_evidence WHERE finding_id = ?", (finding.finding_id,))
        conn.executemany(
            "INSERT INTO finding_evidence (finding_id, artifact_id, role) VALUES (?, ?, ?)",
            [(finding.finding_id, e.artifact_id, e.role) for e in finding.evidence],
        )
    return len(findings)


def load_findings(conn: sqlite3.Connection, struct_norm: str, year: int,
                  *, kind: str | None = None) -> list[Finding]:
    """Read findings back, evidence included."""
    sql = "SELECT * FROM findings WHERE struct_norm = ? AND year = ?"
    params: list[Any] = [struct_norm, year]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY severity DESC NULLS LAST, finding_id"

    out: list[Finding] = []
    for row in conn.execute(sql, params).fetchall():
        evidence = [
            Evidence(r["artifact_id"], r["role"] or "supports")
            for r in conn.execute(
                "SELECT artifact_id, role FROM finding_evidence WHERE finding_id = ? ORDER BY artifact_id",
                (row["finding_id"],),
            )
        ]
        out.append(Finding(
            finding_id=row["finding_id"], struct_norm=row["struct_norm"], year=row["year"],
            kind=row["kind"], component=row["component"], severity=row["severity"],
            confidence=row["confidence"], evidence_tier=row["evidence_tier"],
            detail=json.loads(row["detail_json"]), evidence=evidence,
        ))
    return out

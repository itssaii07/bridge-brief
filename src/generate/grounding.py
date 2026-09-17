"""The grounding gate.

Invariant 2: *every generated sentence carries artifact IDs; a sentence with zero
artifact IDs is dropped before render and counted in ``blocked_unsupported``.*

The gate is **deterministic code**, not an instruction to a model. A drafter —
template-based or an LLM — proposes sentences. The gate decides which survive,
and it does not trust the drafter to have followed any rule:

* Citations are taken from the candidate's declared list *and* from IDs appearing
  in its text, so a drafter cannot slip an uncited claim past by omitting the
  declaration, nor smuggle a claim in by writing an ID it did not declare.
* Every citation is parsed through :mod:`src.ids` and then resolved against the
  ``artifacts`` table. An ID that is well-formed but does not exist does not
  count. A fabricated citation must not be able to launder a sentence.
* A sentence whose surviving citation count is zero is dropped, recorded with a
  reason, and counted. The counter is shown in the UI, because it is a feature.

The gate deliberately makes no judgement about whether a sentence is *true*. It
enforces that every sentence is *attributable*, which is the property that lets a
human check it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import ids
from ..store import artifacts_exist


@dataclass
class CandidateSentence:
    """A sentence proposed for a brief, before the gate has ruled on it."""

    section: str
    text: str
    citations: list[str] = field(default_factory=list)
    finding_id: str | None = None
    confidence: float | None = None
    evidence_tier: str | None = None


@dataclass
class GatedSentence:
    """A sentence that passed, with the citations that justified it."""

    section: str
    text: str
    citations: list[str]
    finding_id: str | None = None
    confidence: float | None = None
    evidence_tier: str | None = None


@dataclass
class BlockedSentence:
    """A sentence the gate dropped, kept for the audit trail."""

    section: str
    text: str
    reason: str


@dataclass
class GateResult:
    """Everything the gate decided, including the counters the UI displays."""

    passed: list[GatedSentence] = field(default_factory=list)
    blocked: list[BlockedSentence] = field(default_factory=list)
    #: Citations that were syntactically valid but resolved to no artifact.
    unresolved_citations: list[str] = field(default_factory=list)
    #: Citations that were not well-formed artifact IDs at all.
    malformed_citations: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Every sentence the drafter proposed."""
        return len(self.passed) + len(self.blocked)

    @property
    def blocked_unsupported(self) -> int:
        """The counter named in invariant 2."""
        return len(self.blocked)

    @property
    def unsupported_rate(self) -> float | None:
        """Blocked over total. ``None`` when nothing was proposed."""
        if not self.total:
            return None
        return round(self.blocked_unsupported / self.total, 4)


REASON_NO_CITATIONS = "sentence carried no artifact IDs"
REASON_NONE_RESOLVED = "every cited artifact ID failed to resolve to a stored artifact"
REASON_EMPTY = "sentence was empty"


def collect_citations(candidate: CandidateSentence) -> tuple[list[str], list[str]]:
    """Gather a candidate's claimed citations.

    Returns ``(well_formed, malformed)``. Sources are the declared list and the
    sentence text, unioned and de-duplicated in first-seen order — declared
    first, because that is the drafter's explicit claim.
    """
    well_formed: list[str] = []
    malformed: list[str] = []
    for value in list(candidate.citations or []):
        text = str(value).strip()
        if not text:
            continue
        if ids.is_artifact_id(text):
            if text not in well_formed:
                well_formed.append(text)
        elif text not in malformed:
            malformed.append(text)
    for found in ids.extract_ids(candidate.text):
        if found not in well_formed:
            well_formed.append(found)
    return well_formed, malformed


def render_with_citations(text: str, citations: list[str]) -> str:
    """Ensure every surviving citation is visible in the sentence itself.

    The UI parses citations back out of the rendered text to drive
    click-to-highlight, so what the inspector reads and what the gate verified
    are the same string. Citations already present inline are not repeated.
    """
    body = (text or "").strip()
    present = set(ids.extract_ids(body))
    missing = [c for c in citations if c not in present]
    if not missing:
        return body
    separator = " " if body and not body.endswith(" ") else ""
    return f"{body}{separator}" + " ".join(f"[{c}]" for c in missing)


def apply_gate(conn: sqlite3.Connection, candidates: list[CandidateSentence]) -> GateResult:
    """Run the gate over a drafter's output.

    Every candidate ends up in exactly one of ``passed`` or ``blocked``; nothing
    is discarded quietly.
    """
    result = GateResult()

    all_claimed: list[str] = []
    per_candidate: list[tuple[CandidateSentence, list[str], list[str]]] = []
    for candidate in candidates:
        well_formed, malformed = collect_citations(candidate)
        per_candidate.append((candidate, well_formed, malformed))
        all_claimed.extend(well_formed)
        for bad in malformed:
            if bad not in result.malformed_citations:
                result.malformed_citations.append(bad)

    # One resolution query for the whole brief.
    resolved = artifacts_exist(conn, all_claimed)

    for candidate, well_formed, malformed in per_candidate:
        text = (candidate.text or "").strip()
        if not text:
            result.blocked.append(BlockedSentence(candidate.section, candidate.text or "",
                                                  REASON_EMPTY))
            continue

        surviving = [c for c in well_formed if c in resolved]
        for claimed in well_formed:
            if claimed not in resolved and claimed not in result.unresolved_citations:
                result.unresolved_citations.append(claimed)

        if not surviving:
            if well_formed or malformed:
                detail = ", ".join((well_formed + malformed)[:5])
                reason = f"{REASON_NONE_RESOLVED} ({detail})"
            else:
                reason = REASON_NO_CITATIONS
            result.blocked.append(BlockedSentence(candidate.section, text, reason))
            continue

        result.passed.append(GatedSentence(
            section=candidate.section,
            text=render_with_citations(text, surviving),
            citations=surviving,
            finding_id=candidate.finding_id,
            confidence=candidate.confidence,
            evidence_tier=candidate.evidence_tier,
        ))

    return result

"""Sentence drafters.

A drafter turns findings into candidate sentences. It is not trusted: whatever it
produces goes through the grounding gate, which is deterministic code (see
``grounding.py``).

Two are provided:

* :class:`TemplateDrafter` — the default. Deterministic, needs no API key and no
  network, and cannot state anything the finding's structured payload does not
  contain. This keeps brief generation runnable out of the box.
* :class:`LLMDrafter` — optional, behind ``ANTHROPIC_API_KEY`` and
  ``BRIDGE_BRIEF_DRAFTER=llm``. Its output is gated exactly like the template
  drafter's, and the gate's counter is what makes using it safe rather than the
  wording of its prompt.

Language rules, applied by both: findings are described as what the *records* say,
never as a judgement about the structure. Nothing here recommends, clears, or
schedules — that would breach invariant 4 in the one place where prose could
smuggle it in.
"""

from __future__ import annotations

import os
from typing import Protocol

from ..analysis.findings import Finding
from .grounding import CandidateSentence

SECTION_SUMMARY = "summary"
SECTION_CONTRADICTIONS = "contradictions"
SECTION_CONDITION = "condition"
SECTION_IMAGERY = "imagery"
SECTION_GAPS = "evidence_gaps"

#: Section order in the rendered brief.
SECTIONS = (SECTION_SUMMARY, SECTION_CONTRADICTIONS, SECTION_CONDITION,
            SECTION_IMAGERY, SECTION_GAPS)

BAND_WORDS = {"good": "good", "fair": "fair", "poor": "poor"}


class Drafter(Protocol):
    name: str

    def draft(self, context: dict) -> list[CandidateSentence]:
        ...


def _qty(value: float | None, units: str | None) -> str:
    if value is None:
        return "an unreported quantity"
    return f"{value:,.0f} {units}" if units else f"{value:,.0f} units"


class TemplateDrafter:
    """Deterministic drafter. Every sentence is a template over finding fields."""

    name = "template@1"

    def draft(self, context: dict) -> list[CandidateSentence]:
        struct = context["struct_norm"]
        year = context["year"]
        findings: list[Finding] = context["findings"]
        images: list[dict] = context.get("images", [])
        regions: list[dict] = context.get("regions", [])
        missing: list[dict] = context.get("missing", [])

        out: list[CandidateSentence] = []
        contradictions = [f for f in findings if f.kind == "contradiction"]
        corroborated = [f for f in findings if f.evidence_tier == "corroborated"]
        single = [f for f in findings if f.evidence_tier == "single_source"]

        # -- Summary ------------------------------------------------------
        # Cites the evidence it counts, so it survives the gate on its own merits
        # rather than by being exempted as a heading.
        if contradictions:
            components = ", ".join(sorted({f.component or "unknown" for f in contradictions}))
            out.append(CandidateSentence(
                section=SECTION_SUMMARY,
                text=(f"For structure {struct} in {year}, the official records disagree about "
                      f"{len(contradictions)} component(s): {components}."),
                citations=sorted({a for f in contradictions for a in f.artifact_ids()}),
            ))
        elif corroborated:
            out.append(CandidateSentence(
                section=SECTION_SUMMARY,
                text=(f"For structure {struct} in {year}, the NBI condition ratings and the NBE "
                      f"element condition states agree on all {len(corroborated)} component(s) "
                      "where both sources are available."),
                citations=sorted({a for f in corroborated for a in f.artifact_ids()}),
            ))

        # -- Contradictions ------------------------------------------------
        for finding in sorted(contradictions, key=lambda f: -(f.severity or 0)):
            detail = finding.detail
            component = finding.component or "component"
            rating = detail.get("nbi_rating")
            band = BAND_WORDS.get(detail.get("nbi_band", ""), detail.get("nbi_band"))
            implied = BAND_WORDS.get(detail.get("implied_band", ""), detail.get("implied_band"))
            deteriorated = _qty(detail.get("deteriorated_qty"), detail.get("units"))
            total = _qty(detail.get("total_qty"), detail.get("units"))
            fraction = detail.get("deteriorated_fraction")
            contradicting = finding.artifact_ids(roles=["contradicts"])

            if detail.get("direction") == "nbi_optimistic":
                text = (
                    f"The {year} NBI record rates the {component} {rating} ({band}), while the "
                    f"{year} NBE element data reports {deteriorated} of {total} "
                    f"({fraction:.1%} of the mapped quantity) in condition states 3-4, which "
                    f"corresponds to a {implied} component."
                ) if fraction is not None else (
                    f"The {year} NBI record rates the {component} {rating} ({band}), while the "
                    f"{year} NBE element data reports {deteriorated} in condition states 3-4."
                )
            else:
                text = (
                    f"The {year} NBI record rates the {component} {rating} ({band}), while the "
                    f"{year} NBE element data reports only {deteriorated} of {total} in "
                    f"condition states 3-4, which corresponds to a {implied} component."
                )
            out.append(CandidateSentence(
                section=SECTION_CONTRADICTIONS, text=text, citations=contradicting,
                finding_id=finding.finding_id, confidence=finding.confidence,
                evidence_tier=finding.evidence_tier,
            ))

            elements = detail.get("elements") or []
            if elements:
                out.append(CandidateSentence(
                    section=SECTION_CONTRADICTIONS,
                    text=(f"This comparison rolls up AASHTO element(s) "
                          f"{', '.join(str(e) for e in elements)} into the {component}."
                          + (" One of these elements is claimed by more than one component, "
                             "so the roll-up is a judgement call."
                             if detail.get("has_ambiguous_element") else "")),
                    citations=finding.artifact_ids(),
                    finding_id=finding.finding_id, confidence=finding.confidence,
                    evidence_tier=finding.evidence_tier,
                ))
            caveats = detail.get("confidence_caveats") or []
            if caveats:
                out.append(CandidateSentence(
                    section=SECTION_CONTRADICTIONS,
                    text=(f"Confidence in this comparison is reduced to "
                          f"{finding.confidence:.2f} because {'; '.join(caveats)}."),
                    citations=finding.artifact_ids(),
                    finding_id=finding.finding_id, confidence=finding.confidence,
                    evidence_tier=finding.evidence_tier,
                ))

        # -- Condition (agreement and single-source) -------------------------
        for finding in sorted(corroborated, key=lambda f: f.component or ""):
            detail = finding.detail
            out.append(CandidateSentence(
                section=SECTION_CONDITION,
                text=(f"The {year} NBI rating of {detail.get('nbi_rating')} for the "
                      f"{finding.component} is consistent with the element condition states "
                      f"recorded for that component; two independent sources agree."),
                citations=finding.artifact_ids(),
                finding_id=finding.finding_id, confidence=finding.confidence,
                evidence_tier=finding.evidence_tier,
            ))
        for finding in sorted(single, key=lambda f: f.component or ""):
            detail = finding.detail
            out.append(CandidateSentence(
                section=SECTION_CONDITION,
                text=(f"The {year} NBI rating of {detail.get('nbi_rating')} for the "
                      f"{finding.component} is recorded by a single source. No element "
                      "condition-state data is available for this component, so this rating "
                      "has not been cross-checked."),
                citations=finding.artifact_ids(),
                finding_id=finding.finding_id, confidence=finding.confidence,
                evidence_tier=finding.evidence_tier,
            ))

        # -- Imagery ---------------------------------------------------------
        uploads = [i for i in images if i["provenance"] == "inspection_upload"]
        if uploads:
            out.append(CandidateSentence(
                section=SECTION_IMAGERY,
                text=(f"{len(uploads)} photograph(s) were supplied for this inspection and are "
                      "labelled as inspection uploads."),
                citations=[i["artifact_id"] for i in uploads],
            ))
        for region in regions:
            out.append(CandidateSentence(
                section=SECTION_IMAGERY,
                text=(f"A candidate {region['defect_class']} region was detected in "
                      f"{region['image_artifact']} at ({region['x']}, {region['y']}), "
                      f"{region['w']}x{region['h']} pixels, with detector confidence "
                      f"{region['confidence']:.2f}. This is an automated proposal on an "
                      "inspection photograph and has not been confirmed."),
                citations=[region["artifact_id"], region["image_artifact"]],
                confidence=region["confidence"], evidence_tier="single_source",
            ))
        # There is deliberately no branch here for reference corpus imagery.
        # A brief is about one structure, and corpus images have a NULL structure,
        # so they never reach this drafter. That is invariant 6 holding by
        # construction rather than by a filter.

        # -- Evidence gaps ----------------------------------------------------
        # Gap sentences cite the artifacts that exist for the structure, because a
        # statement about what is absent is still a statement about this record.
        for gap in missing:
            anchors = context.get("anchor_artifacts") or []
            if not anchors:
                continue
            out.append(CandidateSentence(
                section=SECTION_GAPS,
                text=(f"No {gap['source'].upper()} data is available for this structure in "
                      f"{gap['year']}: {gap['reason']}."),
                citations=anchors[:3],
            ))
        if not uploads:
            anchors = context.get("anchor_artifacts") or []
            if anchors:
                out.append(CandidateSentence(
                    section=SECTION_GAPS,
                    text=("No inspection photographs have been supplied for this structure, so "
                          "no image evidence contributes to this brief."),
                    citations=anchors[:3],
                ))
        return out


class LLMDrafter:
    """Optional LLM drafter. Its output is gated identically to the template one.

    Configuration is by environment variable only; nothing is hardcoded and no
    key is ever written to the database or a log.

        ANTHROPIC_API_KEY      required
        BRIDGE_BRIEF_MODEL     optional model id
        BRIDGE_BRIEF_DRAFTER   set to 'llm' to select this drafter

    The prompt asks for citations, but nothing depends on it complying: the gate
    drops any sentence whose citations do not resolve, and counts it.
    """

    name = "llm@1"
    DEFAULT_MODEL = "claude-sonnet-5"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("BRIDGE_BRIEF_MODEL", self.DEFAULT_MODEL)

    def draft(self, context: dict) -> list[CandidateSentence]:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "The LLM drafter was selected but ANTHROPIC_API_KEY is not set.\n"
                "Either export it, or use the default deterministic drafter by unsetting "
                "BRIDGE_BRIEF_DRAFTER."
            )
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "The LLM drafter needs the anthropic package: pip install -e \".[llm]\""
            ) from exc

        prompt = self._build_prompt(context)
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=self.model, max_tokens=2000,
            system=(
                "You draft sentences for a bridge inspection brief from structured findings. "
                "Rules: state only what the findings contain; end every sentence with the "
                "artifact IDs that support it in square brackets; never recommend maintenance, "
                "never declare a structure safe or unsafe, never schedule work. "
                "Output one sentence per line, nothing else."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")

        candidates: list[CandidateSentence] = []
        for line in text.splitlines():
            line = line.strip().lstrip("-* ").strip()
            if line:
                # Citations are left to be recovered from the text by the gate,
                # which validates and resolves them. Nothing is taken on trust.
                candidates.append(CandidateSentence(section=SECTION_CONTRADICTIONS, text=line))
        return candidates

    @staticmethod
    def _build_prompt(context: dict) -> str:
        import json

        findings = [
            {"finding_id": f.finding_id, "kind": f.kind, "component": f.component,
             "tier": f.evidence_tier, "confidence": f.confidence, "severity": f.severity,
             "artifact_ids": f.artifact_ids(), "detail": f.detail}
            for f in context["findings"]
        ]
        return (
            f"Structure {context['struct_norm']}, inspection year {context['year']}.\n"
            "Findings (JSON):\n" + json.dumps(findings, indent=2, default=str)
        )


def get_drafter(name: str | None = None) -> Drafter:
    """Select a drafter. Defaults to the deterministic template drafter."""
    choice = (name or os.environ.get("BRIDGE_BRIEF_DRAFTER") or "template").strip().lower()
    if choice in ("template", "default"):
        return TemplateDrafter()
    if choice == "llm":
        return LLMDrafter()
    raise ValueError(f"unknown drafter {choice!r}; expected 'template' or 'llm'")

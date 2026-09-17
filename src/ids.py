"""Artifact ID system.

Every addressable unit of evidence in this project gets a stable, deterministic
identifier. Generated sentences cite these IDs and the review UI resolves them
back to the original source artifact. See CLAUDE.md, "Artifact ID system".

    NBI rating        NBI-{struct}-{year}-{component}    NBI-AL013450-2023-deck
    NBE element state NBE-{struct}-{year}-{elem}-cs{n}   NBE-AL013450-2023-12-cs3
    Uploaded photo    IMG-{struct}-{photo}               IMG-AL013450-p03
    Photo region      IMG-{struct}-{photo}-r{n}          IMG-AL013450-p03-r2
    Reference image   REF-{corpus}-{id}                  REF-codebrim-00412
    NDE cell          NDE-{struct}-{method}-{cell}       reserved, not populated

``{struct}`` is the **state-qualified** key built by :func:`structure_key` — the
postal state abbreviation followed by the normalised structure number. CLAUDE.md
writes the bare form (``NBI-013450-2023-deck``); the real published data forced
the state prefix, because NBI item 8 is unique only within a state and 40,374
structure numbers in the 2023 file are shared between states. See
:func:`structure_key` and ASSUMPTIONS.md B5.

Determinism rule: an ID is a pure function of the source coordinates of the
thing it names. It never depends on iteration order, insertion order, wall-clock
time, or how many times ingest has been run. Re-running ingest against the same
files must reproduce byte-identical IDs — that is what makes ingest idempotent
and what lets a citation written last month still resolve today.

Every constructor has a matching parser and the pair round-trips.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Controlled vocabularies
# --------------------------------------------------------------------------

#: NBI condition-rating components (FHWA items 58, 59, 60, 62).
COMPONENTS: tuple[str, ...] = ("deck", "superstructure", "substructure", "culvert")

#: Reference corpora. Reference imagery is never evidence about a named bridge.
CORPORA: tuple[str, ...] = ("codebrim",)

#: Non-destructive evaluation methods. Reserved — no NDE data is ingested yet.
#: Present so the evidence model demonstrably extends to sensor streams.
NDE_METHODS: tuple[str, ...] = ("gpr", "er", "impact_echo", "usw", "ir")

#: Valid AASHTO condition states.
CONDITION_STATES: tuple[int, ...] = (1, 2, 3, 4)


class IdError(ValueError):
    """Raised when an ID cannot be constructed or parsed.

    Always carries the offending value, because the usual cause is a source file
    whose shape differs from what we assumed, and the operator needs to see it.
    """


# --------------------------------------------------------------------------
# Structure-number normalisation
# --------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def normalise_struct(raw: str | None) -> str:
    """Normalise an NBI structure number into the shared join key.

    States format structure numbers inconsistently: NBI item 8 is a 15-character
    field, some states right-pad with spaces, some left-pad with zeros, some
    embed spaces or punctuation, and the same bridge can be spelled differently
    between the NBI file and the NBE file for the same year. This function is
    the single point of truth used by every module, so that the NBI/NBE/uploads
    join cannot silently drift.

    The rules, in order:

    1. Upper-case.
    2. Drop every character that is not ``A-Z`` or ``0-9``. This removes padding
       spaces and punctuation, and guarantees the result contains no ``-``, so
       artifact IDs (which are hyphen-delimited) parse unambiguously.
    3. Strip leading zeros.
    4. If what remains is all digits and shorter than 6 characters, left-pad
       with zeros to width 6. This is the canonical width used throughout
       CLAUDE.md (``013450``) and makes ``13450``, ``013450`` and ``0013450``
       agree.

    Purely alphanumeric identifiers longer than 6 characters, and identifiers
    containing letters, are left at their natural width.

    Raises:
        IdError: if the input is empty, or contains no usable characters.
    """
    if raw is None:
        raise IdError("structure number is None")
    upper = str(raw).strip().upper()
    if not upper:
        raise IdError("structure number is empty")
    cleaned = _NON_ALNUM.sub("", upper)
    if not cleaned:
        raise IdError(f"structure number has no alphanumeric characters: {raw!r}")
    stripped = cleaned.lstrip("0")
    if not stripped:
        # The structure number was all zeros. That is not a real structure, but
        # it is also not our place to invent one: report it.
        raise IdError(f"structure number is all zeros: {raw!r}")
    if stripped.isdigit() and len(stripped) < 6:
        return stripped.rjust(6, "0")
    return stripped


def structure_key(state: str | None, raw: str | None) -> str:
    """Build the shared, nationally-unique structure join key.

    **NBI item 8 is unique only within a state, not nationally.** The real 2023
    file proves it: 40,374 normalised structure numbers are claimed by more than
    one state, structure ``000002`` by six of them, and 112,836 of 621,581 rows
    share a number with another state's bridge. A key built from the structure
    number alone therefore merges unrelated bridges — which would make the
    contradiction engine compare one state's elements against another state's
    ratings and report the collision as a finding.

    The key is the postal state abbreviation followed by the normalised
    structure number: ``AL`` + ``013450`` -> ``AL013450``.

    Two properties this relies on:

    * The abbreviation begins with a letter, so :func:`normalise_struct` is
      idempotent on the composed key (no leading zero is stripped and no
      zero-padding is applied). Every ID constructor can therefore accept an
      already-composed key unchanged.
    * The key stays within ``[A-Z0-9]``, so hyphen-delimited artifact IDs still
      parse unambiguously.

    Args:
        state: postal abbreviation for the publishing state (``"AL"``).
        raw: structure number exactly as published.

    Raises:
        IdError: if the state is absent or unusable, or the structure number is.
            An unidentifiable state is an error, never a default: a key missing
            its state prefix would silently collide with another state's bridge,
            which is the exact failure this function exists to prevent.
    """
    if state is None:
        raise IdError("state is None; cannot build a nationally-unique structure key")
    token = _NON_ALNUM.sub("", str(state).strip().upper())
    if not token:
        raise IdError(f"state has no alphanumeric characters: {state!r}")
    if not token[0].isalpha():
        raise IdError(
            f"state prefix must begin with a letter, got {state!r}: a numeric "
            "prefix would break normalise_struct idempotency on the composed key"
        )
    return f"{token}{normalise_struct(raw)}"


_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_]")


def _token(value: str | None, *, what: str, lower: bool = False) -> str:
    """Sanitise a free-form value into a hyphen-free ID token."""
    if value is None:
        raise IdError(f"{what} is None")
    text = str(value).strip()
    if not text:
        raise IdError(f"{what} is empty")
    token = _SAFE_TOKEN.sub("", text)
    if not token:
        raise IdError(f"{what} has no usable characters: {value!r}")
    return token.lower() if lower else token


def _year(year: int | str) -> int:
    try:
        y = int(year)
    except (TypeError, ValueError) as exc:
        raise IdError(f"year is not an integer: {year!r}") from exc
    if not 1900 <= y <= 2100:
        raise IdError(f"year out of plausible range: {year!r}")
    return y


# --------------------------------------------------------------------------
# Constructors
# --------------------------------------------------------------------------


def nbi_id(struct: str, year: int | str, component: str) -> str:
    """ID for one NBI component condition rating."""
    comp = str(component).strip().lower()
    if comp not in COMPONENTS:
        raise IdError(f"unknown NBI component {component!r}; expected one of {COMPONENTS}")
    return f"NBI-{normalise_struct(struct)}-{_year(year)}-{comp}"


def nbe_id(struct: str, year: int | str, elem_num: int | str, cs: int | str) -> str:
    """ID for one NBE element condition-state quantity."""
    try:
        elem = int(elem_num)
    except (TypeError, ValueError) as exc:
        raise IdError(f"element number is not an integer: {elem_num!r}") from exc
    if elem <= 0:
        raise IdError(f"element number must be positive: {elem_num!r}")
    try:
        state = int(cs)
    except (TypeError, ValueError) as exc:
        raise IdError(f"condition state is not an integer: {cs!r}") from exc
    if state not in CONDITION_STATES:
        raise IdError(f"condition state must be one of {CONDITION_STATES}: {cs!r}")
    return f"NBE-{normalise_struct(struct)}-{_year(year)}-{elem}-cs{state}"


def img_id(struct: str, photo: str) -> str:
    """ID for a photo uploaded for a specific inspection.

    ``photo`` is a stable per-structure photo key derived from the file name, not
    from upload order. A key that looks like a region suffix (``r3``) is rejected
    so that ``IMG-…-{photo}`` can never be confused with ``IMG-…-{photo}-r{n}``.
    """
    key = _token(photo, what="photo key", lower=True)
    if re.fullmatch(r"r\d+", key):
        raise IdError(
            f"photo key {photo!r} collides with the region suffix form 'r<n>'; "
            "rename the photo so its key is not 'r' followed by digits"
        )
    return f"IMG-{normalise_struct(struct)}-{key}"


def region_id(image_artifact_id: str, region_index: int | str) -> str:
    """ID for a region within an uploaded photo, derived from the image's ID.

    ``region_index`` must be deterministic from the region's own geometry
    ordering (see ``src/detect``), never from detector iteration order.
    """
    try:
        idx = int(region_index)
    except (TypeError, ValueError) as exc:
        raise IdError(f"region index is not an integer: {region_index!r}") from exc
    if idx < 0:
        raise IdError(f"region index must be non-negative: {region_index!r}")
    parsed = parse(image_artifact_id)
    if parsed.kind != "IMG":
        raise IdError(f"region parent must be an IMG id, got {image_artifact_id!r}")
    if parsed.region is not None:
        raise IdError(f"region parent is already a region id: {image_artifact_id!r}")
    return f"{image_artifact_id}-r{idx}"


def ref_id(corpus: str, item_id: str) -> str:
    """ID for a public benchmark image. Never evidence about a named bridge."""
    name = _token(corpus, what="corpus", lower=True)
    if name not in CORPORA:
        raise IdError(f"unknown corpus {corpus!r}; expected one of {CORPORA}")
    return f"REF-{name}-{_token(item_id, what='corpus item id', lower=True)}"


def nde_id(struct: str, method: str, cell: str) -> str:
    """ID for one NDE measurement cell. RESERVED — nothing populates this yet.

    It exists because the evidence model is meant to accept sensor streams
    without redesign, and the cheapest proof of that is a working constructor
    and parser for the form. See CLAUDE.md, "Scope decision".
    """
    m = _token(method, what="NDE method", lower=True)
    if m not in NDE_METHODS:
        raise IdError(f"unknown NDE method {method!r}; expected one of {NDE_METHODS}")
    return f"NDE-{normalise_struct(struct)}-{m}-{_token(cell, what='NDE cell', lower=True)}"


# --------------------------------------------------------------------------
# Derived, non-artifact identifiers
# --------------------------------------------------------------------------


def finding_id(kind: str, struct: str, year: int | str, discriminator: str) -> str:
    """Deterministic ID for a finding.

    Findings are regenerated from scratch whenever the analysis re-runs, so their
    IDs must be a function of what the finding is about — not of when it was
    produced. The discriminator distinguishes findings of the same kind about the
    same structure and year (e.g. the component or element involved).
    """
    k = _token(kind, what="finding kind", lower=True)
    disc = str(discriminator).strip().lower()
    payload = f"{k}|{normalise_struct(struct)}|{_year(year)}|{disc}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"F-{k}-{digest}"


def brief_id(struct: str, year: int | str, version: int) -> str:
    if int(version) < 1:
        raise IdError(f"brief version must be >= 1: {version!r}")
    return f"BRIEF-{normalise_struct(struct)}-{_year(year)}-v{int(version)}"


def sentence_id(brief: str, ordinal: int) -> str:
    if int(ordinal) < 0:
        raise IdError(f"sentence ordinal must be non-negative: {ordinal!r}")
    return f"{brief}-s{int(ordinal)}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedId:
    """The source coordinates recovered from an artifact ID.

    ``kind`` is one of NBI, NBE, IMG, REF, NDE. For an IMG id, ``region`` is
    ``None`` for the photo itself and the region index for a region.
    """

    kind: str
    raw: str
    struct: str | None = None
    year: int | None = None
    component: str | None = None
    elem_num: int | None = None
    cs: int | None = None
    photo: str | None = None
    region: int | None = None
    corpus: str | None = None
    item_id: str | None = None
    method: str | None = None
    cell: str | None = None

    @property
    def is_reference(self) -> bool:
        """True when this artifact is corpus imagery, not evidence of a bridge."""
        return self.kind == "REF"


_NBI_RE = re.compile(r"^NBI-([A-Z0-9]+)-(\d{4})-([a-z]+)$")
_NBE_RE = re.compile(r"^NBE-([A-Z0-9]+)-(\d{4})-(\d+)-cs([1-4])$")
_IMG_RE = re.compile(r"^IMG-([A-Z0-9]+)-([a-z0-9_]+?)(?:-r(\d+))?$")
_REF_RE = re.compile(r"^REF-([a-z0-9_]+)-([a-z0-9_]+)$")
_NDE_RE = re.compile(r"^NDE-([A-Z0-9]+)-([a-z0-9_]+)-([a-z0-9_]+)$")


def parse(artifact_id: str) -> ParsedId:
    """Parse any artifact ID back into its source coordinates.

    ``parse(construct(x)) == x`` for every constructor above; the tests assert it.

    Raises:
        IdError: if the string is not a well-formed artifact ID. Callers treat
            this as "this citation does not resolve", which is a reportable
            condition, not something to paper over.
    """
    if not artifact_id or not isinstance(artifact_id, str):
        raise IdError(f"not an artifact id: {artifact_id!r}")
    text = artifact_id.strip()

    if m := _NBI_RE.match(text):
        struct, year, comp = m.groups()
        if comp not in COMPONENTS:
            raise IdError(f"unknown component in {artifact_id!r}: {comp!r}")
        return ParsedId("NBI", text, struct=struct, year=int(year), component=comp)

    if m := _NBE_RE.match(text):
        struct, year, elem, cs = m.groups()
        return ParsedId("NBE", text, struct=struct, year=int(year),
                        elem_num=int(elem), cs=int(cs))

    if m := _IMG_RE.match(text):
        struct, photo, region = m.groups()
        if re.fullmatch(r"r\d+", photo):
            raise IdError(f"ambiguous photo key in {artifact_id!r}")
        return ParsedId("IMG", text, struct=struct, photo=photo,
                        region=int(region) if region is not None else None)

    if m := _REF_RE.match(text):
        corpus, item = m.groups()
        return ParsedId("REF", text, corpus=corpus, item_id=item)

    if m := _NDE_RE.match(text):
        struct, method, cell = m.groups()
        return ParsedId("NDE", text, struct=struct, method=method, cell=cell)

    raise IdError(f"not a recognised artifact id: {artifact_id!r}")


def is_artifact_id(value: str) -> bool:
    """True when ``value`` is a well-formed artifact ID. Never raises."""
    try:
        parse(value)
    except (IdError, TypeError):
        return False
    return True


#: Matches artifact IDs embedded in free text. Used by the grounding gate to
#: find the citations a drafted sentence claims to carry. Deliberately broad:
#: anything it captures is then validated through ``parse``, so a near-miss is
#: rejected as an unresolvable citation rather than silently accepted.
CITATION_RE = re.compile(r"\b(?:NBI|NBE|IMG|REF|NDE)-[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)+\b")


def extract_ids(text: str) -> list[str]:
    """Return the valid artifact IDs appearing in ``text``, in order, deduped."""
    seen: list[str] = []
    for candidate in CITATION_RE.findall(text or ""):
        if is_artifact_id(candidate) and candidate not in seen:
            seen.append(candidate)
    return seen


def photo_key_from_filename(filename: str) -> str:
    """Derive a stable photo key from an uploaded file's name.

    The key is the file's stem, lower-cased and stripped of anything outside
    ``[a-z0-9_]``. It comes from the name the inspector gave the file, so the
    same photo re-uploaded produces the same key and therefore the same artifact
    ID — upload order never enters into it.

    Raises:
        IdError: if the stem yields no usable characters, or collides with the
            region suffix form.
    """
    import os

    stem = os.path.splitext(os.path.basename(str(filename or "")))[0]
    key = _token(stem, what="photo file name", lower=True)
    if re.fullmatch(r"r\d+", key):
        raise IdError(
            f"photo file name {filename!r} yields the key {key!r}, which collides "
            "with the region suffix form 'r<n>'; rename the file"
        )
    return key

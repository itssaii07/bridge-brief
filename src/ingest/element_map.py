"""AASHTO element number -> NBI component classification.

The contradiction engine compares an NBI *component* rating against NBE
*element* condition states, so it needs to know which elements roll up into
which component. AASHTO's national bridge element numbering makes this mostly
mechanical: the 1xx series is superstructure, the 2xx series substructure, the
low numbers decks and slabs.

Two deliberate exclusions, both of which would otherwise bias the engine:

* **Protective-system elements** (the 5xx defect and 8xx protection series)
  describe coatings, wearing surfaces and joint seals rather than the structural
  condition of a component. A bridge whose deck sealant is worn out is not a
  bridge whose deck is structurally poor, and rolling those quantities in would
  manufacture contradictions that are not there.
* **Unmapped elements** are stored, remain citable artifacts, and are counted and
  reported — but do not drive a contradiction. Extending the table from real data
  is preferable to guessing at an element's component from memory.

This module lives in the ingest layer because it is a property of the source
vocabulary, not of the analysis. The analysis layer imports it; the reverse never
happens.
"""

from __future__ import annotations

#: Deck and slab elements: deck surfaces, slabs, and the elements carried on them.
DECK_ELEMENTS = frozenset({
    12,   # reinforced concrete deck
    13,   # prestressed concrete deck
    15,   # prestressed concrete top flange
    16,   # reinforced concrete top flange
    17,   # timber deck
    18,   # orthotropic deck
    22,   # steel deck (corrugated/orthotropic/etc.)
    28,   # steel deck - open grid
    29,   # steel deck - concrete filled grid
    30,   # steel deck - corrugated/orthotropic/etc.
    31,   # timber deck
    38,   # reinforced concrete slab
    54,   # timber slab
    60,   # other slab
    65,   # other deck
})

#: Superstructure elements: girders, beams, trusses, arches, floor systems,
#: bearings and the members that carry load to the substructure.
SUPERSTRUCTURE_ELEMENTS = frozenset({
    102, 104, 105, 106, 107, 109, 110, 111, 112, 113, 115, 116, 117,
    120, 121, 126, 131, 135, 136,
    140, 141, 142, 143, 144, 145, 146, 147, 148, 149,
    152, 154, 155, 156, 161, 162,
})

#: Substructure elements: columns, pier walls, abutments, pile caps, footings.
SUBSTRUCTURE_ELEMENTS = frozenset({
    155,
    202, 203, 204, 205, 206, 207, 208, 210, 211, 212, 213,
    215, 216, 217, 218, 219, 220,
    225, 226, 227, 228, 229, 231, 233, 234, 235,
    240, 241,
})

#: Culvert elements. These also appear in the substructure set in some state
#: practices; for contradiction purposes a culvert structure is compared against
#: NBI item 62 and these are the elements that matter.
CULVERT_ELEMENTS = frozenset({240, 241})

#: Elements that legitimately appear under more than one component in different
#: state practices. They are classified to one component (see below) and the
#: ambiguity is recorded on any finding they contribute to, so a reviewer can see
#: that the roll-up was a judgement call.
AMBIGUOUS_ELEMENTS = frozenset(
    (SUPERSTRUCTURE_ELEMENTS & SUBSTRUCTURE_ELEMENTS)
    | (SUBSTRUCTURE_ELEMENTS & CULVERT_ELEMENTS)
)

#: Protective-system and defect elements, excluded from component roll-ups.
#: The 5xx series is wearing surfaces and protective coatings; the 8xx series is
#: agency-defined defect elements. Both describe protection rather than the
#: structural condition of a component. Numbers outside these bands that are not
#: in the tables above stay unmapped (``None``) rather than being swept in here.
PROTECTIVE_SERIES = (500, 800)


def is_protective(elem_num: int) -> bool:
    """True for defect and protective-system elements, which we never roll up."""
    return any(base <= elem_num < base + 100 for base in PROTECTIVE_SERIES)


def classify_element(elem_num: int | None) -> str | None:
    """Return the NBI component an AASHTO element rolls up into.

    Returns one of ``deck``, ``superstructure``, ``substructure``, ``culvert``,
    the sentinel ``protective`` for excluded protective/defect elements, or
    ``None`` when the element is not in the table — which is a reportable state,
    not an error.

    Order matters: culvert is checked before substructure so that elements 240
    and 241 classify as culvert, and the ambiguous element 155 resolves to
    superstructure (documented in ASSUMPTIONS.md E2).
    """
    if elem_num is None:
        return None
    try:
        number = int(elem_num)
    except (TypeError, ValueError):
        return None

    if is_protective(number):
        return "protective"
    if number in DECK_ELEMENTS:
        return "deck"
    if number in SUPERSTRUCTURE_ELEMENTS:
        return "superstructure"
    if number in CULVERT_ELEMENTS:
        return "culvert"
    if number in SUBSTRUCTURE_ELEMENTS:
        return "substructure"
    return None


def is_ambiguous(elem_num: int | None) -> bool:
    """True when this element is claimed by more than one component."""
    try:
        return int(elem_num) in AMBIGUOUS_ELEMENTS
    except (TypeError, ValueError):
        return False

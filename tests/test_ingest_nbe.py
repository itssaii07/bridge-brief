"""Tests for the NBE element extractor — the project's largest format guess.

The XML strings below are hand-written test fixtures expressing the *shapes* the
parser claims to handle (ASSUMPTIONS.md section D). They are not NBE data and are
never used to produce a brief or a metric. Their job is to pin the parser's
behaviour so that, when a real file arrives and the shape differs, the fix is
visibly localised to ``extract_elements``.
"""

from xml.etree import ElementTree as ET

import pytest

from src.ingest import nbe
from src.ingest.nbe import NbeFormatError, extract_elements

# Layout A: four named condition-state fields as attributes on each element.
FIELDS_ATTRS = """<?xml version="1.0"?>
<NBE>
  <Structure STRUCNUM="000013450">
    <Element EN="12" ELEMNAME="RC Deck" ELEMSCALECODE="SQFT" ELEMQUANTITY="8000"
             ELEMQTYSTATE1="7000" ELEMQTYSTATE2="660" ELEMQTYSTATE3="340" ELEMQTYSTATE4="0"/>
    <Element EN="205" ELEMNAME="RC Column" ELEMSCALECODE="EA" ELEMQUANTITY="8"
             ELEMQTYSTATE1="8" ELEMQTYSTATE2="0" ELEMQTYSTATE3="0" ELEMQTYSTATE4="0"/>
  </Structure>
</NBE>
"""

# Layout A again, but every value as a child element instead of an attribute.
FIELDS_CHILDREN = """<?xml version="1.0"?>
<NBE>
  <Structure>
    <STRUCNUM>13450</STRUCNUM>
    <Element>
      <EN>12</EN><ELEMNAME>RC Deck</ELEMNAME><UNITS>SQFT</UNITS>
      <QUANTITY>8000</QUANTITY>
      <CS1>7000</CS1><CS2>660</CS2><CS3>340</CS3><CS4>0</CS4>
    </Element>
  </Structure>
</NBE>
"""

# Layout B: repeated condition-state child records.
REPEATED_CHILDREN = """<?xml version="1.0"?>
<NBE>
  <Structure STRUCNUM="13450">
    <Element EN="12" ELEMNAME="RC Deck" UNITS="SQFT" QUANTITY="8000">
      <ConditionState STATENUM="1" QTY="7000"/>
      <ConditionState STATENUM="2" QTY="660"/>
      <ConditionState STATENUM="3" QTY="340"/>
      <ConditionState STATENUM="4" QTY="0"/>
    </Element>
  </Structure>
</NBE>
"""

# A namespaced document with a deeper nesting level between structure and element.
NAMESPACED_NESTED = """<?xml version="1.0"?>
<nbe:Export xmlns:nbe="http://example.org/nbe">
  <nbe:Structure nbe:StructureNumber="13450">
    <nbe:Inspection Date="2023-05-01">
      <nbe:BridgeElement nbe:ElementNumber="12" nbe:Quantity="8000"
                         nbe:CS1="7000" nbe:CS2="660" nbe:CS3="340" nbe:CS4="0"/>
    </nbe:Inspection>
  </nbe:Structure>
</nbe:Export>
"""

UNRECOGNISED = """<?xml version="1.0"?>
<SomethingElse><Row a="1" b="2"/></SomethingElse>
"""


def only(records):
    assert len(records) >= 1
    return records[0]


class TestLayouts:
    @pytest.mark.parametrize(
        "xml,layout",
        [(FIELDS_ATTRS, "fields"), (FIELDS_CHILDREN, "fields"),
         (REPEATED_CHILDREN, "children"), (NAMESPACED_NESTED, "fields")],
    )
    def test_every_claimed_shape_yields_the_same_element(self, xml, layout):
        records, stats = extract_elements(ET.fromstring(xml))
        deck = only([r for r in records if r.elem_num == 12])
        assert deck.struct_raw.strip() in {"13450", "000013450"}
        assert deck.total_qty == 8000
        assert deck.cs_qty == {1: 7000.0, 2: 660.0, 3: 340.0, 4: 0.0}
        assert deck.layout == layout
        assert stats["element_nodes"] >= 1

    def test_attributes_and_children_are_both_accepted(self):
        a, _ = extract_elements(ET.fromstring(FIELDS_ATTRS))
        b, _ = extract_elements(ET.fromstring(FIELDS_CHILDREN))
        assert only([r for r in a if r.elem_num == 12]).cs_qty == \
               only([r for r in b if r.elem_num == 12]).cs_qty

    def test_multiple_elements_on_one_structure(self):
        records, _ = extract_elements(ET.fromstring(FIELDS_ATTRS))
        assert sorted(r.elem_num for r in records) == [12, 205]

    def test_namespaces_are_ignored(self):
        records, _ = extract_elements(ET.fromstring(NAMESPACED_NESTED))
        assert len(records) == 1 and records[0].elem_num == 12


class TestTotalQuantity:
    def test_published_total_is_used_and_recorded_as_such(self):
        _, stats = extract_elements(ET.fromstring(FIELDS_ATTRS))
        assert stats["total_qty_published"] == 2
        assert stats["total_qty_summed"] == 0

    def test_absent_total_is_summed_from_the_states_and_recorded_as_such(self):
        xml = """<NBE><Structure STRUCNUM="13450">
          <Element EN="12" CS1="10" CS2="5" CS3="5" CS4="0"/>
        </Structure></NBE>"""
        records, stats = extract_elements(ET.fromstring(xml))
        assert records[0].total_qty == 20
        assert stats["total_qty_summed"] == 1


class TestRejection:
    def test_element_without_a_structure_number_is_counted_not_invented(self):
        xml = """<NBE><Wrapper><Element EN="12" CS1="1"/></Wrapper></NBE>"""
        records, stats = extract_elements(ET.fromstring(xml))
        assert records == []
        assert stats["missing_struct"] == 1

    def test_element_without_an_element_number_is_counted(self):
        xml = """<NBE><Structure STRUCNUM="13450"><Element CS1="1"/></Structure></NBE>"""
        records, stats = extract_elements(ET.fromstring(xml))
        assert records == [] and stats["missing_elem_num"] == 1

    def test_element_with_no_condition_states_is_counted(self):
        xml = """<NBE><Structure STRUCNUM="13450">
                   <Element EN="12" QUANTITY="100"/></Structure></NBE>"""
        records, stats = extract_elements(ET.fromstring(xml))
        assert records == [] and stats["no_condition_states"] == 1

    def test_unrecognised_document_yields_nothing_rather_than_guessing(self):
        records, stats = extract_elements(ET.fromstring(UNRECOGNISED))
        assert records == []
        assert stats["element_nodes"] == 0

    def test_quantities_that_do_not_parse_become_none_not_zero(self):
        xml = """<NBE><Structure STRUCNUM="13450">
          <Element EN="12" QUANTITY="n/a" CS1="7000" CS3="340"/></Structure></NBE>"""
        records, _ = extract_elements(ET.fromstring(xml))
        # QUANTITY was unparseable, so the total falls back to the state sum.
        assert records[0].total_qty == 7340
        assert 2 not in records[0].cs_qty  # absent state stays absent, not zero


class TestFileLevel:
    def test_unparsed_file_fails_loudly_and_points_at_the_one_change_point(self, tmp_path):
        from src.db import connect

        path = tmp_path / "AZ.xml"
        path.write_text("<NBE><Structure STRUCNUM='1'><Element EN='12'/></Structure></NBE>")
        conn = connect(":memory:")
        with pytest.raises(NbeFormatError) as exc:
            nbe.ingest_file(conn, path, 2023, log=lambda *a: None)
        message = str(exc.value)
        assert "extract_elements" in message and "ASSUMPTIONS.md" in message
        # The failure is recorded, not swallowed.
        row = conn.execute("SELECT status FROM ingest_log").fetchone()
        assert row["status"] == "failed"

    def test_state_is_taken_from_the_directory_name(self, tmp_path):
        path = tmp_path / "nbe" / "2023" / "IA" / "elements.xml"
        path.parent.mkdir(parents=True)
        path.write_text(FIELDS_ATTRS)
        assert nbe._state_from_path(path) == "IA"

    def test_missing_directory_reports_the_expected_layout(self, tmp_path):
        from src.db import DataUnavailable

        with pytest.raises(DataUnavailable) as exc:
            nbe.find_files(2023, root=tmp_path)
        assert "AL, AZ, IA" in str(exc.value)

    def test_ingest_is_idempotent_and_ids_are_stable(self, tmp_path):
        from src.db import connect

        path = tmp_path / "nbe" / "2023" / "AL" / "elements.xml"
        path.parent.mkdir(parents=True)
        path.write_text(FIELDS_ATTRS)
        conn = connect(":memory:")

        first = nbe.ingest_file(conn, path, 2023, log=lambda *a: None)
        ids_first = [r[0] for r in conn.execute("SELECT artifact_id FROM elements ORDER BY 1")]
        nbe.ingest_file(conn, path, 2023, force=True, log=lambda *a: None)
        ids_second = [r[0] for r in conn.execute("SELECT artifact_id FROM elements ORDER BY 1")]

        assert ids_first == ids_second          # re-running changes nothing
        assert "NBE-013450-2023-12-cs3" in ids_first
        assert first["structures"] == 1

        # A second run without --force skips the unchanged file entirely.
        assert nbe.ingest_file(conn, path, 2023, log=lambda *a: None)["skipped"] is True

    def test_elements_are_classified_into_components_on_ingest(self, tmp_path):
        from src.db import connect

        path = tmp_path / "nbe" / "2023" / "AL" / "elements.xml"
        path.parent.mkdir(parents=True)
        path.write_text(FIELDS_ATTRS)
        conn = connect(":memory:")
        nbe.ingest_file(conn, path, 2023, log=lambda *a: None)
        classes = dict(conn.execute("SELECT elem_num, elem_class FROM elements GROUP BY elem_num"))
        assert classes == {12: "deck", 205: "substructure"}

    def test_every_ingested_element_is_immediately_resolvable(self, tmp_path):
        from src.db import connect
        from src.store import resolve_artifact

        path = tmp_path / "nbe" / "2023" / "AL" / "elements.xml"
        path.parent.mkdir(parents=True)
        path.write_text(FIELDS_ATTRS)
        conn = connect(":memory:")
        nbe.ingest_file(conn, path, 2023, log=lambda *a: None)
        row = resolve_artifact(conn, "NBE-013450-2023-12-cs3")
        assert row is not None
        assert "condition state 3" in row["summary"]
        assert row["source_path"].endswith("elements.xml")

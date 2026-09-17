"""Tests for NBI header mapping and row parsing.

All inputs are hand-written strings in this file. Nothing here reads from
``data/`` and nothing here is used to produce a brief or a metric.
"""

import csv
import io

import pytest

from src.ingest import nbi
from src.ingest.nbi import NbiFormatError, RowRejected

# A header row in the modern FHWA naming, deliberately in an order that is NOT
# the order the parser lists its fields in — position must never matter.
MODERN_HEADER = [
    "STATE_CODE_001", "COUNTY_CODE_003", "FEATURES_DESC_006A", "FACILITY_CARRIED_007",
    "STRUCTURE_NUMBER_008", "LAT_016", "LONG_017", "YEAR_BUILT_027",
    "DECK_COND_058", "SUPERSTRUCTURE_COND_059", "SUBSTRUCTURE_COND_060", "CULVERT_COND_062",
]

# The older spelling, without item-number suffixes and with fewer columns.
LEGACY_HEADER = [
    "STRUCTURE_NUMBER", "STATE_CODE", "DECK_COND", "SUPERSTRUCTURE_COND", "SUBSTRUCTURE_COND",
]


def read_rows(text: str) -> list[list[str]]:
    """Parse with the real NBI dialect — single-quote text qualifier."""
    return list(csv.reader(io.StringIO(text), **nbi.NBI_DIALECT))


class TestHeaderResolution:
    def test_modern_header_maps_every_field(self):
        mapping = nbi.resolve_headers(MODERN_HEADER)
        assert mapping["struct_raw"] == 4
        assert mapping["deck"] == 8
        assert mapping["culvert"] == 11
        assert mapping["feature_crossed"] == 2   # 006A: trailing letter tolerated

    def test_legacy_header_without_item_numbers_maps_via_synonyms(self):
        mapping = nbi.resolve_headers(LEGACY_HEADER)
        assert mapping["struct_raw"] == 0
        assert mapping["substructure"] == 4
        assert "culvert" not in mapping  # absent optional column stays absent

    def test_column_order_is_irrelevant(self):
        shuffled = list(reversed(MODERN_HEADER))
        mapping = nbi.resolve_headers(shuffled)
        assert shuffled[mapping["deck"]] == "DECK_COND_058"
        assert shuffled[mapping["struct_raw"]] == "STRUCTURE_NUMBER_008"

    def test_missing_required_column_fails_loudly_and_names_it(self):
        header = [h for h in MODERN_HEADER if h != "DECK_COND_058"]
        with pytest.raises(NbiFormatError) as exc:
            nbi.resolve_headers(header)
        message = str(exc.value)
        assert "deck" in message
        assert "SUPERSTRUCTURE_COND_059" in message  # lists what it actually saw

    def test_empty_header_is_fatal(self):
        with pytest.raises(NbiFormatError):
            nbi.resolve_headers([])


class TestRatingParsing:
    @pytest.mark.parametrize("raw,expected", [("7", 7), ("0", 0), ("9", 9), (" 5 ", 5)])
    def test_digits_parse(self, raw, expected):
        value, kept = nbi.parse_rating(raw)
        assert value == expected
        assert kept == raw.strip()

    @pytest.mark.parametrize("raw", ["N", "", "  ", "NA", "-", "10", None])
    def test_everything_else_is_null_and_never_zero(self, raw):
        value, kept = nbi.parse_rating(raw)
        assert value is None
        assert kept == ("" if raw is None else raw.strip())

    def test_original_characters_are_always_preserved(self):
        assert nbi.parse_rating("N")[1] == "N"


class TestCoordinateParsing:
    def test_packed_latitude_converts_to_decimal_degrees(self):
        # 40 deg 25 min 12.34 sec
        assert nbi.parse_coordinate("40251234", is_longitude=False) == pytest.approx(40.42009, abs=1e-4)

    def test_packed_longitude_is_negated_for_the_western_hemisphere(self):
        value = nbi.parse_coordinate("0792512340", is_longitude=True)
        assert value == pytest.approx(-79.42009, abs=1e-4)

    @pytest.mark.parametrize("raw", ["", "00000000", "abc", "99999999", "40991234", None])
    def test_unusable_coordinates_return_none_rather_than_a_wrong_point(self, raw):
        assert nbi.parse_coordinate(raw, is_longitude=False) is None


class TestRowParsing:
    def test_a_full_row_parses(self):
        text = (
            "'01','073','CREEK','SR 12','000000000013450',"
            "'40251234','0792512340','1974','7','6','5','N'\n"
        )
        row = read_rows(text)[0]
        parsed = nbi.parse_row(row, nbi.resolve_headers(MODERN_HEADER))
        assert parsed.struct_norm == "013450"
        assert parsed.struct_raw == "000000000013450"
        assert parsed.state_code == "01"
        assert parsed.facility == "SR 12"
        assert parsed.year_built == 1974
        assert parsed.ratings["deck"] == (7, "7")
        assert parsed.ratings["substructure"] == (5, "5")
        # Item 62 is 'N' on a non-culvert structure: NULL, not zero.
        assert parsed.ratings["culvert"] == (None, "N")

    def test_single_quote_text_qualifier_is_honoured(self):
        """A comma inside a quoted field must not split the row."""
        text = "'01','073','MAIN ST, EAST','SR 12','13450','','','','7','7','7','N'\n"
        row = read_rows(text)[0]
        parsed = nbi.parse_row(row, nbi.resolve_headers(MODERN_HEADER))
        assert parsed.feature_crossed == "MAIN ST, EAST"
        assert parsed.struct_norm == "013450"

    def test_padding_variants_join_to_the_same_structure(self):
        header = nbi.resolve_headers(LEGACY_HEADER)
        keys = set()
        for spelling in ["13450", "013450", "  13450  ", "0000013450"]:
            row = read_rows(f"'{spelling}','01','7','7','7'\n")[0]
            keys.add(nbi.parse_row(row, header).struct_norm)
        assert keys == {"013450"}

    def test_blank_structure_number_is_rejected_with_a_reason(self):
        row = read_rows("'','01','7','7','7'\n")[0]
        with pytest.raises(RowRejected) as exc:
            nbi.parse_row(row, nbi.resolve_headers(LEGACY_HEADER))
        assert "structure number" in str(exc.value)

    def test_unusable_structure_number_is_rejected_not_guessed(self):
        row = read_rows("'0000','01','7','7','7'\n")[0]
        with pytest.raises(RowRejected):
            nbi.parse_row(row, nbi.resolve_headers(LEGACY_HEADER))

    def test_short_row_yields_nulls_rather_than_an_index_error(self):
        row = ["13450", "01", "7"]  # truncated
        parsed = nbi.parse_row(row, nbi.resolve_headers(LEGACY_HEADER))
        assert parsed.ratings["deck"] == (7, "7")
        assert parsed.ratings["superstructure"] == (None, "")

    def test_implausible_year_built_is_dropped(self):
        text = "'01','073','C','F','13450','','','3025','7','7','7','N'\n"
        row = read_rows(text)[0]
        parsed = nbi.parse_row(row, nbi.resolve_headers(MODERN_HEADER))
        assert parsed.year_built is None


class TestMissingData:
    def test_absent_year_directory_reports_where_to_put_the_files(self, tmp_path):
        from src.db import DataUnavailable

        with pytest.raises(DataUnavailable) as exc:
            nbi.find_files(2023, root=tmp_path)
        assert "2023" in str(exc.value) and "HANDOFF" in str(exc.value)

    def test_empty_year_directory_says_so_clearly(self, tmp_path):
        from src.db import DataUnavailable

        (tmp_path / "nbi" / "2023").mkdir(parents=True)
        with pytest.raises(DataUnavailable) as exc:
            nbi.find_files(2023, root=tmp_path)
        assert "no .txt or .csv" in str(exc.value)

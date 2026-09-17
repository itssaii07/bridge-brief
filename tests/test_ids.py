"""Tests for the artifact ID system and structure-number normalisation.

Pure logic only — no data files are touched.
"""

import pytest

from src import ids
from src.ids import IdError


class TestNormaliseStruct:
    @pytest.mark.parametrize(
        "raw",
        ["13450", "013450", "0013450", " 13450 ", "13450   ", "13 450", "013450\t"],
    )
    def test_padding_variants_agree(self, raw):
        """Every spelling a state might use collapses to one join key."""
        assert ids.normalise_struct(raw) == "013450"

    def test_letters_are_upper_cased_and_kept(self):
        assert ids.normalise_struct("ab12cd") == "AB12CD"

    def test_long_numeric_ids_keep_their_natural_width(self):
        assert ids.normalise_struct("000123456789") == "123456789"

    def test_punctuation_is_removed(self):
        # Structure numbers in some states carry separators; they are dropped so
        # that artifact IDs (hyphen-delimited) stay unambiguous.
        assert ids.normalise_struct("01-34/50") == "013450"

    @pytest.mark.parametrize("bad", [None, "", "   ", "---", "0000"])
    def test_unusable_values_are_reported_not_guessed(self, bad):
        with pytest.raises(IdError):
            ids.normalise_struct(bad)


class TestConstructors:
    def test_nbi_matches_the_documented_example(self):
        assert ids.nbi_id("013450", 2023, "deck") == "NBI-013450-2023-deck"

    def test_nbe_matches_the_documented_example(self):
        assert ids.nbe_id("013450", 2023, 12, 3) == "NBE-013450-2023-12-cs3"

    def test_img_and_region_match_the_documented_examples(self):
        img = ids.img_id("013450", "p03")
        assert img == "IMG-013450-p03"
        assert ids.region_id(img, 2) == "IMG-013450-p03-r2"

    def test_ref_matches_the_documented_example(self):
        assert ids.ref_id("codebrim", "00412") == "REF-codebrim-00412"

    def test_nde_form_is_constructible_though_unpopulated(self):
        assert ids.nde_id("013450", "gpr", "a7") == "NDE-013450-gpr-a7"

    def test_ids_are_independent_of_input_spelling(self):
        assert ids.nbi_id("13450", "2023", "DECK") == ids.nbi_id("0013450", 2023, "deck")

    def test_unknown_component_is_rejected(self):
        with pytest.raises(IdError):
            ids.nbi_id("013450", 2023, "railing")

    def test_invalid_condition_state_is_rejected(self):
        with pytest.raises(IdError):
            ids.nbe_id("013450", 2023, 12, 5)

    def test_photo_key_colliding_with_region_suffix_is_rejected(self):
        with pytest.raises(IdError):
            ids.img_id("013450", "r3")

    def test_region_of_a_region_is_rejected(self):
        with pytest.raises(IdError):
            ids.region_id("IMG-013450-p03-r2", 1)

    def test_unknown_corpus_is_rejected(self):
        with pytest.raises(IdError):
            ids.ref_id("imagenet", "1")


class TestRoundTrip:
    def test_nbi_round_trips(self):
        for comp in ids.COMPONENTS:
            p = ids.parse(ids.nbi_id("013450", 2023, comp))
            assert (p.kind, p.struct, p.year, p.component) == ("NBI", "013450", 2023, comp)

    def test_nbe_round_trips(self):
        for cs in ids.CONDITION_STATES:
            p = ids.parse(ids.nbe_id("013450", 2025, 12, cs))
            assert (p.kind, p.elem_num, p.cs, p.year) == ("NBE", 12, cs, 2025)

    def test_image_round_trips_and_region_is_none(self):
        p = ids.parse(ids.img_id("013450", "p03"))
        assert (p.kind, p.struct, p.photo, p.region) == ("IMG", "013450", "p03", None)

    def test_region_round_trips(self):
        p = ids.parse(ids.region_id(ids.img_id("013450", "p03"), 7))
        assert (p.photo, p.region) == ("p03", 7)

    def test_reference_round_trips_and_is_flagged_as_reference(self):
        p = ids.parse(ids.ref_id("codebrim", "00412"))
        assert p.is_reference and p.corpus == "codebrim" and p.item_id == "00412"

    def test_nde_round_trips(self):
        p = ids.parse(ids.nde_id("013450", "gpr", "a7"))
        assert (p.kind, p.method, p.cell) == ("NDE", "gpr", "a7")

    def test_underscored_photo_key_round_trips(self):
        p = ids.parse(ids.img_id("013450", "north_abut_02"))
        assert p.photo == "north_abut_02" and p.region is None

    @pytest.mark.parametrize(
        "bad",
        ["", "NBI-013450-2023", "NBI-013450-23-deck", "NBE-013450-2023-12-cs9",
         "XYZ-1-2", "not an id", "NBI-013450-2023-railing"],
    )
    def test_malformed_ids_are_rejected(self, bad):
        with pytest.raises(IdError):
            ids.parse(bad)
        assert ids.is_artifact_id(bad) is False


class TestDeterminism:
    def test_finding_ids_depend_only_on_what_the_finding_is_about(self):
        a = ids.finding_id("contradiction", "13450", 2023, "deck")
        b = ids.finding_id("contradiction", "013450", "2023", "DECK")
        assert a == b
        assert a != ids.finding_id("contradiction", "013450", 2023, "substructure")

    def test_brief_and_sentence_ids(self):
        b = ids.brief_id("013450", 2023, 2)
        assert b == "BRIEF-013450-2023-v2"
        assert ids.sentence_id(b, 4) == "BRIEF-013450-2023-v2-s4"


class TestCitationExtraction:
    def test_extracts_valid_ids_in_order_without_duplicates(self):
        text = ("Deck rated 7 [NBI-013450-2023-deck] but 340 sq ft in CS3 "
                "[NBE-013450-2023-12-cs3]; see [NBI-013450-2023-deck].")
        assert ids.extract_ids(text) == [
            "NBI-013450-2023-deck",
            "NBE-013450-2023-12-cs3",
        ]

    def test_near_misses_are_not_accepted_as_citations(self):
        assert ids.extract_ids("NBI-013450-2023-railing and NBE-013450-2023-12-cs7") == []

    def test_empty_and_none_are_safe(self):
        assert ids.extract_ids("") == []
        assert ids.extract_ids(None) == []


class TestPhotoKey:
    @pytest.mark.parametrize(
        "name,expected",
        [("P03.JPG", "p03"), ("/tmp/north abutment 2.png", "northabutment2"),
         ("deck_crack_01.jpeg", "deck_crack_01")],
    )
    def test_key_is_derived_from_the_name_not_upload_order(self, name, expected):
        assert ids.photo_key_from_filename(name) == expected

    def test_unusable_file_name_is_reported(self):
        with pytest.raises(IdError):
            ids.photo_key_from_filename("...")

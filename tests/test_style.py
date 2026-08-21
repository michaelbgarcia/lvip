"""Appearance capture (/DA) and house-style derivation."""
import pytest

from acrf_parser import style as S
from acrf_parser.extract import parse_da


# --- /DA parsing -----------------------------------------------------------
@pytest.mark.parametrize("da,expected", [
    ("0.85 0.1 0.1 rg /Helv 8.0 Tf",
     {"text_color": (0.85, 0.1, 0.1), "font_name": "Helv", "font_size": 8.0}),
    ("0 0 1 rg /Arial 10 Tf",
     {"text_color": (0.0, 0.0, 1.0), "font_name": "Arial", "font_size": 10.0}),
    ("0.5 g /TiRo 9 Tf",
     {"text_color": (0.5, 0.5, 0.5), "font_name": "TiRo", "font_size": 9.0}),
    ("0 0 0 1 k /Helv 8 Tf",
     {"text_color": (0.0, 0.0, 0.0), "font_name": "Helv", "font_size": 8.0}),
    ("/Helv 0 Tf",                                  # 0 = auto-size, not a guess
     {"text_color": None, "font_name": "Helv", "font_size": 0.0}),
    ("1 0 0 rg",                                    # colour, no font
     {"text_color": (1.0, 0.0, 0.0), "font_name": "", "font_size": 0.0}),
    ("", {"text_color": None, "font_name": "", "font_size": 0.0}),
])
def test_parse_da(da, expected):
    assert parse_da(da) == expected


def test_cmyk_uses_the_standard_conversion():
    """R = (1-C)(1-K), not 1-(C+K) - the additive form loses the K interaction."""
    assert parse_da("0 0.9 0.9 0.1 k")["text_color"] == (0.9, 0.09, 0.09)


def test_appearance_is_captured_from_the_pdf(doc):
    """`annot.colors` is empty for FreeText; the styling is in /DA."""
    a = next(a for a in doc.iter_annotations() if a.text == "BRTHDTC")
    assert a.text_color == (0.85, 0.1, 0.1)
    assert (a.font_name, a.font_size) == ("Helv", 8.0)
    assert all(x.text_color is not None for x in doc.iter_annotations())


# --- house style -----------------------------------------------------------
@pytest.fixture(scope="session")
def house(doc):
    return S.derive_house_style(doc)


def test_default_rule_is_measured_from_every_annotation(house, doc):
    assert house.default.samples == len(list(doc.iter_annotations())) == 17
    assert house.default.text_color == (0.85, 0.1, 0.1)
    assert house.default.color_agreement == 1.0
    assert house.default.placement == "right_of_field"
    assert house.default.placement_agreement == 1.0


def test_the_settled_rule_is_the_one_to_apply(house):
    """VARIABLE markup: 7 samples, unanimous on colour, size and placement."""
    rule = house.for_type("VARIABLE")
    assert rule.scope == "VARIABLE" and rule.samples == 7
    assert (rule.font_name, rule.font_size) == ("Helv", 8.0)
    assert rule.size_agreement == 1.0 and rule.placement == "right_of_field"
    assert rule.settled


def test_disagreement_surfaces_instead_of_averaging(house):
    """Domain headers are 8pt on three pages and 9pt on two.

    The answer is "the corpus does not agree", not 8.5pt - a size nobody chose
    and which no reviewer would recognise as wrong.
    """
    rule = house.by_type["DOMAIN_HEADER"]
    assert rule.font_size == 8.0 and rule.size_agreement == 0.5
    assert not rule.settled
    assert "corpus does not agree; needs a human decision" in rule.evidence


def test_unsettled_rules_are_listed_for_review(house):
    scopes = {r.scope for r in house.unsettled()}
    assert "DOMAIN_HEADER" in scopes and "VARIABLE" not in scopes


def test_thin_evidence_falls_back_to_the_corpus_default(house):
    """A type seen once must not out-vote a convention seen seventeen times."""
    assert house.by_type["NOT_SUBMITTED"].samples == 1
    assert house.for_type("NOT_SUBMITTED") is house.default
    assert house.for_type("VARIABLE") is house.by_type["VARIABLE"]


def test_an_unseen_type_gets_the_default(house):
    assert house.for_type("DERIVATION_RULE") is house.default


def test_placement_is_measured_only_where_there_is_a_field(house):
    """Colour and font come from every annotation; placement needs a link."""
    rule = house.by_type["DOMAIN_HEADER"]
    assert rule.samples == 4 and rule.placement_samples == 0
    assert house.by_type["VARIABLE"].placement_samples == 7


def test_offsets_are_page_relative(house):
    rule = house.for_type("VARIABLE")
    assert 0.0 < rule.offset_x_pct < 1.0
    assert abs(rule.offset_y_pct) < 1.0


def test_style_derives_across_a_corpus(doc, second_doc):
    """One sponsor, many studies: the conventions are counted over all of them."""
    house = S.derive_house_style([doc, second_doc])
    assert house.default.samples == 21          # 17 + 4
    assert len(house.documents) == 2
    assert house.for_type("VARIABLE").samples == 10


def test_summary(house):
    s = S.summarize_style(house)
    assert s["font"] == "Helv 8.0pt" and s["settled"] is True
    assert "DOMAIN_HEADER" in s["unsettled_scopes"]

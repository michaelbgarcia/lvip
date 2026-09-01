"""Phase 6b tests: markup that belongs to the form rather than to a field.

The regression these guard is a silent one. Domain headers and form-level
constants parsed and classified correctly and were then dropped, because every
mechanism after Phase 3 is keyed on a field id and they have no field. Nothing
errored - the annotated PDF simply came out missing the markup across the top of
every page.
"""
import pytest

from acrf_parser import anchors, annotations as ann
from acrf_parser.models import FIELD_SCOPE


# --- reading it off an annotated CRF ---------------------------------------
def test_a_page_can_carry_several_form_level_annotations(coloured_doc):
    """The case the one-domain-per-form model could not hold."""
    found = [a.text for a in coloured_doc.form_annotations(1)]
    assert found == ["DS=Disposition", "DM=Demographics", "DSCAT = PROTOCOL MILESTONE"]


def test_form_level_markup_is_ordered_left_to_right(coloured_doc):
    """The order it was drawn in, which is the order it will be drawn back in."""
    found = coloured_doc.form_annotations(1)
    assert [a.bbox.x0 for a in found] == sorted(a.bbox.x0 for a in found)


def test_a_domain_header_is_form_level_by_type(coloured_doc):
    a = next(a for a in coloured_doc.form_annotations(1) if a.text == "DS=Disposition")
    assert a.annot_type == ann.DOMAIN_HEADER
    assert "describes the form" in a.scope_evidence[0]


def test_a_form_level_constant_is_found_by_position(coloured_doc):
    """`DSCAT = PROTOCOL MILESTONE` is an ordinary CONSTANT_ASSIGNMENT - only
    where it sits says it belongs to the form."""
    a = next(a for a in coloured_doc.form_annotations(1)
             if a.text.startswith("DSCAT"))
    assert a.annot_type == ann.CONSTANT_ASSIGNMENT
    assert a.scope_evidence == ["unlinked markup above the first field on the page"]


def test_field_markup_is_not_swept_up(coloured_doc):
    """The position rule must not eat markup that simply failed to link."""
    texts = {a.text for a in coloured_doc.form_annotations()}
    assert "DSTERM" not in texts and "RFICDTC" not in texts
    assert all(a.scope == FIELD_SCOPE
               for a in coloured_doc.iter_annotations() if a.text == "DSTERM")


def test_a_cross_reference_is_left_out(doc):
    """Form-level in every other sense, but "See Page 7" is a fact about one
    document's pagination - learned as the form's markup it would be proposed
    for a study where page 7 is a different form."""
    assert not [a for a in doc.form_annotations() if a.annot_type == ann.CROSS_REFERENCE]


# --- the anchor ------------------------------------------------------------
def test_every_page_with_a_form_gets_an_anchor(blank_doc):
    assert [a.id for a in blank_doc.iter_anchors()] == [
        f"p{p.number}h" for p in blank_doc.pages if p.form_name]


def test_the_anchor_names_itself_as_a_header_not_a_question(blank_doc):
    a = blank_doc.anchor("p1h")
    assert a.text == "[form header] Demographics"


def test_the_anchor_is_a_starting_point_not_a_box(blank_doc):
    """Zero width on purpose: the first annotation lands *on* it and the rest
    chain rightwards, rather than everything being placed beside one box."""
    a = blank_doc.anchor("p1h")
    assert a.bbox.width == 0
    assert a.bbox.y0 < min(f.bbox.y0 for f in blank_doc.page(1).fields)


def test_the_anchor_lines_up_with_the_page_s_own_left_margin(blank_doc):
    page = blank_doc.page(1)
    assert page.anchor.bbox.x0 == pytest.approx(
        min(l.bbox.x0 for l in page.content_lines if l.text.strip()), abs=0.5)


def test_a_page_no_form_claims_gets_no_anchor():
    """Markup about a form nobody could name has nowhere to be filed."""
    from acrf_parser.models import Page
    assert anchors.build_anchor(Page(number=1, width=595, height=842, rotation=0,
                                     text="")) is None


# --- reporting -------------------------------------------------------------
def test_the_summary_says_which_form_carries_what(coloured_doc):
    s = anchors.summarize_form_level(coloured_doc)
    assert s["anchors"] == 2 and s["form_annotations"] == 4
    assert s["forms_with_several"] == 1
    assert s["by_form"]["Informed Consent"][0] == "DS=Disposition"

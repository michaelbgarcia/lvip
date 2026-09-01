"""Phase 2 tests: form identity, continuation pages, cross references."""
import pytest

from acrf_parser import forms
from acrf_parser.models import Annotation, BBox, Page, TextGroup


def test_every_page_gets_a_form(doc):
    assert all(p.form_name for p in doc.pages)
    assert [f.name for f in doc.forms] == [
        "Demographics", "Medical History", "Disposition", "Eligibility Criteria"]


def test_title_line_wins_and_is_cleaned(doc):
    """"Form: Demographics" names the form "Demographics", prefix stripped."""
    p = doc.page(1)
    assert p.form_name == "Demographics" and p.form_confidence == forms.CONF_TITLE_LINE
    assert "Form: Demographics" in p.form_evidence[0]


def test_continuation_page_joins_its_form(doc):
    """"Medical History (continued)" is page 2 of one form, not a second form."""
    mh = doc.form("Medical History")
    assert mh.pages == [2, 3] and mh.continuation_pages == [3]
    assert doc.page(2).is_continuation is False
    assert doc.page(3).is_continuation is True
    assert "title marked continued" in doc.page(3).form_evidence


def test_untitled_page_named_by_its_domain_annotation(doc):
    """The Disposition page prints no title at all - only DS=Disposition says so."""
    p = doc.page(4)
    assert p.form_name == "Disposition" and p.form_domain == "DS"
    assert p.form_confidence == forms.CONF_DOMAIN_ANNOT
    assert "domain header annotation DS=Disposition" in p.form_evidence


def test_domains_assigned(doc):
    assert {f.name: f.domain for f in doc.forms} == {
        "Demographics": "DM", "Medical History": "MH",
        "Disposition": "DS", "Eligibility Criteria": "EL"}


def test_cross_reference_resolved(doc):
    ref = doc.page(3).cross_references[0]
    assert (ref.target_page, ref.resolved, ref.target_form) == (2, True, "Medical History")


def test_dangling_cross_reference_is_kept_unresolved(doc):
    """"See Page 7" in a 5-page document is a finding about the PDF, not noise."""
    ref = doc.page(5).cross_references[0]
    assert ref.target_page == 7 and ref.resolved is False


def test_title_only_named_the_form_it_titles(doc):
    """A form title must not also be reported as a field on its own form."""
    assert "Form: Demographics" not in [f.raw_text for f in doc.page(1).fields]


# --- title cleanup ---------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Form: Demographics", "Demographics"),
    ("Form: Medical History (continued)", "Medical History"),
    ("CRF - Adverse Events", "Adverse Events"),
    ("Concomitant Medications cont'd", "Concomitant Medications"),
    ("Vital Signs [continued]", "Vital Signs"),
    ("Adverse   Events", "Adverse Events"),
])
def test_clean_title(raw, expected):
    assert forms.clean_title(raw) == expected


# --- inheritance, on synthetic pages ---------------------------------------
def _page(number, title=None, annots=(), height=842.0):
    p = Page(number=number, width=595, height=height, rotation=0, text="")
    if title:
        p.groups.append(TextGroup(page=number, text=title, bbox=BBox.of((60, 70, 300, 90)),
                                  region="BODY", role="SECTION_HEADER", size=13, bold=True))
    p.annotations = [Annotation(page=number, text=t, bbox=BBox.of((330, 20, 500, 40)))
                     for t in annots]
    return p


def test_untitled_page_inherits_from_the_page_before():
    pages = [_page(1, "Form: Adverse Events"), _page(2)]
    result = forms.detect_forms(pages)
    assert pages[1].form_name == "Adverse Events"
    assert pages[1].is_continuation and pages[1].form_confidence == forms.CONF_INHERITED
    assert result[0].pages == [1, 2]


def test_forward_cross_reference_resolves():
    """"See Page 3" on page 2 has to wait for page 3 to be named - which is why
    cross references are a second pass rather than a running decision."""
    pages = [_page(1, "Form: Demographics"), _page(2, annots=["See Page 3"]),
             _page(3, "Form: Laboratory")]
    forms.detect_forms(pages)
    assert pages[1].form_name == "Laboratory"
    assert pages[1].form_confidence == forms.CONF_CROSS_REF


def test_same_form_returning_later_is_one_form():
    """A study that interleaves modules can come back to a form; both runs are it."""
    pages = [_page(1, "Form: Vital Signs"), _page(2, "Form: Laboratory"),
             _page(3, "Form: Vital Signs")]
    result = forms.detect_forms(pages)
    vs = next(f for f in result if f.name == "Vital Signs")
    assert vs.pages == [1, 3] and vs.continuation_pages == [3]


def test_running_header_is_not_read_as_a_title(doc):
    """"STUDY XYZ-123" tops every page and names no form."""
    assert all(p.form_name != "STUDY XYZ-123" for p in doc.pages)


def test_source_records_which_signal_named_the_form(doc):
    """`source` is the signal; `evidence` is the sentence explaining it."""
    assert [f.source for f in doc.forms] == [
        forms.TITLE_LINE, forms.TITLE_LINE, forms.DOMAIN_ANNOTATION, forms.TITLE_LINE]
    assert doc.page(4).form_source == forms.DOMAIN_ANNOTATION


# --- against the real MSG CRF ----------------------------------------------
# The fixture above prints "Form: Demographics" at the top of the page, which is
# the easy case and the one this module was written against. A real CRF prints
# the study identification band there instead, and the form's own name below it.
def test_the_study_band_is_not_the_form_title(msg_run):
    """Every page of the MSG CRF opens with "CDISC Study CDISC01", set bold,
    above and to the left of the form's own name. Taking the topmost bold
    heading named all 22 pages after the study."""
    assert msg_run.blank.page(6).form_name == "DEMOGRAPHY"
    assert msg_run.blank.page(8).form_name == "MEDICAL AND SURGICAL HISTORY"
    assert not any("CDISC01" in p.form_name for p in msg_run.blank.pages)


def test_a_form_named_study_something_is_still_a_form(msg_run):
    """"Study" heads the identification band and also starts a real form name.

    What separates them is what follows: an identifier carries a code, a title
    carries a word.
    """
    assert msg_run.blank.page(19).form_name == "STUDY MEDICATION INVENTORY"


def test_page_n_of_m_is_a_continuation_marker(msg_run):
    """"CORNELL SCALE ... (CSDD) (PAGE 2 OF 2)" is one form over two pages."""
    names = [msg_run.blank.page(n).form_name for n in (14, 15)]
    assert names[0] == names[1] and names[0].startswith("CORNELL SCALE")
    assert "PAGE" not in names[0].upper().split("(")[-1]


def test_a_table_header_cell_is_not_a_title(msg_run):
    """Bold, short and centred describes a form title and also every cell of a
    table's header row. What separates them is that a title is alone on its line."""
    assert msg_run.blank.page(19).form_name != "Number of Tablets Dispensed"


def test_an_instruction_line_is_not_a_title(msg_run):
    """Centred, bold, near the top - and forty words long."""
    assert not msg_run.blank.page(14).form_name.startswith("Instructions")

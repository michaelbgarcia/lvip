"""Phases 4-5 tests: statement splitting and annotation classification."""
import pytest

from acrf_parser import annotations as ann
from acrf_parser.models import Annotation, BBox, Page


def _type(text, first=False):
    return ann.classify(text, first=first).annot_type


# --- classification, one case per type ------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("DM=Demographics", ann.DOMAIN_HEADER),
    ("MH = Medical History", ann.DOMAIN_HEADER),
    ("BRTHDTC", ann.VARIABLE),
    ("DM.BRTHDTC", ann.VARIABLE),
    ("MHENRF=ONGOING", ann.CONSTANT_ASSIGNMENT),
    ('CMDOSU = "mg"', ann.CONSTANT_ASSIGNMENT),
    ("RACEOTH when SUPPDM.QNAM=RACEOTH", ann.SUPP_QUALIFIER),
    ('SUPPDS.QVAL when QNAM = "PROTVER"', ann.SUPP_QUALIFIER),
    ("[NOT SUBMITTED]", ann.NOT_SUBMITTED),
    ("Not Submitted", ann.NOT_SUBMITTED),
    ("See Page 7", ann.CROSS_REFERENCE),
    ("see pg. 12", ann.CROSS_REFERENCE),
    ("AGE derived from BRTHDTC and RFSTDTC", ann.DERIVATION_RULE),
    ("If yes then record the start date", ann.DERIVATION_RULE),
    ("Record in local units only", ann.NOTE),
])
def test_classification(text, expected):
    assert _type(text) == expected


def test_every_fixture_annotation_classifies(doc):
    by_type = {}
    for a in doc.iter_annotations():
        by_type.setdefault(a.annot_type, []).append(a.text)
    assert by_type[ann.DOMAIN_HEADER] == ["DM=Demographics", "MH=Medical History",
                                          "DS=Disposition", "EL=Eligibility"]
    assert by_type[ann.CROSS_REFERENCE] == ["See Page 2", "See Page 7"]
    assert by_type[ann.NOT_SUBMITTED] == ["[NOT SUBMITTED]"]
    assert by_type[ann.CONSTANT_ASSIGNMENT] == ["MHENRF=ONGOING"]
    assert len(by_type[ann.VARIABLE]) == 7 and len(by_type[ann.SUPP_QUALIFIER]) == 2
    assert all(a.type_confidence > 0 and a.type_evidence for a in doc.iter_annotations())


def test_supp_qualifier_is_parsed_not_just_labelled(doc):
    a = next(a for a in doc.iter_annotations() if a.text.startswith("RACEOTH"))
    assert a.parsed == {"domain": "DM", "qnam": "RACEOTH", "variable": "RACEOTH"}
    b = next(a for a in doc.iter_annotations() if a.text.startswith("SUPPDS"))
    assert b.parsed == {"domain": "DS", "qnam": "PROTVER", "variable": "QVAL"}


def test_supp_is_checked_before_assignment(doc):
    """It contains an `=` and would read as a constant assignment if it were not."""
    p = ann.classify('SUPPDS.QVAL when QNAM = "PROTVER"')
    assert p.annot_type == ann.SUPP_QUALIFIER
    assert "mentions SUPP/QNAM" in p.evidence


def test_cross_reference_carries_its_target(doc):
    a = next(a for a in doc.iter_annotations() if a.text == "See Page 7")
    assert a.parsed["target_page"] == 7


# --- the genuinely ambiguous XX=YYYY shape ---------------------------------
def test_lowercase_right_side_settles_a_domain_header():
    p = ann.classify("DS=Disposition")
    assert p.annot_type == ann.DOMAIN_HEADER and p.confidence == ann.CONF_STRUCTURAL
    assert p.parsed == {"domain": "DS", "label": "Disposition"}


def test_all_caps_right_side_is_resolved_by_position():
    """"DS=COMPLETED" is a constant; the same shape first on the page is a header."""
    assert _type("DS=COMPLETED", first=False) == ann.CONSTANT_ASSIGNMENT
    assert _type("DS=COMPLETED", first=True) == ann.DOMAIN_HEADER
    both = ann.classify("DS=COMPLETED", first=True)
    assert both.confidence == ann.CONF_AMBIGUOUS      # scored down, not hidden


def test_unknown_domain_code_scores_lower_than_a_cdisc_one():
    known = ann.classify("MH=Medical History")
    unknown = ann.classify("EL=Eligibility")          # EL is not a CDISC domain
    assert known.confidence > unknown.confidence
    assert "CDISC domain" in known.evidence[0]


def test_long_left_side_is_never_a_domain_header():
    assert _type("MHENRF=ONGOING", first=True) == ann.CONSTANT_ASSIGNMENT


# --- Phase 4 splitting -----------------------------------------------------
@pytest.mark.parametrize("text,parts", [
    ("AESTDTC AEENDTC", ["AESTDTC", "AEENDTC"]),
    ("CMTRT / CMDOSE", ["CMTRT", "CMDOSE"]),
    ("VSORRES, VSORRESU", ["VSORRES", "VSORRESU"]),
    ("BRTHDTC", ["BRTHDTC"]),
    ('SUPPDS.QVAL when QNAM = "PROTVER"', ['SUPPDS.QVAL when QNAM = "PROTVER"']),
    ("MHENRF=ONGOING", ["MHENRF=ONGOING"]),
    ("[NOT SUBMITTED]", ["[NOT SUBMITTED]"]),
    ("Record in local units", ["Record in local units"]),
    ("", []),
])
def test_split_parts(text, parts):
    assert ann.split_parts(text) == parts


def test_multi_statement_box_classifies_each_statement():
    page = Page(number=1, width=595, height=842, rotation=0, text="")
    page.annotations = [Annotation(page=1, text="AESTDTC AEENDTC",
                                  bbox=BBox.of((300, 100, 400, 120)))]
    a = ann.extract_annotations([page])[0]
    assert [p.text for p in a.parts] == ["AESTDTC", "AEENDTC"]
    assert all(p.annot_type == ann.VARIABLE for p in a.parts)
    assert len(a.parsed["parts"]) == 2
    assert "2 statements in one box" in a.type_evidence


def test_ids_are_assigned_and_unique(doc):
    ids = [a.id for a in doc.iter_annotations()]
    assert len(ids) == len(set(ids)) == 17
    assert doc.annotation("p1a1").text == "BRTHDTC"
    assert doc.annotation("p1a1").form_name == "Demographics"


def test_summary(doc):
    s = ann.summarize_annotations(list(doc.iter_annotations()))
    assert s["annotations"] == 17 and s["statements"] == 17 and s["ambiguous"] == []
    assert s["by_type"]["VARIABLE"] == 7

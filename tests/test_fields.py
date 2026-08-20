"""Phase 3 tests: which groups become fields, label cleanup, options, controls."""
import pytest

from acrf_parser import fields


def test_field_counts_per_page(doc):
    assert [len(p.fields) for p in doc.pages] == [4, 3, 2, 1, 4]
    assert len(list(doc.iter_fields())) == 14


def test_fields_carry_their_form(doc):
    assert {f.form_name for f in doc.page(3).fields} == {"Medical History"}
    assert doc.page(1).fields[0].key == ("demographics", "date of birth")


def test_raw_and_clean_text_are_both_kept(doc):
    """Numbering and the trailing colon come off `text` and stay in `raw_text`."""
    f = doc.page(5).fields[0]
    assert f.raw_text.startswith("1. Have hypochondroplasia")
    assert f.item_number == "1."
    assert f.text.startswith("Have hypochondroplasia")
    assert f.normalized_text.startswith("have hypochondroplasia")


def test_trailing_colon_stripped_from_the_key(doc):
    """The key must not depend on punctuation the study happened to print."""
    f = doc.page(4).fields[0]
    assert f.raw_text.endswith("enrolled:")
    assert f.text.endswith("enrolled")


def test_wrapped_question_is_one_field(doc):
    assert len(doc.page(4).fields) == 1
    assert doc.page(4).fields[0].text.startswith("Please record protocol version")


def test_response_options_attach_rather_than_becoming_fields(doc):
    """Original/Amendment 1-4 are one question's codelist, not five questions."""
    f = doc.page(4).fields[0]
    assert f.option_texts == ["Original", "Amendment 1", "Amendment 2",
                              "Amendment 3", "Amendment 4"]
    assert all(o.bbox.x0 > f.bbox.x1 for o in f.options)    # geometry kept for Phase 6


def test_controls_attach_regardless_of_label_length(doc):
    """"Age" is 100pt from its answer box and "Date of birth" 66pt from the same
    kind of box; a distance threshold would take one and drop the other."""
    assert all(f.control_kinds == ["BOX"] for f in doc.page(1).fields)
    assert all(f.control_kinds == ["CIRCLE"] for f in doc.page(5).fields)


def test_question_does_not_claim_controls_across_the_gutter(doc):
    """The Disposition radio circles answer the *options*, not the question."""
    assert doc.page(4).fields[0].control_kinds == []
    assert len([c for c in doc.page(4).controls if c.kind == "CIRCLE"]) == 5


def test_section_does_not_echo_the_form_title(doc):
    """"Form: Demographics" names the form; repeating it on every field says nothing."""
    assert all(f.section == "" for f in doc.iter_fields())


def test_headers_and_footers_are_not_fields(doc):
    texts = {f.text for f in doc.iter_fields()}
    assert "STUDY XYZ-123" not in texts
    assert "Generated On: 15 Nov 2024 18:35:29" not in texts


def test_annotation_markup_is_not_a_field(doc):
    """The whole point of Phase 1's from_annotation flag."""
    texts = {f.text for f in doc.iter_fields()}
    assert not texts & {"BRTHDTC", "AGE", "DM=Demographics", "[NOT SUBMITTED]"}


def test_field_ids_are_unique_and_stable(doc):
    ids = [f.id for f in doc.iter_fields()]
    assert len(ids) == len(set(ids))
    assert ids[0] == "p1f0" and doc.field("p1f0").text == "Date of birth"


def test_summary(doc):
    s = fields.summarize_fields(list(doc.iter_fields()))
    assert s["fields"] == 14 and s["with_options"] == 1 and s["numbered"] == 4


# --- label splitting -------------------------------------------------------
@pytest.mark.parametrize("raw,item,label", [
    ("1. Have hypochondroplasia", "1.", "Have hypochondroplasia"),
    ("a) Systolic", "a)", "Systolic"),
    ("(3) Weight", "(3)", "Weight"),
    ("iv. Diastolic", "iv.", "Diastolic"),
    ("• Ongoing", "•", "Ongoing"),
    ("Date of birth:", "", "Date of birth"),
    ("Was the subject enrolled?", "", "Was the subject enrolled"),
    ("Plain label", "", "Plain label"),
])
def test_split_label(raw, item, label):
    assert fields._split_label(raw) == (item, label)


def test_same_label_under_two_forms_keeps_two_keys(doc):
    """"Condition" on both Medical History pages is one key; the key carries the form."""
    conditions = [f for f in doc.iter_fields() if f.text == "Condition"]
    assert len(conditions) == 2
    assert {f.key for f in conditions} == {("medical history", "condition")}

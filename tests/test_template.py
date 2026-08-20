"""Phase 8 tests: template creation, and applying one to an unannotated CRF."""
import copy

import pytest

from acrf_parser import template as T
from acrf_parser.models import BBox


@pytest.fixture
def edited(blank_doc):
    """A private copy of the blank document, for tests that alter its fields."""
    return copy.deepcopy(blank_doc)


@pytest.fixture(scope="session")
def tmpl(doc):
    return T.build_template(doc)


def test_template_covers_every_form(tmpl):
    assert [f["name"] for f in tmpl["forms"]] == [
        "Demographics", "Medical History", "Disposition", "Eligibility Criteria"]
    assert T.summarize_template(tmpl)["fields"] == 13      # 14 fields, 13 keys
    assert T.summarize_template(tmpl)["variables"] == [
        "AGE", "BRTHDTC", "MHENDTC", "MHENRF", "MHSTDTC", "MHTERM", "QVAL", "RACE", "SEX"]


def test_nothing_is_stored_in_points(tmpl):
    """A template that hard-codes x=330 breaks the moment a study re-flows a form."""
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in {"bbox", "x0", "y0", "x1", "y1"}, f"absolute box at {path}.{k}"
                if k.startswith("rel_"):
                    assert 0.0 <= v <= 1.0, f"{path}.{k} = {v} is not page-relative"
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(tmpl)


def test_pages_are_stored_as_offsets_not_numbers(tmpl):
    """Medical History being pages 2-3 here and 11-12 elsewhere is not a difference."""
    mh = next(f for f in tmpl["forms"] if f["name"] == "Medical History")
    assert mh["continuation_offsets"] == [1]
    assert sorted(o for f in mh["fields"] for o in f["page_offsets"]) == [0, 0, 0, 1, 1]


def test_markup_placement_is_a_word_not_a_coordinate(tmpl):
    dm = next(f for f in tmpl["forms"] if f["name"] == "Demographics")
    dob = next(f for f in dm["fields"] if f["field_text"] == "Date of birth")
    assert dob["annotations"][0]["placement"]["relative_label"] == T.RIGHT_OF
    assert dob["annotations"][0]["variable"] == "BRTHDTC"


def test_one_entry_per_key_with_occurrences_merged(tmpl):
    """"Condition" is on both Medical History pages, annotated on only one - and
    the bare copy must not shadow the annotated one."""
    mh = next(f for f in tmpl["forms"] if f["name"] == "Medical History")
    cond = [f for f in mh["fields"] if f["normalized_text"] == "condition"]
    assert len(cond) == 1
    assert cond[0]["occurrences"] == 2 and cond[0]["page_offsets"] == [0, 1]
    assert [a["variable"] for a in cond[0]["annotations"]] == ["MHTERM"]


def test_options_are_kept_with_the_field(tmpl):
    ds = next(f for f in tmpl["forms"] if f["name"] == "Disposition")
    opts = ds["fields"][0]["options"]
    assert [o["text"] for o in opts][:2] == ["Original", "Amendment 1"]


def test_save_and_load_round_trip(tmpl, tmp_path):
    path = T.save_template(tmpl, tmp_path / "t.json")
    assert T.load_template(path) == tmpl


# --- applying the template to a CRF with no markup at all ------------------
def test_forms_still_detected_without_markup(blank_doc):
    """Three of four forms print their own title, so they survive the strip."""
    assert [f.name for f in blank_doc.forms] == [
        "Demographics", "Medical History", "Eligibility Criteria"]
    assert not list(blank_doc.iter_annotations())


def test_template_reproposes_the_markup(tmpl, blank_doc):
    matches = {m.field_text: m for m in T.apply_template(tmpl, blank_doc)
               if m.method == T.EXACT_KEY}
    assert [a["variable"] for a in matches["Date of birth"].annotations] == ["BRTHDTC"]
    assert [a["variable"] for a in matches["Start Date"].annotations] == ["MHSTDTC"]
    assert [a["variable"] for a in matches["Stop Date"].annotations] == ["MHENDTC"]
    assert matches["Date of birth"].confidence == T.CONF_EXACT


def test_continuation_page_field_still_resolves(tmpl, blank_doc):
    """Page 3's "Condition" carries no markup of its own; the key supplies it."""
    conds = [m for m in T.apply_template(tmpl, blank_doc)
             if m.field_text == "Condition" and m.page == 3]
    assert [a["variable"] for a in conds[0].annotations] == ["MHTERM"]


def test_a_form_that_only_markup_named_degrades_honestly(tmpl, blank_doc):
    """The Disposition page has no printed title - DS=Disposition was the only
    thing naming it. Strip that and the page is inherited by the form above, so
    its field can only match on text, under the wrong form, at low confidence.
    That is the correct answer, and the method says so."""
    m = next(m for m in T.apply_template(tmpl, blank_doc)
             if m.field_text.startswith("Please record"))
    assert m.form_name == "Medical History"        # inherited, not Disposition
    assert m.method == T.TEXT_ONLY and m.confidence == T.CONF_TEXT_ONLY
    assert "the key is (form, field)" in m.evidence[0]


def test_every_field_gets_a_verdict(tmpl, blank_doc):
    matches = T.apply_template(tmpl, blank_doc)
    assert len(matches) == len(list(blank_doc.iter_fields()))
    assert all(m.evidence for m in matches)
    assert T.summarize_matches(matches)["by_method"] == {"EXACT_KEY": 13, "TEXT_ONLY": 1}


def test_a_reworded_label_falls_back_to_position(tmpl, edited):
    """Position matching is the "label was re-worded, form was not redrawn" case."""
    fld = next(f for f in edited.iter_fields() if f.text == "Date of birth")
    fld.text = "Birth date of the subject"          # same place, different words
    m = next(m for m in T.apply_template(tmpl, edited) if m.field_id == fld.id)
    assert m.method == T.POSITION
    assert [a["variable"] for a in m.annotations] == ["BRTHDTC"]


def test_a_field_the_template_never_saw_is_reported_unmatched(tmpl, edited):
    """New label, and nowhere near a known position: no answer is the answer."""
    fld = next(f for f in edited.iter_fields() if f.text == "Sex")
    fld.text, fld.bbox = "Handedness", BBox.of((0, 700, 50, 720))
    m = next(m for m in T.apply_template(tmpl, edited) if m.field_id == fld.id)
    assert m.method == "" and m.confidence == 0.0 and m.annotations == []
    assert m.evidence == ["no template entry for this field"]

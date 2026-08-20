"""Phase 7 tests: the SQLite knowledge base and the (form, field) primary key."""
import pytest

from acrf_parser import kb
from acrf_parser.kb import KnowledgeBase, build_kb


@pytest.fixture
def db(doc, tmp_path):
    build_kb(doc, tmp_path / "kb.sqlite")
    with KnowledgeBase(tmp_path / "kb.sqlite") as k:
        yield k


@pytest.fixture
def two_study_db(doc, second_doc, tmp_path):
    path = tmp_path / "both.sqlite"
    build_kb(doc, path)
    build_kb(second_doc, path)
    with KnowledgeBase(path) as k:
        yield k


def test_everything_lands(db):
    assert db.stats() == {"documents": 1, "forms": 4, "fields": 14,
                          "annotations": 17, "links": 9, "mapped_keys": 9}


def test_lookup_returns_the_variable(db):
    rows = db.lookup("Medical History", "Start Date")
    assert len(rows) == 1
    assert rows[0]["variable"] == "MHSTDTC" and rows[0]["annot_type"] == "VARIABLE"
    assert rows[0]["domain"] == "MH" and rows[0]["link_score"] > 0.8


def test_lookup_normalizes_both_halves_of_the_key(db):
    assert db.variable_for("  medical HISTORY ", "start date!") == "MHSTDTC"


def test_the_key_is_the_form_and_the_field(two_study_db):
    """"Start Date" alone is not an answer. It is MHSTDTC on one form and
    AESTDTC on another, and the knowledge base must not blend them."""
    assert two_study_db.variable_for("Medical History", "Start Date") == "MHSTDTC"
    assert two_study_db.variable_for("Adverse Events", "Start Date") == "AESTDTC"
    assert two_study_db.variable_for("Demographics", "Start Date") is None


def test_both_studies_accumulate(two_study_db):
    s = two_study_db.stats()
    assert s["documents"] == 2 and s["forms"] == 5
    assert {f["name"] for f in two_study_db.forms()} >= {"Adverse Events", "Demographics"}


def test_field_map_folds_occurrences_not_conclusions(db):
    """"Condition" is printed on both Medical History pages and annotated on one.

    Two field rows, one mapped key - and the fold happens in the view, so the
    page-level detail is still there to argue with.
    """
    row = next(r for r in db.field_map() if r["field_key"] == "medical history|condition")
    assert row["variables"] == "MHTERM" and row["variants"] == 1
    fields = db.con.execute(
        "SELECT page FROM fields WHERE field_key = 'medical history|condition'"
        " ORDER BY page").fetchall()
    assert [r["page"] for r in fields] == [2, 3]


def test_no_disagreements_in_the_fixture(db):
    assert db.disagreements() == []


def test_rejected_links_are_stored_but_kept_out_of_the_answer(doc, tmp_path):
    path = tmp_path / "kb.sqlite"
    build_kb(doc, path)
    con = kb.connect(path)
    total = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    shown = con.execute("SELECT COUNT(*) FROM field_annotations").fetchone()[0]
    assert shown == con.execute(
        "SELECT COUNT(*) FROM links WHERE rejected = 0").fetchone()[0]
    assert total >= shown
    con.close()


def test_reparsing_the_same_file_replaces_it(doc, tmp_path):
    path = tmp_path / "kb.sqlite"
    build_kb(doc, path)
    build_kb(doc, path)
    with KnowledgeBase(path) as k:
        assert k.stats()["documents"] == 1 and k.stats()["fields"] == 14


def test_replace_false_refuses_a_duplicate(doc, tmp_path):
    path = tmp_path / "kb.sqlite"
    build_kb(doc, path)
    with pytest.raises(ValueError, match="already in this knowledge base"):
        build_kb(doc, path, replace=False)


def test_deleting_a_document_cascades(doc, tmp_path):
    path = tmp_path / "kb.sqlite"
    build_kb(doc, path)
    con = kb.connect(path)
    con.execute("DELETE FROM documents")
    con.commit()
    assert con.execute("SELECT COUNT(*) FROM fields").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 0
    con.close()


def test_field_map_is_scoped_to_one_form(two_study_db):
    keys = [r["field_key"] for r in two_study_db.field_map("Adverse Events")]
    assert keys and all(k.startswith("adverse events|") for k in keys)


def test_annotations_are_queryable_by_variable(db):
    rows = db.con.execute(
        "SELECT text, page FROM annotations WHERE variable = 'MHENRF'").fetchall()
    assert [(r["text"], r["page"]) for r in rows] == [("MHENRF=ONGOING", 2)]

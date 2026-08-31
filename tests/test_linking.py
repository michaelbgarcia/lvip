"""Phase 6 tests: row-aware scored linking, and what it refuses to link."""
from acrf_parser import annotations as ann
from acrf_parser import linking
from acrf_parser.models import Annotation, BBox, ColumnBand, Field, Page


def _pairs(doc):
    return {(doc.field(l.field_id).text, doc.annotation(l.annotation_id).text)
            for l in doc.links if not l.rejected}


def test_the_expected_links_and_only_those(doc):
    assert _pairs(doc) == {
        ("Date of birth", "BRTHDTC"), ("Age", "AGE"), ("Sex", "SEX"), ("Race", "RACE"),
        ("Condition", "MHTERM"), ("Start Date", "MHSTDTC"), ("Ongoing", "MHENRF=ONGOING"),
        ("Stop Date", "MHENDTC"),
        ("Please record protocol version on which subject is currently enrolled",
         'SUPPDS.QVAL when QNAM = "PROTVER"'),
    }


def test_every_link_carries_its_evidence(doc):
    for l in doc.links:
        assert l.evidence and 0.0 <= l.link_score <= 1.0


def test_markup_with_no_field_on_its_row_stays_unlinked(doc):
    """The nearest-neighbour trap. Page 1 carries two annotations with no field
    beside them; a nearest match would glue both onto "Race"."""
    assert [a.text for a in linking.unlinked_annotations(doc)] == [
        "RACEOTH when SUPPDM.QNAM=RACEOTH", "[NOT SUBMITTED]"]
    race = next(f for f in doc.page(1).fields if f.text == "Race")
    assert [doc.annotation(l.annotation_id).text for l in doc.links_for(race.id)] == ["RACE"]


def test_form_level_markup_is_never_linked_to_a_field(doc):
    """Domain headers and see-page pointers describe the form; Phase 2 owns them."""
    linked = {l.annotation_id for l in doc.links}
    for a in doc.iter_annotations():
        if a.annot_type in linking.FORM_LEVEL:
            assert a.id not in linked


def test_unannotated_fields_are_reported(doc):
    """Page 3 repeats "Condition" with no markup; the criteria page has none at all."""
    texts = [f.text for f in linking.unlinked_fields(doc)]
    assert texts.count("Condition") == 1
    assert sum(1 for t in texts if t.startswith("Have ")) == 4


def test_domain_agreement_raises_the_score(doc):
    """MHSTDTC agrees with form domain MH; BRTHDTC cannot agree with DM at all."""
    mh = next(l for l in doc.links if doc.annotation(l.annotation_id).text == "MHSTDTC")
    dm = next(l for l in doc.links if doc.annotation(l.annotation_id).text == "BRTHDTC")
    assert mh.link_score > dm.link_score
    assert "variable agrees with form domain MH" in mh.evidence
    assert not any("agrees with form domain" in e for e in dm.evidence)


def test_link_notes_which_option_it_sits_beside(doc):
    l = next(l for l in doc.links if doc.annotation(l.annotation_id).text.startswith("SUPPDS"))
    assert any("aligned with option" in e for e in l.evidence)


def test_summary(doc):
    s = linking.summarize_links(doc)
    assert s["links"] == 9 and s["mean_link_score"] > 0.7


# --- contention, on a synthetic page --------------------------------------
def _page(fields_spec, annots_spec):
    """fields_spec/annots_spec: (text, y0, y1) at fixed x. One column."""
    p = Page(number=1, width=595, height=842, rotation=0, text="")
    p.column_bands = [ColumnBand(0, 0, 595)]
    p.form_name, p.form_domain = "Adverse Events", "AE"
    p.fields = [Field(id=f"p1f{i}", form_name="Adverse Events", page=1, text=t,
                      raw_text=t, bbox=BBox.of((60, y0, 150, y1)), column=0)
                for i, (t, y0, y1) in enumerate(fields_spec)]
    p.annotations = [Annotation(page=1, text=t, bbox=BBox.of((330, y0, 430, y1)))
                     for t, y0, y1 in annots_spec]
    ann.extract_annotations([p])
    return p


def test_two_variables_cannot_claim_one_field():
    """Both markup boxes overlap "Start Date"'s row, but only one can be its
    variable - the loser falls to the field it also reaches."""
    page = _page([("Start Date", 100, 114), ("End Date", 112, 126)],
                 [("AESTDTC", 100, 114), ("AEENDTC", 112, 126)])
    links = linking.link_page(page)
    accepted = {(l.field_id, l.annotation_id) for l in links if not l.rejected}
    assert accepted == {("p1f0", "p1a0"), ("p1f1", "p1a1")}


def test_losing_candidates_are_kept_for_audit():
    page = _page([("Start Date", 100, 114), ("End Date", 112, 126)],
                 [("AESTDTC", 100, 114), ("AEENDTC", 112, 126)])
    links = linking.link_page(page)
    rejected = [l for l in links if l.rejected]
    assert rejected, "a scored candidate that lost must still be recorded"
    assert all(l.evidence[-1].startswith(("annotation already linked", "field already has"))
               for l in rejected)


def test_a_field_may_hold_one_annotation_per_type():
    """A variable and its SUPPQUAL qualifier legitimately share one field."""
    page = _page([("Race", 100, 114)],
                 [("RACE", 100, 114), ("RACEOTH when SUPPDM.QNAM=RACEOTH", 102, 116)])
    links = [l for l in linking.link_page(page) if not l.rejected]
    assert {l.annotation_id for l in links} == {"p1a0", "p1a1"}
    assert {l.field_id for l in links} == {"p1f0"}


def test_a_field_may_hold_several_annotations_of_one_type():
    """One consent date annotated four ways - the case a one-per-type cap lost.

    All four sit on the label's own row, which is what makes them its own rather
    than the next field's.
    """
    page = _page([("Date of informed consent", 100, 114)],
                 [("DSTERM", 100, 114), ("RFICDTC", 100, 114),
                  ("DSSTDTC", 101, 115)])
    links = [l for l in linking.link_page(page) if not l.rejected]
    assert {l.field_id for l in links} == {"p1f0"}
    assert len(links) == 3


def test_a_second_annotation_of_a_type_must_share_the_field_s_row():
    """The protection the cap was really for: markup a hair off the row is the
    neighbouring field's, and a field that already has one must not take it."""
    page = _page([("Start Date", 100, 114)],
                 [("AESTDTC", 100, 114), ("AEENDTC", 116, 130)])
    links = linking.link_page(page)
    accepted = [l for l in links if not l.rejected]
    assert [doc_text(page, l) for l in accepted] == ["AESTDTC"]
    refused = next(l for l in links if l.rejected)
    assert "belongs to the neighbouring field" in refused.evidence[-1]


def test_one_field_cannot_absorb_a_whole_page_of_one_type():
    page = _page([("Start Date", 100, 114)],
                 [(f"AETEST{i}", 100, 114) for i in range(linking.MAX_PER_TYPE + 2)])
    accepted = [l for l in linking.link_page(page) if not l.rejected]
    assert len(accepted) == linking.MAX_PER_TYPE


def doc_text(page, link):
    return next(a.text for a in page.annotations if a.id == link.annotation_id)


def test_row_gate_rejects_markup_floating_between_rows():
    page = _page([("Start Date", 100, 114)], [("AESTDTC", 300, 314)])
    assert linking.link_page(page) == []


def test_markup_just_off_its_row_still_links():
    """Real aCRFs draw markup a few points high; that is not a different row."""
    page = _page([("Start Date", 100, 114)], [("AESTDTC", 116, 130)])
    links = [l for l in linking.link_page(page) if not l.rejected]
    assert len(links) == 1
    assert any("adjacent row" in e for e in links[0].evidence)

"""Deterministic pre-fill: the tiers, and what they refuse to do."""
import pytest

from acrf_parser import prefill as pf
from acrf_parser.models import BBox, Field
from acrf_parser.prefill import PrefillIndex, similarity


def _field(form, text, fid="x1"):
    return Field(id=fid, form_name=form, page=1, text=text, raw_text=text,
                 bbox=BBox.of((60, 100, 150, 114)))


# --- similarity ------------------------------------------------------------
@pytest.mark.parametrize("a,b,lo,hi", [
    ("start date", "start date", 1.0, 1.0),
    ("conditions", "condition", 0.9, 1.0),               # plural is one field
    ("adverse event", "adverse events", 0.9, 1.0),
    ("start date of condition", "start date", 0.65, 0.85),   # qualifier added
    ("date", "start date", 0.0, 0.6),                    # one word proves nothing
    ("handedness", "condition", 0.0, 0.3),
])
def test_similarity(a, b, lo, hi):
    assert lo <= similarity(a, b) <= hi


def test_opposites_stay_below_the_fuzzy_floor():
    """The pair that must never fuzzy-match. Its margin is thin - if this test
    ever fails, the floor moved, not the metric."""
    assert similarity("start date", "stop date") < pf.FUZZY_FLOOR
    assert similarity("start date", "end date") < pf.FUZZY_FLOOR


# --- tiers -----------------------------------------------------------------
def test_exact_key_is_the_only_tier_that_auto_approves(index):
    """The safety property. A fuzzy match must never look like a fact."""
    exact = index.match(_field("Medical History", "Start Date"))
    assert exact.best.tier == pf.EXACT_KEY and exact.status == pf.AUTO

    for form, text, domain in [("Medical History", "Conditions", "MH"),
                               ("Concomitant Medications", "Start Date", "CM")]:
        r = index.match(_field(form, text), domain=domain)
        assert r.best.tier != pf.EXACT_KEY
        assert r.status == pf.NEEDS_REVIEW, f"{r.best.tier} must not auto-approve"


def test_exact_key_carries_its_source(index):
    r = index.match(_field("Medical History", "Start Date"))
    assert r.best.variable == "MHSTDTC"
    assert "Medical History" in r.best.source and "Start Date" in r.best.source


def test_domain_pattern_reaches_a_domain_never_seen(index):
    """CM appears nowhere in the corpus, but SDTM's prefix convention does."""
    r = index.match(_field("Concomitant Medications", "Start Date"), domain="CM")
    assert r.best.tier == pf.DOMAIN_PATTERN and r.best.variable == "CMSTDTC"
    assert "became STDTC in 2 domains" in r.best.evidence[0]


def test_domain_pattern_needs_more_than_one_domain(index):
    """"Ongoing" became ENRF in MH alone - one domain is a coincidence."""
    assert "ongoing" in index.suffixes
    assert index.suffixes["ongoing"]["ENRF"] == {"MH"}
    r = index.match(_field("Vital Signs", "Ongoing"), domain="VS")
    assert r.best.tier != pf.DOMAIN_PATTERN


def test_cross_form_consensus_refuses_when_the_corpus_disagrees(index):
    """"Start Date" is MHSTDTC and AESTDTC. The form is load-bearing, and the
    algorithm works that out from the disagreement rather than being told."""
    r = index.match(_field("Vital Signs", "Start Date"), domain="VS")
    consensus = next(c for c in [r.best, *r.alternates]
                     if c.tier == pf.CROSS_FORM_CONSENSUS)
    assert consensus.confidence == 0.0 and consensus.variable == ""
    assert "load-bearing" in consensus.evidence[0]


def test_cross_form_consensus_fires_when_every_form_agrees():
    """A label that means one thing everywhere is safe to carry across forms."""
    idx = PrefillIndex._build([
        {"file_name": f"s{i}.pdf", "form_name": form, "domain": dom,
         "field_key": f"{form.lower()}|sex", "field_text": "Sex",
         "normalized_text": "sex", "annotation_text": "SEX", "annot_type": "VARIABLE",
         "variable": "SEX", "link_score": 0.9}
        for i, (form, dom) in enumerate([("demographics", "DM"), ("screening", "SC"),
                                         ("baseline", "VS")])])
    r = idx.match(_field("New Form", "Sex"))
    assert r.best.tier == pf.CROSS_FORM_CONSENSUS and r.best.variable == "SEX"
    assert r.status == pf.NEEDS_REVIEW           # still not auto
    assert "all 3 forms" in r.best.evidence[0]


def test_fuzzy_matches_within_a_form(index):
    r = index.match(_field("Medical History", "Start Date of Condition"), domain="MH")
    assert r.best.tier == pf.FUZZY_SAME_FORM and r.best.variable == "MHSTDTC"
    assert "Start Date" in r.best.evidence[0]


def test_fuzzy_never_crosses_forms(index):
    """A near-miss under the wrong form is not evidence about this one."""
    r = index.match(_field("Eligibility Criteria", "Conditions"), domain="EL")
    assert r.best.tier != pf.FUZZY_SAME_FORM


def test_unreachable_field_goes_to_the_agent(index):
    r = index.match(_field("Vital Signs", "Handedness"), domain="VS")
    assert r.best.tier == pf.NEEDS_MAPPING and r.status == pf.NEEDS_MAPPING_STATUS
    assert r.best.variable == "" and r.best.confidence == 0.0


def test_alternates_are_kept(index):
    """Losing tiers stay visible - the reviewer sees what else was considered."""
    r = index.match(_field("Adverse Events", "Start Date"), domain="AE")
    assert r.best.tier == pf.EXACT_KEY
    assert {c.tier for c in r.alternates} >= {pf.FUZZY_SAME_FORM, pf.DOMAIN_PATTERN}


def test_aliases_give_the_reviewer_context(index):
    r = index.match(_field("Medical History", "Start Date of Condition"), domain="MH")
    assert r.aliases == ["Start Date"]


# --- several annotations on one field --------------------------------------
def _consent_corpus(*annotations, file_name="prior.pdf", **kw):
    """One field, seen in history carrying each of `annotations`."""
    return PrefillIndex._build([
        {"file_name": file_name, "form_name": "Informed Consent", "domain": "DS",
         "field_key": "informed consent|date of informed consent",
         "field_text": "Date of informed consent",
         "normalized_text": "date of informed consent",
         "annotation_text": text, "annot_type": "VARIABLE",
         "variable": text.split("=")[0], "link_score": 0.9, **kw}
        for text in annotations])


def test_an_exact_key_proposes_every_annotation_it_was_seen_with():
    """The real shape of a consent date: four statements, none of them the one."""
    idx = _consent_corpus("DSTERM", "DSDECOD=INFORMED CONSENT OBTAINED",
                          "RFICDTC", "DSSTDTC")
    r = idx.match(_field("Informed Consent", "Date of informed consent"), domain="DS")
    assert r.best.tier == pf.EXACT_KEY
    assert len(r.annotations) == 4
    assert {c.annotation_text for c in r.annotations} == {
        "DSTERM", "DSDECOD=INFORMED CONSENT OBTAINED", "RFICDTC", "DSSTDTC"}
    assert all(r.status_of(c) == pf.AUTO for c in r.annotations)


def test_a_companion_says_why_it_is_there():
    idx = _consent_corpus("DSTERM", "RFICDTC")
    r = idx.match(_field("Informed Consent", "Date of informed consent"), domain="DS")
    assert r.companions
    assert any("carried alongside" in e for e in r.companions[0].evidence)


def test_the_same_statement_written_two_ways_is_one_annotation():
    """Re-ordering a qualifier does not make a second statement."""
    idx = _consent_corpus('SUPPDS.QVAL when QNAM = "PROTVER"',
                          'QVAL when SUPPDS.QNAM = "PROTVER"')
    r = idx.match(_field("Informed Consent", "Date of informed consent"), domain="DS")
    assert len(r.annotations) == 1


def test_a_rejected_companion_is_not_proposed_again():
    """Each annotation of a set is filtered on its own - striking one off must
    not take the others with it, and must not come back because they survived."""
    idx = _consent_corpus("DSTERM", "RFICDTC", "DSSTDTC")
    idx._seed_rejections([("informed consent|date of informed consent", "RFICDTC")])
    r = idx.match(_field("Informed Consent", "Date of informed consent"), domain="DS")
    assert [c.annotation_text for c in r.annotations] == ["DSSTDTC", "DSTERM"]


def test_a_field_with_one_annotation_has_no_companions(index):
    r = index.match(_field("Medical History", "Start Date"), domain="MH")
    assert r.companions == [] and len(r.annotations) == 1


def test_only_exact_keys_bring_a_set(index):
    """A fuzzy tier is a guess about which variable a label means; multiplying a
    guess by four multiplies the reviewer's work, not the evidence."""
    r = index.match(_field("Medical History", "Start Date of Condition"), domain="MH")
    assert r.best.tier == pf.FUZZY_SAME_FORM and r.companions == []


def test_a_set_keeps_the_order_it_was_drawn_in():
    """Alphabetical is an order nobody chose. Annotations are drawn left to right
    in the reviewer's own sequence, so their x is that sequence, recoverable."""
    idx = PrefillIndex._build([
        {"file_name": "prior.pdf", "form_name": "Informed Consent", "domain": "DS",
         "field_key": "informed consent|date of informed consent",
         "field_text": "Date of informed consent",
         "normalized_text": "date of informed consent",
         "annotation_text": text, "annot_type": "VARIABLE", "variable": text,
         "annotation_bbox": [x, 100.0, x + 40, 114.0], "link_score": 0.9}
        for text, x in [("DSSTDTC", 480.0), ("DSTERM", 340.0), ("RFICDTC", 410.0)]])
    r = idx.match(_field("Informed Consent", "Date of informed consent"), domain="DS")
    assert [c.annotation_text for c in r.annotations] == ["DSTERM", "RFICDTC", "DSSTDTC"]


def test_the_index_lends_copies_not_its_own_candidates():
    """`_drop_rejected` edits what it is handed; if that were the stored object,
    one rejected field would zero the key for every later field like it."""
    idx = _consent_corpus("DSTERM")
    idx._seed_rejections([("informed consent|date of informed consent", "DSTERM")])
    idx.match(_field("Informed Consent", "Date of informed consent"), domain="DS")
    stored = idx.by_key[("informed consent", "date of informed consent")]
    assert stored.confidence == pf.CONF_EXACT
    assert not any("rejected" in e for e in stored.evidence)


def test_an_empty_corpus_is_honest(doc):
    """A first study with no history: every row is the agent's, and says so."""
    empty = PrefillIndex()
    results = pf.prefill_document(doc, empty)
    assert all(r.best.tier == pf.NEEDS_MAPPING for r in results)
    s = pf.summarize_prefill(results)
    assert s["auto_fill_rate"] == 0.0 and s["reaches_agent"] == len(results)


def test_summary_reports_the_number_that_matters(index, blank_doc):
    s = pf.summarize_prefill(pf.prefill_document(blank_doc, index))
    assert s["fields"] == 14
    assert s["by_status"]["AUTO"] == 9 and s["auto_fill_rate"] == 0.643


def test_learned_suffixes(index):
    assert index.suffixes["start date"]["STDTC"] == {"AE", "MH"}
    assert index.suffixes["stop date"]["ENDTC"] == {"AE", "MH"}


# --- one study wins: the corpus is evidence, not an accumulator ------------
def _form_row(file_name, x, y, text="DS=Disposition", page_offset=0,
              trust=pf.GEOMETRIC):
    return {"file_name": file_name, "form_name": "Disposition",
            "normalized_name": "disposition", "domain": "DS",
            "annotation_text": text, "annot_type": "DOMAIN_HEADER", "variable": "",
            "annotation_bbox": [x * 600, y * 800, x * 600 + 80, y * 800 + 12],
            "annotation_relative": {"rel_x_pct": x, "rel_y_pct": y,
                                    "rel_w_pct": 0.13, "rel_h_pct": 0.015},
            "page_offset": page_offset, "trust": trust}


def _form_index(rows):
    idx = PrefillIndex()
    idx._load_form_annotations(rows)
    return idx


def _kept(idx, form="disposition"):
    return list(idx.form_sets[form].values())


def test_one_study_wins_a_page_however_the_others_word_it():
    """The bug: a page came out with its header on it once per study in the
    corpus. Wording is not the axis - three sponsors saying the same thing three
    ways is still one header, and text identity would only catch the first."""
    idx = _form_index([_form_row("a.pdf", 0.08, 0.05, "DS=Disposition"),
                       _form_row("b.pdf", 0.12, 0.05, "DS = Disposition Status"),
                       _form_row("c.pdf", 0.15, 0.05, "DS=Disposition (per SDTM)")])
    assert [c.annotation_text for c in _kept(idx)] == ["DS=Disposition"]
    assert {c.annotation_text for c in idx.form_alternates["disposition"]} == {
        "DS = Disposition Status", "DS=Disposition (per SDTM)"}


def test_the_winner_brings_its_whole_page_not_the_best_of_each():
    """Cherry-picking the strongest statement from each study builds a page in
    three house styles that no annotator ever drew. The set goes together."""
    idx = _form_index(
        [_form_row("a.pdf", 0.08, 0.05, "DS=Disposition"),
         _form_row("a.pdf", 0.08, 0.09, "DSDECOD"),
         _form_row("a.pdf", 0.08, 0.13, "DSSTDTC"),
         _form_row("b.pdf", 0.40, 0.05, "DS = Disposition Status"),
         _form_row("b.pdf", 0.40, 0.09, "DSTERM")])
    assert {c.file_name for c in _kept(idx)} == {"a.pdf"}
    assert sorted(c.annotation_text for c in _kept(idx)) == sorted(
        ["DS=Disposition", "DSDECOD", "DSSTDTC"])


def test_a_statement_repeated_within_one_study_is_kept():
    """The reason position is in the key at all: one page saying
    `[NOT SUBMITTED]` against three questions carries three."""
    idx = _form_index([_form_row("a.pdf", 0.30, y, text="[NOT SUBMITTED]")
                       for y in (0.32, 0.55, 0.87)])
    assert len(_kept(idx)) == 3


def test_trust_outranks_everything_including_a_fuller_record():
    """A reviewer's sign-off is the record, however much markup a geometric
    guess piled up beside it."""
    idx = _form_index([_form_row("approved.pdf", 0.08, 0.05,
                                 trust=pf.HUMAN_APPROVED),
                       _form_row("guessed.pdf", 0.40, 0.05, "DSTERM"),
                       _form_row("guessed.pdf", 0.40, 0.09, "DSDECOD"),
                       _form_row("guessed.pdf", 0.40, 0.13, "DSSTDTC")])
    assert {c.file_name for c in _kept(idx)} == {"approved.pdf"}


def test_the_fuller_record_wins_a_tie():
    """Equal trust and equal score: the study that wrote the header *and* its
    two constants knew more about this page than the one that wrote a header."""
    idx = _form_index([_form_row("a.pdf", 0.08, 0.05),
                       _form_row("z.pdf", 0.40, 0.05, "DS = Disposition Status"),
                       _form_row("z.pdf", 0.40, 0.09, "DSDECOD")])
    assert {c.file_name for c in _kept(idx)} == {"z.pdf"}


def test_an_exact_tie_resolves_the_same_way_every_run():
    """Arbitrary, but stable - a workbook diff has to mean something."""
    rows = [_form_row("b.pdf", 0.12, 0.05, "DS = Disposition Status"),
            _form_row("a.pdf", 0.08, 0.05, "DS=Disposition")]
    assert ([c.file_name for c in _kept(_form_index(rows))]
            == [c.file_name for c in _kept(_form_index(list(reversed(rows))))]
            == ["a.pdf"])


def test_each_page_of_a_form_is_won_separately():
    """A two-page questionnaire heads both pages; deduping is per page."""
    idx = _form_index([_form_row("a.pdf", 0.08, 0.05, page_offset=0),
                       _form_row("a.pdf", 0.08, 0.05, page_offset=1),
                       _form_row("b.pdf", 0.11, 0.06, "DS = Disposition Status",
                                 page_offset=0)])
    assert sorted(pf._statement_page(c) for c in _kept(idx)) == [0, 1]


def test_the_losers_are_handed_on_as_alternates_not_dropped():
    """The handoff. Only one answer is drawn, but discarding the rest would make
    the corpus look unanimous when it was not - and the agent is the reader who
    can tell which wording suits a CRF this pipeline has never seen. Where they
    are *presented* is the workbook's business, not this layer's."""
    from acrf_parser.models import BBox, FormAnchor

    idx = _form_index([_form_row("a.pdf", 0.08, 0.05, "DS=Disposition"),
                       _form_row("b.pdf", 0.12, 0.05, "DS = Disposition Status")])
    anchor = FormAnchor(id="p3", form_name="Disposition", page=3,
                        bbox=BBox.of((0, 0, 10, 10)))
    p = idx.match_form(anchor, domain="DS", page_offset=0)
    assert [c.annotation_text for c in p.annotations] == ["DS=Disposition"]
    assert [c.annotation_text for c in p.alternates] == ["DS = Disposition Status"]


def test_a_corpus_with_no_file_names_is_left_alone():
    """Written before the column existed: guessing which rows are one study
    would lose exactly the repetitions the position key protects."""
    idx = _form_index([_form_row("", 0.30, y, text="[NOT SUBMITTED]")
                       for y in (0.32, 0.55, 0.87)])
    assert len(_kept(idx)) == 3


# --- the same rule, one level down: fields -------------------------------
def _field_rows(file_name, statements, trust=pf.GEOMETRIC):
    return [{"file_name": file_name, "form_name": "Medical History", "domain": "MH",
             "field_key": "medical history|conditions", "field_text": "Conditions",
             "normalized_text": "conditions", "annotation_text": t,
             "annot_type": "VARIABLE", "variable": t.split()[0], "link_score": 0.9,
             "trust": trust, "annotation_bbox": [200 + 40 * i, 100, 260 + 40 * i, 112],
             "annotation_relative": {"rel_x_pct": 0.4, "rel_y_pct": 0.2,
                                     "rel_w_pct": 0.1, "rel_h_pct": 0.02},
             "relative_label": "", "offset_x_pct": 0.02, "offset_y_pct": 0.0,
             "text_color": None, "fill_color": None, "font_name": None,
             "font_size": None}
            for i, t in enumerate(statements)]


def test_a_field_keeps_its_set_but_from_one_study():
    """A field really does carry several statements, so the set is never
    collapsed to one - but two sponsors wording the same mapping differently
    must not both be drawn on it."""
    idx = PrefillIndex._build(_field_rows("a.pdf", ["MHTERM", "MHOCCUR"])
                              + _field_rows("b.pdf", ["MHTERM (verbatim)",
                                                      "MHOCCUR = Y"]))
    kept = idx.key_sets[("medical history", "conditions")]
    assert sorted(c.annotation_text for c in kept.values()) == ["MHOCCUR", "MHTERM"]
    assert {c.file_name for c in kept.values()} == {"a.pdf"}


def test_a_field_s_rival_studies_are_carried_as_alternates():
    """Same contract as the form layer: evidence for the agent, never rows."""
    idx = PrefillIndex._build(_field_rows("a.pdf", ["MHTERM"])
                              + _field_rows("b.pdf", ["MHTERM (verbatim)"]))
    r = idx.match(_field("Medical History", "Conditions"))
    assert r.best.annotation_text == "MHTERM"
    # `alternates` also carries rival *tiers* for the field; the rival *study*
    # is the one this test is about.
    assert "MHTERM (verbatim)" in [c.annotation_text for c in r.alternates]
    assert [c.file_name for c in idx.key_alternates[
        ("medical history", "conditions")]] == ["b.pdf"]


def test_a_field_answered_by_one_study_gains_no_alternates():
    """The ordinary case must not grow noise in its evidence column."""
    idx = PrefillIndex._build(_field_rows("a.pdf", ["MHTERM", "MHOCCUR"]))
    r = idx.match(_field("Medical History", "Conditions"))
    assert idx.key_alternates[("medical history", "conditions")] == []
    assert not any("differently" in e for e in r.best.evidence)

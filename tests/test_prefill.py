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

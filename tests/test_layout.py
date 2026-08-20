"""Layout pass tests: wrapped-line grouping, columns, regions, roles."""
import pytest

from acrf_parser import layout
from acrf_parser.models import BBox, ColumnBand, Control, Page, Rule, TextLine

WRAPPED = "Please record protocol version on which subject is currently enrolled:"


@pytest.fixture(scope="session")
def ds(doc):
    """The Disposition page: two columns, a wrapped question, radio options."""
    return doc.page(4)


# --- the wrapped-text fix --------------------------------------------------
def test_wrapped_question_becomes_one_group(ds):
    hits = [g for g in ds.groups if g.text == WRAPPED]
    assert len(hits) == 1
    assert hits[0].line_count == 6              # six rendered lines, one label
    assert hits[0].normalized_text == "please record protocol version on which subject is currently enrolled"


def test_constituent_lines_are_kept(ds):
    """Merges stay auditable: the original lines survive on the group."""
    g = next(g for g in ds.groups if g.text == WRAPPED)
    assert [l.text for l in g.lines][:2] == ["Please record", "protocol"]
    assert all(l.group_id == ds.groups.index(g) for l in g.lines)
    assert g.bbox.y1 - g.bbox.y0 > g.lines[0].bbox.height * 5   # spans all six


def test_response_options_are_not_merged(ds):
    """Stacked short labels in the response column must stay separate fields."""
    opts = [g for g in ds.groups if g.text.startswith(("Original", "Amendment"))]
    assert len(opts) == 5
    assert all(g.line_count == 1 for g in opts)


def test_single_line_labels_unaffected(doc):
    texts = [g.text for g in doc.page(1).groups]
    for label in ("Date of birth", "Age", "Sex", "Race"):
        assert label in texts


def test_split_annotation_is_rejoined(ds):
    """Annotation markup wraps too - Phase 5 needs the whole string."""
    g = next(g for g in ds.groups if g.text.startswith("SUPPDS"))
    assert g.text == 'SUPPDS.QVAL when QNAM = "PROTVER"'
    assert g.from_annotation_only()


# --- right-aligned criteria page (uniform pitch, checkbox-anchored items) ---
@pytest.fixture(scope="session")
def elig(doc):
    return doc.page(5)


def test_grouping_ignores_muPDF_block_splits(elig):
    """MuPDF splits a single criterion across blocks; grouping must not follow it.

    Criterion 1 arrives as blocks [2, 3] - the exact split seen in a real aCRF -
    and criterion 3 spans four blocks. Both are one label.
    """
    one = next(g for g in elig.groups if g.text.startswith("1."))
    assert len(one.block_ids) > 1                     # MuPDF really did split it
    assert one.text == ("1. Have hypochondroplasia or short stature condition "
                        "other than ACH (e.g., trisomy 21, pseudoachondroplasia)")
    three = next(g for g in elig.groups if g.text.startswith("3."))
    assert len(three.block_ids) > 1 and three.line_count == 8


def test_uniform_pitch_items_split_on_numbering_and_checkbox(elig):
    """Pitch is identical within and between items, so only numbering and the
    checkboxes mark the boundaries."""
    items = [g for g in elig.groups if g.role == layout.QUESTION]
    assert len(items) == 4
    assert [g.text[:2] for g in items] == ["1.", "2.", "3.", "4."]


def test_mid_item_colon_does_not_split(elig):
    """"2. Have any of the following:" continues into its semicolon list."""
    two = next(g for g in elig.groups if g.text.startswith("2."))
    assert two.line_count == 7
    assert two.text.endswith("Inflammatory bowel disease; Autonomic neuropathy")


def test_cross_reference_annotation_kept_separate(elig):
    assert any(a.text == "See Page 7" for a in elig.annotations)
    g = next(g for g in elig.groups if g.text == "See Page 7")
    assert g.from_annotation_only() and g.role == layout.UNKNOWN


# --- columns ---------------------------------------------------------------
def test_two_column_split_detected(ds):
    assert len(ds.column_bands) == 2
    q, r = ds.column_bands
    assert (q.role_hint, r.role_hint) == (layout.QUESTION_ZONE, layout.RESPONSE_ZONE)
    assert q.x1 < r.x0                          # a real gutter between them


def test_gutter_annotation_does_not_hide_the_split(ds):
    """The SUPPDS annotation sits in the gutter; columns come from content only."""
    gutter = ds.column_bands[0].x1, ds.column_bands[1].x0
    annot = next(a for a in ds.annotations if a.text.startswith("SUPPDS"))
    assert gutter[0] < annot.bbox.cx < gutter[1]    # genuinely in the gutter
    assert len(ds.column_bands) == 2


def test_single_column_pages_stay_single(doc):
    assert [len(doc.page(n).column_bands) for n in (1, 2, 3)] == [1, 1, 1]


# --- regions ---------------------------------------------------------------
def test_regions(ds, doc):
    header = [g.text for g in ds.groups if g.region == layout.HEADER]
    assert "Generated On: 15 Nov 2024 18:35:29" in header
    assert ds.body_top == 92.0                  # the full-width rule, not the default
    assert any(g.text == WRAPPED and g.region == layout.BODY for g in ds.groups)


def test_repeated_field_label_is_not_a_running_header(doc):
    """"Condition" repeats across both Medical History pages but is a field."""
    g = next(g for g in doc.page(2).groups if g.text == "Condition")
    assert g.region == layout.BODY and g.role == layout.QUESTION


def test_running_header_detected(doc):
    g = next(g for g in doc.page(2).groups if g.text == "STUDY XYZ-123")
    assert g.region == layout.HEADER and g.role == layout.PAGE_HEADER


# --- roles -----------------------------------------------------------------
def test_roles_on_two_column_page(ds):
    q = next(g for g in ds.groups if g.text == WRAPPED)
    assert q.role == layout.QUESTION and q.role_confidence >= 0.8
    assert "in question zone" in q.role_evidence
    for g in (g for g in ds.groups if g.text.startswith(("Original", "Amendment"))):
        assert g.role == layout.RESPONSE_OPTION
        assert "own row-aligned control" in g.role_evidence


def test_roles_on_single_column_page(doc):
    """No column split, so roles come from punctuation and control geometry."""
    p = doc.page(1)
    assert next(g for g in p.groups if g.text == "Date of birth").role == layout.QUESTION
    assert next(g for g in p.groups if g.text == "Form: Demographics").role == layout.SECTION_HEADER


def test_roles_never_drop_anything(doc):
    """Role is a scored attribute, not a filter - every line lands in some group."""
    for p in doc.pages:
        assert sum(g.line_count for g in p.groups) == len([l for l in p.lines if l.text.strip()])
        assert all(0.0 <= g.role_confidence <= 1.0 for g in p.groups)


# --- graphics extraction ---------------------------------------------------
def test_rules_and_controls(ds, doc):
    assert [r.orientation for r in ds.rules] == ["H", "H"]
    assert all(r.span_pct > 0.7 for r in ds.rules)
    assert len([c for c in ds.controls if c.kind == "CIRCLE"]) == 5
    assert len([c for c in doc.page(1).controls if c.kind == "BOX"]) == 4


# --- merge predicate unit tests --------------------------------------------
def _line(text, x0, y0, x1, y1, size=10.0, col=0, block=0):
    return TextLine(text=text, bbox=BBox.of((x0, y0, x1, y1)), page=1, block_no=block,
                    line_no=0, size=size, region=layout.BODY, column=col)


def _page(*lines, **kw):
    p = Page(number=1, width=595, height=842, rotation=0, text="")
    p.column_bands = kw.get("bands", [ColumnBand(0, 0, 300), ColumnBand(1, 400, 595)])
    p.rules, p.controls = kw.get("rules", []), kw.get("controls", [])
    p.lines = list(lines)                # anchors are derived from the page's lines
    return p


def test_merge_predicate_pitch():
    p = _page()
    a = _line("wrapped one", 60, 100, 150, 113)
    assert layout._mergeable(a, _line("wrapped two", 60, 112, 150, 125), p)      # tight leading
    assert not layout._mergeable(a, _line("next field", 60, 130, 150, 143), p)   # 2x pitch


def test_merge_predicate_alignment_and_style():
    """Lines drawn as separate blocks must align to merge."""
    p = _page()
    a = _line("left aligned", 60, 100, 150, 113, block=0)
    assert not layout._mergeable(a, _line("indented", 220, 112, 290, 125, block=1), p)
    assert not layout._mergeable(a, _line("bigger", 60, 112, 150, 125, size=14, block=1), p)


def test_merge_predicate_trusts_same_block():
    """Within one MuPDF block the paragraph flow is already known - only pitch is checked."""
    p = _page()
    a = _line("left aligned", 60, 100, 150, 113, block=7)
    assert layout._mergeable(a, _line("indented", 220, 112, 290, 125, block=7), p)
    assert not layout._mergeable(a, _line("far below", 220, 140, 290, 153, block=7), p)


def test_merge_predicate_rule_between():
    rule = Rule(1, BBox.of((55, 113.5, 540, 114.5)), "H", 0.9)
    a, b = _line("above rule", 60, 100, 150, 113), _line("below rule", 60, 115, 150, 128)
    assert layout._mergeable(a, b, _page())
    assert not layout._mergeable(a, b, _page(rules=[rule]))       # separator wins


def test_merge_predicate_control_is_band_scoped():
    """A control in the line's own band starts a new field; one across the gutter does not."""
    a, b = _line("line one", 60, 100, 150, 113), _line("line two", 60, 112, 150, 125)
    own = Control(1, BBox.of((170, 110, 200, 126)), "BOX")        # band 0, adjacent to b
    across = Control(1, BBox.of((500, 110, 510, 120)), "CIRCLE")  # band 1, far right
    assert not layout._mergeable(a, b, _page(a, b, controls=[own]))
    assert layout._mergeable(a, b, _page(a, b, controls=[across]))


def test_control_anchors_only_the_topmost_line():
    """A control level with a whole wrapped label anchors line 1, not every line.

    Otherwise a tall checkbox beside a three-line question would split it in two.
    """
    a = _line("first line", 60, 100, 150, 113)
    b = _line("second line", 60, 112, 150, 125)
    tall = Control(1, BBox.of((170, 98, 200, 128)), "BOX")        # spans both rows
    p = _page(a, b, controls=[tall])
    anchors = layout._anchor_ids(p)
    assert id(a) in anchors and id(b) not in anchors
    assert layout._mergeable(a, b, p)                             # so the label survives


def test_merge_predicate_punctuation():
    p = _page()
    a = _line("ends with colon:", 60, 100, 150, 113)
    assert not layout._mergeable(a, _line("new label", 60, 112, 150, 125), p)
    b = _line("1) numbered item", 60, 112, 150, 125)
    assert not layout._mergeable(_line("intro", 60, 100, 150, 113), b, p)


def test_join_handles_hyphenation():
    assert layout._join(["Concomi-", "tant medication"]) == "Concomitant medication"
    assert layout._join(["Start", "Date"]) == "Start Date"

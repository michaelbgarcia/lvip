"""The pipeline scored against a real aCRF and its own blank.

`data/blankcrf_annotated.pdf` and `data/blankcrf.pdf` are the CDISC SDTM
Metadata Submission Guidelines example CRF - the same 22-page form, once with
the sponsor's 206 SDTM annotations on it and once without. Because they are the
same CRF, the annotated one is a complete answer key for the blank one, and the
whole loop can be scored with no human labelling anywhere in it:

    annotated CRF -> corpus -> stage the blank -> approve -> draw -> compare

Every threshold below is a floor, set just under a measured result, so the
numbers can only be moved deliberately. They are not aspirations: a run that
misses one is a regression against work somebody actually shipped.

The synthetic fixture in `tests/sample_pdf.py` is still the right place for a
case this CRF has no example of - see the note in `conftest.py`. It is not the
right place to calibrate a threshold, because it was drawn to match the
thresholds.
"""
from collections import Counter

import pymupdf
import pytest

from acrf_parser import annotations as ann
from acrf_parser import parse_pdf
from tests import msg


# --- the answer key --------------------------------------------------------
def test_the_two_files_are_the_same_crf(msg_run):
    """Everything here rests on that, so it is checked rather than assumed."""
    assert msg_run.annotated.page_count == msg_run.blank.page_count == 22
    for a, b in zip(msg_run.annotated.pages, msg_run.blank.pages):
        assert (a.width, a.height) == (b.width, b.height)
        assert a.rotation == b.rotation
    assert not any(p.annotations for p in msg_run.blank.pages)


def test_the_answer_key_is_the_markup_and_nothing_else(msg_truth):
    """209 FreeText annotations, three of them empty. And four sticky notes.

    The sticky notes ("Accepted set by Me") are what a reviewer leaves in
    Acrobat. They are not markup, they are not part of the form, and reading
    them as markup put four of them in the workbook and on the output PDF.
    """
    assert len(msg_truth) == 206
    assert all(t.text.strip() for t in msg_truth)
    assert {t.page for t in msg_truth} == set(range(3, 23))


def test_review_sticky_notes_are_not_read_as_markup(msg_run, msg_truth):
    """The parser has to make the same exclusions the answer key does.

    Two of them: a reviewer's sticky notes, and the three FreeText boxes that
    carry no text at all. Neither can be classified, linked, staged or drawn,
    and counting them tells a reviewer the file has more markup than it has.
    """
    assert len(list(msg_run.annotated.iter_annotations())) == len(msg_truth)
    texts = {a.text for a in msg_run.annotated.iter_annotations()}
    assert all(t.strip() for t in texts)
    assert not any("set by Me" in t for t in texts)
    assert {a.subtype for a in msg_run.annotated.iter_annotations()} == {"FreeText"}
    # And they really are in the file - otherwise this test proves nothing.
    with pymupdf.open(msg.ANNOTATED) as pdf:
        subtypes = Counter(a.type[1] for p in pdf for a in (p.annots() or []))
    assert subtypes["Text"] == 4


# --- what the pipeline achieves -------------------------------------------
def test_almost_every_annotation_is_reproduced(msg_score):
    """Recall: of the sponsor's 206 statements, how many came back at all."""
    assert msg_score.recall == 1.0, msg.report(msg_score)


def test_almost_nothing_is_invented(msg_score):
    """Precision: of what we drew, how much the sponsor also drew.

    The two have to be read together. Drawing three annotations perfectly and
    losing two hundred scores 1.0 here and is not an improvement.
    """
    assert msg_score.precision >= 0.98, msg.report(msg_score)


def test_annotations_land_where_the_sponsor_put_them(msg_score):
    """The headline: distance from the original box's centre, in points.

    Zero, for most of them: the box is drawn at the recorded left edge, on the
    recorded row, at the recorded width, so it lands on the sponsor's own
    rectangle. What is left is the handful that had to be moved to clear
    something, and those are reported as adjustments rather than hidden.
    """
    assert msg_score.median_distance == 0.0, msg.report(msg_score)
    assert msg_score.within(12) >= 0.92
    assert msg_score.within(72) >= 0.98


def test_the_left_edge_is_essentially_exact(msg_score):
    """What a reader actually follows down a column of markup.

    Immune to the padding difference that centre distance is not, which is why
    it is the fairer read of placement - and it says the placement arithmetic is
    reproducing the original position rather than approximating it.
    """
    left = sorted(abs(p.drawn_rect[0] - p.truth.rect[0]) for p in msg_score.matched)
    assert left[len(left) // 2] <= 0.5
    assert sum(1 for v in left if v <= 4) / len(left) >= 0.92


def test_the_house_style_is_reproduced(msg_score):
    """Colour, font family and size, as re-read from the written PDF.

    Font is scored by family rather than by name: PyMuPDF can only embed the
    base-14 faces in an annotation appearance, so a study drawn in Arial can
    never come back as Arial, and scoring the name would measure the PDF library
    rather than anything decided here.
    """
    assert msg_score.style_match("color") >= 0.97
    assert msg_score.style_match("font") == 1.0
    assert msg_score.style_match("size") == 1.0


def test_nothing_is_drawn_off_its_page(msg_run):
    for p in msg_run.placements:
        page = msg_run.blank.page(p.page)
        assert 0 <= p.rect.x0 and p.rect.x1 <= page.width + 0.5, p
        assert 0 <= p.rect.y0 and p.rect.y1 <= page.height + 0.5, p


def test_no_two_annotations_overlap(msg_run):
    """Silently overprinted markup is the failure a reviewer finds by eye, on
    page 40, after the file has gone out."""
    by_page: dict[int, list] = {}
    for p in msg_run.placements:
        by_page.setdefault(p.page, []).append(p.rect)
    for page, rects in by_page.items():
        clashes = [(a, b) for i, a in enumerate(rects) for b in rects[i + 1:]
                   if a.h_overlap(b) > 0 and a.v_overlap(b) > 0]
        assert not clashes, f"page {page}: {clashes[:3]}"


def test_the_output_reads_back_as_what_was_written(msg_run):
    """The strongest claim available: the thing produced is the thing readable.

    Re-parsed with the same parser - form detection, field extraction,
    classification and linking all running fresh on the output.
    """
    again = parse_pdf(msg_run.out_pdf)
    drawn = Counter(msg.key(p.text) for p in msg_run.placements)
    read = Counter(msg.key(a.text) for a in again.iter_annotations())
    assert read == drawn


# --- the structural properties the score depends on ------------------------
def test_the_two_parses_agree_on_every_form_name(msg_run):
    """The property the whole pipeline hangs off.

    The primary key is `(form_name, field_text)`, so if the annotated CRF calls
    page 6 "DEMOGRAPHY" and the blank calls it something else, nothing learned
    from that page can ever be found again. It is also the sharpest available
    test of form detection: the two files print identical text, so any
    disagreement is the parser disagreeing with itself.
    """
    disagreements = [(a.number, a.form_name, b.form_name)
                     for a, b in zip(msg_run.annotated.pages, msg_run.blank.pages)
                     if a.form_name != b.form_name]
    assert disagreements == []


def test_forms_are_named_by_their_printed_titles(msg_run):
    """Not by the study identification band, which is what "topmost heading" gets.

    Every page of this CRF opens with "CDISC Study CDISC01" set bold above and
    to the left of the form's own name. Taking the topmost bold heading named all
    22 pages after the study and collapsed the CRF into three forms.
    """
    names = {p.number: p.form_name for p in msg_run.blank.pages}
    assert names[4] == names[5] == "ELIGIBILITY CRITERIA"
    assert names[6] == "DEMOGRAPHY"
    assert names[8] == "MEDICAL AND SURGICAL HISTORY"
    assert names[21] == "ADVERSE EVENTS"
    assert names[22] == "PRIOR / CONCOMITANT MEDICATIONS"
    assert not any("CDISC01" in n for n in names.values())


def test_a_two_page_form_is_one_form(msg_run):
    """"(PAGE 2 OF 2)" is a continuation marker, not a page number to recoil from."""
    cornell = [p.number for p in msg_run.blank.pages
               if p.form_name.startswith("CORNELL SCALE")]
    assert cornell == [14, 15]
    form = msg_run.blank.form(msg_run.blank.page(14).form_name)
    assert form.pages == [14, 15] and form.continuation_pages == [15]


def test_landscape_pages_are_read_in_reading_order(msg_run):
    """Pages 9, 21 and 22 carry /Rotate 90.

    PyMuPDF hands back their coordinates in unrotated page space while
    `page.rect` reports the rotated view, so every "same row" and column test
    was asking about the wrong axis on those three pages - a sixth of this CRF's
    markup. Rotating once at extraction is what fixes it, and this is what says
    it stayed fixed: the pages come out landscape, with fields on them.
    """
    for number in (9, 21, 22):
        page = msg_run.blank.page(number)
        assert page.rotation == 90
        assert (page.width, page.height) == (792.0, 612.0)
        assert page.fields, f"page {number} yielded no fields"
        for f in page.fields:
            assert f.bbox.x1 <= page.width and f.bbox.y1 <= page.height


def test_a_landscape_page_reproduces_its_markup(msg_score):
    """The three turned pages carry 35 of the 206 annotations between them."""
    turned = [p for p in msg_score.pairs if p.truth.page in (9, 21, 22)]
    assert len(turned) == 35
    assert all(p.matched for p in turned)


def test_the_findings_convention_is_classified(msg_run):
    """`QSORRES when QSTESTCD = MMSEA1` - a third of this file.

    Every SDTM findings domain is annotated this way, and all seventy of them
    read as unclassified prose before `CONDITIONAL_VARIABLE` existed: no
    variable parsed, no domain resolved, and `NOTE` shown to the reviewer for
    the most structured thing on the page.
    """
    by_type = Counter(a.annot_type for a in msg_run.annotated.iter_annotations())
    assert by_type[ann.CONDITIONAL_VARIABLE] >= 55
    assert by_type[ann.NOTE] <= 6

    a = next(a for a in msg_run.annotated.iter_annotations()
             if a.text.startswith("VSORRES / VSORRESU"))
    assert a.parsed["variables"] == ["VSORRES", "VSORRESU"]
    assert a.parsed["condition"] == {"variable": "VSTESTCD",
                                     "values": ["SYSBP", "DIABP"]}
    assert a.parsed["domain"] == "VS"


def test_every_annotation_reaches_a_row(msg_run):
    """Markup that reaches no field must still be somebody's to review.

    It used to be dropped unless it sat above the page's first field, which lost
    35 of this CRF's annotations - not to a wrong answer, to no answer: not in
    the workbook, not in the corpus, not on the output, and not in front of the
    reviewer who was meant to be finding them.
    """
    staged = {r.row_id for r in msg_run.imported.rows}
    for a in msg_run.annotated.iter_annotations():
        assert a.scope in ("FIELD", "FORM")
    assert staged, "no rows at all"
    parsed = {msg.key(a.text) for a in msg_run.annotated.iter_annotations()}
    proposed = {msg.key(r.annotation_text) for r in msg_run.imported.rows
                if r.annotation_text}
    lost = parsed - proposed
    assert not lost, sorted(lost)[:10]


# --- the two ways of holding a corpus --------------------------------------
@pytest.mark.parametrize("attribute,floor", [
    ("recall", 1.0), ("precision", 0.98),
])
def test_a_sqlite_corpus_reaches_the_same_answers(tmp_path, msg_score,
                                                  attribute, floor):
    """The path the CLI actually takes: `--db` in, `--corpus` out, PDFs never
    reopened. It is a second implementation of every lookup in `prefill` and
    `style`, and nothing but this keeps the two from drifting apart.
    """
    from tests.msg_pipeline import run
    scored = msg.score(run(tmp_path / "kb", via_kb=True).reread())
    assert getattr(scored, attribute) >= floor
    assert abs(getattr(scored, attribute) - getattr(msg_score, attribute)) < 0.01
    assert abs(scored.median_distance - msg_score.median_distance) < 1.0


def test_almost_nothing_has_to_be_moved(msg_run):
    """A move is a placement a reviewer has to check, so there should be few.

    The count matters more than it looks. When the writer treated every word on
    the page as an obstacle and searched the whole page width for a gap, every
    annotation "moved" and most of them moved a long way - which is the same as
    reporting nothing, because a list of two hundred things to check is not a
    list. Trusting a position a previous study actually shipped, and bounding
    how far the search may carry a box, is what got it down to this.
    """
    moved = msg_run.written.adjusted
    assert len(moved) <= 20, [f"{p.label}: {p.adjustments}" for p in moved]
    assert all(p.adjustments for p in moved)
    # And the substitution note is not counted as a move: this corpus is set in
    # Arial, which no PDF annotation appearance can embed, so every single row
    # substitutes a font and none of them is a placement problem.
    assert msg_run.written.to_dict()["substituted_fonts"] == [
        "font substituted for Arial,BoldItalic"]


def test_a_wrapped_annotation_keeps_its_shape(msg_run, msg_truth):
    """The sponsor sets long markup in a narrow column, not across the page.

    "Reason for Discontinuation / 0 Ongoing / 1 Adverse Event / ..." is six
    lines in 189 points. Drawn as one line it is over 600 points wide, will not
    fit beside anything, and the placement search gives up and clamps it to the
    margin - so the shape has to be reproduced, not just the position.
    """
    truth = next(t for t in msg_truth if t.text.startswith("Reason for Discontinuation"))
    drawn = next(p for p in msg_run.placements
                 if p.text.startswith("Reason for Discontinuation"))
    assert abs(drawn.rect.width - (truth.rect[2] - truth.rect[0])) < 1.0
    assert drawn.rect.height > 3 * truth.font_size


def test_markup_on_a_turned_page_reads_horizontally(msg_run):
    """The rect goes in unrotated page space; the glyphs must not.

    Without saying so, a FreeText's text is laid out along the *unrotated* axes,
    so on a landscape CRF page - a portrait page carrying /Rotate 90 - every
    annotation came out turned on its side and clipped by its own box. Correctly
    positioned and completely unreadable, which is the worst of both.
    """
    with pymupdf.open(msg_run.out_pdf) as pdf:
        for number in (9, 21, 22):
            page = pdf[number - 1]
            assert page.rotation == 90
            drawn = list(page.annots() or [])
            assert drawn, f"page {number} has no annotations"
            for a in drawn:
                kind, value = pdf.xref_get_key(a.xref, "Rotate")
                assert (kind, value) == ("int", "90"), (number, a.info)

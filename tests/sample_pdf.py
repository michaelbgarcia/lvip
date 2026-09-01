"""Builds a small synthetic aCRF PDF: real text + real FreeText annotations.

Mirrors the shapes later phases must handle - a domain header, plain variables,
a constant assignment, a supp qualifier, [NOT SUBMITTED], and a "See Page X"
continuation page.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

RED = (0.85, 0.1, 0.1)
# The study's house style is red on pale yellow. Two colours, not one: a corpus
# where the fill and the text colour differ is the only one that can catch a
# pipeline collapsing them back together.
YELLOW = (1.0, 0.98, 0.77)

# (form title, [(label, y)], [(annotation text, x, y)])
PAGES = [
    (
        "Form: Demographics",
        [("Date of birth", 120), ("Age", 150), ("Sex", 180), ("Race", 210)],
        [("DM=Demographics", 330, 88),
         ("BRTHDTC", 330, 118),
         ("AGE", 330, 148),
         ("SEX", 330, 178),
         ("RACE", 330, 208),
         ("RACEOTH when SUPPDM.QNAM=RACEOTH", 330, 232),
         ("[NOT SUBMITTED]", 330, 262)],
    ),
    (
        "Form: Medical History",
        [("Condition", 120), ("Start Date", 150), ("Ongoing", 180)],
        [("MH=Medical History", 330, 88),
         ("MHTERM", 330, 118),
         ("MHSTDTC", 330, 148),
         ("MHENRF=ONGOING", 330, 178)],
    ),
    (
        "Form: Medical History (continued)",
        [("Condition", 120), ("Stop Date", 150)],
        [("See Page 2", 330, 88), ("MHENDTC", 330, 148)],
    ),
]


# Page 4 reproduces a real Disposition page: a wrapped 6-line question in the
# left column, radio options in the right column, rules bounding the body, and
# an annotation sitting in the gutter between the columns.
WRAP_LINES = ["Please record", "protocol", "version on", "which subject",
              "is currently", "enrolled:"]
OPTIONS = ["Original", "Amendment 1", "Amendment 2", "Amendment 3", "Amendment 4"]


def _disposition_page(doc: pymupdf.Document) -> None:
    page = doc.new_page()
    page.insert_text((60, 60), "Version", fontsize=10, fontname="hebo")
    page.insert_text((60, 76), "Generated On: 15 Nov 2024 18:35:29", fontsize=10, fontname="hebo")
    page.draw_line(pymupdf.Point(55, 92), pymupdf.Point(540, 92))     # body top rule

    for i, txt in enumerate(WRAP_LINES):                              # 12pt pitch = wrapped
        page.insert_text((60, 112 + i * 12), txt, fontsize=10)
    for i, txt in enumerate(OPTIONS):                                 # 20pt pitch = distinct
        page.insert_text((430, 112 + i * 20), txt, fontsize=10)
        page.draw_circle(pymupdf.Point(515, 108 + i * 20), 5)         # radio control

    page.draw_line(pymupdf.Point(55, 216), pymupdf.Point(540, 216))   # body bottom rule
    for text, rect in (
        ("DS=Disposition", pymupdf.Rect(60, 20, 240, 40)),
        ('SUPPDS.QVAL when QNAM = "PROTVER"', pymupdf.Rect(250, 130, 420, 160)),  # in gutter
    ):
        a = page.add_freetext_annot(rect, text, fontsize=9, text_color=RED,
                                    fill_color=YELLOW)
        a.set_info(content=text, title="annotator")
        a.update(text_color=RED, fill_color=YELLOW)


# Page 5 reproduces an Eligibility Criteria page: right-aligned criteria, uniform
# line pitch within *and between* items, one checkbox per item. Nothing but the
# numbering and the checkboxes marks where one criterion ends and the next begins.
# Drawn line by line, so grouping cannot lean on MuPDF's block numbering.
CRITERIA = [
    ["1. Have hypochondroplasia or", "short stature condition other than",
     "ACH (e.g., trisomy 21,", "pseudoachondroplasia)"],
    ["2. Have any of the following:", "Hypo/hyper-thyroidism;",
     "Insulin-requiring diabetes", "mellitus; Autoimmune",
     "inflammatory disease;", "Inflammatory bowel disease;", "Autonomic neuropathy"],
    ["3. Have a history of any of the", "following: Renal insufficiency;",
     "Chronic anemia; Baseline", "systolic BP < 70 mm Hg or", "recurrent",
     "symptomatic/orthostatic", "hypotension; Cardiac or vascular", "disease"],
    ["4. Have a clinically significant", "finding or arrhythmia on",
     "screening ECG that indicates", "abnormal cardiac function or",
     "conduction or QTc-F > 450 msec"],
]
RIGHT_MARGIN = 480.0


def _eligibility_page(doc: pymupdf.Document) -> None:
    page = doc.new_page()
    page.insert_text((60, 40), "Form: Eligibility Criteria", fontsize=10, fontname="hebo")
    page.insert_text((60, 56), "Generated On: 10 Jun 2019 17:31:09", fontsize=10, fontname="hebo")
    page.draw_line(pymupdf.Point(55, 66), pymupdf.Point(540, 66))

    y = 90.0
    for item in CRITERIA:
        for i, txt in enumerate(item):
            w = pymupdf.get_text_length(txt, fontname="helv", fontsize=10)
            page.insert_text((RIGHT_MARGIN - w, y), txt, fontsize=10)   # right aligned
            if i == 0:
                page.draw_circle(pymupdf.Point(492, y - 3), 6)          # one box per item
            y += 12                                                     # uniform pitch
    for text, rect in (
        ("EL=Eligibility", pymupdf.Rect(60, 10, 200, 28)),
        ("See Page 7", pymupdf.Rect(250, 30, 340, 50)),
    ):
        a = page.add_freetext_annot(rect, text, fontsize=9, text_color=RED,
                                    fill_color=YELLOW)
        a.set_info(content=text, title="annotator")
        a.update(text_color=RED, fill_color=YELLOW)


def build(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for title, fields, annots in PAGES:
        page = doc.new_page()                       # A4 default
        page.insert_text((60, 60), "STUDY XYZ-123", fontsize=9)
        page.insert_text((60, 88), title, fontsize=13, fontname="hebo")
        for label, y in fields:
            page.insert_text((60, y), label, fontsize=10)
            page.draw_rect(pymupdf.Rect(180, y - 11, 300, y + 4))   # answer box
        for text, x, y in annots:
            rect = pymupdf.Rect(x, y - 12, x + 210, y + 4)
            a = page.add_freetext_annot(rect, text, fontsize=8, text_color=RED,
                                        fill_color=YELLOW)
            a.set_info(content=text, title="annotator")
            a.update(text_color=RED, fill_color=YELLOW)
    _disposition_page(doc)
    _eligibility_page(doc)
    doc.save(path)
    doc.close()
    return path


# A second, unrelated study. Its Adverse Events form has a "Start Date" field
# too - mapping to AESTDTC, not MHSTDTC. Two studies in one knowledge base is
# what makes the (form_name, field_text) key testable rather than merely stated.
SECOND_STUDY = [
    ("Form: Adverse Events",
     [("Adverse event", 120), ("Start Date", 150), ("Stop Date", 180)],
     [("AE=Adverse Events", 330, 88), ("AETERM", 330, 118),
      ("AESTDTC", 330, 148), ("AEENDTC", 330, 178)]),
]


def build_second_study(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for title, fields, annots in SECOND_STUDY:
        page = doc.new_page()
        page.insert_text((60, 60), "STUDY ABC-999", fontsize=9)
        page.insert_text((60, 88), title, fontsize=13, fontname="hebo")
        for label, y in fields:
            page.insert_text((60, y), label, fontsize=10)
            page.draw_rect(pymupdf.Rect(180, y - 11, 300, y + 4))
        for text, x, y in annots:
            a = page.add_freetext_annot(pymupdf.Rect(x, y - 12, x + 210, y + 4),
                                        text, fontsize=8, text_color=RED,
                                        fill_color=YELLOW)
            a.set_info(content=text, title="annotator")
            a.update(text_color=RED, fill_color=YELLOW)
    doc.save(path)
    doc.close()
    return path


# A third study, colour-coded by domain, reproducing the shape of a real
# Informed Consent page. Two things the other two fixtures cannot express:
#
# * **Several form-level annotations on one page.** DS=Disposition and
#   DM=Demographics side by side, plus a form-level constant beside them. None
#   of it belongs to a printed question.
# * **Fill that follows the domain, not the annotation type.** DSTERM and
#   RFICDTC are both plain VARIABLE markup on the same field on the same row,
#   drawn in different colours because one is DS and the other is DM. RFICDTC is
#   the hard half: DM's own variables carry no prefix, so nothing in the text
#   says DM and only the corpus's own history can answer for it.
DS_FILL = (1.0, 0.98, 0.77)      # pale yellow
DM_FILL = (0.78, 0.89, 0.96)     # pale blue

CONSENT_FORM_LEVEL = [
    ("DS=Disposition", 60, 22, DS_FILL),
    ("DM=Demographics", 190, 22, DM_FILL),
    ("DSCAT = PROTOCOL MILESTONE", 320, 22, DS_FILL),
]
CONSENT_FIELD_LEVEL = [
    ("DSTERM", 250, 130, DS_FILL),
    ("DSDECOD=INFORMED CONSENT OBTAINED", 310, 130, DS_FILL),
    ("RFICDTC", 470, 130, DM_FILL),
    ("DSSTDTC", 520, 130, DS_FILL),
]
# A Demographics page, so the DM fill is attested by more than one statement.
# Written qualified (DM.BRTHDTC), which is how a sponsor says "DM" about a
# variable whose own name does not.
DEMOG_MARKUP = [
    ("DM=Demographics", 60, 22, DM_FILL),
    ("DM.BRTHDTC", 250, 130, DM_FILL),
    ("DM.SEX", 250, 160, DM_FILL),
]


def _freetext(page, text, x, y, fill, width=200):
    a = page.add_freetext_annot(pymupdf.Rect(x, y - 12, x + width, y + 4), text,
                                fontsize=8, text_color=RED, fill_color=fill)
    a.set_info(content=text, title="annotator")
    a.update(text_color=RED, fill_color=fill)


def build_colour_coded_study(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()

    page = doc.new_page()
    page.insert_text((60, 60), "Form: Informed Consent", fontsize=13, fontname="hebo")
    page.insert_text((60, 76), "Generated On: 10 Jun 2019 17:31:09", fontsize=9)
    page.insert_text((60, 130), "Date of informed consent", fontsize=10)
    page.draw_rect(pymupdf.Rect(180, 119, 240, 134))
    for text, x, y, fill in CONSENT_FORM_LEVEL + CONSENT_FIELD_LEVEL:
        _freetext(page, text, x, y, fill, width=min(200, 6 * len(text)))

    page = doc.new_page()
    page.insert_text((60, 60), "Form: Demographics", fontsize=13, fontname="hebo")
    page.insert_text((60, 130), "Date of birth", fontsize=10)
    page.insert_text((60, 160), "Sex", fontsize=10)
    for y in (130, 160):
        page.draw_rect(pymupdf.Rect(180, y - 11, 240, y + 4))
    for text, x, y, fill in DEMOG_MARKUP:
        _freetext(page, text, x, y, fill, width=min(200, 6 * len(text)))

    doc.save(path)
    doc.close()
    return path


if __name__ == "__main__":
    print(build(Path(__file__).resolve().parents[1] / "data" / "sample_acrf.pdf"))

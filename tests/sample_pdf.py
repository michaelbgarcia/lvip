"""Builds a small synthetic aCRF PDF: real text + real FreeText annotations.

Mirrors the shapes later phases must handle - a domain header, plain variables,
a constant assignment, a supp qualifier, [NOT SUBMITTED], and a "See Page X"
continuation page.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

RED = (0.85, 0.1, 0.1)

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
        a = page.add_freetext_annot(rect, text, fontsize=9, text_color=RED)
        a.set_info(content=text, title="annotator")
        a.update()


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
            a = page.add_freetext_annot(rect, text, fontsize=8, text_color=RED)
            a.set_info(content=text, title="annotator")
            a.update()
    _disposition_page(doc)
    doc.save(path)
    doc.close()
    return path


if __name__ == "__main__":
    print(build(Path(__file__).resolve().parents[1] / "data" / "sample_acrf.pdf"))

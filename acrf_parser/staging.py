"""Phase 9 - the staging workbook.

The contract between all three lanes of the workflow. Python parses the blank
CRF and pre-fills what history already answers; the agent fills the rows history
could not reach; a human reviews the whole thing in Excel; and it comes back to
Python to be written onto the PDF.

Design follows from who reads it:

* **Copilot / the agent** works best on one flat table with self-describing
  column names, so the work surface is a single sheet with one row per field and
  no merged cells, no nesting.
* **The human** needs to see *why* a row was filled, so the match tier, score and
  source study sit beside the suggestion rather than in a hidden audit log. A
  suggestion that looks as authoritative as a fact is the failure mode here.
* **The importer** needs geometry to place the annotation, but geometry is noise
  to the first two readers and must not be hand-edited. So it lives on a locked
  `Geometry` sheet keyed by `row_id`, out of the way and impossible to typo.

Only `EXACT_KEY` rows arrive as AUTO. Everything else is NEEDS_REVIEW or
NEEDS_MAPPING regardless of score, so no fuzzy match can slip through unlooked-at.

The unit of a row is one **annotation**, not one field. A CRF field routinely
carries several statements - "Date of informed consent" is DSTERM, DSDECOD=
INFORMED CONSENT OBTAINED, RFICDTC and DSSTDTC - so the key is (`row_id`,
`annot_seq`): the field it belongs to, and which of that field's annotations it
is. History exports the whole set it has seen; a reviewer adds the rest by
copying a row and giving it the next free seq. One statement per row is what
lets each annotation keep its own type, colour and placement, and what lets the
importer name the one that is wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import prefill as pf
from .models import Document
from .prefill import PrefillIndex, prefill_document
from .style import HouseStyle, derive_house_style

SHEET_WORK, SHEET_GEOM = "Annotations", "Geometry"
SHEET_STYLE, SHEET_FORMS, SHEET_README = "HouseStyle", "Forms", "Instructions"

STATUSES = ["AUTO", "NEEDS_REVIEW", "NEEDS_MAPPING", "APPROVED", "REJECTED"]
ANNOT_TYPES = ["VARIABLE", "CONSTANT_ASSIGNMENT", "SUPP_QUALIFIER", "NOT_SUBMITTED",
               "DERIVATION_RULE", "CROSS_REFERENCE", "DOMAIN_HEADER", "NOTE"]

# Columns the human and the agent fill in. Everything else is evidence or is locked.
#
# `form_name` is editable, and deliberately so. A blank CRF carries no
# domain-header annotations, so a page with no printed title is inherited by the
# form above it - the Disposition page reads as "Medical History". The reviewer
# is the only one who can see that and fix it, and since the primary key is
# (form_name, field_text), an uncorrectable form name would file the pair under
# the wrong form forever.
#
# `annot_seq` is editable for the same reason: it is how a reviewer says "this
# field needs a second annotation". They copy the row, change the seq, and write
# the second statement - no other column tells the importer that is what they
# meant.
EDITABLE = ("annot_seq", "form_name", "final_variable", "final_annotation",
            "final_annot_type", "status", "reviewer_note", "color_rgb", "font_name",
            "font_size", "placement")

HEADERS = [
    ("row_id", 10), ("annot_seq", 10), ("form_name", 22), ("page", 6), ("field_text", 42),
    ("item_number", 11), ("options", 26), ("control", 9),
    ("suggested_variable", 18), ("suggested_annotation", 26), ("suggested_annot_type", 20),
    ("match_tier", 22), ("match_score", 11), ("match_source", 38), ("match_reason", 52),
    ("known_aliases", 26),
    ("final_variable", 16), ("final_annotation", 26), ("final_annot_type", 18),
    ("status", 15), ("reviewer_note", 30),
    ("color_rgb", 14), ("font_name", 11), ("font_size", 10), ("placement", 17),
]

FILL_AUTO = PatternFill("solid", fgColor="E8F5E9")      # settled: nothing to do
FILL_REVIEW = PatternFill("solid", fgColor="FFF8E1")    # suggestion: look at it
FILL_MAPPING = PatternFill("solid", fgColor="FFEBEE")   # the agent's real work
FILL_HEADER = PatternFill("solid", fgColor="263238")
FILL_LOCKED = PatternFill("solid", fgColor="F5F5F5")
STATUS_FILL = {"AUTO": FILL_AUTO, "NEEDS_REVIEW": FILL_REVIEW,
               "NEEDS_MAPPING": FILL_MAPPING}


@dataclass
class StagingRow:
    """One annotation of one field of the blank CRF, as it appears in the workbook."""
    row_id: str
    values: dict[str, Any]
    geometry: dict[str, Any]
    annot_seq: int = 1

    @property
    def key(self) -> tuple[str, int]:
        """What identifies this row in the workbook, and on the way back in."""
        return (self.row_id, self.annot_seq)


def build_staging(doc: Document, index: PrefillIndex | None = None,
                  house: HouseStyle | None = None) -> list[StagingRow]:
    """Pre-fill a parsed blank CRF into staging rows.

    One row per annotation history proposes, and always at least one per field:
    a field history has nothing for still gets its NEEDS_MAPPING row, because a
    field with no row is a field nobody is asked about.

    With no index the sheet is still produced - every row simply arrives as
    NEEDS_MAPPING, which is the honest state of a first study with no history.
    """
    index = index or PrefillIndex()
    house = house or derive_house_style([])
    results = {r.field_id: r for r in prefill_document(doc, index)}

    rows: list[StagingRow] = []
    for page in doc.pages:
        for fld in page.fields:
            r = results.get(fld.id)
            candidates = r.annotations if r else [None]
            for seq, candidate in enumerate(candidates, start=1):
                rows.append(_row(doc, page, fld, r, candidate, seq, house))
    return rows


def _row(doc, page, fld, result, best, seq: int, house: HouseStyle) -> StagingRow:
    status = (result.status_of(best) if result and best
              else pf.NEEDS_MAPPING_STATUS)
    rule = house.for_type(best.annot_type if best and best.annot_type else "VARIABLE")
    return StagingRow(
        row_id=fld.id,
        annot_seq=seq,
        values={
            "row_id": fld.id,
            "annot_seq": seq,
            "form_name": fld.form_name,
            "page": fld.page,
            "field_text": fld.text,
            "item_number": fld.item_number,
            "options": " | ".join(fld.option_texts),
            "control": ",".join(fld.control_kinds),
            "suggested_variable": best.variable if best else "",
            "suggested_annotation": best.annotation_text if best else "",
            "suggested_annot_type": best.annot_type if best else "",
            "match_tier": best.tier if best else pf.NEEDS_MAPPING,
            "match_score": round(best.confidence, 3) if best else 0.0,
            "match_source": best.source if best else "",
            "match_reason": "; ".join(best.evidence) if best else "",
            "known_aliases": ", ".join(result.aliases) if result else "",
            # An AUTO row is pre-accepted; anything else is left blank on purpose,
            # so a reviewer cannot mistake a suggestion for a decision.
            "final_variable": best.variable if status == pf.AUTO else "",
            "final_annotation": best.annotation_text if status == pf.AUTO else "",
            "final_annot_type": best.annot_type if status == pf.AUTO else "",
            "status": status,
            "reviewer_note": "",
            "color_rgb": _hex(rule.text_color),
            "font_name": rule.font_name,
            "font_size": rule.font_size or "",
            "placement": rule.placement,
        },
        geometry={
            "row_id": fld.id, "annot_seq": seq, "page": fld.page,
            "page_width": page.width, "page_height": page.height,
            **fld.bbox.relative(page.width, page.height),
            "offset_x_pct": rule.offset_x_pct, "offset_y_pct": rule.offset_y_pct,
            "group_id": fld.group_id, "field_confidence": fld.confidence,
        },
    )


def _hex(color) -> str:
    """RGB floats to the hex string a spreadsheet can show and a human can read."""
    if not color:
        return ""
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in color[:3])


# --- workbook --------------------------------------------------------------
def write_staging(doc: Document, path: str | Path, index: PrefillIndex | None = None,
                  house: HouseStyle | None = None) -> Path:
    """Write the staging workbook for one blank CRF."""
    rows = build_staging(doc, index, house)
    house = house or derive_house_style([])
    wb = Workbook()
    _work_sheet(wb, rows)
    _geometry_sheet(wb, rows)
    _style_sheet(wb, house)
    _forms_sheet(wb, doc, index or PrefillIndex())
    _readme_sheet(wb, doc, rows)
    wb.remove(wb["Sheet"]) if "Sheet" in wb.sheetnames else None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _work_sheet(wb: Workbook, rows: list[StagingRow]) -> None:
    ws = wb.create_sheet(SHEET_WORK, 0)
    names = [h for h, _ in HEADERS]
    ws.append(names)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL_HEADER
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for r in rows:
        ws.append([r.values.get(n, "") for n in names])

    editable = {names.index(n) + 1 for n in EDITABLE}
    for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
        status = ws.cell(row=i, column=names.index("status") + 1).value
        fill = STATUS_FILL.get(status)
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=cell.column in
                                       {names.index("field_text") + 1,
                                        names.index("match_reason") + 1})
            # Lock everything the importer relies on; leave the work columns open.
            cell.protection = Protection(locked=cell.column not in editable)
            if cell.column not in editable:
                cell.fill = FILL_LOCKED
        if fill:
            ws.cell(row=i, column=names.index("status") + 1).fill = fill

    _validate(ws, names, "status", STATUSES, len(rows))
    _validate(ws, names, "final_annot_type", ANNOT_TYPES, len(rows))
    for idx, (_, width) in enumerate(HEADERS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = ws.dimensions
    # Sheet protection is deliberately not enabled: it would block Copilot from
    # writing at all. The locked flags mark intent, and the importer is what
    # actually enforces it - validation on the way back in, not a UI lock.


def _validate(ws, names: list[str], column: str, options: list[str], n_rows: int) -> None:
    col = get_column_letter(names.index(column) + 1)
    dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"',
                        allow_blank=True, showDropDown=False)
    ws.add_data_validation(dv)
    dv.add(f"{col}2:{col}{max(2, n_rows + 1)}")


def _geometry_sheet(wb: Workbook, rows: list[StagingRow]) -> None:
    """Locked, keyed by (row_id, annot_seq). The importer's input, not the human's.

    A sibling row a reviewer adds has no geometry row of its own - they cannot
    write to this sheet, and should not have to. The importer inherits the
    field's geometry from seq 1 instead; every row of one field describes the
    same box on the page, so there is nothing to lose in doing so.
    """
    ws = wb.create_sheet(SHEET_GEOM)
    keys = ["row_id", "annot_seq", "page", "page_width", "page_height",
            "rel_x_pct", "rel_y_pct", "rel_w_pct", "rel_h_pct",
            "offset_x_pct", "offset_y_pct", "group_id", "field_confidence"]
    ws.append(keys)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL_HEADER
    for r in rows:
        ws.append([r.geometry.get(k, "") for k in keys])
    for i in range(1, len(keys) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 14
    ws.freeze_panes = "B2"


def _style_sheet(wb: Workbook, house: HouseStyle) -> None:
    """The derived house style, with agreement, so unsettled rules get decided once."""
    ws = wb.create_sheet(SHEET_STYLE)
    ws.append(["scope", "samples", "color_rgb", "color_agreement", "font_name",
               "font_size", "size_agreement", "placement", "placement_agreement",
               "offset_x_pct", "offset_y_pct", "settled", "evidence"])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL_HEADER
    for rule in [house.default, *[house.by_type[k] for k in sorted(house.by_type)]]:
        ws.append([rule.scope, rule.samples, _hex(rule.text_color), rule.color_agreement,
                   rule.font_name, rule.font_size, rule.size_agreement, rule.placement,
                   rule.placement_agreement, rule.offset_x_pct, rule.offset_y_pct,
                   "yes" if rule.settled else "NO", "; ".join(rule.evidence)])
        if not rule.settled:
            for cell in ws[ws.max_row]:
                cell.fill = FILL_REVIEW
    for i, w in enumerate([22, 9, 11, 15, 11, 10, 14, 16, 18, 13, 13, 9, 70], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def _forms_sheet(wb: Workbook, doc: Document, index: PrefillIndex) -> None:
    """Form inventory, with the domain resolved.

    A blank CRF has no domain-header annotations, so `domain` is empty on every
    form it parses. Where history knows the form, its domain is filled in from
    there and `domain_source` says so - the importer validates against this, and
    a domain nobody could establish must not read as an agreed one.
    """
    ws = wb.create_sheet(SHEET_FORMS)
    ws.append(["form_name", "domain", "domain_source", "pages", "continuation_pages",
               "confidence", "source", "evidence"])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = FILL_HEADER
    for f in doc.forms:
        domain, origin = f.domain, "this CRF"
        if not domain:
            domain, origin = index.domain_for(f.name), "previous studies"
        ws.append([f.name, domain, origin if domain else "unknown",
                   ", ".join(map(str, f.pages)),
                   ", ".join(map(str, f.continuation_pages)), round(f.confidence, 2),
                   f.source, "; ".join(f.evidence)])
    for i, w in enumerate([26, 9, 17, 14, 18, 11, 22, 60], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def _readme_sheet(wb: Workbook, doc: Document, rows: list[StagingRow]) -> None:
    """Written for both readers: a human skimming, and an agent given the sheet."""
    ws = wb.create_sheet(SHEET_README)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.values["status"]] = counts.get(r.values["status"], 0) + 1
    fields = len({r.row_id for r in rows})
    lines = [
        ("aCRF staging workbook", True),
        (f"Source: {Path(doc.path).name} · {doc.page_count} pages · "
         f"{len(doc.forms)} forms · {fields} fields · {len(rows)} annotation rows", False),
        (f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}", False),
        ("", False),
        ("How to use this workbook", True),
        ("1. Work the 'Annotations' sheet. One row per ANNOTATION, not per field:", False),
        ("   a field with four annotations has four rows, sharing one row_id and", False),
        ("   numbered 1, 2, 3, 4 in annot_seq.", False),
        ("2. Rows are pre-filled from previously annotated studies where the", False),
        ("   (form_name, field_text) pair was seen before. Those arrive as AUTO", False),
        ("   with final_variable already set - confirm, do not retype.", False),
        ("3. NEEDS_REVIEW means a suggestion was found by similarity, not by an", False),
        ("   exact match. Check match_tier, match_score and match_source, then", False),
        ("   put your decision in final_variable / final_annotation.", False),
        ("4. NEEDS_MAPPING means history had nothing. These need real mapping work.", False),
        ("5. Check form_name. A page with no printed title is assumed to belong to", False),
        ("   the form above it, which is often wrong on a blank CRF. Correct it here -", False),
        ("   the mapping is stored under (form_name, field_text), so a wrong form name", False),
        ("   files the answer where nobody will find it again.", False),
        ("6. Set status to APPROVED or REJECTED on every row before importing.", False),
        ("", False),
        ("Adding a second annotation to a field", True),
        ("- Copy the whole row, paste it directly beneath, and put the next free", False),
        ("  number in annot_seq (leave annot_seq blank and it will be numbered for", False),
        ("  you, with a warning). Keep row_id exactly as it is - that is what says", False),
        ("  which field the annotation belongs to.", False),
        ("- Write one statement per row. 'DSTERM' and 'RFICDTC' are two rows, not", False),
        ("  one cell holding both: each gets its own type, colour and placement,", False),
        ("  and they are drawn side by side in annot_seq order.", False),
        ("- Delete a surplus row rather than blanking it, but never delete every", False),
        ("  row of a field - each field needs at least one.", False),
        ("", False),
        ("Rules", True),
        ("- Only fill the unshaded columns. Shaded columns are evidence and are", False),
        ("  regenerated on every export; edits to them are ignored on import.", False),
        ("- Do not sort or reorder rows, and do not invent a row_id. row_id is the", False),
        ("  key that ties each row to its position on the PDF; a row_id that is not", False),
        ("  in this CRF cannot be placed. Adding rows is allowed only as described", False),
        ("  above: a copy of an existing row, with a new annot_seq.", False),
        ("- (row_id, annot_seq) must be unique. Two rows with the same pair is an", False),
        ("  error, because neither can be told from the other on the way back in.", False),
        ("- Formatting columns are pre-filled from the house style measured across", False),
        ("  previous studies. Change them only for a deliberate exception.", False),
        ("- See the 'HouseStyle' sheet for rules marked settled=NO. Those are cases", False),
        ("  where past studies disagreed and someone needs to decide once.", False),
        ("", False),
        ("Status of this workbook", True),
    ]
    for status in STATUSES:
        if counts.get(status):
            lines.append((f"  {status}: {counts[status]} row(s)", False))
    for text, bold in lines:
        ws.append([text])
        if bold:
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True, size=12)
    ws.column_dimensions["A"].width = 96


def summarize_staging(rows: list[StagingRow]) -> dict[str, Any]:
    tiers: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for r in rows:
        tiers[r.values["match_tier"]] = tiers.get(r.values["match_tier"], 0) + 1
        statuses[r.values["status"]] = statuses.get(r.values["status"], 0) + 1
    total = len(rows) or 1
    per_field: dict[str, int] = {}
    for r in rows:
        per_field[r.row_id] = per_field.get(r.row_id, 0) + 1
    return {"rows": len(rows), "fields": len(per_field),
            "by_tier": dict(sorted(tiers.items())),
            "by_status": dict(sorted(statuses.items())),
            "auto_fill_rate": round(statuses.get("AUTO", 0) / total, 3),
            "multi_annotation_fields": sum(1 for n in per_field.values() if n > 1)}

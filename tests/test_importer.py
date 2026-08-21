"""Phase 9c tests: reading the staging workbook back, per row."""
import pytest
from openpyxl import load_workbook

from acrf_parser import importer as imp
from acrf_parser import staging as st
from acrf_parser.importer import WARNING, read_staging, write_review_copy


@pytest.fixture
def returned(blank_doc, index, house, tmp_path):
    """A workbook that has been out to Excel and edited - well and badly."""
    path = st.write_staging(blank_doc, tmp_path / "out.xlsx", index=index, house=house)
    wb = load_workbook(path)
    ws = wb[st.SHEET_WORK]
    names = [c.value for c in ws[1]]
    col = lambda n: names.index(n) + 1

    def edit(row, **cells):
        # Assign through .value: cell(..., value=None) is a no-op in openpyxl,
        # so the obvious spelling silently fails to clear a cell.
        for name, value in cells.items():
            ws.cell(row=row, column=col(name)).value = value

    for i in range(2, ws.max_row + 1):                 # reviewer approves the AUTO rows
        if ws.cell(row=i, column=col("status")).value == "AUTO":
            edit(i, status="APPROVED")
    out = tmp_path / "returned.xlsx"
    wb.save(out)
    return out, wb, ws, edit


def _read(path, doc):
    return read_staging(path, doc)


def test_a_clean_return_imports_whole(returned, blank_doc):
    path, wb, ws, _ = returned
    report = _read(path, blank_doc)
    assert len(report.rows) == 14
    assert len(report.approved()) == 9          # the AUTO rows the reviewer approved
    assert not report.errors


def test_rows_are_matched_by_row_id_not_position(returned, blank_doc, tmp_path):
    """Sorting a column in Excel must not shift annotations onto other fields."""
    path, wb, ws, _ = returned
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    for i, values in enumerate(reversed(rows), start=2):     # reverse every row
        for j, v in enumerate(values, start=1):
            ws.cell(row=i, column=j, value=v)
    shuffled = tmp_path / "shuffled.xlsx"
    wb.save(shuffled)
    report = _read(shuffled, blank_doc)
    assert not report.errors
    by_id = {r.row_id: r for r in report.rows}
    assert by_id["p1f0"].annotation_text == "BRTHDTC"
    assert by_id["p2f1"].annotation_text == "MHSTDTC"


def test_one_bad_row_does_not_block_the_others(returned, blank_doc, tmp_path):
    """The whole point of per-row validation."""
    path, wb, ws, edit = returned
    edit(2, color_rgb="crimson")                # row p1f0 only
    broken = tmp_path / "broken.xlsx"
    wb.save(broken)
    report = _read(broken, blank_doc)
    blocked = report.blocked()
    assert [r.row_id for r in blocked] == ["p1f0"]
    assert len(report.approved()) == 8          # the other eight still import


# --- identity --------------------------------------------------------------
def test_deleted_row_is_caught(returned, blank_doc, tmp_path):
    path, wb, ws, _ = returned
    ws.delete_rows(2)
    out = tmp_path / "deleted.xlsx"
    wb.save(out)
    report = _read(out, blank_doc)
    assert [i.code for i in report.issues] == ["MISSING_ROW"]
    assert "p1f0" in report.issues[0].message


def test_unknown_row_id_is_caught(returned, blank_doc, tmp_path):
    path, wb, ws, edit = returned
    edit(2, row_id="p9f9")
    out = tmp_path / "unknown.xlsx"
    wb.save(out)
    codes = {i.code for i in _read(out, blank_doc).errors}
    assert "UNKNOWN_ROW_ID" in codes and "MISSING_ROW" in codes


def test_duplicate_row_id_is_caught(returned, blank_doc, tmp_path):
    path, wb, ws, edit = returned
    edit(3, row_id="p1f0")
    out = tmp_path / "dupe.xlsx"
    wb.save(out)
    assert "DUPLICATE_ROW_ID" in {i.code for i in _read(out, blank_doc).errors}


def test_editing_the_label_out_from_under_its_row_id_is_caught(returned, blank_doc, tmp_path):
    path, wb, ws, edit = returned
    edit(2, field_text="Age of subject")
    out = tmp_path / "altered.xlsx"
    wb.save(out)
    row = next(r for r in _read(out, blank_doc).rows if r.row_id == "p1f0")
    assert not row.ok
    assert [i.code for i in row.issues] == ["ROW_ALTERED"]


# --- decisions -------------------------------------------------------------
def test_approved_without_an_annotation_is_blocked(returned, blank_doc, tmp_path):
    path, wb, ws, edit = returned
    edit(2, final_variable=None, final_annotation=None)
    out = tmp_path / "empty.xlsx"
    wb.save(out)
    row = next(r for r in _read(out, blank_doc).rows if r.row_id == "p1f0")
    assert "MISSING_DECISION" in {i.code for i in row.issues} and not row.ready


def test_unreviewed_rows_warn_and_are_not_written(returned, blank_doc):
    path, wb, ws, _ = returned
    report = _read(path, blank_doc)
    pending = [r for r in report.rows if r.status == "NEEDS_MAPPING"]
    assert len(pending) == 5
    assert all(not r.ready for r in pending)
    assert all("NOT_REVIEWED" in {i.code for i in r.issues} for r in pending)


def test_malformed_variable_is_an_error(returned, blank_doc, tmp_path):
    path, wb, ws, edit = returned
    edit(2, final_variable="brthdtc!")
    out = tmp_path / "badvar.xlsx"
    wb.save(out)
    row = next(r for r in _read(out, blank_doc).rows if r.row_id == "p1f0")
    assert "BAD_VARIABLE" in {i.code for i in row.issues} and not row.ok


def test_declared_type_is_checked_against_the_text(returned, blank_doc, tmp_path):
    """The workbook cannot claim a type its own annotation text contradicts."""
    path, wb, ws, edit = returned
    edit(2, final_annot_type="NOT_SUBMITTED")     # but the text is "BRTHDTC"
    out = tmp_path / "type.xlsx"
    wb.save(out)
    row = next(r for r in _read(out, blank_doc).rows if r.row_id == "p1f0")
    issue = next(i for i in row.issues if i.code == "TYPE_MISMATCH")
    assert issue.severity == WARNING and row.ready       # flagged, still imported
    assert "reads as VARIABLE" in issue.message


@pytest.mark.parametrize("variable,domain,foreign", [
    ("BRTHDTC", "DM", ""), ("AGE", "DM", ""), ("SEX", "DM", ""), ("RACE", "DM", ""),
    ("USUBJID", "MH", ""), ("MHTERM", "MH", ""), ("VSORRES", "VS", ""),
    ("DSTERM", "MH", "DS"), ("AESTDTC", "MH", "AE"), ("CMTRT", "MH", "CM"),
])
def test_only_genuinely_foreign_variables_warn(variable, domain, foreign):
    """DM's own variables carry no prefix; warning on them would be noise that
    teaches reviewers to ignore the check."""
    assert imp._foreign_domain(variable, domain) == foreign


# --- formatting ------------------------------------------------------------
@pytest.mark.parametrize("column,value,code", [
    ("color_rgb", "crimson", "BAD_COLOR"),
    ("color_rgb", "#GG0000", "BAD_COLOR"),
    ("font_size", 96, "BAD_FONT_SIZE"),
    ("font_size", "big", "BAD_FONT_SIZE"),
    ("placement", "somewhere", "BAD_PLACEMENT"),
    ("status", "MAYBE", "BAD_STATUS"),
])
def test_formatting_errors(returned, blank_doc, tmp_path, column, value, code):
    path, wb, ws, edit = returned
    edit(2, **{column: value})
    out = tmp_path / f"{code}.xlsx"
    wb.save(out)
    row = next(r for r in _read(out, blank_doc).rows if r.row_id == "p1f0")
    assert code in {i.code for i in row.issues} and not row.ok


def test_colour_is_resolved_for_the_writer(returned, blank_doc):
    path, _, _, _ = returned
    row = next(r for r in _read(path, blank_doc).rows if r.row_id == "p1f0")
    # #D91A1A back to floats. The 8-bit round trip quantises 0.85/0.1 slightly;
    # that drift is invisible on the page and stable across re-exports.
    assert row.text_color == (0.851, 0.102, 0.102)
    assert row.font_name == "Helv" and row.font_size == 8.0


def test_geometry_reaches_the_writer(returned, blank_doc):
    path, _, _, _ = returned
    row = next(r for r in _read(path, blank_doc).rows if r.row_id == "p1f0")
    assert row.geometry["page_width"] > 1
    assert 0 < row.geometry["rel_x_pct"] < 1


def test_missing_geometry_row_is_an_error(returned, blank_doc, tmp_path):
    path, wb, ws, _ = returned
    wb[st.SHEET_GEOM].delete_rows(2)
    out = tmp_path / "nogeom.xlsx"
    wb.save(out)
    row = next(r for r in _read(out, blank_doc).rows if r.row_id == "p1f0")
    assert "MISSING_GEOMETRY" in {i.code for i in row.issues}


def test_uncalculated_formula_is_not_read_as_a_blank(returned, blank_doc, tmp_path):
    """If the agent writes a formula and nobody opens the file in Excel, the
    cached value is empty - which would otherwise import as "no decision"."""
    path, wb, ws, edit = returned
    edit(2, final_annotation="=CONCAT(H2,\"\")")
    out = tmp_path / "formula.xlsx"
    wb.save(out)
    row = next(r for r in _read(out, blank_doc).rows if r.row_id == "p1f0")
    assert "UNEVALUATED_FORMULA" in {i.code for i in row.issues} and not row.ok


# --- reporting -------------------------------------------------------------
def test_report_summary(returned, blank_doc):
    path, _, _, _ = returned
    d = _read(path, blank_doc).to_dict()
    assert d["rows"] == 14 and d["approved"] == 9 and d["blocked"] == 0
    assert d["by_status"] == {"APPROVED": 9, "NEEDS_MAPPING": 5}


def test_review_copy_puts_the_problem_in_the_row(returned, blank_doc, tmp_path):
    """The reviewer is already in Excel; tell them there, not in a log."""
    path, wb, ws, edit = returned
    edit(2, color_rgb="crimson")
    broken = tmp_path / "broken.xlsx"
    wb.save(broken)
    report = _read(broken, blank_doc)
    out = write_review_copy(report, broken, tmp_path / "reviewed.xlsx")

    ws2 = load_workbook(out)[st.SHEET_WORK]
    names = [c.value for c in ws2[1]]
    assert "import_issues" in names
    text = ws2.cell(row=2, column=names.index("import_issues") + 1).value
    assert "[ERROR]" in text and "crimson" in text


def test_importing_without_the_source_pdf_still_validates(returned):
    """`doc` is optional: content checks work standalone, identity checks need it."""
    path, _, _, _ = returned
    report = read_staging(path)
    assert len(report.rows) == 14 and len(report.approved()) == 9
    assert not any(i.code == "MISSING_ROW" for i in report.issues)

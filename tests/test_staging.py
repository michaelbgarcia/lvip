"""Phase 9 tests: the staging workbook and its contract with the importer."""
import pytest
from openpyxl import load_workbook

from acrf_parser import prefill as pf
from acrf_parser import staging as st
from acrf_parser.prefill import PrefillIndex


@pytest.fixture(scope="session")
def book(blank_doc, index, house, tmp_path_factory):
    path = st.write_staging(blank_doc, tmp_path_factory.mktemp("xl") / "staging.xlsx",
                            index=index, house=house)
    return load_workbook(path)


@pytest.fixture(scope="session")
def work(book):
    ws = book[st.SHEET_WORK]
    names = [c.value for c in ws[1]]
    return ws, names, [dict(zip(names, r)) for r in ws.iter_rows(min_row=2, values_only=True)]


def test_sheets(book):
    assert book.sheetnames == [st.SHEET_WORK, st.SHEET_GEOM, st.SHEET_STYLE,
                               st.SHEET_FORMS, st.SHEET_README]


def test_one_row_per_field(work, blank_doc):
    _, _, rows = work
    assert len(rows) == len(list(blank_doc.iter_fields())) == 14
    assert [r["row_id"] for r in rows] == [f.id for f in blank_doc.iter_fields()]


def test_auto_rows_arrive_decided(work):
    _, _, rows = work
    auto = [r for r in rows if r["status"] == "AUTO"]
    assert len(auto) == 9
    assert all(r["match_tier"] == pf.EXACT_KEY for r in auto)
    assert all(r["final_variable"] == r["suggested_variable"] for r in auto)


def test_unresolved_rows_are_left_blank_on_purpose(work):
    """A suggestion must never sit in the answer column looking like a decision."""
    _, _, rows = work
    for r in rows:
        if r["status"] != "AUTO":
            assert not r["final_variable"]
            assert not r["final_annotation"]


def test_evidence_travels_with_the_suggestion(work):
    """The reviewer sees why, next to what - not in a separate audit log."""
    _, names, rows = work
    for col in ("match_tier", "match_score", "match_source", "match_reason"):
        assert col in names
    row = next(r for r in rows if r["row_id"] == "p1f0")
    assert row["match_tier"] == pf.EXACT_KEY and row["match_score"] == 0.95
    assert "sample_acrf.pdf" in row["match_source"]
    assert "seen in" in row["match_reason"]


def test_formatting_is_prefilled_from_house_style(work):
    _, _, rows = work
    row = rows[0]
    assert row["color_rgb"] == "#D91A1A"
    assert row["font_name"] == "Helv" and row["font_size"] == 8.0
    assert row["placement"] == "right_of_field"


def test_status_column_has_a_dropdown(book):
    ws = book[st.SHEET_WORK]
    assert ws.data_validations.dataValidation
    options = " ".join(dv.formula1 for dv in ws.data_validations.dataValidation)
    for status in ("AUTO", "NEEDS_REVIEW", "APPROVED", "REJECTED"):
        assert status in options


def test_evidence_columns_are_locked_and_work_columns_are_not(book):
    ws = book[st.SHEET_WORK]
    names = [c.value for c in ws[1]]
    locked = ws.cell(row=2, column=names.index("match_tier") + 1)
    open_ = ws.cell(row=2, column=names.index("final_variable") + 1)
    assert locked.protection.locked and not open_.protection.locked


# --- the importer's half of the contract -----------------------------------
def test_geometry_is_keyed_by_row_id_and_kept_off_the_work_sheet(book, work):
    _, names, rows = work
    geom = book[st.SHEET_GEOM]
    gnames = [c.value for c in geom[1]]
    grows = [dict(zip(gnames, r)) for r in geom.iter_rows(min_row=2, values_only=True)]
    assert [g["row_id"] for g in grows] == [r["row_id"] for r in rows]
    assert "rel_x_pct" not in names          # noise on the work surface
    assert "rel_x_pct" in gnames             # but present for the importer


def test_stored_geometry_is_page_relative(book):
    geom = book[st.SHEET_GEOM]
    gnames = [c.value for c in geom[1]]
    for row in geom.iter_rows(min_row=2, values_only=True):
        g = dict(zip(gnames, row))
        for key in ("rel_x_pct", "rel_y_pct", "rel_w_pct", "rel_h_pct"):
            assert 0.0 <= g[key] <= 1.0
        assert g["page_width"] > 1 and g["page_height"] > 1


def test_house_style_sheet_flags_what_needs_deciding(book):
    ws = book[st.SHEET_STYLE]
    names = [c.value for c in ws[1]]
    rows = [dict(zip(names, r)) for r in ws.iter_rows(min_row=2, values_only=True)]
    variable = next(r for r in rows if r["scope"] == "VARIABLE")
    headers = next(r for r in rows if r["scope"] == "DOMAIN_HEADER")
    assert variable["settled"] == "yes"
    assert headers["settled"] == "NO" and headers["size_agreement"] < 1.0


def test_forms_sheet_lists_the_parse(book, blank_doc):
    ws = book[st.SHEET_FORMS]
    rows = [r[0] for r in ws.iter_rows(min_row=2, values_only=True)]
    assert rows == [f.name for f in blank_doc.forms]


def test_instructions_state_the_row_id_rule(book):
    text = "\n".join(str(r[0]) for r in book[st.SHEET_README].iter_rows(values_only=True))
    assert "row_id" in text and "Do not add, delete, sort or reorder rows" in text
    assert "NEEDS_MAPPING" in text


# --- no history yet --------------------------------------------------------
def test_a_first_study_still_produces_a_workbook(blank_doc, tmp_path):
    """No corpus is not an error - it is a sheet where every row is the agent's."""
    path = st.write_staging(blank_doc, tmp_path / "cold.xlsx", index=PrefillIndex())
    ws = load_workbook(path)[st.SHEET_WORK]
    names = [c.value for c in ws[1]]
    rows = [dict(zip(names, r)) for r in ws.iter_rows(min_row=2, values_only=True)]
    assert len(rows) == 14
    assert all(r["status"] == "NEEDS_MAPPING" for r in rows)
    assert all(not r["final_variable"] for r in rows)


def test_summary(blank_doc, index, house):
    s = st.summarize_staging(st.build_staging(blank_doc, index, house))
    assert s["rows"] == 14 and s["auto_fill_rate"] == 0.643
    assert s["by_status"]["AUTO"] == 9

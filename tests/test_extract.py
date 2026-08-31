"""Phase 1 tests: extraction completeness and annotation/text separation."""
import json

import pytest

from acrf_parser import ACRFParser, BBox, dump_json, summarize


def test_pages_and_dimensions(doc):
    assert doc.page_count == 5
    assert [p.number for p in doc.pages] == [1, 2, 3, 4, 5]  # 1-based
    p = doc.page(1)
    assert (round(p.width), round(p.height)) == (595, 842)   # A4
    assert p.rotation == 0


def test_text_layers_present(doc):
    p = doc.page(1)
    assert "Date of birth" in p.text
    assert p.blocks and p.lines and p.words
    assert all(w.page == 1 for w in p.words)
    assert any(l.bold and "Demographics" in l.text for l in p.lines)  # form title


def test_annotations_extracted(doc):
    texts = [a.text for a in doc.iter_annotations()]
    assert len(texts) == 17
    for expected in ("DM=Demographics", "BRTHDTC", "[NOT SUBMITTED]", "See Page 2",
                     "RACEOTH when SUPPDM.QNAM=RACEOTH"):
        assert expected in texts
    a = next(a for a in doc.iter_annotations() if a.text == "BRTHDTC")
    assert a.page == 1 and a.subtype == "FreeText" and a.content == "BRTHDTC"
    assert a.bbox.width > 0 and a.normalized_text == "brthdtc"


def test_annotation_text_excluded_from_content(doc):
    """Annotation markup must not masquerade as a CRF field label."""
    p = doc.page(1)
    content = [l.text for l in p.content_lines]
    assert "Date of birth" in content
    assert "BRTHDTC" not in content
    assert "DM=Demographics" not in content
    assert len(p.content_lines) < len(p.lines)


def test_annotations_sit_right_of_fields(doc):
    """Geometry sanity: markup is to the right of, and row-aligned with, its label."""
    p = doc.page(1)
    label = next(l for l in p.content_lines if l.text == "Date of birth")
    annot = next(a for a in p.annotations if a.text == "BRTHDTC")
    assert annot.bbox.x0 > label.bbox.x1
    assert annot.bbox.v_overlap(label.bbox) > 0.5


def test_relative_coordinates(doc):
    p = doc.page(1)
    rel = next(a for a in p.annotations if a.text == "AGE").bbox.relative(p.width, p.height)
    assert 0 < rel["rel_x_pct"] < 1 and 0 < rel["rel_y_pct"] < 1


def test_json_roundtrip(doc, tmp_path):
    out = dump_json(doc, tmp_path / "d.json")
    data = json.loads(out.read_text())
    assert data["page_count"] == 5
    assert data["pages"][0]["annotations"][0]["bbox"] == list(
        doc.page(1).annotations[0].bbox.as_tuple())


def test_summary_and_missing_file(doc, tmp_path):
    s = summarize(doc)
    assert s["annotations"] == 17 and s["pages_without_text"] == []
    with pytest.raises(FileNotFoundError):
        ACRFParser(tmp_path / "nope.pdf")


def test_bbox_geometry():
    a, b = BBox.of((0, 0, 10, 10)), BBox.of((5, 5, 15, 15))
    assert a.v_overlap(b) == 0.5 and a.h_overlap(b) == 0.5
    assert a.merge(b).as_tuple() == (0, 0, 15, 15)
    assert BBox.of((1, 1, 2, 2)).inside_frac(a) == 1.0


def _fill_pdf(path):
    """A CRF with the three ways a study can colour an annotation's background."""
    import pymupdf
    d = pymupdf.open()
    page = d.new_page()
    page.insert_text((72, 72), "Date of birth")
    a = page.add_freetext_annot(pymupdf.Rect(200, 60, 360, 80), "BRTHDTC",
                                fontsize=8, text_color=(0.85, 0.1, 0.1),
                                fill_color=(1, 1, 0.6))          # painted in /AP
    a.set_info(content="BRTHDTC")
    a.update(fill_color=(1, 1, 0.6), text_color=(0.85, 0.1, 0.1))
    sq = page.add_rect_annot(pymupdf.Rect(200, 100, 360, 120))    # /IC
    sq.set_colors(stroke=(0, 0, 0), fill=(0.8, 0.9, 1))
    sq.update()
    d.save(path)
    return path


def test_annotation_fill_is_separate_from_text_colour(tmp_path):
    """Red-on-yellow is two facts. Reading /C as "the colour" collapses them."""
    from acrf_parser import parse_pdf
    doc = parse_pdf(_fill_pdf(tmp_path / "fill.pdf"))
    free = next(a for a in doc.iter_annotations() if a.subtype == "FreeText")
    assert free.fill_color == (1.0, 1.0, 0.6) and free.fill_source == "APPEARANCE"
    assert free.text_color == (0.85, 0.1, 0.1)
    assert free.fill_color != free.text_color


def test_interior_colour_is_the_fill(tmp_path):
    from acrf_parser import parse_pdf
    doc = parse_pdf(_fill_pdf(tmp_path / "fill.pdf"))
    sq = next(a for a in doc.iter_annotations() if a.subtype == "Square")
    assert sq.fill_color == (0.8, 0.9, 1.0) and sq.fill_source == "IC"
    assert sq.color == (0.0, 0.0, 0.0)      # /C is the border here, not the fill


def test_the_studys_own_fill_is_read_back(doc):
    """The sample corpus is red on pale yellow; both colours must survive."""
    a = next(a for a in doc.iter_annotations() if a.text == "BRTHDTC")
    assert a.text_color == (0.85, 0.1, 0.1)
    assert a.fill_color == (1.0, 0.98, 0.77) and a.fill_source == "APPEARANCE"


def test_unfilled_annotation_reports_no_fill(tmp_path):
    """Absent must not be guessed at: a bare box has no fill, not a white one."""
    import pymupdf
    from acrf_parser import parse_pdf
    d = pymupdf.open()
    page = d.new_page()
    a = page.add_freetext_annot(pymupdf.Rect(200, 60, 360, 80), "AGE",
                                fontsize=8, text_color=(0.85, 0.1, 0.1))
    a.set_info(content="AGE")
    a.update()
    d.save(tmp_path / "bare.pdf")
    a = next(iter(parse_pdf(tmp_path / "bare.pdf").iter_annotations()))
    assert a.fill_color is None and a.fill_source == ""


def test_an_annotations_own_box_is_not_a_response_control(doc):
    """A filled annotation paints a rectangle. It is markup, not an answer box."""
    p = doc.page(1)
    for c in p.controls:
        assert not any(c.bbox.inside_frac(a.bbox) >= 0.9 for a in p.annotations)

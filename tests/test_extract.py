"""Phase 1 tests: extraction completeness and annotation/text separation."""
import json

import pytest

from acrf_parser import ACRFParser, BBox, dump_json, parse_pdf, summarize


@pytest.fixture(scope="session")
def doc(sample_pdf):
    return parse_pdf(sample_pdf)


def test_pages_and_dimensions(doc):
    assert doc.page_count == 3
    assert [p.number for p in doc.pages] == [1, 2, 3]        # 1-based
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
    assert len(texts) == 13
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
    assert data["page_count"] == 3
    assert data["pages"][0]["annotations"][0]["bbox"] == list(
        doc.page(1).annotations[0].bbox.as_tuple())


def test_summary_and_missing_file(doc, tmp_path):
    s = summarize(doc)
    assert s["annotations"] == 13 and s["pages_without_text"] == []
    with pytest.raises(FileNotFoundError):
        ACRFParser(tmp_path / "nope.pdf")


def test_bbox_geometry():
    a, b = BBox.of((0, 0, 10, 10)), BBox.of((5, 5, 15, 15))
    assert a.v_overlap(b) == 0.5 and a.h_overlap(b) == 0.5
    assert a.merge(b).as_tuple() == (0, 0, 15, 15)
    assert BBox.of((1, 1, 2, 2)).inside_frac(a) == 1.0

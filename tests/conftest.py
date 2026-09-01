"""Fixtures for both halves of the suite.

Two bodies of evidence, doing different jobs.

**The MSG pair** (`data/blankcrf_annotated.pdf` and `data/blankcrf.pdf`) is real
annotator work: the CDISC SDTM Metadata Submission Guidelines example CRF, once
annotated and once blank, the same 22 pages either way. It is the calibration
set - every threshold in this parser should be answerable to it, and
`test_msg_fidelity.py` scores the whole pipeline against it end to end.

**The synthetic fixture** (`tests/sample_pdf.py`) is kept for the cases this one
real CRF does not happen to contain: a study that colour-codes its markup by
SDTM domain, two studies that disagree about what "Start Date" means, a page
named by nothing but its domain-header annotation. Those situations are real,
and a suite that only tested what the MSG pair exercises would stop covering
them. Each retained synthetic test says which case it is holding.
"""
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.sample_pdf import build  # noqa: E402


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory) -> Path:
    return build(tmp_path_factory.mktemp("pdf") / "sample_acrf.pdf")


@pytest.fixture(scope="session")
def doc(sample_pdf):
    from acrf_parser import parse_pdf
    return parse_pdf(sample_pdf)


@pytest.fixture(scope="session")
def blank_pdf(sample_pdf, tmp_path_factory) -> Path:
    """The same CRF with every annotation deleted - the next study's blank form.

    What a template has to be applied to, and the only honest test of whether
    the parse survives without the markup layer it was built from.
    """
    out = tmp_path_factory.mktemp("blank") / "blank_acrf.pdf"
    pdf = pymupdf.open(sample_pdf)
    for page in pdf:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
    pdf.save(out)
    pdf.close()
    return out


@pytest.fixture(scope="session")
def blank_doc(blank_pdf):
    from acrf_parser import parse_pdf
    return parse_pdf(blank_pdf)


@pytest.fixture(scope="session")
def second_doc(tmp_path_factory):
    """A different study whose Adverse Events form also has a "Start Date"."""
    from acrf_parser import parse_pdf
    from tests.sample_pdf import build_second_study
    return parse_pdf(build_second_study(tmp_path_factory.mktemp("pdf2") / "second.pdf"))


@pytest.fixture(scope="session")
def coloured_doc(tmp_path_factory):
    """A study that colour-codes its markup by SDTM domain.

    The shape of a real Informed Consent page: several form-level annotations
    across the top, and one field carrying DS markup and DM markup side by side
    in different fills.
    """
    from acrf_parser import parse_pdf
    from tests.sample_pdf import build_colour_coded_study
    return parse_pdf(build_colour_coded_study(
        tmp_path_factory.mktemp("pdf3") / "coloured.pdf"))


@pytest.fixture(scope="session")
def coloured_blank(coloured_doc, tmp_path_factory):
    """The same CRF stripped of its markup - the next study's blank form."""
    from acrf_parser import parse_pdf
    out = tmp_path_factory.mktemp("blank3") / "coloured_blank.pdf"
    pdf = pymupdf.open(coloured_doc.path)
    for page in pdf:
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
    pdf.save(out)
    pdf.close()
    return parse_pdf(out)


@pytest.fixture(scope="session")
def corpus(doc, second_doc):
    """Two finished studies standing in for a historical corpus."""
    return [doc, second_doc]


@pytest.fixture(scope="session")
def index(corpus):
    from acrf_parser.prefill import PrefillIndex
    return PrefillIndex.from_documents(corpus)


@pytest.fixture(scope="session")
def house(corpus):
    from acrf_parser.style import derive_house_style
    return derive_house_style(corpus)


# --- the MSG pair ----------------------------------------------------------
@pytest.fixture(scope="session")
def msg_truth():
    """The sponsor's own annotations, read straight off the annotated PDF."""
    from tests.msg import ground_truth
    return ground_truth()


@pytest.fixture(scope="session")
def msg_run(tmp_path_factory):
    """One full pass over the MSG pair, with every intermediate kept.

    Session-scoped because it parses two 22-page PDFs, builds a corpus, writes a
    workbook, reads it back and draws a PDF - a few seconds, and every test here
    is asking a different question about the same run.
    """
    from tests.msg_pipeline import run
    return run(tmp_path_factory.mktemp("msg"))


@pytest.fixture(scope="session")
def msg_score(msg_run, msg_truth):
    """How close that pass came, scored off the PDF it actually produced."""
    from tests.msg import score
    return score(msg_run.reread(), msg_truth)

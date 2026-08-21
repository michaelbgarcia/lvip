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

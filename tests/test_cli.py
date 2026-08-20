"""End-to-end: the CLI writes an extract, a template and a knowledge base."""
import json

from acrf_parser.cli import main
from acrf_parser.kb import KnowledgeBase


def test_cli_writes_every_artifact(sample_pdf, tmp_path, capsys):
    out, db = tmp_path / "out", tmp_path / "kb.sqlite"
    assert main([str(sample_pdf), "-o", str(out), "--db", str(db)]) == 0

    extract = json.loads((out / f"{sample_pdf.stem}.extract.json").read_text())
    assert extract["page_count"] == 5
    assert [f["name"] for f in extract["forms"]][0] == "Demographics"
    assert extract["pages"][0]["fields"][0]["text"] == "Date of birth"
    assert extract["links"][0]["link_score"] > 0

    template = json.loads((out / f"{sample_pdf.stem}.template.json").read_text())
    assert template["template_version"] == 1 and len(template["forms"]) == 4

    with KnowledgeBase(db) as kb:
        assert kb.variable_for("Medical History", "Start Date") == "MHSTDTC"

    printed = capsys.readouterr().out
    assert "mean_link_score" in printed and str(db) in printed


def test_cli_can_skip_the_template(sample_pdf, tmp_path):
    out = tmp_path / "out"
    assert main([str(sample_pdf), "-o", str(out), "--no-template", "--quiet"]) == 0
    assert not (out / f"{sample_pdf.stem}.template.json").exists()

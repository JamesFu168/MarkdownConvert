from domain.extraction_schema import Evidence, TextField, empty_extraction
from services.locator import (
    VerificationSources,
    block_text,
    build_model_input,
    normalize_ws,
    segment_markdown,
    verify_evidence,
    verify_quote,
)
from tests.conftest import SAMPLE_MARKDOWN


def test_segment_offsets_point_into_original_markdown():
    blocks = segment_markdown(SAMPLE_MARKDOWN)
    assert [b.block_id for b in blocks] == [f"b{i:04d}" for i in range(1, len(blocks) + 1)]
    kinds = [b.kind for b in blocks]
    assert "heading" in kinds and "table" in kinds and "paragraph" in kinds
    table = next(b for b in blocks if b.kind == "table")
    text = block_text(SAMPLE_MARKDOWN, table)
    assert text.startswith("| Name") and "7732-18-5" in text
    # offsets must reproduce the original text exactly (no rewriting)
    assert "".join(block_text(SAMPLE_MARKDOWN, b) for b in blocks).replace("\n", "") == SAMPLE_MARKDOWN.replace("\n", "")


def test_model_input_only_adds_markers():
    blocks = segment_markdown(SAMPLE_MARKDOWN)
    mi = build_model_input(SAMPLE_MARKDOWN, blocks)
    assert mi.count("[[b") == len(blocks)
    stripped = "\n".join(line for line in mi.split("\n") if not line.startswith("[[b"))
    assert stripped.replace("\n", "") == SAMPLE_MARKDOWN.replace("\n", "")


def test_normalize_ws_keeps_digits_symbols_units():
    assert normalize_ws("  10  -  20 %  w/w ") == "10 - 20 % w/w"


def test_verify_quote_statuses():
    blocks = segment_markdown(SAMPLE_MARKDOWN)
    src = VerificationSources(markdown=SAMPLE_MARKDOWN, blocks=blocks)
    table_id = next(b.block_id for b in blocks if b.kind == "table")
    assert verify_quote(Evidence(quote="7732-18-5", block_id=table_id), src).status == "verified"
    assert verify_quote(Evidence(quote="7732-18-5", block_id="b0001"), src).status == "verified_elsewhere"
    assert verify_quote(Evidence(quote="benzene 71-43-2"), src).status == "not_found"
    assert verify_quote(Evidence(quote="7732-18-5"), VerificationSources()).status == "unverifiable"
    assert verify_quote(Evidence(quote=None), src).status == "no_quote"


def test_verify_quote_against_pdf_text_layer():
    src = VerificationSources(pages=["page one text", "Flash point: > 93 °C"])
    assert verify_quote(Evidence(quote="> 93 °C", page=2), src).status == "verified"
    qc = verify_quote(Evidence(quote="> 93 °C", page=1), src)
    assert qc.status == "verified_elsewhere" and qc.matched_page == 2


def test_verify_evidence_produces_findings():
    e = empty_extraction()
    e.document.product_name = TextField(value="X", raw_text="X", status="found", evidence=[Evidence(quote="does not exist anywhere")])
    checks, findings = verify_evidence(e, VerificationSources(markdown=SAMPLE_MARKDOWN, blocks=segment_markdown(SAMPLE_MARKDOWN)))
    assert checks[0]["status"] == "not_found"
    assert findings[0].code == "quote_not_found"

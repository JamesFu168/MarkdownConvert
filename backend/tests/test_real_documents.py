"""Checks against the user's own PDF/DOCX files in TEST_SDS_DIR. Skipped when unset or empty.

No documents are generated here. The tests only assert that the storage / inspection /
conversion flow runs and reports; they make NO claim about extraction accuracy.
"""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.realdocs

TEST_DIR = os.environ.get("TEST_SDS_DIR")
FILES = sorted(p for p in Path(TEST_DIR).glob("*") if p.suffix.lower() in (".pdf", ".docx")) if TEST_DIR and Path(TEST_DIR).is_dir() else []

if not FILES:
    pytest.skip("TEST_SDS_DIR not set or contains no PDF/DOCX", allow_module_level=True)

from adapters.markitdown_adapter import MarkItDownConverter  # noqa: E402
from domain.errors import PermanentError  # noqa: E402
from services.document_check import check_document, sniff_format  # noqa: E402
from services.locator import build_model_input, segment_markdown  # noqa: E402


@pytest.mark.parametrize("path", FILES, ids=[p.name for p in FILES])
def test_inspect_and_convert_real_document(path, tmp_path):
    data = path.read_bytes()
    try:
        fmt = sniff_format(data, path.name, None)
        check = check_document(data, fmt, min_text_chars_per_page=40)
    except PermanentError as e:
        pytest.skip(f"{path.name}: {e.code} - {e.message}")
    summary = check.summary()
    summary.pop("sha256", None)
    if check.classification == "needs_ocr":
        pytest.skip(f"{path.name}: needs_ocr (no text layer) - recorded, not an extraction failure")
    conv = MarkItDownConverter().convert(data, fmt, path.name)
    assert conv.markdown.strip(), "MarkItDown returned empty markdown for a document with a text layer"
    blocks = segment_markdown(conv.markdown)
    assert blocks
    mi = build_model_input(conv.markdown, blocks)
    assert mi.count("[[b") == len(blocks)
    report = {"file": path.name, "format": fmt.value, "classification": check.classification, "page_count": check.page_count,
              "total_text_chars": check.total_text_chars, "markdown_chars": len(conv.markdown), "blocks": len(blocks),
              "converter": f"{conv.converter} {conv.converter_version}", "convert_ms": conv.elapsed_ms}
    print(json.dumps(report, ensure_ascii=False))

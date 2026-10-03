import io
import zipfile

import pytest

from domain.errors import CorruptDocumentError, DocumentTooLargeError, EncryptedDocumentError, UnsupportedFormatError
from domain.models import DocumentFormat
from services.document_check import check_document, check_size, sniff_format


def _zip(names: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, d in names.items():
            zf.writestr(n, d)
    return buf.getvalue()


def test_sniff_formats():
    assert sniff_format(b"%PDF-1.4 ...", "a.pdf", None) == DocumentFormat.PDF
    docx_container = _zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"})
    assert sniff_format(docx_container, "a.docx", None) == DocumentFormat.DOCX
    with pytest.raises(UnsupportedFormatError):
        sniff_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "legacy.doc", None)
    with pytest.raises(UnsupportedFormatError):
        sniff_format(_zip({"mimetype": b"application/vnd.oasis.opendocument.text"}), "a.odt", None)
    with pytest.raises(CorruptDocumentError):
        sniff_format(b"garbage", "a.pdf", None)
    with pytest.raises(UnsupportedFormatError):
        sniff_format(b"garbage", "a.txt", "text/plain")


def test_size_limits():
    with pytest.raises(DocumentTooLargeError):
        check_size(11, 10)
    with pytest.raises(CorruptDocumentError):
        check_size(0, 10)
    check_size(10, 10)


def test_pdf_classification_via_injected_text_layer():
    def pages(_: bytes):
        return ["", "   ", ""], {}

    c = check_document(b"%PDF", DocumentFormat.PDF, min_text_chars_per_page=40, pdf_pages_text=pages)
    assert c.classification == "needs_ocr" and "all_pages_without_text_layer" in c.warnings

    def partial(_: bytes):
        return ["x" * 100, "", "y" * 100], {}

    c = check_document(b"%PDF", DocumentFormat.PDF, min_text_chars_per_page=40, pdf_pages_text=partial)
    assert c.classification == "partial_text" and c.detail["low_text_pages"] == [2]

    def ok(_: bytes):
        return ["x" * 100, "y" * 100], {"pdf_metadata_producer": "p"}

    c = check_document(b"%PDF", DocumentFormat.PDF, min_text_chars_per_page=40, pdf_pages_text=ok)
    assert c.classification == "processable" and c.page_count == 2 and c.total_text_chars == 200


def test_encrypted_and_corrupt_are_raised():
    def enc(_: bytes):
        raise EncryptedDocumentError("PDF 已加密")

    with pytest.raises(EncryptedDocumentError):
        check_document(b"%PDF", DocumentFormat.PDF, min_text_chars_per_page=40, pdf_pages_text=enc)

    with pytest.raises(CorruptDocumentError):
        check_document(_zip({"other": b"x"}), DocumentFormat.DOCX, min_text_chars_per_page=40)


def test_docx_without_text_is_needs_ocr():
    data = _zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document><w:body></w:body></w:document>",
                 "word/media/image1.png": b"\x89PNG"})
    c = check_document(data, DocumentFormat.DOCX, min_text_chars_per_page=40)
    assert c.classification == "needs_ocr" and "docx_without_text" in c.warnings

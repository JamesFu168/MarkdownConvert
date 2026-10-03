"""Format sniffing and content-quality checks (stage ``validate_document``).

Scanned PDFs, empty text pages, encrypted PDFs, oversized or corrupt files never pass
silently: they end as ``needs_ocr`` / ``encrypted_document`` / ``corrupt_document`` /
``unsupported_format`` with a human-readable reason. Pages with very little text may not
be detected as scanned; that limitation is recorded in ``docs/limitations.md``.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from domain.errors import (
    CorruptDocumentError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    UnsupportedFormatError,
)
from domain.models import DocumentFormat

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc / .xls

CONTENT_TYPES = {
    DocumentFormat.PDF: "application/pdf",
    DocumentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

Classification = str  # processable | partial_text | needs_ocr


@dataclass
class DocumentCheck:
    format: DocumentFormat
    size_bytes: int
    sha256: str
    classification: Classification
    page_count: int | None = None
    pages_text_chars: list[int] | None = None
    text_layer: list[str] | None = None  # per-page text (PDF) or [full text] (DOCX); used for quote verification
    total_text_chars: int = 0
    warnings: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "classification": self.classification,
            "page_count": self.page_count,
            "pages_text_chars": self.pages_text_chars,
            "total_text_chars": self.total_text_chars,
            "warnings": self.warnings,
            **self.detail,
        }


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_format(data: bytes, file_name: str | None, content_type: str | None) -> DocumentFormat:
    """Decide the format from magic bytes first, extension second. DOC (OLE) is rejected explicitly."""
    head = data[:8]
    ext = (file_name or "").rsplit(".", 1)[-1].lower() if file_name and "." in file_name else ""
    if head.startswith(OLE_MAGIC) or ext == "doc":
        raise UnsupportedFormatError("舊式 .doc 格式不支援，請先另存為 .docx", details={"extension": ext or "doc"})
    if head.startswith(PDF_MAGIC):
        return DocumentFormat.PDF
    if head.startswith(ZIP_MAGIC):
        if _zip_is_docx(data):
            return DocumentFormat.DOCX
        raise UnsupportedFormatError("ZIP 容器不是有效的 DOCX（缺少 word/document.xml）", details={"extension": ext})
    if ext == "pdf":
        raise CorruptDocumentError("副檔名為 .pdf 但檔頭不是 %PDF", details={"extension": ext})
    if ext == "docx":
        raise CorruptDocumentError("副檔名為 .docx 但不是 ZIP 容器", details={"extension": ext})
    raise UnsupportedFormatError("僅支援 PDF 與 DOCX", details={"extension": ext, "content_type": content_type})


def _zip_is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            return "word/document.xml" in names and "[Content_Types].xml" in names
    except zipfile.BadZipFile:
        return False


def check_size(size: int, max_bytes: int) -> None:
    if size > max_bytes:
        raise DocumentTooLargeError(f"文件大小 {size} bytes 超過上限 {max_bytes} bytes", details={"size": size, "max": max_bytes})
    if size == 0:
        raise CorruptDocumentError("文件為空")


# --- PDF ---------------------------------------------------------------------

def _pdf_pages_text(data: bytes) -> tuple[list[str], dict[str, Any]]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    detail: dict[str, Any] = {}
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as e:
        raise CorruptDocumentError(f"PDF 無法讀取: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise CorruptDocumentError(f"PDF 無法讀取: {type(e).__name__}: {e}") from e
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except Exception:  # noqa: BLE001
            ok = 0
        if not ok:
            raise EncryptedDocumentError("PDF 已加密，無法讀取")
        detail["encrypted_with_empty_password"] = True
    pages: list[str] = []
    try:
        for p in reader.pages:
            pages.append(p.extract_text() or "")
    except Exception as e:  # noqa: BLE001
        raise CorruptDocumentError(f"PDF 文字層讀取失敗: {type(e).__name__}: {e}") from e
    detail["pdf_metadata_producer"] = (reader.metadata or {}).get("/Producer") if reader.metadata else None
    return pages, detail


# --- DOCX --------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _docx_text(data: bytes) -> tuple[str, dict[str, Any]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            media = [n for n in zf.namelist() if n.startswith("word/media/")]
    except (KeyError, zipfile.BadZipFile) as e:
        raise CorruptDocumentError(f"DOCX 結構損毀: {e}") from e
    text = _TAG_RE.sub(" ", xml)
    text = re.sub(r"\s+", " ", text).strip()
    return text, {"docx_media_count": len(media), "docx_paragraphs": xml.count("<w:p ") + xml.count("<w:p>"),
                  "docx_tables": xml.count("<w:tbl>")}


# --- Entry point -------------------------------------------------------------

def check_document(
    data: bytes,
    fmt: DocumentFormat,
    *,
    min_text_chars_per_page: int,
    pdf_pages_text: Callable[[bytes], tuple[list[str], dict[str, Any]]] = _pdf_pages_text,
    docx_text: Callable[[bytes], tuple[str, dict[str, Any]]] = _docx_text,
) -> DocumentCheck:
    size = len(data)
    digest = sha256_hex(data)
    warnings: list[str] = []
    if fmt == DocumentFormat.PDF:
        pages, detail = pdf_pages_text(data)
        counts = [len(p.strip()) for p in pages]
        total = sum(counts)
        low_pages = [i + 1 for i, c in enumerate(counts) if c < min_text_chars_per_page]
        if not pages:
            raise CorruptDocumentError("PDF 沒有任何頁面")
        if len(low_pages) == len(pages):
            classification = "needs_ocr"
            warnings.append("all_pages_without_text_layer")
        elif low_pages:
            classification = "partial_text"
            warnings.append(f"pages_without_text_layer:{','.join(map(str, low_pages))}")
        else:
            classification = "processable"
        detail["low_text_pages"] = low_pages
        return DocumentCheck(format=fmt, size_bytes=size, sha256=digest, classification=classification,
                             page_count=len(pages), pages_text_chars=counts, text_layer=pages, total_text_chars=total,
                             warnings=warnings, detail=detail)
    text, detail = docx_text(data)
    total = len(text)
    if total < min_text_chars_per_page:
        classification = "needs_ocr"
        warnings.append("docx_without_text" if detail.get("docx_media_count") else "docx_empty")
    else:
        classification = "processable"
    return DocumentCheck(format=fmt, size_bytes=size, sha256=digest, classification=classification, page_count=None,
                         pages_text_chars=None, text_layer=[text], total_text_chars=total, warnings=warnings, detail=detail)

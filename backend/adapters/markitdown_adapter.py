"""MarkItDown conversion adapter (plugins disabled; only pdf/docx extras installed)."""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Protocol

from domain.errors import CorruptDocumentError
from domain.models import DocumentFormat


@dataclass
class ConversionResult:
    markdown: str
    converter: str
    converter_version: str
    elapsed_ms: int
    warnings: list[str] = field(default_factory=list)


class DocumentConverter(Protocol):
    name: str
    version: str

    def convert(self, data: bytes, fmt: DocumentFormat, file_name: str) -> ConversionResult: ...


class MarkItDownConverter:
    name = "markitdown"

    def __init__(self) -> None:
        import markitdown
        from markitdown import MarkItDown

        self.version: str = getattr(markitdown, "__version__", "unknown")
        self._md = MarkItDown(enable_plugins=False)

    def convert(self, data: bytes, fmt: DocumentFormat, file_name: str) -> ConversionResult:
        started = time.monotonic()
        ext = ".pdf" if fmt == DocumentFormat.PDF else ".docx"
        try:
            result = self._md.convert_stream(io.BytesIO(data), file_extension=ext)
        except Exception as e:  # markitdown raises its own hierarchy; conversion failure is permanent for this file
            raise CorruptDocumentError(f"MarkItDown failed to convert {fmt.value}: {type(e).__name__}: {e}",
                                       details={"converter": self.name, "version": self.version}) from e
        text = result.text_content or ""
        warnings: list[str] = []
        if not text.strip():
            warnings.append("empty_markdown")
        return ConversionResult(markdown=text, converter=self.name, converter_version=self.version,
                                elapsed_ms=int((time.monotonic() - started) * 1000), warnings=warnings)


class StaticConverter:
    """Test double: returns a fixed markdown string."""
    name = "static"
    version = "test"

    def __init__(self, markdown: str, *, fail: Exception | None = None):
        self.markdown = markdown
        self.fail = fail
        self.calls = 0

    def convert(self, data: bytes, fmt: DocumentFormat, file_name: str) -> ConversionResult:
        self.calls += 1
        if self.fail:
            raise self.fail
        return ConversionResult(markdown=self.markdown, converter=self.name, converter_version=self.version, elapsed_ms=1)

"""Phase-6 extension point: OCR / layout providers (e.g. Azure Document Intelligence Layout).

Phase 1 ships only the interface and a ``NotConfiguredOcrProvider`` so the pipeline can
report ``needs_ocr`` without pretending to OCR. A real implementation must be added as a
NEW experiment arm; it must not silently change arms A-D.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from domain.errors import NeedsOcrError


@dataclass
class OcrPage:
    page: int
    text: str
    confidence: float | None = None


@dataclass
class OcrResult:
    pages: list[OcrPage]
    provider: str
    version: str
    cost: dict | None = None  # measured cost when the provider reports it; None = unknown
    warnings: list[str] = field(default_factory=list)


class OcrProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    def ocr(self, data: bytes, *, pages: list[int] | None = None) -> OcrResult: ...


class NotConfiguredOcrProvider:
    name = "none"

    def __init__(self, endpoint: str | None = None):
        self.endpoint = endpoint

    def is_configured(self) -> bool:
        return False

    def ocr(self, data: bytes, *, pages: list[int] | None = None) -> OcrResult:
        raise NeedsOcrError("Document needs OCR but no OCR provider is configured in phase 1",
                            details={"docintel_endpoint_set": bool(self.endpoint)})

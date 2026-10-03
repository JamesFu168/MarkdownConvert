"""Extraction provider port.

A provider performs exactly ONE model call per ``extract`` invocation. Retry budget,
deadline accounting and persistence of every attempt live in ``services.extraction``
so that the real adapter and the test doubles share the same control flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from domain.models import InputStrategy, UsageRecord


@dataclass
class ModelInput:
    strategy: InputStrategy
    sha256: str
    text: str | None = None  # markdown with block ids (markdown strategy)
    file_bytes: bytes | None = None  # original document (original_document strategy)
    file_name: str | None = None
    mime_type: str | None = None

    @property
    def size_bytes(self) -> int:
        if self.file_bytes is not None:
            return len(self.file_bytes)
        return len((self.text or "").encode("utf-8"))


@dataclass
class ProviderResponse:
    output_text: str
    model: str
    response_id: str | None
    request_id: str | None
    usage: UsageRecord | None
    http_status: int | None = None
    incomplete_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ExtractionProvider(Protocol):
    name: str
    is_test_double: bool

    def extract(
        self,
        *,
        model: str,
        instructions: str,
        model_input: ModelInput,
        json_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
        deadline_seconds: float,
        pdf_detail: str | None,
    ) -> ProviderResponse:
        """Single attempt. Raises ``domain.errors.Provider*Error`` on failure."""
        ...

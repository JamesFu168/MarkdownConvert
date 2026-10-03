"""Shared fixtures. No synthetic SDS documents are created: pipeline tests inject a fake
``check_document`` and a ``StaticConverter``; the upload bytes only carry a PDF magic header."""
from __future__ import annotations

import os
from typing import Any

import pytest

from adapters.markitdown_adapter import StaticConverter
from adapters.mock_provider import MockProvider, ScriptedProvider
from adapters.ocr_interface import NotConfiguredOcrProvider
from adapters.storage_memory import MemoryBlobStore, MemoryQueue, MemoryRepository
from domain.models import DocumentFormat
from services import pipeline as pipeline_mod
from services.config import Settings
from services.container import build_container, reset_container
from services.cost import ModelPrice, PricingTable
from services.document_check import DocumentCheck, sha256_hex
from services.experiments import load_arms

FAKE_PDF = b"%PDF-1.7\n% not a real document; magic bytes only for upload sniffing in tests\n"

SAMPLE_MARKDOWN = (
    "# Section 1\n\nProduct identifier: Test Product\n\n"
    "## Section 3\n\n| Name | CAS | Concentration |\n| --- | --- | --- |\n| Ethanol | 64-17-5 | 10 - 20 % |\n| Water | 7732-18-5 | >= 80 % |\n\n"
    "## Section 9\n\nFlash point: > 93 °C (closed cup)\n"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("EXTRACTION_PROVIDER", "OPENAI_API_KEY", "OPENAI_MODEL_BASELINE", "OPENAI_MODEL_CANDIDATE", "WEBSITE_INSTANCE_ID",
              "WEBSITE_SITE_NAME", "AUTH_MODE", "TEST_SDS_DIR"):
        monkeypatch.delenv(k, raising=False)
    reset_container()
    yield
    reset_container()


@pytest.fixture
def settings() -> Settings:
    return Settings(extraction_provider="mock", openai_timeout_seconds=5.0, openai_max_attempts=2, processing_lease_seconds=600,
                    min_text_chars_per_page=40, auth_mode="local_dev")


@pytest.fixture
def pricing() -> PricingTable:
    return PricingTable(version="test-pricing", currency="USD",
                        models={"scripted-model": ModelPrice(input_per_1m=1.0, output_per_1m=4.0, cached_input_per_1m=0.5, effective_date="2026-01-01")})


@pytest.fixture
def fake_check(monkeypatch):
    """Replace the real document inspection with one driven by a configurable classification."""
    state: dict[str, Any] = {"classification": "processable", "pages": ["Section 1 Product identifier: Test Product " * 3,
                                                                          "Section 3 Ethanol 64-17-5 10 - 20 % Water 7732-18-5 >= 80 % " * 2,
                                                                          "Section 9 Flash point: > 93 °C (closed cup) " * 2]}

    def _check(data: bytes, fmt: DocumentFormat, *, min_text_chars_per_page: int) -> DocumentCheck:
        pages = state["pages"]
        return DocumentCheck(format=fmt, size_bytes=len(data), sha256=sha256_hex(data), classification=state["classification"],
                             page_count=len(pages), pages_text_chars=[len(p) for p in pages], text_layer=pages,
                             total_text_chars=sum(len(p) for p in pages), warnings=list(state.get("warnings", [])))

    monkeypatch.setattr(pipeline_mod, "check_document", _check)
    return state


@pytest.fixture
def make_container(settings, pricing, fake_check):
    def _make(provider=None, converter=None, settings_override: Settings | None = None, queue: MemoryQueue | None = None):
        s = settings_override or settings
        return build_container(
            s, repo=MemoryRepository(), blob=MemoryBlobStore(), queue=queue or MemoryQueue(),
            provider=provider or MockProvider("success"), converter=converter or StaticConverter(SAMPLE_MARKDOWN), pricing=pricing,
            arms=load_arms(s.experiments_path), ocr=NotConfiguredOcrProvider(),
        )
    return _make


@pytest.fixture
def container(make_container):
    return make_container()


def upload(container, *, arms=("B",), owner="owner-1", data: bytes = FAKE_PDF, name="user-file.pdf", experiment_id=None, key=None):
    return container.jobs.create_jobs(owner_id=owner, file_name=name, content_type="application/pdf", data=data, arms=list(arms),
                                      experiment_id=experiment_id, idempotency_key=key)


def drain(container, max_messages: int = 10):
    """Deliver queued messages to the processor like the Functions host would (one at a time)."""
    from domain.models import QueueMessage

    processed = []
    for _ in range(max_messages):
        body = container.queue.pop()
        if body is None:
            break
        processed.append(container.processor.process_message(QueueMessage.model_validate_json(body)))
    return processed


__all__ = ["FAKE_PDF", "SAMPLE_MARKDOWN", "ScriptedProvider", "upload", "drain", "os"]

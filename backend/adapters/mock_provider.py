"""Test doubles for the extraction provider.

``MockProvider`` never contacts a network. Its output is schema-valid but carries NO
extraction knowledge (every field ``unreadable``). Results produced by it are labelled
``is_test_double=True`` everywhere and excluded from effectiveness statistics.

Behaviour is selectable (``MOCK_PROVIDER_BEHAVIOR``) so error paths can be exercised:
``success`` | ``rate_limit`` | ``timeout`` | ``server_error`` | ``invalid_json`` |
``schema_violation`` | ``refusal`` | ``unsupported_file`` | ``slow:<seconds>``.
"""
from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from domain.errors import (
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnsupportedInputError,
)
from domain.extraction_schema import empty_extraction
from domain.models import UsageRecord

from .provider_base import ModelInput, ProviderResponse


def _usage(input_chars: int) -> UsageRecord:
    # Deliberately crude; flagged as test double so it never enters cost reports as measured usage.
    inp = max(1, input_chars // 4)
    raw = {"input_tokens": inp, "output_tokens": 256, "total_tokens": inp + 256,
           "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
    return UsageRecord(input_tokens=inp, output_tokens=256, total_tokens=inp + 256, cached_tokens=0, reasoning_tokens=0, raw=raw)


class MockProvider:
    name = "mock"
    is_test_double = True

    def __init__(self, behavior: str = "success"):
        self.behavior = behavior
        self.calls: list[dict[str, Any]] = []

    def extract(self, *, model: str, instructions: str, model_input: ModelInput, json_schema: dict[str, Any],
                schema_name: str, max_output_tokens: int, deadline_seconds: float, pdf_detail: str | None) -> ProviderResponse:
        self.calls.append({"model": model, "strategy": model_input.strategy.value, "input_sha256": model_input.sha256,
                           "pdf_detail": pdf_detail, "max_output_tokens": max_output_tokens})
        b = self.behavior
        if b == "rate_limit":
            raise ProviderTransientError("mock 429", code="rate_limited", retry_after_seconds=0.01, details={"http_status": 429})
        if b == "timeout":
            raise ProviderTimeoutError("mock timeout")
        if b == "server_error":
            raise ProviderTransientError("mock 503", details={"http_status": 503})
        if b == "refusal":
            raise ProviderPermanentError("mock refusal", code="model_refusal")
        if b == "unsupported_file":
            raise ProviderUnsupportedInputError("mock: model does not accept file input")
        if b.startswith("slow:"):
            time.sleep(float(b.split(":", 1)[1]))
        size = model_input.size_bytes
        if b == "invalid_json":
            text = "{ this is not json"
        elif b == "schema_violation":
            text = json.dumps({"schema_version": "pilot-v1", "unexpected": True})
        else:
            payload = empty_extraction("unreadable").model_dump()
            payload["extraction_notes"] = "TEST DOUBLE OUTPUT - no model was called"
            text = json.dumps(payload, ensure_ascii=False)
        return ProviderResponse(
            output_text=text,
            model=model,
            response_id=f"mock-resp-{len(self.calls)}",
            request_id=f"mock-req-{len(self.calls)}",
            usage=_usage(size),
            http_status=200,
            raw={"mock": True, "behavior": b},
        )


class ScriptedProvider:
    """Replays a queue of outcomes; each item is a ProviderResponse, an Exception, or a callable returning one."""
    name = "scripted"
    is_test_double = True

    def __init__(self, script: list[ProviderResponse | Exception | Callable[[], ProviderResponse]]):
        self.script: deque = deque(script)
        self.calls: list[dict[str, Any]] = []

    def extract(self, *, model: str, instructions: str, model_input: ModelInput, json_schema: dict[str, Any],
                schema_name: str, max_output_tokens: int, deadline_seconds: float, pdf_detail: str | None) -> ProviderResponse:
        self.calls.append({"model": model, "strategy": model_input.strategy.value, "deadline": deadline_seconds})
        if not self.script:
            raise AssertionError("ScriptedProvider: no more scripted outcomes")
        item = self.script.popleft()
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, ProviderResponse):
            return item()
        return item


def success_response(payload: dict[str, Any] | None = None, *, model: str = "scripted-model",
                     usage: UsageRecord | None = None, response_id: str = "resp_1") -> ProviderResponse:
    body = payload if payload is not None else empty_extraction("unreadable").model_dump()
    return ProviderResponse(output_text=json.dumps(body, ensure_ascii=False), model=model, response_id=response_id,
                            request_id="req_1", usage=usage or _usage(1000), http_status=200, raw={"scripted": True})

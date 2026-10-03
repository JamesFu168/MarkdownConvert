"""Contract tests for the real adapter using a fake SDK client (no network)."""
import base64
from types import SimpleNamespace

import httpx
import openai
import pytest

from adapters.openai_adapter import OpenAIProvider, usage_from_response
from adapters.provider_base import ModelInput
from domain.errors import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnsupportedInputError,
)
from domain.extraction_schema import empty_extraction, strict_json_schema
from domain.models import InputStrategy


class FakeResponses:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _client(outcome):
    return SimpleNamespace(responses=FakeResponses(outcome))


def _http_err(cls, status, headers=None, message="err"):
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(status, headers=headers or {}, request=req)
    return cls(message, response=resp, body=None)


def _ok_response(text, usage=None, status="completed", output=None):
    usage_obj = SimpleNamespace(model_dump=lambda: usage or {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                                               "input_tokens_details": {"cached_tokens": 2},
                                                               "output_tokens_details": {"reasoning_tokens": 1}})
    return SimpleNamespace(id="resp_123", model="model-x", status=status, output_text=text, usage=usage_obj, output=output or [],
                           incomplete_details=SimpleNamespace(reason="max_output_tokens") if status == "incomplete" else None,
                           model_dump=lambda: {"id": "resp_123", "status": status}, _request_id="req_abc")


def _call(provider, strategy=InputStrategy.MARKDOWN, pdf_detail=None, mime="application/pdf"):
    mi = ModelInput(strategy=strategy, sha256="s", text="[[b0001]]\nhello", file_bytes=b"%PDF-1.4 data", file_name="document.pdf", mime_type=mime)
    return provider.extract(model="model-x", instructions="inst", model_input=mi, json_schema=strict_json_schema(), schema_name="sds",
                            max_output_tokens=1000, deadline_seconds=12.5, pdf_detail=pdf_detail)


def test_requires_api_key():
    with pytest.raises(ProviderConfigurationError):
        OpenAIProvider(None)


def test_request_shape_text_and_file():
    client = _client(_ok_response(empty_extraction().model_dump_json()))
    p = OpenAIProvider("sk", client=client)
    r = _call(p)
    kw = client.responses.kwargs
    assert kw["model"] == "model-x" and kw["instructions"] == "inst" and kw["store"] is False and kw["timeout"] == 12.5
    assert kw["text"]["format"]["type"] == "json_schema" and kw["text"]["format"]["strict"] is True
    assert kw["input"][0]["content"][0] == {"type": "input_text", "text": "[[b0001]]\nhello"}
    assert r.response_id == "resp_123" and r.request_id == "req_abc"
    assert r.usage.input_tokens == 10 and r.usage.cached_tokens == 2 and r.usage.reasoning_tokens == 1

    _call(p, strategy=InputStrategy.ORIGINAL_DOCUMENT, pdf_detail="high")
    item = client.responses.kwargs["input"][0]["content"][0]
    assert item["type"] == "input_file" and item["filename"] == "document.pdf" and item["detail"] == "high"
    assert item["file_data"] == "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 data").decode()

    _call(p, strategy=InputStrategy.ORIGINAL_DOCUMENT, pdf_detail="high", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    item = client.responses.kwargs["input"][0]["content"][0]
    assert "detail" not in item  # detail only applies to PDF


def test_error_mapping():
    p = OpenAIProvider("sk", client=_client(_http_err(openai.RateLimitError, 429, {"retry-after": "3"})))
    with pytest.raises(ProviderTransientError) as ei:
        _call(p)
    assert ei.value.retry_after_seconds == 3.0 and ei.value.details["http_status"] == 429

    p = OpenAIProvider("sk", client=_client(openai.APITimeoutError(request=httpx.Request("POST", "https://x"))))
    with pytest.raises(ProviderTimeoutError):
        _call(p)

    p = OpenAIProvider("sk", client=_client(_http_err(openai.InternalServerError, 503)))
    with pytest.raises(ProviderTransientError):
        _call(p)

    p = OpenAIProvider("sk", client=_client(_http_err(openai.AuthenticationError, 401)))
    with pytest.raises(ProviderConfigurationError):
        _call(p)

    p = OpenAIProvider("sk", client=_client(_http_err(openai.NotFoundError, 404)))
    with pytest.raises(ProviderPermanentError) as ei:
        _call(p)
    assert ei.value.code == "model_not_found"

    p = OpenAIProvider("sk", client=_client(_http_err(openai.BadRequestError, 400, message="Input file type is not supported for this model")))
    with pytest.raises(ProviderUnsupportedInputError):
        _call(p, strategy=InputStrategy.ORIGINAL_DOCUMENT)

    p = OpenAIProvider("sk", client=_client(_http_err(openai.BadRequestError, 400, message="invalid schema")))
    with pytest.raises(ProviderPermanentError) as ei:
        _call(p)
    assert ei.value.code == "bad_request"


def test_incomplete_and_refusal_are_permanent():
    p = OpenAIProvider("sk", client=_client(_ok_response("", status="incomplete")))
    with pytest.raises(ProviderPermanentError) as ei:
        _call(p)
    assert ei.value.code == "output_incomplete" and ei.value.details["usage"]["input_tokens"] == 10

    refusal = [SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="no")])]
    p = OpenAIProvider("sk", client=_client(_ok_response("", output=refusal)))
    with pytest.raises(ProviderPermanentError) as ei:
        _call(p)
    assert ei.value.code == "model_refusal"


def test_usage_parsing_handles_missing_details():
    u = usage_from_response(SimpleNamespace(model_dump=lambda: {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}))
    assert u.cached_tokens is None and u.reasoning_tokens is None and u.raw["total_tokens"] == 3
    assert usage_from_response(None) is None

"""Real OpenAI adapter: Responses API + Structured Outputs (strict JSON schema).

* original_document strategy -> ``input_file`` (base64 data URL, filename, optional PDF ``detail``)
* markdown strategy          -> ``input_text``

The SDK's own retries are disabled (``max_retries=0``) so that the application-level
budget in ``services.extraction`` (max attempts, per-call deadline, Retry-After) is the
only retry layer. ``store=False`` keeps SDS content out of OpenAI-side storage.

Verified against openai==3.24.0 signatures (``client.responses.create`` parameters,
``ResponseInputFileParam``, ``ResponseFormatTextJSONSchemaConfigParam``, usage details).
Live calls have NOT been executed in this repository until a key and model names are set.
"""
from __future__ import annotations

import base64
import time
from typing import Any

from domain.errors import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUnsupportedInputError,
)
from domain.models import InputStrategy, UsageRecord

from .provider_base import ModelInput, ProviderResponse

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _retry_after_seconds(exc: Any) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return float(ms) / 1000.0
        except ValueError:
            pass
    ra = headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except ValueError:
            return None
    return None


def _request_id(exc_or_resp: Any) -> str | None:
    rid = getattr(exc_or_resp, "request_id", None) or getattr(exc_or_resp, "_request_id", None)
    if rid:
        return rid
    resp = getattr(exc_or_resp, "response", None)
    headers = getattr(resp, "headers", None)
    return headers.get("x-request-id") if headers else None


def usage_from_response(usage: Any) -> UsageRecord | None:
    if usage is None:
        return None
    raw = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    itd = raw.get("input_tokens_details") or {}
    otd = raw.get("output_tokens_details") or {}
    return UsageRecord(
        input_tokens=raw.get("input_tokens"),
        output_tokens=raw.get("output_tokens"),
        total_tokens=raw.get("total_tokens"),
        cached_tokens=itd.get("cached_tokens"),
        reasoning_tokens=otd.get("reasoning_tokens"),
        raw=raw,
    )


class OpenAIProvider:
    name = "openai"
    is_test_double = False

    def __init__(self, api_key: str | None, *, client: Any | None = None):
        if client is None:
            if not api_key:
                raise ProviderConfigurationError("OPENAI_API_KEY is not configured")
            from openai import OpenAI  # imported lazily so mock mode never needs the SDK configured

            client = OpenAI(api_key=api_key, max_retries=0)
        self._client = client

    def build_request(self, *, model: str, instructions: str, model_input: ModelInput, json_schema: dict[str, Any],
                      schema_name: str, max_output_tokens: int, pdf_detail: str | None) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if model_input.strategy == InputStrategy.ORIGINAL_DOCUMENT:
            if model_input.file_bytes is None:
                raise ProviderPermanentError("original_document strategy requires file bytes", code="invalid_model_input")
            mime = model_input.mime_type or PDF_MIME
            data_url = f"data:{mime};base64," + base64.b64encode(model_input.file_bytes).decode("ascii")
            item: dict[str, Any] = {"type": "input_file", "filename": model_input.file_name or "document", "file_data": data_url}
            if pdf_detail and mime == PDF_MIME:
                item["detail"] = pdf_detail
            content.append(item)
            content.append({"type": "input_text", "text": "Extract the SDS fields from the attached document."})
        else:
            if model_input.text is None:
                raise ProviderPermanentError("markdown strategy requires text", code="invalid_model_input")
            content.append({"type": "input_text", "text": model_input.text})
        return {
            "model": model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": json_schema, "strict": True}},
            "max_output_tokens": max_output_tokens,
            "store": False,
        }

    def extract(self, *, model: str, instructions: str, model_input: ModelInput, json_schema: dict[str, Any],
                schema_name: str, max_output_tokens: int, deadline_seconds: float, pdf_detail: str | None) -> ProviderResponse:
        import openai

        request = self.build_request(model=model, instructions=instructions, model_input=model_input, json_schema=json_schema,
                                     schema_name=schema_name, max_output_tokens=max_output_tokens, pdf_detail=pdf_detail)
        started = time.monotonic()
        try:
            resp = self._client.responses.create(**request, timeout=deadline_seconds)
        except openai.APITimeoutError as e:
            raise ProviderTimeoutError(f"OpenAI request timed out after {deadline_seconds}s",
                                       details={"request_id": _request_id(e)}) from e
        except openai.RateLimitError as e:
            raise ProviderTransientError("OpenAI rate limited (429)", code="rate_limited",
                                         retry_after_seconds=_retry_after_seconds(e),
                                         details={"http_status": 429, "request_id": _request_id(e)}) from e
        except openai.APIConnectionError as e:
            raise ProviderTransientError("OpenAI connection error", code="connection_error",
                                         details={"request_id": _request_id(e)}) from e
        except openai.InternalServerError as e:
            raise ProviderTransientError("OpenAI server error", code="server_error",
                                         retry_after_seconds=_retry_after_seconds(e),
                                         details={"http_status": getattr(e, "status_code", None), "request_id": _request_id(e)}) from e
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise ProviderConfigurationError("OpenAI authentication/permission error",
                                             details={"http_status": getattr(e, "status_code", None), "request_id": _request_id(e)}) from e
        except openai.NotFoundError as e:
            raise ProviderPermanentError(f"OpenAI resource not found (model {model!r}?)", code="model_not_found",
                                         details={"http_status": 404, "request_id": _request_id(e)}) from e
        except openai.BadRequestError as e:
            msg = str(e)
            low = msg.lower()
            if model_input.strategy == InputStrategy.ORIGINAL_DOCUMENT and ("file" in low and ("not supported" in low or "unsupported" in low or "does not support" in low)):
                raise ProviderUnsupportedInputError(f"Model {model!r} rejected file input: {msg}",
                                                    details={"http_status": 400, "request_id": _request_id(e)}) from e
            raise ProviderPermanentError(f"OpenAI bad request: {msg}", code="bad_request",
                                         details={"http_status": 400, "request_id": _request_id(e)}) from e
        except openai.APIStatusError as e:
            status = getattr(e, "status_code", None) or 0
            if status >= 500 or status == 408:
                raise ProviderTransientError(f"OpenAI status {status}", details={"http_status": status, "request_id": _request_id(e)}) from e
            raise ProviderPermanentError(f"OpenAI status {status}: {e}", details={"http_status": status, "request_id": _request_id(e)}) from e

        elapsed_ms = int((time.monotonic() - started) * 1000)
        raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
        raw["_elapsed_ms"] = elapsed_ms
        usage = usage_from_response(getattr(resp, "usage", None))
        rid = _request_id(resp)

        # Refusals are permanent for this input.
        for item in getattr(resp, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", None) == "refusal":
                    raise ProviderPermanentError(f"Model refused: {getattr(part, 'refusal', '')}", code="model_refusal",
                                                 details={"response_id": resp.id, "request_id": rid, "usage": usage.model_dump() if usage else None})

        incomplete = None
        if getattr(resp, "status", None) == "incomplete":
            incomplete = getattr(getattr(resp, "incomplete_details", None), "reason", None) or "incomplete"
            raise ProviderPermanentError(f"Response incomplete: {incomplete}", code="output_incomplete",
                                         details={"reason": incomplete, "response_id": resp.id, "request_id": rid,
                                                  "usage": usage.model_dump() if usage else None})

        text = getattr(resp, "output_text", None) or ""
        return ProviderResponse(output_text=text, model=getattr(resp, "model", model), response_id=resp.id, request_id=rid,
                                usage=usage, http_status=200, incomplete_reason=incomplete, raw=raw)

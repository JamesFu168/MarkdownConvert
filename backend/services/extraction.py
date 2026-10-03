"""Model-call orchestration: attempt budget, deadlines, Retry-After, usage/cost persistence.

Per queue invocation at most ``max_attempts`` provider calls are made (default 2). Each
attempt is persisted (raw response or error) BEFORE the result is interpreted, so a crash
after the API answered still leaves an auditable record. Exactly-once is not claimed: an
API call that completed but whose record was never saved can be repeated on retry.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from adapters.provider_base import ExtractionProvider, ModelInput, ProviderResponse
from domain.errors import PermanentError, SchemaViolationError, TransientError
from domain.extraction_schema import SCHEMA_VERSION, SdsExtraction, strict_json_schema
from domain.models import ModelCallRecord, utcnow

from .config import BACKEND_DIR, Settings
from .cost import PricingTable, compute_cost

MAX_RETRY_SLEEP_SECONDS = 30.0
DEFAULT_RETRY_SLEEP_SECONDS = 2.0


def load_prompt(version: str) -> str:
    p = BACKEND_DIR / "prompts" / f"{version}.md"
    return p.read_text(encoding="utf-8")


@dataclass
class ExtractionOutcome:
    result: SdsExtraction
    raw_output: dict[str, Any]
    response: ProviderResponse
    calls: list[ModelCallRecord]


class ExtractionService:
    def __init__(self, provider: ExtractionProvider, settings: Settings, pricing: PricingTable, *,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.provider = provider
        self.settings = settings
        self.pricing = pricing
        self._sleep = sleep
        self._clock = clock
        self.schema = strict_json_schema()

    def run(self, *, model: str, model_input: ModelInput, instructions: str, pdf_detail: str | None,
            persist_attempt: Callable[[int, dict[str, Any]], None], on_call_recorded: Callable[[ModelCallRecord], None],
            total_budget_seconds: float | None = None) -> ExtractionOutcome:
        max_attempts = max(1, self.settings.openai_max_attempts)
        per_call = self.settings.openai_timeout_seconds
        budget_start = self._clock()
        calls: list[ModelCallRecord] = []
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if total_budget_seconds is not None:
                remaining = total_budget_seconds - (self._clock() - budget_start)
                if remaining <= 1:
                    break
                deadline = min(per_call, remaining)
            else:
                deadline = per_call
            started = utcnow()
            t0 = self._clock()
            rec = ModelCallRecord(attempt=attempt, provider=self.provider.name, model=model,
                                  is_test_double=self.provider.is_test_double, started_at=started)
            try:
                resp = self.provider.extract(model=model, instructions=instructions, model_input=model_input,
                                             json_schema=self.schema, schema_name="sds_extraction_pilot_v1",
                                             max_output_tokens=self.settings.openai_max_output_tokens,
                                             deadline_seconds=deadline, pdf_detail=pdf_detail)
            except (TransientError, PermanentError) as e:
                rec.ended_at = utcnow()
                rec.elapsed_ms = int((self._clock() - t0) * 1000)
                rec.error_code = e.code
                rec.http_status = e.details.get("http_status") if e.details else None
                rec.request_id = e.details.get("request_id") if e.details else None
                usage_detail = (e.details or {}).get("usage")
                if usage_detail:
                    from domain.models import UsageRecord
                    rec.usage = UsageRecord.model_validate(usage_detail)
                rec.cost = compute_cost(rec.usage, model, self.pricing, is_test_double=self.provider.is_test_double)
                persist_attempt(attempt, {"error": e.to_dict(), "call": rec.model_dump(mode="json")})
                calls.append(rec)
                on_call_recorded(rec)
                last_exc = e
                if isinstance(e, PermanentError):
                    raise
                if attempt < max_attempts:
                    self._sleep(self._retry_delay(e, budget_start, total_budget_seconds))
                continue

            rec.ended_at = utcnow()
            rec.elapsed_ms = int((self._clock() - t0) * 1000)
            rec.success = True
            rec.http_status = resp.http_status
            rec.response_id = resp.response_id
            rec.request_id = resp.request_id
            rec.usage = resp.usage
            rec.cost = compute_cost(resp.usage, resp.model or model, self.pricing, is_test_double=self.provider.is_test_double)
            persist_attempt(attempt, {"response": resp.raw, "output_text": resp.output_text, "call": rec.model_dump(mode="json")})
            calls.append(rec)
            on_call_recorded(rec)

            try:
                parsed = self._parse(resp.output_text)
            except SchemaViolationError as e:
                rec.success = False
                rec.error_code = e.code
                on_call_recorded(rec)
                last_exc = e
                if attempt < max_attempts:
                    self._sleep(DEFAULT_RETRY_SLEEP_SECONDS if total_budget_seconds is None else 0.0)
                    continue
                raise
            return ExtractionOutcome(result=parsed, raw_output=json.loads(resp.output_text), response=resp, calls=calls)

        if isinstance(last_exc, TransientError):
            raise TransientError(f"model call budget exhausted after {len(calls)} attempt(s): {last_exc.message}",
                                 code=last_exc.code, details={"attempts": len(calls)})
        if last_exc is not None:
            raise last_exc
        raise TransientError("no model attempt could be made within the remaining time budget", code="budget_exhausted")

    def _retry_delay(self, e: TransientError, budget_start: float, total_budget: float | None) -> float:
        delay = e.retry_after_seconds if e.retry_after_seconds is not None else DEFAULT_RETRY_SLEEP_SECONDS
        delay = min(max(delay, 0.0), MAX_RETRY_SLEEP_SECONDS)
        if total_budget is not None:
            remaining = total_budget - (self._clock() - budget_start)
            delay = max(0.0, min(delay, remaining - 1))
        return delay

    @staticmethod
    def _parse(text: str) -> SdsExtraction:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise SchemaViolationError(f"model output is not valid JSON: {e}", code="invalid_json") from e
        try:
            result = SdsExtraction.model_validate(data)
        except ValidationError as e:
            raise SchemaViolationError("model output does not match the extraction schema",
                                       details={"errors": json.loads(e.json())[:20]}) from e
        if result.schema_version != SCHEMA_VERSION:
            raise SchemaViolationError(f"unexpected schema_version {result.schema_version!r}")
        return result

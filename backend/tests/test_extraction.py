import pytest

from adapters.mock_provider import ScriptedProvider, success_response
from adapters.provider_base import ModelInput
from domain.errors import ProviderPermanentError, ProviderTransientError, TransientError
from domain.models import InputStrategy
from services.config import Settings
from services.extraction import ExtractionService


def _svc(provider, pricing, **kw):
    s = Settings(extraction_provider="mock", openai_timeout_seconds=kw.get("timeout", 100.0), openai_max_attempts=kw.get("attempts", 2))
    clock = {"t": 0.0}
    sleeps = []

    def sleep(x):
        sleeps.append(x)
        clock["t"] += x

    svc = ExtractionService(provider, s, pricing, sleep=sleep, clock=lambda: clock["t"])
    return svc, sleeps, clock


def _input():
    return ModelInput(strategy=InputStrategy.MARKDOWN, sha256="x", text="hello")


def test_max_two_calls_then_transient_raised(pricing):
    p = ScriptedProvider([ProviderTransientError("a"), ProviderTransientError("b"), success_response()])
    svc, sleeps, _ = _svc(p, pricing)
    saved = []
    with pytest.raises(TransientError):
        svc.run(model="m", model_input=_input(), instructions="i", pdf_detail=None, persist_attempt=lambda a, d: saved.append(a),
                on_call_recorded=lambda r: None)
    assert len(p.calls) == 2 and saved == [1, 2]


def test_retry_after_is_capped_and_budget_limits_deadline(pricing):
    p = ScriptedProvider([ProviderTransientError("429", retry_after_seconds=500.0), success_response()])
    svc, sleeps, clock = _svc(p, pricing, timeout=100.0)
    svc.run(model="m", model_input=_input(), instructions="i", pdf_detail=None, persist_attempt=lambda a, d: None,
            on_call_recorded=lambda r: None, total_budget_seconds=120.0)
    assert sleeps == [30.0]  # capped at MAX_RETRY_SLEEP_SECONDS
    assert p.calls[0]["deadline"] == 100.0
    assert p.calls[1]["deadline"] == pytest.approx(90.0)  # remaining budget 120-30


def test_permanent_error_not_retried(pricing):
    p = ScriptedProvider([ProviderPermanentError("bad", code="bad_request"), success_response()])
    svc, _, _ = _svc(p, pricing)
    with pytest.raises(ProviderPermanentError):
        svc.run(model="m", model_input=_input(), instructions="i", pdf_detail=None, persist_attempt=lambda a, d: None,
                on_call_recorded=lambda r: None)
    assert len(p.calls) == 1


def test_no_attempt_when_budget_already_spent(pricing):
    p = ScriptedProvider([success_response()])
    svc, _, clock = _svc(p, pricing)
    with pytest.raises(TransientError) as ei:
        svc.run(model="m", model_input=_input(), instructions="i", pdf_detail=None, persist_attempt=lambda a, d: None,
                on_call_recorded=lambda r: None, total_budget_seconds=0.5)
    assert ei.value.code == "budget_exhausted" and p.calls == []

import json

import pytest

from adapters.markitdown_adapter import StaticConverter
from adapters.mock_provider import MockProvider, ScriptedProvider, success_response
from domain.errors import ProviderTimeoutError, ProviderTransientError, TransientError
from domain.extraction_schema import Evidence, TextField, empty_extraction
from domain.models import JobStatus, QueueMessage, Stage
from services.storage import BlobPaths
from tests.conftest import SAMPLE_MARKDOWN, drain, upload


def _msg(container):
    return QueueMessage.model_validate_json(container.queue.messages[-1])


def test_markdown_strategy_happy_path(container):
    _, body = upload(container, arms=("B",))
    runs = drain(container)
    run = runs[0]
    assert run.status == JobStatus.SUCCEEDED
    assert [s.status for s in run.stages] == ["done", "done", "done", "done", "done"]
    job_id, run_id = body["job_id"], body["run_id"]
    assert container.blob.exists(BlobPaths.result(job_id, run_id))
    assert container.blob.exists(BlobPaths.validation(job_id, run_id))
    assert container.blob.exists(BlobPaths.model_response(job_id, run_id, 1))
    model_input = container.blob.get(BlobPaths.model_input(job_id, run_id)).decode("utf-8")
    assert "[[b0001]]" in model_input and "7732-18-5" in model_input
    result = json.loads(container.blob.get(BlobPaths.result(job_id, run_id)))
    assert result["is_test_double"] is True and result["immutable"] is True
    assert run.metrics["tokens"]["real_calls"] == 0 and run.metrics["cost"]["status"] == "unknown"
    assert run.lease_owner is None and run.finished_at is not None
    assert container.repo.get_job("owner-1", job_id).status == JobStatus.SUCCEEDED
    assert set(run.checkpoints) == {s.value for s in Stage}


def test_original_strategy_skips_convert_and_sends_file(make_container):
    provider = MockProvider("success")
    c = make_container(provider=provider)
    upload(c, arms=("A",))
    run = drain(c)[0]
    assert run.status == JobStatus.SUCCEEDED
    assert run.stage_record(Stage.CONVERT).status == "skipped"
    assert provider.calls[0]["strategy"] == "original_document"
    assert c.converter.calls == 0


def test_duplicate_message_after_completion_does_not_call_provider_again(make_container):
    provider = MockProvider("success")
    c = make_container(provider=provider)
    upload(c)
    msg = _msg(c)
    drain(c)
    assert len(provider.calls) == 1
    c.processor.process_message(msg)
    assert len(provider.calls) == 1


def test_live_lease_drops_duplicate_and_expired_lease_is_taken_over(make_container):
    provider = MockProvider("success")
    c = make_container(provider=provider)
    _, body = upload(c)
    msg = _msg(c)
    run = c.repo.get_run(body["job_id"], body["run_id"])
    from datetime import timedelta

    from domain.models import utcnow

    run.status = JobStatus.PROCESSING
    run.lease_owner = "other"
    run.lease_expires_at = utcnow() + timedelta(minutes=5)
    c.repo.update_run(run)
    c.processor.process_message(msg)
    assert provider.calls == []  # dropped
    run = c.repo.get_run(body["job_id"], body["run_id"])
    run.lease_expires_at = utcnow() - timedelta(seconds=1)
    c.repo.update_run(run)
    out = c.processor.process_message(msg)
    assert out.status == JobStatus.SUCCEEDED and any(w.startswith("lease_taken_over_from:other") for w in out.warnings)


def test_transient_error_defers_and_resume_reuses_checkpoints(make_container):
    provider = ScriptedProvider([ProviderTransientError("503", details={"http_status": 503}), ProviderTimeoutError("t/o"),
                                 success_response()])
    converter = StaticConverter(SAMPLE_MARKDOWN)
    c = make_container(provider=provider, converter=converter)
    _, body = upload(c, arms=("B",))
    msg = _msg(c)
    with pytest.raises(TransientError):
        c.processor.process_message(msg)
    run = c.repo.get_run(body["job_id"], body["run_id"])
    assert run.status == JobStatus.QUEUED and run.error["will_retry"] is True
    assert len(run.model_calls) == 2 and all(not m.success for m in run.model_calls)
    assert run.checkpoints["convert"]["done"] and "extract" not in run.checkpoints
    assert c.blob.exists(BlobPaths.model_response(body["job_id"], body["run_id"], 2))
    # second delivery (as the queue would do)
    out = c.processor.process_message(msg)
    assert out.status == JobStatus.SUCCEEDED
    assert converter.calls == 1  # conversion reused via checkpoint
    assert out.attempt_no == 2 and out.stage_record(Stage.CONVERT).detail.get("reused_checkpoint") is True
    assert len(provider.calls) == 3


def test_rate_limit_then_success_records_both_calls_and_respects_retry_after(make_container, monkeypatch):
    sleeps = []
    provider = ScriptedProvider([ProviderTransientError("429", code="rate_limited", retry_after_seconds=7.0, details={"http_status": 429}),
                                 success_response()])
    c = make_container(provider=provider)
    c.processor.extraction._sleep = sleeps.append
    upload(c, arms=("B",))
    run = drain(c)[0]
    assert run.status == JobStatus.SUCCEEDED
    assert [m.error_code for m in run.model_calls] == ["rate_limited", None]
    assert sleeps == [7.0]
    assert run.metrics["tokens"]["calls"] == 2


def test_permanent_errors_fail_without_retry(make_container):
    provider = MockProvider("refusal")
    c = make_container(provider=provider)
    _, body = upload(c)
    run = drain(c)[0]
    assert run.status == JobStatus.FAILED and run.error["code"] == "model_refusal"
    assert len(provider.calls) == 1
    assert not c.blob.exists(BlobPaths.result(body["job_id"], body["run_id"]))


def test_needs_ocr_document_fails_with_reason(make_container, fake_check):
    fake_check["classification"] = "needs_ocr"
    provider = MockProvider("success")
    c = make_container(provider=provider)
    upload(c)
    run = drain(c)[0]
    assert run.status == JobStatus.FAILED and run.error["code"] == "needs_ocr"
    assert provider.calls == []


def test_unsupported_file_input_marks_failure_not_silent_fallback(make_container):
    provider = MockProvider("unsupported_file")
    c = make_container(provider=provider)
    upload(c, arms=("C",))
    run = drain(c)[0]
    assert run.status == JobStatus.FAILED and run.error["code"] == "unsupported_file_input"


def test_schema_violation_twice_fails(make_container):
    provider = MockProvider("schema_violation")
    c = make_container(provider=provider)
    upload(c, arms=("B",))
    run = drain(c)[0]
    assert run.status == JobStatus.FAILED and run.error["code"] == "schema_violation"
    assert len(provider.calls) == 2


def test_invalid_json_then_success(make_container):
    provider = ScriptedProvider([
        type("R", (), {})(),  # placeholder replaced below
    ])
    from adapters.provider_base import ProviderResponse

    bad = ProviderResponse(output_text="{not json", model="scripted-model", response_id="r0", request_id="q0", usage=None, http_status=200)
    provider.script.clear()
    provider.script.extend([bad, success_response()])
    c = make_container(provider=provider)
    upload(c, arms=("B",))
    run = drain(c)[0]
    assert run.status == JobStatus.SUCCEEDED and run.model_calls[0].error_code == "invalid_json"


def test_result_with_findings_goes_to_needs_review_and_verifies_quotes(make_container):
    payload = empty_extraction().model_dump()
    payload["document"]["product_name"] = TextField(value="Test Product", raw_text="Test Product", status="found",
                                                    evidence=[Evidence(quote="Product identifier: Test Product", block_id="b0002")]).model_dump()
    payload["document"]["supplier_name"] = TextField(value="Nobody", raw_text="Nobody", status="found",
                                                     evidence=[Evidence(quote="Nobody Inc")]).model_dump()
    c = make_container(provider=ScriptedProvider([success_response(payload)]))
    _, body = upload(c, arms=("B",))
    run = drain(c)[0]
    assert run.status == JobStatus.NEEDS_REVIEW
    validation = json.loads(c.blob.get(BlobPaths.validation(body["job_id"], body["run_id"])))
    statuses = {ch["path"]: ch["status"] for ch in validation["evidence_checks"]}
    assert statuses["document.product_name.evidence[0]"] == "verified"
    assert statuses["document.supplier_name.evidence[0]"] == "not_found"
    assert "quote_not_found" in {f["code"] for f in validation["findings"]}


def test_poison_handler_marks_failed(container):
    _, body = upload(container)
    msg = _msg(container)
    run = container.processor.handle_poison(msg, dequeue_count=3, raw=None)
    assert run.status == JobStatus.FAILED and run.error["code"] == "poison_queue"
    assert container.repo.get_job("owner-1", body["job_id"]).status == JobStatus.FAILED


def test_real_provider_usage_and_cost_are_measured(make_container):
    from domain.models import UsageRecord

    usage = UsageRecord(input_tokens=2_000_000, output_tokens=1_000_000, cached_tokens=1_000_000, reasoning_tokens=500_000)
    provider = ScriptedProvider([success_response(usage=usage)])
    provider.is_test_double = False
    provider.name = "scripted-real"
    c = make_container(provider=provider)
    upload(c, arms=("B",))
    run = drain(c)[0]
    assert run.metrics["tokens"]["real_calls"] == 1
    # 1M uncached*1 + 1M cached*0.5 + 1M output*4 = 5.5
    assert run.metrics["cost"]["status"] == "measured" and abs(run.metrics["cost"]["measured_amount"] - 5.5) < 1e-9
    assert run.metrics["tokens"]["reasoning_included_in_output"] == 500_000

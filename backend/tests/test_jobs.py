import json
from datetime import timedelta

import pytest

from adapters.storage_memory import MemoryQueue
from domain.errors import BadRequestError, ConflictError, DocumentTooLargeError, ProviderConfigurationError, UnsupportedFormatError
from domain.models import JobStatus, QueueMessage, utcnow
from services.config import Settings
from tests.conftest import FAKE_PDF, drain, upload


def test_create_job_returns_202_and_enqueues_locators_only(container):
    status, body = upload(container, arms=("A", "B"))
    assert status == 202
    assert len(body["jobs"]) == 2 and body["status_url"].startswith("/api/jobs/")
    assert len(container.queue.messages) == 2
    msg = QueueMessage.model_validate_json(container.queue.messages[0])
    assert set(json.loads(container.queue.messages[0]).keys()) == {"job_id", "run_id", "document_id", "owner_id", "schema_version"}
    run = container.repo.get_run(msg.job_id, msg.run_id)
    assert run.status == JobStatus.QUEUED and run.config.arm in ("A", "B")
    doc = container.repo.get_document("owner-1", body["document_id"])
    assert doc.file_name == "user-file.pdf" and doc.blob_path == f"documents/{doc.document_id}/original.pdf"
    assert container.blob.get(doc.blob_path) == FAKE_PDF


def test_config_snapshot_separates_models_and_strategies(container):
    _, body = upload(container, arms=("A", "B", "C", "D"))
    cfgs = {j["arm"]: container.repo.get_run(j["job_id"], j["run_id"]).config for j in body["jobs"]}
    assert cfgs["A"].input_strategy.value == "original_document" and cfgs["B"].input_strategy.value == "markdown"
    assert cfgs["A"].model == cfgs["B"].model == "mock-baseline"
    assert cfgs["C"].model == cfgs["D"].model == "mock-candidate"
    assert cfgs["A"].converter is None and cfgs["B"].converter == "static"


def test_rejections(container):
    with pytest.raises(BadRequestError):
        upload(container, arms=("Z",))
    with pytest.raises(UnsupportedFormatError):
        upload(container, data=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", name="old.doc")
    with pytest.raises(DocumentTooLargeError):
        upload(container, data=b"%PDF" + b"0" * (container.settings.max_upload_bytes + 1))


def test_idempotency_replay_and_conflict(container):
    s1, b1 = upload(container, key="k1")
    s2, b2 = upload(container, key="k1")
    assert s1 == 202 and s2 == 202
    assert b1["job_id"] == b2["job_id"] and b2["idempotent_replay"] is True
    assert len(container.queue.messages) == 1
    with pytest.raises(ConflictError):
        upload(container, key="k1", arms=("A",))


def test_enqueue_failure_keeps_recoverable_record_and_replay_completes_it(make_container):
    queue = MemoryQueue(fail_next=1)
    c = make_container(queue=queue)
    status, body = upload(c, arms=("A", "B"), key="k2")
    assert status == 503 and body["error"]["code"] == "enqueue_failed"
    statuses = sorted(j["status"] for j in body["jobs"])
    assert statuses == ["enqueue_pending", "queued"]
    # resend with same key: only the pending one is enqueued
    status, body = upload(c, arms=("A", "B"), key="k2")
    assert status == 202 and all(j["status"] == "queued" for j in body["jobs"])
    assert queue.sent_count == 2


def test_reconcile_requeues_pending_and_expired_leases(make_container):
    queue = MemoryQueue(fail_next=1)
    c = make_container(queue=queue)
    status, body = upload(c, arms=("A",))
    assert status == 503
    job_id, run_id = body["job_id"], body["run_id"]
    run = c.repo.get_run(job_id, run_id)
    run.updated_at = utcnow() - timedelta(minutes=5)
    c.repo.update_run(run)
    out = c.jobs.reconcile(pending_older_than_seconds=60)
    assert out["requeued_enqueue_pending"] == 1
    assert c.repo.get_run(job_id, run_id).status == JobStatus.QUEUED

    # expired lease
    run = c.repo.get_run(job_id, run_id)
    run.status = JobStatus.PROCESSING
    run.lease_owner = "dead-instance"
    run.lease_expires_at = utcnow() - timedelta(seconds=1)
    c.repo.update_run(run)
    out = c.jobs.reconcile()
    assert out["requeued_expired_lease"] == 1
    r = c.repo.get_run(job_id, run_id)
    assert r.status == JobStatus.QUEUED and any(w.startswith("lease_expired_requeued") for w in r.warnings)


def test_retry_rules(container):
    _, body = upload(container)
    job_id, run_id = body["job_id"], body["run_id"]
    with pytest.raises(ConflictError):
        container.jobs.retry(owner_id="owner-1", job_id=job_id, reason="too early")
    drain(container)
    run = container.repo.get_run(job_id, run_id)
    run.status = JobStatus.FAILED
    container.repo.update_run(run)
    job = container.repo.get_job("owner-1", job_id)
    job.status = JobStatus.FAILED
    container.repo.update_job(job)
    out = container.jobs.retry(owner_id="owner-1", job_id=job_id, reason="manual")
    assert out["previous_run_id"] == run_id and out["status"] == "queued"
    job = container.repo.get_job("owner-1", job_id)
    assert job.run_count == 2 and job.latest_run_id == out["run_id"]
    new_run = container.repo.get_run(job_id, out["run_id"])
    assert new_run.previous_run_id == run_id and new_run.retry_reason == "manual"


def test_real_provider_without_models_fails_fast_before_storing(make_container, pricing):
    s = Settings(extraction_provider="openai", openai_api_key="sk-test", openai_model_baseline=None, openai_model_candidate=None)
    with pytest.raises(ProviderConfigurationError):
        s.validate_for_provider()
    from adapters.mock_provider import MockProvider

    c = make_container(provider=MockProvider(), settings_override=s)
    with pytest.raises(ProviderConfigurationError):
        upload(c, arms=("A",))
    assert c.blob.paths() == []

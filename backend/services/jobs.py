"""Job creation (upload -> blob -> table -> queue), idempotency, retry and reconciliation."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from domain.errors import BadRequestError, ConflictError, NotFoundError, TransientError
from domain.models import (
    DocumentRecord,
    ExperimentArm,
    InputStrategy,
    JobRecord,
    JobStatus,
    QueueMessage,
    RunConfigSnapshot,
    RunRecord,
    iso,
    utcnow,
)
from services.locator import WS_NORMALIZATION_VERSION

from .config import Settings
from .document_check import CONTENT_TYPES, check_size, sha256_hex, sniff_format
from .storage import BlobPaths, BlobStore, QueueStore, Repository

log = logging.getLogger("sds.jobs")


def new_id() -> str:
    return uuid.uuid4().hex


class JobService:
    def __init__(self, repo: Repository, blob: BlobStore, queue: QueueStore, settings: Settings,
                 arms: dict[str, ExperimentArm], *, provider_name: str, provider_is_test_double: bool,
                 converter_name: str | None, converter_version: str | None, pricing_version: str | None):
        self.repo = repo
        self.blob = blob
        self.queue = queue
        self.settings = settings
        self.arms = arms
        self.provider_name = provider_name
        self.provider_is_test_double = provider_is_test_double
        self.converter_name = converter_name
        self.converter_version = converter_version
        self.pricing_version = pricing_version

    # ------------------------------------------------------------------ create
    def build_config(self, arm: ExperimentArm) -> RunConfigSnapshot:
        model = self.settings.model_for(arm.model_role)
        uses_converter = arm.input_strategy == InputStrategy.MARKDOWN
        return RunConfigSnapshot(
            arm=arm.arm, input_strategy=arm.input_strategy, model_role=arm.model_role, model=model,
            provider=self.provider_name, is_test_double=self.provider_is_test_double,
            prompt_version=self.settings.prompt_version, schema_version="pilot-v1",
            converter=self.converter_name if uses_converter else None,
            converter_version=self.converter_version if uses_converter else None,
            pdf_file_detail=self.settings.openai_file_detail or "default",
            max_output_tokens=self.settings.openai_max_output_tokens,
            timeout_seconds=self.settings.openai_timeout_seconds, max_attempts=self.settings.openai_max_attempts,
            pricing_table_version=self.pricing_version, whitespace_normalization_version=WS_NORMALIZATION_VERSION,
        )

    def _payload_hash(self, doc_sha: str, arms: list[str], experiment_id: str | None) -> str:
        return hashlib.sha256(json.dumps({"sha": doc_sha, "arms": sorted(arms), "exp": experiment_id}).encode()).hexdigest()

    def create_jobs(self, *, owner_id: str, file_name: str, content_type: str | None, data: bytes, arms: list[str],
                    experiment_id: str | None, idempotency_key: str | None) -> tuple[int, dict[str, Any]]:
        """Returns (http_status, body). 202 when all runs are queued, 503 when some are still enqueue_pending."""
        if not arms:
            raise BadRequestError("至少需要一個 experiment arm")
        unknown = [a for a in arms if a not in self.arms]
        if unknown:
            raise BadRequestError(f"不允許的 arm: {', '.join(unknown)}", details={"allowed": sorted(self.arms)})
        check_size(len(data), self.settings.max_upload_bytes)
        fmt = sniff_format(data, file_name, content_type)
        doc_sha = sha256_hex(data)
        payload_hash = self._payload_hash(doc_sha, arms, experiment_id)

        if idempotency_key:
            existing = self.repo.get_idempotency(owner_id, idempotency_key)
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise ConflictError("Idempotency-Key 已用於不同的 payload")
                return self._replay(owner_id, existing)

        # Config snapshots are built first so a misconfigured provider fails before anything is stored.
        configs = {a: self.build_config(self.arms[a]) for a in arms}

        document_id = new_id()
        blob_path = BlobPaths.original(document_id, fmt.value)
        self.blob.put(blob_path, data, CONTENT_TYPES[fmt])
        doc = DocumentRecord(document_id=document_id, owner_id=owner_id, file_name=file_name, format=fmt,
                             content_type=CONTENT_TYPES[fmt], size_bytes=len(data), sha256=doc_sha, blob_path=blob_path)
        self.repo.put_document(doc)

        job_ids: list[tuple[str, str]] = []
        for arm in arms:
            job_id, run_id = new_id(), new_id()
            run = RunRecord(run_id=run_id, job_id=job_id, owner_id=owner_id, document_id=document_id, config=configs[arm])
            job = JobRecord(job_id=job_id, owner_id=owner_id, document_id=document_id, file_name=file_name, arm=arm,
                            experiment_id=experiment_id, latest_run_id=run_id)
            self.repo.put_run(run)
            self.repo.put_job(job)
            job_ids.append((job_id, run_id))

        if idempotency_key:
            self.repo.put_idempotency(owner_id, idempotency_key, {"payload_hash": payload_hash, "document_id": document_id,
                                                                  "jobs": [{"job_id": j, "run_id": r} for j, r in job_ids]})
        return self._enqueue_all(owner_id, document_id, job_ids, replayed=False)

    def _replay(self, owner_id: str, record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        job_ids = [(j["job_id"], j["run_id"]) for j in record["jobs"]]
        return self._enqueue_all(owner_id, record["document_id"], job_ids, replayed=True)

    def _enqueue_all(self, owner_id: str, document_id: str, job_ids: list[tuple[str, str]], *, replayed: bool) -> tuple[int, dict[str, Any]]:
        items = []
        pending = 0
        for job_id, run_id in job_ids:
            job = self.repo.get_job(owner_id, job_id)
            run = self.repo.get_run(job_id, run_id)
            if job is None or run is None:
                raise NotFoundError("job referenced by idempotency record no longer exists")
            if run.status == JobStatus.ENQUEUE_PENDING:
                try:
                    self.enqueue_run(job, run)
                except Exception as e:  # noqa: BLE001 - queue outage: keep recoverable record
                    log.warning("enqueue failed for run %s: %s", run_id, type(e).__name__)
                    pending += 1
            items.append({"job_id": job_id, "run_id": run_id, "arm": job.arm, "status": self.repo.get_run(job_id, run_id).status.value,
                          "status_url": f"/api/jobs/{job_id}"})
        body: dict[str, Any] = {"document_id": document_id, "jobs": items, "job_id": items[0]["job_id"], "run_id": items[0]["run_id"],
                                "status_url": items[0]["status_url"], "idempotent_replay": replayed}
        if pending:
            body["error"] = {"code": "enqueue_failed", "retryable": True,
                             "message": f"{pending} 個任務尚未入列，記錄已保留；請以相同 Idempotency-Key 重送或等待 reconciliation"}
            return 503, body
        return 202, body

    def enqueue_run(self, job: JobRecord, run: RunRecord) -> None:
        msg = QueueMessage(job_id=job.job_id, run_id=run.run_id, document_id=job.document_id, owner_id=job.owner_id)
        self.queue.send(msg.model_dump_json())
        run.status = JobStatus.QUEUED
        run.updated_at = utcnow()
        self.repo.update_run(run)
        job.status = JobStatus.QUEUED
        job.updated_at = utcnow()
        self.repo.update_job(job)

    # ------------------------------------------------------------------- retry
    def retry(self, *, owner_id: str, job_id: str, reason: str | None) -> dict[str, Any]:
        job = self.repo.get_job(owner_id, job_id)
        if job is None:
            raise NotFoundError("job not found")
        latest = self.repo.get_run(job_id, job.latest_run_id)
        if latest is None or latest.status not in (JobStatus.FAILED, JobStatus.NEEDS_REVIEW):
            raise ConflictError("只有 failed 或 needs_review 的 run 可以重試", details={"status": latest.status.value if latest else None})
        if job.run_count >= self.settings.max_retries_per_job:
            raise ConflictError(f"已達重試上限 {self.settings.max_retries_per_job}")
        new_run = RunRecord(run_id=new_id(), job_id=job_id, owner_id=owner_id, document_id=job.document_id,
                            config=self.build_config(self.arms[job.arm]), previous_run_id=latest.run_id, retry_reason=reason)
        self.repo.put_run(new_run)
        job.latest_run_id = new_run.run_id
        job.run_count += 1
        job.status = JobStatus.ENQUEUE_PENDING
        self.repo.update_job(job)
        try:
            self.enqueue_run(job, new_run)
        except Exception as e:  # noqa: BLE001
            raise TransientError("新 run 已建立但入列失敗，reconciliation 會重送", code="enqueue_failed",
                                 details={"job_id": job_id, "run_id": new_run.run_id}) from e
        return {"job_id": job_id, "run_id": new_run.run_id, "previous_run_id": latest.run_id, "status": new_run.status.value,
                "status_url": f"/api/jobs/{job_id}"}

    # ---------------------------------------------------------------- reconcile
    def reconcile(self, *, pending_older_than_seconds: int = 60) -> dict[str, Any]:
        """Re-enqueue runs stuck in enqueue_pending and runs whose processing lease expired."""
        now = utcnow()
        requeued_pending = requeued_expired = 0
        for run in self.repo.scan_runs(statuses=[JobStatus.ENQUEUE_PENDING], updated_before=now - timedelta(seconds=pending_older_than_seconds)):
            job = self.repo.get_job(run.owner_id, run.job_id)
            if job is None:
                continue
            try:
                self.enqueue_run(job, run)
                requeued_pending += 1
            except Exception as e:  # noqa: BLE001
                log.warning("reconcile: enqueue still failing for %s: %s", run.run_id, type(e).__name__)
        for run in self.repo.scan_runs(statuses=[JobStatus.PROCESSING], updated_before=None):
            if run.lease_expires_at and run.lease_expires_at < now:
                job = self.repo.get_job(run.owner_id, run.job_id)
                if job is None:
                    continue
                run.warnings.append(f"lease_expired_requeued:{iso(now)}")
                run.lease_owner = None
                run.lease_expires_at = None
                run.status = JobStatus.ENQUEUE_PENDING
                run.updated_at = now
                try:
                    self.repo.update_run(run)
                    self.enqueue_run(job, run)
                    requeued_expired += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("reconcile: requeue of expired lease failed for %s: %s", run.run_id, type(e).__name__)
        return {"requeued_enqueue_pending": requeued_pending, "requeued_expired_lease": requeued_expired, "at": iso(now)}

    # ------------------------------------------------------------------- views
    @staticmethod
    def job_view(job: JobRecord, run: RunRecord | None) -> dict[str, Any]:
        return {
            "job_id": job.job_id, "document_id": job.document_id, "file_name": job.file_name, "arm": job.arm,
            "experiment_id": job.experiment_id, "status": job.status.value, "run_count": job.run_count,
            "created_at": iso(job.created_at), "updated_at": iso(job.updated_at),
            "latest_run": JobService.run_view(run) if run else None,
        }

    @staticmethod
    def run_view(run: RunRecord) -> dict[str, Any]:
        return {
            "run_id": run.run_id, "status": run.status.value, "stage": run.stage.value if run.stage else None,
            "attempt_no": run.attempt_no, "previous_run_id": run.previous_run_id, "retry_reason": run.retry_reason,
            "created_at": iso(run.created_at), "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
            "updated_at": iso(run.updated_at), "lease_expires_at": iso(run.lease_expires_at),
            "stages": [{"stage": s.stage.value, "status": s.status, "elapsed_ms": s.elapsed_ms, "detail": s.detail} for s in run.stages],
            "warnings": run.warnings, "error": run.error, "config": run.config.model_dump(mode="json") if run.config else None,
            "metrics": run.metrics, "review_version": run.review_version,
            "model_calls": [c.model_dump(mode="json") for c in run.model_calls],
        }

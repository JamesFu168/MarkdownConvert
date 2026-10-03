"""Storage ports (Blob / Table-like repository / Queue) and blob path conventions.

Paths are built from UUIDs and versions only; original file names are metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.models import DocumentRecord, JobRecord, JobStatus, ReviewRecord, RunRecord


class BlobStore(Protocol):
    def put(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...

    def get(self, path: str) -> bytes: ...

    def exists(self, path: str) -> bool: ...


class QueueStore(Protocol):
    def send(self, body: str) -> None: ...


class Repository(Protocol):
    # documents
    def put_document(self, doc: DocumentRecord) -> None: ...

    def get_document(self, owner_id: str, document_id: str) -> DocumentRecord | None: ...

    # jobs
    def put_job(self, job: JobRecord) -> None: ...

    def get_job(self, owner_id: str, job_id: str) -> JobRecord | None: ...

    def update_job(self, job: JobRecord) -> None: ...

    def list_jobs(self, owner_id: str, *, status: JobStatus | None, cursor: str | None, limit: int) -> tuple[list[JobRecord], str | None]: ...

    def list_jobs_by_experiment(self, owner_id: str, experiment_id: str) -> list[JobRecord]: ...

    # runs
    def put_run(self, run: RunRecord) -> None: ...

    def get_run(self, job_id: str, run_id: str) -> RunRecord | None: ...

    def update_run(self, run: RunRecord) -> None:
        """Conditional update using ``run.etag``; raises LeaseConflictError when the stored etag differs."""
        ...

    def list_runs(self, job_id: str) -> list[RunRecord]: ...

    def scan_runs(self, *, statuses: list[JobStatus], updated_before: datetime | None) -> list[RunRecord]: ...

    # idempotency
    def get_idempotency(self, owner_id: str, key: str) -> dict | None: ...

    def put_idempotency(self, owner_id: str, key: str, record: dict) -> None: ...

    # reviews
    def put_review(self, review: ReviewRecord) -> None: ...

    def list_reviews(self, run_id: str) -> list[ReviewRecord]: ...


class BlobPaths:
    @staticmethod
    def original(document_id: str, ext: str) -> str:
        return f"documents/{document_id}/original.{ext}"

    @staticmethod
    def document_check(document_id: str) -> str:
        return f"documents/{document_id}/check.json"

    @staticmethod
    def text_layer(document_id: str) -> str:
        return f"documents/{document_id}/textlayer.json"

    @staticmethod
    def markdown(document_id: str, converter_version: str) -> str:
        return f"documents/{document_id}/markdown/{converter_version}/content.md"

    @staticmethod
    def blocks(document_id: str, converter_version: str) -> str:
        return f"documents/{document_id}/markdown/{converter_version}/blocks.json"

    @staticmethod
    def run_prefix(job_id: str, run_id: str) -> str:
        return f"jobs/{job_id}/runs/{run_id}/"

    @classmethod
    def model_input(cls, job_id: str, run_id: str) -> str:
        return cls.run_prefix(job_id, run_id) + "model-input.txt"

    @classmethod
    def model_response(cls, job_id: str, run_id: str, attempt: int) -> str:
        return cls.run_prefix(job_id, run_id) + f"model-response-{attempt}.json"

    @classmethod
    def result(cls, job_id: str, run_id: str) -> str:
        return cls.run_prefix(job_id, run_id) + "result.json"

    @classmethod
    def validation(cls, job_id: str, run_id: str) -> str:
        return cls.run_prefix(job_id, run_id) + "validation.json"

    @classmethod
    def review(cls, job_id: str, run_id: str, version: int) -> str:
        return cls.run_prefix(job_id, run_id) + f"reviews/v{version:04d}.json"


ARTIFACT_KINDS: dict[str, str] = {
    "original": "application/octet-stream",
    "markdown": "text/markdown; charset=utf-8",
    "model-input": "text/plain; charset=utf-8",
    "result": "application/json",
    "validation": "application/json",
}

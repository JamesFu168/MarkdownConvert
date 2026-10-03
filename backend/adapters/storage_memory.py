"""In-memory storage used by automated tests (and nothing else)."""
from __future__ import annotations

import base64
import json
import threading
import uuid
from datetime import datetime

from domain.errors import LeaseConflictError, NotFoundError
from domain.models import DocumentRecord, JobRecord, JobStatus, ReviewRecord, RunRecord


class MemoryBlobStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, str]] = {}

    def put(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._data[path] = (bytes(data), content_type)

    def get(self, path: str) -> bytes:
        if path not in self._data:
            raise NotFoundError(f"blob not found: {path}")
        return self._data[path][0]

    def exists(self, path: str) -> bool:
        return path in self._data

    def paths(self) -> list[str]:
        return sorted(self._data)


class MemoryQueue:
    def __init__(self, *, fail_next: int = 0) -> None:
        self.messages: list[str] = []
        self.fail_next = fail_next
        self.sent_count = 0

    def send(self, body: str) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("simulated queue outage")
        self.sent_count += 1
        self.messages.append(body)

    def pop(self) -> str | None:
        return self.messages.pop(0) if self.messages else None


class MemoryRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.documents: dict[tuple[str, str], DocumentRecord] = {}
        self.jobs: dict[tuple[str, str], JobRecord] = {}
        self.runs: dict[tuple[str, str], RunRecord] = {}
        self.idem: dict[tuple[str, str], dict] = {}
        self.reviews: dict[str, list[ReviewRecord]] = {}

    # documents
    def put_document(self, doc: DocumentRecord) -> None:
        with self._lock:
            self.documents[(doc.owner_id, doc.document_id)] = doc.model_copy(deep=True)

    def get_document(self, owner_id: str, document_id: str) -> DocumentRecord | None:
        d = self.documents.get((owner_id, document_id))
        return d.model_copy(deep=True) if d else None

    # jobs
    def put_job(self, job: JobRecord) -> None:
        with self._lock:
            job.etag = uuid.uuid4().hex
            self.jobs[(job.owner_id, job.job_id)] = job.model_copy(deep=True)

    def get_job(self, owner_id: str, job_id: str) -> JobRecord | None:
        j = self.jobs.get((owner_id, job_id))
        return j.model_copy(deep=True) if j else None

    def update_job(self, job: JobRecord) -> None:
        with self._lock:
            job.etag = uuid.uuid4().hex
            self.jobs[(job.owner_id, job.job_id)] = job.model_copy(deep=True)

    def list_jobs(self, owner_id: str, *, status: JobStatus | None, cursor: str | None, limit: int) -> tuple[list[JobRecord], str | None]:
        items = [j for (o, _), j in self.jobs.items() if o == owner_id and (status is None or j.status == status)]
        items.sort(key=lambda j: j.created_at, reverse=True)
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode()) if cursor else 0
        page = items[offset: offset + limit]
        nxt = base64.urlsafe_b64encode(str(offset + limit).encode()).decode() if offset + limit < len(items) else None
        return [j.model_copy(deep=True) for j in page], nxt

    def list_jobs_by_experiment(self, owner_id: str, experiment_id: str) -> list[JobRecord]:
        return [j.model_copy(deep=True) for (o, _), j in self.jobs.items() if o == owner_id and j.experiment_id == experiment_id]

    # runs
    def put_run(self, run: RunRecord) -> None:
        with self._lock:
            run.etag = uuid.uuid4().hex
            self.runs[(run.job_id, run.run_id)] = run.model_copy(deep=True)

    def get_run(self, job_id: str, run_id: str) -> RunRecord | None:
        r = self.runs.get((job_id, run_id))
        return r.model_copy(deep=True) if r else None

    def update_run(self, run: RunRecord) -> None:
        with self._lock:
            current = self.runs.get((run.job_id, run.run_id))
            if current is None:
                raise NotFoundError("run not found")
            if run.etag != current.etag:
                raise LeaseConflictError("run was modified concurrently (etag mismatch)")
            run.etag = uuid.uuid4().hex
            self.runs[(run.job_id, run.run_id)] = run.model_copy(deep=True)

    def list_runs(self, job_id: str) -> list[RunRecord]:
        rs = [r.model_copy(deep=True) for (j, _), r in self.runs.items() if j == job_id]
        rs.sort(key=lambda r: r.created_at)
        return rs

    def scan_runs(self, *, statuses: list[JobStatus], updated_before: datetime | None) -> list[RunRecord]:
        out = []
        for r in self.runs.values():
            if r.status in statuses and (updated_before is None or r.updated_at <= updated_before):
                out.append(r.model_copy(deep=True))
        return out

    # idempotency
    def get_idempotency(self, owner_id: str, key: str) -> dict | None:
        v = self.idem.get((owner_id, key))
        return json.loads(json.dumps(v)) if v else None

    def put_idempotency(self, owner_id: str, key: str, record: dict) -> None:
        self.idem[(owner_id, key)] = record

    # reviews
    def put_review(self, review: ReviewRecord) -> None:
        self.reviews.setdefault(review.run_id, []).append(review.model_copy(deep=True))

    def list_reviews(self, run_id: str) -> list[ReviewRecord]:
        return [r.model_copy(deep=True) for r in self.reviews.get(run_id, [])]

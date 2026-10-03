"""Job / Run / Document records and their state machine."""
from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class JobStatus(str, enum.Enum):
    ENQUEUE_PENDING = "enqueue_pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.NEEDS_REVIEW, JobStatus.FAILED)


class Stage(str, enum.Enum):
    VALIDATE_DOCUMENT = "validate_document"
    CONVERT = "convert"
    EXTRACT = "extract"
    VALIDATE_RESULT = "validate_result"
    PERSIST = "persist"


STAGE_ORDER: list[Stage] = [Stage.VALIDATE_DOCUMENT, Stage.CONVERT, Stage.EXTRACT, Stage.VALIDATE_RESULT, Stage.PERSIST]


class InputStrategy(str, enum.Enum):
    ORIGINAL_DOCUMENT = "original_document"
    MARKDOWN = "markdown"


class ModelRole(str, enum.Enum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


class DocumentFormat(str, enum.Enum):
    PDF = "pdf"
    DOCX = "docx"


class ExperimentArm(BaseModel):
    """One experiment arm (A/B/C/D). Defined by versioned config, not by the browser."""
    arm: str
    input_strategy: InputStrategy
    model_role: ModelRole
    description: str = ""


class StageRecord(BaseModel):
    stage: Stage
    status: Literal["pending", "running", "done", "skipped", "failed"] = "pending"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    elapsed_ms: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class DocumentRecord(BaseModel):
    document_id: str
    owner_id: str
    file_name: str
    format: DocumentFormat
    content_type: str
    size_bytes: int
    sha256: str
    blob_path: str
    created_at: datetime = Field(default_factory=utcnow)


class UsageRecord(BaseModel):
    """Raw usage as returned by the provider plus derived subset-safe totals."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None  # subset of input_tokens
    reasoning_tokens: int | None = None  # subset of output_tokens
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelCallRecord(BaseModel):
    attempt: int
    provider: str
    model: str
    is_test_double: bool
    started_at: datetime
    ended_at: datetime | None = None
    elapsed_ms: int | None = None
    success: bool = False
    error_code: str | None = None
    http_status: int | None = None
    response_id: str | None = None
    request_id: str | None = None
    usage: UsageRecord | None = None
    cost: dict[str, Any] | None = None  # see services.cost


class RunConfigSnapshot(BaseModel):
    arm: str
    input_strategy: InputStrategy
    model_role: ModelRole
    model: str
    provider: str
    is_test_double: bool
    prompt_version: str
    schema_version: str
    converter: str | None
    converter_version: str | None
    pdf_file_detail: str
    max_output_tokens: int
    timeout_seconds: float
    max_attempts: int
    pricing_table_version: str | None
    whitespace_normalization_version: str


class RunRecord(BaseModel):
    run_id: str
    job_id: str
    owner_id: str
    document_id: str
    attempt_no: int = 0  # how many queue deliveries have started processing this run
    status: JobStatus = JobStatus.ENQUEUE_PENDING
    stage: Stage | None = None
    stages: list[StageRecord] = Field(default_factory=lambda: [StageRecord(stage=s) for s in STAGE_ORDER])
    config: RunConfigSnapshot | None = None
    checkpoints: dict[str, Any] = Field(default_factory=dict)  # stage -> blob paths / data reused on retry
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    model_calls: list[ModelCallRecord] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    previous_run_id: str | None = None
    retry_reason: str | None = None
    review_version: int = 0
    etag: str | None = None  # optimistic concurrency token from the store

    def stage_record(self, stage: Stage) -> StageRecord:
        for s in self.stages:
            if s.stage == stage:
                return s
        rec = StageRecord(stage=stage)
        self.stages.append(rec)
        return rec


class JobRecord(BaseModel):
    job_id: str
    owner_id: str
    document_id: str
    file_name: str
    arm: str
    experiment_id: str | None = None
    status: JobStatus = JobStatus.ENQUEUE_PENDING
    latest_run_id: str
    run_count: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    etag: str | None = None


class QueueMessage(BaseModel):
    """Only locators travel through the queue; never document content."""
    job_id: str
    run_id: str
    document_id: str
    owner_id: str
    schema_version: Literal["queue-v1"] = "queue-v1"


class ReviewRecord(BaseModel):
    run_id: str
    version: int
    reviewer_id: str
    created_at: datetime = Field(default_factory=utcnow)
    corrected: dict[str, Any]  # full corrected extraction (same schema as machine result)
    note: str | None = None
    changed_paths: list[str] = Field(default_factory=list)

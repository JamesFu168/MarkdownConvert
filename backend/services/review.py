"""Versioned human corrections. The machine result is immutable; every correction is a new version."""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from domain.errors import BadRequestError, ConflictError
from domain.extraction_schema import SdsExtraction
from domain.models import JobRecord, ReviewRecord, RunRecord, utcnow

from .storage import BlobPaths, BlobStore, Repository


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def changed_paths(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    fb, fa = flatten(before), flatten(after)
    return sorted(k for k in set(fb) | set(fa) if fb.get(k, "__missing__") != fa.get(k, "__missing__"))


class ReviewService:
    def __init__(self, repo: Repository, blob: BlobStore):
        self.repo = repo
        self.blob = blob

    def save(self, *, job: JobRecord, run: RunRecord, reviewer_id: str, corrected: dict[str, Any],
             expected_review_version: int, note: str | None) -> ReviewRecord:
        if expected_review_version != run.review_version:
            raise ConflictError(f"review version mismatch: expected {expected_review_version}, current {run.review_version}",
                                details={"current_review_version": run.review_version})
        try:
            model = SdsExtraction.model_validate(corrected)
        except ValidationError as e:
            raise BadRequestError("corrected result does not match the extraction schema",
                                  details={"errors": json.loads(e.json())[:20]}) from e
        result_path = BlobPaths.result(job.job_id, run.run_id)
        if not self.blob.exists(result_path):
            raise BadRequestError("run has no machine result to correct")
        machine = json.loads(self.blob.get(result_path).decode("utf-8"))
        base = machine["extraction"]
        if run.review_version > 0:
            prev = self.repo.list_reviews(run.run_id)
            if prev:
                base = prev[-1].corrected
        corrected_dump = model.model_dump(mode="json")
        review = ReviewRecord(run_id=run.run_id, version=run.review_version + 1, reviewer_id=reviewer_id,
                              corrected=corrected_dump, note=note, changed_paths=changed_paths(base, corrected_dump))
        self.blob.put(BlobPaths.review(job.job_id, run.run_id, review.version), review.model_dump_json(indent=2).encode("utf-8"),
                      "application/json")
        self.repo.put_review(review)
        run.review_version = review.version
        run.updated_at = utcnow()
        self.repo.update_run(run)
        return review

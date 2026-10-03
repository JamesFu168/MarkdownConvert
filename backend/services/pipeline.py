"""Background processing pipeline (runs inside the Storage Queue trigger).

Stages: validate_document -> convert (skipped for original_document) -> extract ->
validate_result -> persist. Every finished stage writes a checkpoint into the run record
so a retried delivery (duplicate message, lease expiry, transient provider error) resumes
instead of redoing paid work where possible.

Lease: the run is claimed with an ETag-conditional update. A second delivery for a run that
is already processing under a live lease is dropped; an expired lease can be taken over.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from adapters.markitdown_adapter import DocumentConverter
from adapters.ocr_interface import OcrProvider
from adapters.provider_base import ExtractionProvider, ModelInput
from domain.errors import LeaseConflictError, NeedsOcrError, PermanentError, SdsError, TransientError
from domain.extraction_schema import SdsExtraction
from domain.models import (
    DocumentFormat,
    InputStrategy,
    JobRecord,
    JobStatus,
    ModelCallRecord,
    QueueMessage,
    RunRecord,
    Stage,
    iso,
    utcnow,
)
from domain.validation import Finding, review_required, validate_extraction

from .config import Settings
from .cost import PricingTable, aggregate_costs
from .document_check import CONTENT_TYPES, DocumentCheck, check_document, sha256_hex
from .extraction import ExtractionService, load_prompt
from .locator import Block, VerificationSources, build_model_input, segment_markdown, verify_evidence
from .storage import BlobPaths, BlobStore, Repository

log = logging.getLogger("sds.pipeline")


class Processor:
    def __init__(self, repo: Repository, blob: BlobStore, settings: Settings, provider: ExtractionProvider,
                 converter: DocumentConverter, pricing: PricingTable, ocr: OcrProvider, *,
                 instance_id: str = "local", clock: Callable[[], float] = time.monotonic):
        self.repo = repo
        self.blob = blob
        self.settings = settings
        self.provider = provider
        self.converter = converter
        self.pricing = pricing
        self.ocr = ocr
        self.instance_id = instance_id
        self._clock = clock
        self.extraction = ExtractionService(provider, settings, pricing)
        self.instructions = load_prompt(settings.prompt_version)

    # ------------------------------------------------------------------ public
    def process_message(self, msg: QueueMessage) -> RunRecord | None:
        run = self.repo.get_run(msg.job_id, msg.run_id)
        if run is None:
            log.warning("run %s not found; dropping message", msg.run_id)
            return None
        if run.status.terminal:
            log.info("run %s already %s; duplicate message ignored", run.run_id, run.status.value)
            return run
        now = utcnow()
        if run.status == JobStatus.PROCESSING and run.lease_expires_at and run.lease_expires_at > now:
            log.info("run %s is processing under a live lease held by %s; dropping duplicate", run.run_id, run.lease_owner)
            return run
        job = self.repo.get_job(msg.owner_id, msg.job_id)
        if job is None:
            log.warning("job %s not found; dropping message", msg.job_id)
            return None

        # Acquire lease (conditional on etag)
        if run.status == JobStatus.PROCESSING:
            run.warnings.append(f"lease_taken_over_from:{run.lease_owner}")
        run.status = JobStatus.PROCESSING
        run.lease_owner = self.instance_id
        run.lease_expires_at = now + timedelta(seconds=self.settings.processing_lease_seconds)
        run.attempt_no += 1
        run.started_at = run.started_at or now
        run.updated_at = now
        try:
            self.repo.update_run(run)
        except LeaseConflictError:
            log.info("run %s lease lost to another worker", run.run_id)
            return self.repo.get_run(msg.job_id, msg.run_id)
        self._set_job_status(job, JobStatus.PROCESSING)

        invocation_start = self._clock()
        try:
            self._execute(job, run, invocation_start)
        except PermanentError as e:
            self._fail(job, run, e)
        except TransientError as e:
            self._defer(job, run, e)
            raise
        except SdsError as e:
            self._fail(job, run, e)
        except Exception as e:  # noqa: BLE001 - unexpected: record and let the queue retry
            err = TransientError(f"unexpected error: {type(e).__name__}: {e}", code="unexpected_error")
            log.exception("unexpected error in run %s", run.run_id)
            self._defer(job, run, err)
            raise err from e
        return run

    def handle_poison(self, msg: QueueMessage, *, dequeue_count: int | None, raw: str | None) -> RunRecord | None:
        run = self.repo.get_run(msg.job_id, msg.run_id)
        job = self.repo.get_job(msg.owner_id, msg.job_id)
        if run is None or job is None:
            return None
        if run.status.terminal:
            return run
        err = PermanentError("message moved to poison queue after repeated failures", code="poison_queue",
                             details={"dequeue_count": dequeue_count, "last_error": run.error})
        self._fail(job, run, err)
        return run

    # --------------------------------------------------------------- internals
    def _execute(self, job: JobRecord, run: RunRecord, invocation_start: float) -> None:
        doc = self.repo.get_document(run.owner_id, run.document_id)
        if doc is None:
            raise PermanentError("document record missing", code="document_missing")
        data = self.blob.get(doc.blob_path)
        strategy = run.config.input_strategy if run.config else InputStrategy.ORIGINAL_DOCUMENT

        check = self._stage(run, Stage.VALIDATE_DOCUMENT, lambda: self._validate_document(run, doc.document_id, data, doc.format))

        if strategy == InputStrategy.MARKDOWN:
            conv = self._stage(run, Stage.CONVERT, lambda: self._convert(run, doc.document_id, data, doc.format))
        else:
            self._skip(run, Stage.CONVERT, "original_document strategy sends the file itself")
            conv = None

        remaining = max(5.0, self.settings.processing_lease_seconds - (self._clock() - invocation_start) - 30.0)
        extract = self._stage(run, Stage.EXTRACT, lambda: self._extract(job, run, doc.document_id, data, doc.format, doc.file_name,
                                                                         strategy, conv, remaining))

        validation = self._stage(run, Stage.VALIDATE_RESULT, lambda: self._validate_result(job, run, extract, check, conv, doc.format))
        self._stage(run, Stage.PERSIST, lambda: self._persist(job, run, extract, validation))

    # --- stage runner with checkpoints
    def _stage(self, run: RunRecord, stage: Stage, fn: Callable[[], Any]) -> Any:
        rec = run.stage_record(stage)
        if stage.value in run.checkpoints and run.checkpoints[stage.value].get("done"):
            rec.status = "done"
            rec.detail = {**rec.detail, "reused_checkpoint": True}
            run.stage = stage
            return self._load_checkpoint(run, stage)
        run.stage = stage
        rec.status = "running"
        rec.started_at = utcnow()
        self._touch(run)
        t0 = self._clock()
        try:
            out = fn()
        except Exception:
            rec.status = "failed"
            rec.ended_at = utcnow()
            rec.elapsed_ms = int((self._clock() - t0) * 1000)
            raise
        rec.status = "done"
        rec.ended_at = utcnow()
        rec.elapsed_ms = int((self._clock() - t0) * 1000)
        self._touch(run)
        return out

    def _skip(self, run: RunRecord, stage: Stage, reason: str) -> None:
        rec = run.stage_record(stage)
        rec.status = "skipped"
        rec.detail = {"reason": reason}

    def _touch(self, run: RunRecord) -> None:
        run.updated_at = utcnow()
        run.lease_expires_at = run.updated_at + timedelta(seconds=self.settings.processing_lease_seconds)
        self.repo.update_run(run)

    def _checkpoint(self, run: RunRecord, stage: Stage, data: dict[str, Any]) -> None:
        run.checkpoints[stage.value] = {"done": True, **data}

    def _load_checkpoint(self, run: RunRecord, stage: Stage) -> Any:
        cp = run.checkpoints[stage.value]
        if stage == Stage.VALIDATE_DOCUMENT:
            return json.loads(self.blob.get(cp["check_path"]).decode("utf-8"))
        if stage == Stage.CONVERT:
            md = self.blob.get(cp["markdown_path"]).decode("utf-8")
            blocks = [Block(**b) for b in json.loads(self.blob.get(cp["blocks_path"]).decode("utf-8"))["blocks"]]
            return {"markdown": md, "blocks": blocks, "converter_version": cp["converter_version"]}
        if stage == Stage.EXTRACT:
            raw = json.loads(self.blob.get(cp["raw_result_path"]).decode("utf-8"))
            return {"result": SdsExtraction.model_validate(raw["extraction"]), "raw": raw}
        if stage == Stage.VALIDATE_RESULT:
            return json.loads(self.blob.get(cp["validation_path"]).decode("utf-8"))
        return cp

    # --- stage implementations
    def _validate_document(self, run: RunRecord, document_id: str, data: bytes, fmt: DocumentFormat) -> dict[str, Any]:
        check: DocumentCheck = check_document(data, fmt, min_text_chars_per_page=self.settings.min_text_chars_per_page)
        self.blob.put(BlobPaths.document_check(document_id), json.dumps(check.summary(), ensure_ascii=False, indent=2).encode("utf-8"), "application/json")
        self.blob.put(BlobPaths.text_layer(document_id), json.dumps({"pages": check.text_layer}, ensure_ascii=False).encode("utf-8"), "application/json")
        run.stage_record(Stage.VALIDATE_DOCUMENT).detail = {"classification": check.classification, "page_count": check.page_count,
                                                             "total_text_chars": check.total_text_chars}
        run.warnings.extend(f"document:{w}" for w in check.warnings)
        if check.classification == "needs_ocr":
            if self.ocr.is_configured():
                raise PermanentError("OCR provider configured but OCR arms are a phase-6 feature", code="needs_ocr")
            raise NeedsOcrError("文件沒有可抽取的文字層（掃描或影像 PDF），第一階段不執行 OCR", details=check.summary())
        self._checkpoint(run, Stage.VALIDATE_DOCUMENT, {"check_path": BlobPaths.document_check(document_id),
                                                        "text_layer_path": BlobPaths.text_layer(document_id),
                                                        "classification": check.classification})
        return check.summary()

    def _convert(self, run: RunRecord, document_id: str, data: bytes, fmt: DocumentFormat) -> dict[str, Any]:
        version = self.converter.version
        md_path = BlobPaths.markdown(document_id, version)
        blocks_path = BlobPaths.blocks(document_id, version)
        if self.blob.exists(md_path) and self.blob.exists(blocks_path):
            md = self.blob.get(md_path).decode("utf-8")
            blocks = [Block(**b) for b in json.loads(self.blob.get(blocks_path).decode("utf-8"))["blocks"]]
            run.stage_record(Stage.CONVERT).detail = {"reused_document_conversion": True, "markdown_chars": len(md), "blocks": len(blocks)}
        else:
            res = self.converter.convert(data, fmt, "document")
            md = res.markdown
            blocks = segment_markdown(md)
            self.blob.put(md_path, md.encode("utf-8"), "text/markdown; charset=utf-8")
            self.blob.put(blocks_path, json.dumps({"normalization": "offsets-into-original-markdown", "blocks": [b.to_dict() for b in blocks]}).encode("utf-8"), "application/json")
            run.stage_record(Stage.CONVERT).detail = {"converter": res.converter, "converter_version": res.converter_version,
                                                      "markdown_chars": len(md), "blocks": len(blocks), "converter_elapsed_ms": res.elapsed_ms}
            run.warnings.extend(f"convert:{w}" for w in res.warnings)
            if not md.strip():
                raise PermanentError("MarkItDown 轉換結果為空，無法以 Markdown 策略抽取", code="empty_markdown")
        self._checkpoint(run, Stage.CONVERT, {"markdown_path": md_path, "blocks_path": blocks_path, "converter_version": version})
        return {"markdown": md, "blocks": blocks, "converter_version": version}

    def _extract(self, job: JobRecord, run: RunRecord, document_id: str, data: bytes, fmt: DocumentFormat, file_name: str,
                 strategy: InputStrategy, conv: dict[str, Any] | None, budget_seconds: float) -> dict[str, Any]:
        if strategy == InputStrategy.MARKDOWN:
            text = build_model_input(conv["markdown"], conv["blocks"])
            model_input = ModelInput(strategy=strategy, sha256=sha256_hex(text.encode("utf-8")), text=text)
            self.blob.put(BlobPaths.model_input(job.job_id, run.run_id), text.encode("utf-8"), "text/plain; charset=utf-8")
        else:
            model_input = ModelInput(strategy=strategy, sha256=sha256_hex(data), file_bytes=data,
                                     file_name=f"document.{fmt.value}", mime_type=CONTENT_TYPES[fmt])
            self.blob.put(BlobPaths.model_input(job.job_id, run.run_id),
                          json.dumps({"strategy": strategy.value, "file": BlobPaths.original(document_id, fmt.value), "sha256": model_input.sha256,
                                      "note": "original document bytes are sent as input_file; see original artifact"}).encode("utf-8"),
                          "application/json")
        run.metrics["model_input"] = {"strategy": strategy.value, "sha256": model_input.sha256, "bytes": model_input.size_bytes,
                                      "markdown_chars": len(conv["markdown"]) if conv else None,
                                      "estimated_tokens_chars_div_4": (len(model_input.text) // 4) if model_input.text else None}
        self._touch(run)

        def persist_attempt(attempt: int, payload: dict[str, Any]) -> None:
            self.blob.put(BlobPaths.model_response(job.job_id, run.run_id, attempt), json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

        def on_call(rec: ModelCallRecord) -> None:
            run.model_calls = [c for c in run.model_calls if c.attempt != rec.attempt] + [rec]
            self._touch(run)

        pdf_detail = self.settings.openai_file_detail if fmt == DocumentFormat.PDF else None
        outcome = self.extraction.run(model=run.config.model if run.config else "unknown", model_input=model_input, instructions=self.instructions,
                                      pdf_detail=pdf_detail, persist_attempt=persist_attempt, on_call_recorded=on_call,
                                      total_budget_seconds=budget_seconds)
        raw_result = {"extraction": outcome.result.model_dump(mode="json"), "response_id": outcome.response.response_id,
                      "request_id": outcome.response.request_id, "model": outcome.response.model,
                      "provider": self.provider.name, "is_test_double": self.provider.is_test_double}
        raw_path = BlobPaths.run_prefix(job.job_id, run.run_id) + "extraction-raw.json"
        self.blob.put(raw_path, json.dumps(raw_result, ensure_ascii=False).encode("utf-8"), "application/json")
        self._checkpoint(run, Stage.EXTRACT, {"raw_result_path": raw_path, "response_id": outcome.response.response_id})
        return {"result": outcome.result, "raw": raw_result}

    def _validate_result(self, job: JobRecord, run: RunRecord, extract: dict[str, Any], check: dict[str, Any],
                         conv: dict[str, Any] | None, fmt: DocumentFormat) -> dict[str, Any]:
        result: SdsExtraction = extract["result"]
        findings = validate_extraction(result)
        sources = VerificationSources()
        if conv:
            sources.markdown = conv["markdown"]
            sources.blocks = conv["blocks"]
        text_layer_path = run.checkpoints.get(Stage.VALIDATE_DOCUMENT.value, {}).get("text_layer_path")
        if text_layer_path and self.blob.exists(text_layer_path):
            pages = json.loads(self.blob.get(text_layer_path).decode("utf-8")).get("pages") or []
            if fmt == DocumentFormat.PDF:
                sources.pages = pages
            elif pages:
                sources.full_text = pages[0]
        checks, ev_findings = verify_evidence(result, sources)
        findings.extend(ev_findings)
        needs_review = review_required(findings) or check.get("classification") == "partial_text"
        if check.get("classification") == "partial_text":
            findings.append(Finding("partial_text_layer", "warning", "document", "部分頁面沒有文字層，抽取結果可能不完整"))
        validation = {"schema_valid": True, "findings": [f.to_dict() for f in findings], "evidence_checks": checks,
                      "review_required": needs_review, "verification_sources": {"markdown": sources.markdown is not None,
                                                                                "pdf_text_layer": sources.pages is not None,
                                                                                "docx_text": sources.full_text is not None}}
        path = BlobPaths.validation(job.job_id, run.run_id)
        self.blob.put(path, json.dumps(validation, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")
        run.stage_record(Stage.VALIDATE_RESULT).detail = {"findings": len(findings), "review_required": needs_review}
        self._checkpoint(run, Stage.VALIDATE_RESULT, {"validation_path": path, "review_required": needs_review})
        return validation

    def _persist(self, job: JobRecord, run: RunRecord, extract: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
        result_doc = {**extract["raw"], "job_id": job.job_id, "run_id": run.run_id, "arm": job.arm,
                      "config": run.config.model_dump(mode="json") if run.config else None,
                      "validation_summary": {"findings": len(validation["findings"]), "review_required": validation["review_required"]},
                      "persisted_at": iso(utcnow()), "immutable": True}
        path = BlobPaths.result(job.job_id, run.run_id)
        self.blob.put(path, json.dumps(result_doc, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")
        self._checkpoint(run, Stage.PERSIST, {"result_path": path})
        self._finalize_metrics(run)
        run.status = JobStatus.NEEDS_REVIEW if validation["review_required"] else JobStatus.SUCCEEDED
        run.finished_at = utcnow()
        run.lease_owner = None
        run.lease_expires_at = None
        run.stage = Stage.PERSIST
        run.updated_at = run.finished_at
        self.repo.update_run(run)
        self._set_job_status(job, run.status)
        return {"result_path": path}

    def _finalize_metrics(self, run: RunRecord) -> None:
        real = [c for c in run.model_calls if not c.is_test_double]
        run.metrics["tokens"] = {
            "input": sum(c.usage.input_tokens or 0 for c in real if c.usage), "cached_input": sum(c.usage.cached_tokens or 0 for c in real if c.usage),
            "output": sum(c.usage.output_tokens or 0 for c in real if c.usage), "reasoning_included_in_output": sum(c.usage.reasoning_tokens or 0 for c in real if c.usage),
            "calls": len(run.model_calls), "real_calls": len(real), "test_double_calls": len(run.model_calls) - len(real),
            "calls_without_usage": sum(1 for c in real if c.usage is None),
        }
        run.metrics["cost"] = aggregate_costs([c.cost for c in run.model_calls])
        run.metrics["stage_elapsed_ms"] = {s.stage.value: s.elapsed_ms for s in run.stages}
        if run.started_at:
            run.metrics["queue_wait_ms"] = int((run.started_at - run.created_at).total_seconds() * 1000)

    # --- terminal transitions
    def _fail(self, job: JobRecord, run: RunRecord, e: SdsError) -> None:
        run.status = JobStatus.FAILED
        run.error = {**e.to_dict(), "stage": run.stage.value if run.stage else None, "at": iso(utcnow())}
        run.finished_at = utcnow()
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = run.finished_at
        self._finalize_metrics(run)
        self._save_run_best_effort(run)
        self._set_job_status(job, JobStatus.FAILED)

    def _defer(self, job: JobRecord, run: RunRecord, e: TransientError) -> None:
        """Transient failure: keep checkpoints, release lease, let the queue redeliver."""
        run.status = JobStatus.QUEUED
        run.error = {**e.to_dict(), "stage": run.stage.value if run.stage else None, "at": iso(utcnow()), "will_retry": True}
        run.warnings.append(f"transient:{e.code}:attempt{run.attempt_no}")
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = utcnow()
        self._save_run_best_effort(run)
        self._set_job_status(job, JobStatus.QUEUED)

    def _save_run_best_effort(self, run: RunRecord) -> None:
        try:
            self.repo.update_run(run)
        except LeaseConflictError:
            fresh = self.repo.get_run(run.job_id, run.run_id)
            if fresh is not None:
                run.etag = fresh.etag
                self.repo.update_run(run)

    def _set_job_status(self, job: JobRecord, status: JobStatus) -> None:
        job.status = status
        job.updated_at = utcnow()
        self.repo.update_job(job)

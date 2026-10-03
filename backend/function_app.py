"""Azure Functions entry points (Python v2 programming model).

HTTP routes only validate, store and enqueue; all parsing happens in the Storage Queue
trigger. No background threads or asyncio tasks are started from HTTP handlers.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import azure.functions as func

from domain.errors import BadRequestError, NotFoundError, SdsError
from domain.extraction_schema import SdsExtraction
from domain.models import JobStatus, ModelRole, QueueMessage
from services.auth import resolve_principal
from services.container import get_container
from services.experiments import export_rows, rows_to_csv, summarize_experiment
from services.gold import load_gold_by_sha
from services.scoring import score_extraction
from services.storage import ARTIFACT_KINDS, BlobPaths

log = logging.getLogger("sds.http")

app = func.FunctionApp()

JSON = "application/json; charset=utf-8"


# ----------------------------------------------------------------------------- helpers

def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, ensure_ascii=False, default=str), status_code=status, mimetype=JSON, headers=headers)


def _error(e: SdsError, request_id: str) -> func.HttpResponse:
    body = {"code": e.code, "message": e.message, "request_id": request_id, "retryable": e.retryable}
    if e.details:
        body["details"] = e.details
    return _json(body, e.http_status)


def _handle(req: func.HttpRequest, fn: Callable[[str, str], func.HttpResponse], *, auth: bool = True) -> func.HttpResponse:
    request_id = req.headers.get("x-request-id") or uuid.uuid4().hex
    try:
        owner = ""
        if auth:
            owner = resolve_principal(req.headers, get_container().settings).owner_id
        resp = fn(owner, request_id)
        resp.headers["x-request-id"] = request_id
        return resp
    except SdsError as e:
        log.info("request %s failed: %s %s", request_id, e.code, type(e).__name__)
        return _error(e, request_id)
    except Exception as e:  # noqa: BLE001
        log.exception("request %s unexpected failure", request_id)
        return _json({"code": "internal_error", "message": f"{type(e).__name__}", "request_id": request_id}, 500)


def _owned_job(owner: str, job_id: str):
    c = get_container()
    job = c.repo.get_job(owner, job_id)
    if job is None:
        raise NotFoundError("任務不存在或無權限")
    return c, job


def _owned_run(owner: str, job_id: str, run_id: str):
    c, job = _owned_job(owner, job_id)
    run = c.repo.get_run(job_id, run_id)
    if run is None or run.owner_id != owner:
        raise NotFoundError("run 不存在或無權限")
    return c, job, run


def _read_json(req: func.HttpRequest) -> dict[str, Any]:
    try:
        body = req.get_json()
    except ValueError as e:
        raise BadRequestError("request body 必須是 JSON") from e
    if not isinstance(body, dict):
        raise BadRequestError("request body 必須是 JSON object")
    return body


# ----------------------------------------------------------------------------- public info

@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    def run(_: str, rid: str) -> func.HttpResponse:
        c = get_container()
        return _json({"status": "ok", "provider": c.provider.name, "is_test_double": c.provider.is_test_double,
                      "converter": {"name": c.converter.name, "version": c.converter.version}, "auth_mode": c.settings.auth_mode,
                      "schema_version": "pilot-v1", "prompt_version": c.settings.prompt_version, "request_id": rid})
    return _handle(req, run, auth=False)


@app.route(route="config", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def config(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c = get_container()
        return _json({
            "allowed_arms": [a.model_dump(mode="json") for a in c.arms.values()],
            "max_upload_bytes": c.settings.max_upload_bytes,
            "accepted_formats": ["pdf", "docx"],
            "provider": c.provider.name, "is_test_double": c.provider.is_test_double,
            "models": {"baseline": c.settings.model_for(ModelRole.BASELINE) if c.settings.extraction_provider == "mock" or c.settings.openai_model_baseline else None,
                       "candidate": c.settings.model_for(ModelRole.CANDIDATE) if c.settings.extraction_provider == "mock" or c.settings.openai_model_candidate else None},
            "auth_mode": c.settings.auth_mode, "owner_id": owner, "poll_interval_ms": 3000,
        })
    return _handle(req, run)


# ----------------------------------------------------------------------------- jobs

@app.route(route="jobs", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def create_job(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c = get_container()
        file = req.files.get("file") if req.files else None
        if file is None:
            raise BadRequestError("multipart 欄位 'file' 為必填")
        data = file.stream.read()
        arms_raw = (req.form.get("arms") if req.form else None) or "A"
        arms = [a.strip().upper() for a in arms_raw.split(",") if a.strip()]
        experiment_id = (req.form.get("experiment_id") if req.form else None) or None
        status, body = c.jobs.create_jobs(owner_id=owner, file_name=file.filename or "upload", content_type=file.content_type,
                                          data=data, arms=arms, experiment_id=experiment_id,
                                          idempotency_key=req.headers.get("idempotency-key"))
        headers = {"Location": body["status_url"]}
        if body.get("idempotent_replay"):
            headers["Idempotent-Replayed"] = "true"
        return _json(body, status, headers)
    return _handle(req, run)


@app.route(route="jobs", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def list_jobs(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c = get_container()
        status_raw = req.params.get("status")
        status = None
        if status_raw:
            try:
                status = JobStatus(status_raw)
            except ValueError as e:
                raise BadRequestError(f"未知 status: {status_raw}") from e
        limit = max(1, min(100, int(req.params.get("limit", "20"))))
        jobs, nxt = c.repo.list_jobs(owner, status=status, cursor=req.params.get("cursor"), limit=limit)
        items = [c.jobs.job_view(j, c.repo.get_run(j.job_id, j.latest_run_id)) for j in jobs]
        return _json({"items": items, "next_cursor": nxt})
    return _handle(req, run)


@app.route(route="jobs/{job_id}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def get_job(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c, job = _owned_job(owner, req.route_params["job_id"])
        latest = c.repo.get_run(job.job_id, job.latest_run_id)
        view = c.jobs.job_view(job, latest)
        view["runs"] = [c.jobs.run_view(r) for r in c.repo.list_runs(job.job_id)]
        return _json(view)
    return _handle(req, run)


@app.route(route="jobs/{job_id}/runs/{run_id}/result", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def get_result(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c, job, r = _owned_run(owner, req.route_params["job_id"], req.route_params["run_id"])
        if r.status in (JobStatus.ENQUEUE_PENDING, JobStatus.QUEUED, JobStatus.PROCESSING):
            return _json({"ready": False, "status": r.status.value, "stage": r.stage.value if r.stage else None, "run": c.jobs.run_view(r)}, 202)
        if r.status == JobStatus.FAILED:
            return _json({"ready": False, "status": "failed", "error": r.error, "run": c.jobs.run_view(r)}, 200)
        result = json.loads(c.blob.get(BlobPaths.result(job.job_id, r.run_id)).decode("utf-8"))
        validation = json.loads(c.blob.get(BlobPaths.validation(job.job_id, r.run_id)).decode("utf-8"))
        reviews = [rv.model_dump(mode="json") for rv in c.repo.list_reviews(r.run_id)]
        return _json({"ready": True, "status": r.status.value, "job_id": job.job_id, "run_id": r.run_id, "arm": job.arm,
                      "is_test_double": result.get("is_test_double", False), "result": result, "validation": validation,
                      "reviews": reviews, "run": c.jobs.run_view(r)})
    return _handle(req, run)


@app.route(route="jobs/{job_id}/runs/{run_id}/artifacts/{kind}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def get_artifact(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        kind = req.route_params["kind"]
        if kind not in ARTIFACT_KINDS:
            raise NotFoundError(f"未知 artifact kind；允許: {', '.join(ARTIFACT_KINDS)}")
        c, job, r = _owned_run(owner, req.route_params["job_id"], req.route_params["run_id"])
        doc = c.repo.get_document(owner, job.document_id)
        if doc is None:
            raise NotFoundError("文件不存在")
        content_type = ARTIFACT_KINDS[kind]
        if kind == "original":
            path, content_type = doc.blob_path, doc.content_type
        elif kind == "markdown":
            cp = r.checkpoints.get("convert")
            if not cp:
                raise NotFoundError("此 run 沒有 Markdown（原始文件策略或尚未轉檔）")
            path = cp["markdown_path"]
        elif kind == "model-input":
            path = BlobPaths.model_input(job.job_id, r.run_id)
            if r.config and r.config.input_strategy.value == "original_document":
                content_type = "application/json"
        elif kind == "result":
            path = BlobPaths.result(job.job_id, r.run_id)
        else:
            path = BlobPaths.validation(job.job_id, r.run_id)
        if not c.blob.exists(path):
            raise NotFoundError("artifact 尚未產生")
        data = c.blob.get(path)
        disposition = "inline" if kind in ("original",) else "attachment"
        ext = {"original": doc.file_name.rsplit(".", 1)[-1] if "." in doc.file_name else doc.format.value, "markdown": "md",
               "model-input": "txt", "result": "json", "validation": "json"}[kind]
        return func.HttpResponse(data, status_code=200, mimetype=content_type,
                                 headers={"Content-Disposition": f'{disposition}; filename="{job.job_id}-{kind}.{ext}"',
                                          "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
    return _handle(req, run)


@app.route(route="jobs/{job_id}/runs/{run_id}/review", methods=["PATCH"], auth_level=func.AuthLevel.ANONYMOUS)
def patch_review(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c, job, r = _owned_run(owner, req.route_params["job_id"], req.route_params["run_id"])
        body = _read_json(req)
        if "expected_review_version" not in body or "corrected" not in body:
            raise BadRequestError("需要 expected_review_version 與 corrected")
        review = c.reviews.save(job=job, run=r, reviewer_id=owner, corrected=body["corrected"],
                                expected_review_version=int(body["expected_review_version"]), note=body.get("note"))
        return _json({"run_id": r.run_id, "review_version": review.version, "changed_paths": review.changed_paths,
                      "created_at": review.created_at.isoformat()})
    return _handle(req, run)


@app.route(route="jobs/{job_id}/retry", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def retry_job(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c = get_container()
        reason = None
        try:
            reason = (req.get_json() or {}).get("reason")
        except ValueError:
            reason = None
        return _json(c.jobs.retry(owner_id=owner, job_id=req.route_params["job_id"], reason=reason), 202)
    return _handle(req, run)


# ----------------------------------------------------------------------------- experiments

def _experiment_pairs(c, owner: str, experiment_id: str):
    jobs = c.repo.list_jobs_by_experiment(owner, experiment_id)
    pairs = []
    for j in jobs:
        r = c.repo.get_run(j.job_id, j.latest_run_id)
        if r is not None:
            pairs.append((j, r))
    docs = {j.document_id: c.repo.get_document(owner, j.document_id) for j in jobs}
    gold = load_gold_by_sha(c.settings.gold_dir)
    scores: dict[str, Any] = {}
    for j, r in pairs:
        d = docs.get(j.document_id)
        if d is None or d.sha256 not in gold or r.status not in (JobStatus.SUCCEEDED, JobStatus.NEEDS_REVIEW):
            continue
        if r.config and r.config.is_test_double:
            continue
        path = BlobPaths.result(j.job_id, r.run_id)
        if not c.blob.exists(path):
            continue
        result = SdsExtraction.model_validate(json.loads(c.blob.get(path).decode("utf-8"))["extraction"])
        scores[r.run_id] = score_extraction(result, gold[d.sha256])
    return pairs, docs, scores


@app.route(route="experiments/{experiment_id}/summary", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def experiment_summary(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c = get_container()
        eid = req.route_params["experiment_id"]
        pairs, docs, scores = _experiment_pairs(c, owner, eid)
        summary = summarize_experiment(eid, pairs, scores, c.arms)
        summary["documents_detail"] = [{"document_id": d.document_id, "file_name": d.file_name, "format": d.format.value, "sha256": d.sha256,
                                        "has_gold": d.sha256 in load_gold_by_sha(c.settings.gold_dir)} for d in docs.values() if d]
        summary["jobs"] = [{"job_id": j.job_id, "run_id": r.run_id, "arm": j.arm, "document_id": j.document_id, "status": r.status.value,
                            "scored": r.run_id in scores, "is_test_double": bool(r.config and r.config.is_test_double),
                            "score": scores.get(r.run_id, {}).get("metrics")} for j, r in pairs]
        return _json(summary)
    return _handle(req, run)


@app.route(route="experiments/{experiment_id}/export", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def experiment_export(req: func.HttpRequest) -> func.HttpResponse:
    def run(owner: str, rid: str) -> func.HttpResponse:
        c = get_container()
        eid = req.route_params["experiment_id"]
        pairs, docs, scores = _experiment_pairs(c, owner, eid)
        rows = export_rows(eid, pairs, scores, {k: v.sha256 for k, v in docs.items() if v}, {k: v.format.value for k, v in docs.items() if v})
        if (req.params.get("format") or "csv").lower() == "json":
            return _json({"experiment_id": eid, "rows": rows, "scores": scores})
        csv_text = rows_to_csv(rows)
        return func.HttpResponse(("﻿" + csv_text).encode("utf-8"), status_code=200, mimetype="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="experiment-{eid}.csv"'})
    return _handle(req, run)


# ----------------------------------------------------------------------------- internal ops

@app.route(route="internal/reconcile", methods=["POST"], auth_level=func.AuthLevel.ADMIN)
def reconcile(req: func.HttpRequest) -> func.HttpResponse:
    def run(_: str, rid: str) -> func.HttpResponse:
        c = get_container()
        older = int(req.params.get("pending_older_than_seconds", "60"))
        return _json(c.jobs.reconcile(pending_older_than_seconds=older))
    return _handle(req, run, auth=False)


# ----------------------------------------------------------------------------- queue triggers

@app.queue_trigger(arg_name="msg", queue_name="%SDS_QUEUE_NAME%", connection="AzureWebJobsStorage")
def process_job(msg: func.QueueMessage) -> None:
    body = msg.get_body().decode("utf-8")
    message = QueueMessage.model_validate_json(body)
    log.info("processing run %s (dequeue_count=%s)", message.run_id, msg.dequeue_count)
    get_container().processor.process_message(message)


@app.queue_trigger(arg_name="msg", queue_name="%SDS_QUEUE_NAME%-poison", connection="AzureWebJobsStorage")
def process_poison(msg: func.QueueMessage) -> None:
    body = msg.get_body().decode("utf-8")
    try:
        message = QueueMessage.model_validate_json(body)
    except Exception:  # noqa: BLE001
        log.error("poison message is not a valid QueueMessage; dropping")
        return
    log.warning("poison message for run %s", message.run_id)
    get_container().processor.handle_poison(message, dequeue_count=msg.dequeue_count, raw=None)

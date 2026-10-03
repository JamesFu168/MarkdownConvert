"""Azure Blob / Table / Queue implementations (work against Azurite locally).

Table layout
  sdsdocuments   PK=owner_id      RK=document_id
  sdsjobs        PK=owner_id      RK=<inverted-ms>_<job_id>   (newest first; job_id also stored as property)
  sdsruns        PK=job_id        RK=run_id                   (ETag used for lease / conditional updates)
  sdsidempotency PK=owner_id      RK=idempotency key (hashed)
  sdsreviews     PK=run_id        RK=version (zero padded)

Records are stored as JSON in chunked string properties ``d0..dN`` (Table string
properties are limited to 64 KiB each).
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.storage.blob import ContentSettings

from domain.errors import LeaseConflictError, NotFoundError
from domain.models import DocumentRecord, JobRecord, JobStatus, ReviewRecord, RunRecord

from .storage_clients import blob_service, queue_client, table_service

CHUNK = 60_000
TABLE_SUFFIXES = {"documents": "documents", "jobs": "jobs", "runs": "runs", "idem": "idempotency", "reviews": "reviews"}


def _pack(pk: str, rk: str, model: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = model.model_dump_json(exclude={"etag"}) if hasattr(model, "model_dump_json") else json.dumps(model)
    entity: dict[str, Any] = {"PartitionKey": pk, "RowKey": rk, "chunks": 0}
    for i in range(0, max(1, len(payload)), CHUNK):
        entity[f"d{i // CHUNK}"] = payload[i:i + CHUNK]
        entity["chunks"] = i // CHUNK + 1
    if extra:
        entity.update(extra)
    return entity


def _unpack(entity: dict[str, Any]) -> str:
    n = int(entity.get("chunks", 1))
    return "".join(entity.get(f"d{i}", "") for i in range(n))


def _etag(entity: Any) -> str | None:
    meta = getattr(entity, "metadata", None) or {}
    return meta.get("etag") or entity.get("etag") if isinstance(entity, dict) else meta.get("etag")


def _inverted_ms(dt: datetime) -> str:
    ms = int(dt.astimezone(UTC).timestamp() * 1000)
    return f"{10**13 - ms:013d}"


def _safe_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class AzureBlobStore:
    def __init__(self, connection_string: str, container: str):
        self._svc = blob_service(connection_string)
        self._container = self._svc.get_container_client(container)
        try:
            self._container.create_container()
        except ResourceExistsError:
            pass

    def put(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._container.upload_blob(path, data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

    def get(self, path: str) -> bytes:
        try:
            return self._container.download_blob(path).readall()
        except ResourceNotFoundError as e:
            raise NotFoundError(f"blob not found: {path}") from e

    def exists(self, path: str) -> bool:
        return self._container.get_blob_client(path).exists()


class AzureQueue:
    """Sends base64-encoded messages, matching the Functions queue trigger's default decoding."""

    def __init__(self, connection_string: str, queue_name: str):
        self._client = queue_client(connection_string, queue_name)
        try:
            self._client.create_queue()
        except ResourceExistsError:
            pass

    def send(self, body: str) -> None:
        self._client.send_message(body.encode("utf-8"))


class AzureTableRepository:
    def __init__(self, connection_string: str | None, *, table_prefix: str = "sds"):
        self._svc = table_service(connection_string)
        self._t = {}
        for key, suffix in TABLE_SUFFIXES.items():
            self._t[key] = self._svc.create_table_if_not_exists(f"{table_prefix}{suffix}")

    def delete_all_tables(self) -> None:
        """Test helper: drop every table created with this prefix."""
        for client in self._t.values():
            client.delete_table()

    # ---- helpers
    def _get(self, table: str, pk: str, rk: str) -> tuple[dict | None, str | None]:
        try:
            e = self._t[table].get_entity(pk, rk)
        except ResourceNotFoundError:
            return None, None
        return e, e.metadata.get("etag")

    def _query(self, table: str, flt: str, params: dict[str, Any], *, top: int | None = None, cursor: str | None = None):
        pages = self._t[table].query_entities(flt, parameters=params, results_per_page=top).by_page(continuation_token=cursor)
        page = next(pages)
        items = list(page)
        return items, pages.continuation_token

    # ---- documents
    def put_document(self, doc: DocumentRecord) -> None:
        self._t["documents"].upsert_entity(_pack(doc.owner_id, doc.document_id, doc), mode=UpdateMode.REPLACE)

    def get_document(self, owner_id: str, document_id: str) -> DocumentRecord | None:
        e, _ = self._get("documents", owner_id, document_id)
        return DocumentRecord.model_validate_json(_unpack(e)) if e else None

    # ---- jobs
    def _job_rk(self, job: JobRecord) -> str:
        return f"{_inverted_ms(job.created_at)}_{job.job_id}"

    def put_job(self, job: JobRecord) -> None:
        entity = _pack(job.owner_id, self._job_rk(job), job, {"job_id": job.job_id, "status": job.status.value,
                                                             "experiment_id": job.experiment_id or ""})
        self._t["jobs"].upsert_entity(entity, mode=UpdateMode.REPLACE)

    def update_job(self, job: JobRecord) -> None:
        self.put_job(job)

    def get_job(self, owner_id: str, job_id: str) -> JobRecord | None:
        items, _ = self._query("jobs", "PartitionKey eq @pk and job_id eq @jid", {"pk": owner_id, "jid": job_id}, top=1)
        return JobRecord.model_validate_json(_unpack(items[0])) if items else None

    def list_jobs(self, owner_id: str, *, status: JobStatus | None, cursor: str | None, limit: int) -> tuple[list[JobRecord], str | None]:
        flt = "PartitionKey eq @pk"
        params: dict[str, Any] = {"pk": owner_id}
        if status:
            flt += " and status eq @st"
            params["st"] = status.value
        token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode()) if cursor else None
        items, nxt = self._query("jobs", flt, params, top=limit, cursor=token)
        next_cursor = base64.urlsafe_b64encode(json.dumps(nxt).encode()).decode() if nxt else None
        return [JobRecord.model_validate_json(_unpack(e)) for e in items], next_cursor

    def list_jobs_by_experiment(self, owner_id: str, experiment_id: str) -> list[JobRecord]:
        ents = self._t["jobs"].query_entities("PartitionKey eq @pk and experiment_id eq @ex", parameters={"pk": owner_id, "ex": experiment_id})
        return [JobRecord.model_validate_json(_unpack(e)) for e in ents]

    # ---- runs
    def put_run(self, run: RunRecord) -> None:
        entity = _pack(run.job_id, run.run_id, run, {"status": run.status.value, "updated_at": run.updated_at.isoformat()})
        meta = self._t["runs"].upsert_entity(entity, mode=UpdateMode.REPLACE)
        run.etag = (meta or {}).get("etag")
        if not run.etag:
            _, run.etag = self._get("runs", run.job_id, run.run_id)

    def get_run(self, job_id: str, run_id: str) -> RunRecord | None:
        e, etag = self._get("runs", job_id, run_id)
        if not e:
            return None
        run = RunRecord.model_validate_json(_unpack(e))
        run.etag = etag
        return run

    def update_run(self, run: RunRecord) -> None:
        entity = _pack(run.job_id, run.run_id, run, {"status": run.status.value, "updated_at": run.updated_at.isoformat()})
        try:
            meta = self._t["runs"].update_entity(entity, mode=UpdateMode.REPLACE, etag=run.etag or "*",
                                                 match_condition=MatchConditions.IfNotModified)
        except (ResourceModifiedError, HttpResponseError) as e:
            status = getattr(e, "status_code", None)
            if isinstance(e, ResourceModifiedError) or status == 412:
                raise LeaseConflictError("run was modified concurrently (etag mismatch)") from e
            raise
        run.etag = (meta or {}).get("etag")

    def list_runs(self, job_id: str) -> list[RunRecord]:
        ents = self._t["runs"].query_entities("PartitionKey eq @pk", parameters={"pk": job_id})
        runs = []
        for e in ents:
            r = RunRecord.model_validate_json(_unpack(e))
            r.etag = e.metadata.get("etag")
            runs.append(r)
        runs.sort(key=lambda r: r.created_at)
        return runs

    def scan_runs(self, *, statuses: list[JobStatus], updated_before: datetime | None) -> list[RunRecord]:
        # Note: the Tables SDK substitutes parameters by splitting on spaces, so parentheses need surrounding spaces.
        clauses = " or ".join(f"status eq @s{i}" for i in range(len(statuses)))
        params = {f"s{i}": s.value for i, s in enumerate(statuses)}
        flt = f"( {clauses} )"
        if updated_before is not None:
            flt += " and updated_at le @ub"
            params["ub"] = updated_before.isoformat()
        out = []
        for e in self._t["runs"].query_entities(flt, parameters=params):
            r = RunRecord.model_validate_json(_unpack(e))
            r.etag = e.metadata.get("etag")
            out.append(r)
        return out

    # ---- idempotency
    def get_idempotency(self, owner_id: str, key: str) -> dict | None:
        e, _ = self._get("idem", owner_id, _safe_key(key))
        return json.loads(_unpack(e)) if e else None

    def put_idempotency(self, owner_id: str, key: str, record: dict) -> None:
        self._t["idem"].upsert_entity(_pack(owner_id, _safe_key(key), record), mode=UpdateMode.REPLACE)

    # ---- reviews
    def put_review(self, review: ReviewRecord) -> None:
        self._t["reviews"].upsert_entity(_pack(review.run_id, f"{review.version:06d}", review), mode=UpdateMode.REPLACE)

    def list_reviews(self, run_id: str) -> list[ReviewRecord]:
        ents: Iterable = self._t["reviews"].query_entities("PartitionKey eq @pk", parameters={"pk": run_id})
        return sorted((ReviewRecord.model_validate_json(_unpack(e)) for e in ents), key=lambda r: r.version)

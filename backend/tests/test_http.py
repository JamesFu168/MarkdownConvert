"""HTTP routes exercised through azure.functions request objects against an in-memory container."""
import json
import uuid

import azure.functions as func
import pytest

import function_app
from domain.models import QueueMessage
from services import container as container_mod
from tests.conftest import FAKE_PDF

LOCAL = {"host": "localhost:7071"}


@pytest.fixture
def app(container, monkeypatch):
    monkeypatch.setattr(container_mod, "_container", container)
    return container


def _multipart(fields: dict[str, str], file_name: str, data: bytes, content_type="application/pdf"):
    boundary = "----sds" + uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_name}\"\r\n"
             f"Content-Type: {content_type}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _req(method, url, *, body=b"", headers=None, params=None, route_params=None):
    h = dict(LOCAL)
    h.update(headers or {})
    return func.HttpRequest(method=method, url=f"http://localhost:7071/api/{url}", headers=h, params=params or {},
                            route_params=route_params or {}, body=body)


def _post_job(arms="B", experiment_id=None, headers=None):
    fields = {"arms": arms}
    if experiment_id:
        fields["experiment_id"] = experiment_id
    body, ct = _multipart(fields, "my.pdf", FAKE_PDF)
    h = {"content-type": ct}
    h.update(headers or {})
    return function_app.create_job(_req("POST", "jobs", body=body, headers=h))


def test_health_and_config_anonymous(app):
    r = function_app.health(_req("GET", "health"))
    assert r.status_code == 200 and json.loads(r.get_body())["is_test_double"] is True
    r = function_app.config(_req("GET", "config"))
    cfg = json.loads(r.get_body())
    assert [a["arm"] for a in cfg["allowed_arms"]] == ["A", "B", "C", "D"] and cfg["owner_id"] == "local-dev-user"


def test_upload_poll_result_flow(app):
    r = _post_job(arms="A,B", experiment_id="e1")
    assert r.status_code == 202, r.get_body()
    body = json.loads(r.get_body())
    job_id, run_id = body["job_id"], body["run_id"]
    assert r.headers["Location"] == f"/api/jobs/{job_id}"

    r = function_app.get_job(_req("GET", f"jobs/{job_id}", route_params={"job_id": job_id}))
    assert json.loads(r.get_body())["status"] == "queued"
    r = function_app.get_result(_req("GET", "x", route_params={"job_id": job_id, "run_id": run_id}))
    assert r.status_code == 202 and json.loads(r.get_body())["ready"] is False

    # process as the queue trigger would
    for m in list(app.queue.messages):
        app.processor.process_message(QueueMessage.model_validate_json(m))

    r = function_app.get_result(_req("GET", "x", route_params={"job_id": job_id, "run_id": run_id}))
    res = json.loads(r.get_body())
    assert r.status_code == 200 and res["ready"] is True and res["is_test_double"] is True
    assert res["validation"]["schema_valid"] is True

    r = function_app.list_jobs(_req("GET", "jobs", params={"limit": "1"}))
    lst = json.loads(r.get_body())
    assert len(lst["items"]) == 1 and lst["next_cursor"]
    r = function_app.list_jobs(_req("GET", "jobs", params={"limit": "1", "cursor": lst["next_cursor"]}))
    assert len(json.loads(r.get_body())["items"]) == 1

    for kind in ("original", "result", "validation", "model-input"):
        r = function_app.get_artifact(_req("GET", "x", route_params={"job_id": job_id, "run_id": run_id, "kind": kind}))
        assert r.status_code == 200, kind
    r = function_app.get_artifact(_req("GET", "x", route_params={"job_id": job_id, "run_id": run_id, "kind": "../../etc"}))
    assert r.status_code == 404

    # review
    corrected = res["result"]["extraction"]
    corrected["document"]["supplier_name"] = {"value": "ACME", "raw_text": "ACME", "status": "found", "evidence": []}
    r = function_app.patch_review(_req("PATCH", "x", route_params={"job_id": job_id, "run_id": run_id},
                                       body=json.dumps({"expected_review_version": 0, "corrected": corrected}).encode()))
    assert r.status_code == 200 and json.loads(r.get_body())["review_version"] == 1
    r = function_app.patch_review(_req("PATCH", "x", route_params={"job_id": job_id, "run_id": run_id},
                                       body=json.dumps({"expected_review_version": 0, "corrected": corrected}).encode()))
    assert r.status_code == 409

    # experiment summary & export
    r = function_app.experiment_summary(_req("GET", "x", route_params={"experiment_id": "e1"}))
    s = json.loads(r.get_body())
    assert s["arms"]["A"]["technically_completed"] == 1 and s["arms"]["A"]["scoring"]["scored_documents"] == 0
    r = function_app.experiment_export(_req("GET", "x", route_params={"experiment_id": "e1"}))
    assert r.status_code == 200 and r.get_body().startswith("﻿".encode())


def test_owner_isolation_and_auth(app, monkeypatch):
    r = _post_job()
    job_id = json.loads(r.get_body())["job_id"]
    # non-loopback host is refused in local_dev mode
    r = function_app.get_job(_req("GET", "x", headers={"host": "evil.example.com"}, route_params={"job_id": job_id}))
    assert r.status_code == 403
    # a different owner (easyauth) cannot see the job
    object.__setattr__(app.settings, "auth_mode", "easyauth")
    r = function_app.get_job(_req("GET", "x", headers={"x-ms-client-principal-id": "someone-else"}, route_params={"job_id": job_id}))
    assert r.status_code == 404
    r = function_app.get_job(_req("GET", "x", route_params={"job_id": job_id}))
    assert r.status_code == 401
    object.__setattr__(app.settings, "auth_mode", "local_dev")


def test_errors_have_code_message_request_id(app):
    r = function_app.create_job(_req("POST", "jobs", body=b"", headers={"content-type": "application/json"}))
    assert r.status_code == 400
    e = json.loads(r.get_body())
    assert set(e) >= {"code", "message", "request_id"} and e["code"] == "bad_request"
    r = function_app.get_job(_req("GET", "x", route_params={"job_id": "nope"}))
    assert r.status_code == 404


def test_idempotency_header_replay(app):
    r1 = _post_job(headers={"idempotency-key": "abc"})
    r2 = _post_job(headers={"idempotency-key": "abc"})
    assert json.loads(r1.get_body())["job_id"] == json.loads(r2.get_body())["job_id"]
    assert r2.headers.get("Idempotent-Replayed") == "true"
    r3 = _post_job(arms="A", headers={"idempotency-key": "abc"})
    assert r3.status_code == 409

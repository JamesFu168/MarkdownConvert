"""Architecture test: a parse that outlives the 230 s HTTP limit is still queryable and completes.

The provider blocks on an Event while a fake clock advances 240 s. This proves decoupling of
HTTP from processing; it says nothing about real model latency.
"""
import threading

import pytest

from adapters.mock_provider import ScriptedProvider, success_response
from domain.models import JobStatus, QueueMessage
from tests.conftest import upload

pytestmark = pytest.mark.slow


def test_job_outliving_http_limit_is_queryable_and_completes(make_container):
    gate = threading.Event()
    started = threading.Event()
    clock = {"t": 0.0}

    def slow_call():
        started.set()
        gate.wait(timeout=10)
        clock["t"] += 240.0  # simulate 4 minutes inside the model call
        return success_response()

    provider = ScriptedProvider([slow_call])
    c = make_container(provider=provider)
    c.processor._clock = lambda: clock["t"]
    status, body = upload(c, arms=("B",))
    assert status == 202  # HTTP returned before any processing
    msg = QueueMessage.model_validate_json(c.queue.pop())

    worker = threading.Thread(target=c.processor.process_message, args=(msg,))
    worker.start()
    assert started.wait(timeout=5)
    # while "230+ seconds" pass, the status API still answers
    run = c.repo.get_run(body["job_id"], body["run_id"])
    assert run.status == JobStatus.PROCESSING and run.stage.value == "extract" and run.lease_expires_at is not None
    gate.set()
    worker.join(timeout=10)
    run = c.repo.get_run(body["job_id"], body["run_id"])
    assert run.status == JobStatus.SUCCEEDED
    assert run.stage_record(__import__("domain.models", fromlist=["Stage"]).Stage.EXTRACT).elapsed_ms >= 240_000

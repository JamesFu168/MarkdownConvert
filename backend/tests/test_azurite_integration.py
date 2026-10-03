"""Runs only when SDS_TEST_AZURITE=1 and Azurite is listening on the default ports."""
import os
import uuid

import pytest

pytestmark = pytest.mark.azurite

if not os.environ.get("SDS_TEST_AZURITE"):
    pytest.skip("set SDS_TEST_AZURITE=1 with Azurite running", allow_module_level=True)

from adapters.storage_azure import AzureBlobStore, AzureQueue, AzureTableRepository  # noqa: E402
from domain.errors import LeaseConflictError  # noqa: E402
from domain.models import DocumentFormat, DocumentRecord, JobRecord, JobStatus, RunRecord  # noqa: E402

CONN = "UseDevelopmentStorage=true"


@pytest.fixture
def repo():
    r = AzureTableRepository(CONN, table_prefix="sdstest" + uuid.uuid4().hex[:6])
    yield r
    r.delete_all_tables()  # keep the shared Azurite instance clean for the local Functions host


def test_repository_roundtrip_and_etag_conflict(repo):
    owner = "owner-" + uuid.uuid4().hex[:8]
    doc = DocumentRecord(document_id=uuid.uuid4().hex, owner_id=owner, file_name="f.pdf", format=DocumentFormat.PDF, content_type="application/pdf",
                         size_bytes=3, sha256="abc", blob_path="documents/x/original.pdf")
    repo.put_document(doc)
    assert repo.get_document(owner, doc.document_id).sha256 == "abc"

    job_ids = []
    for _ in range(3):
        run = RunRecord(run_id=uuid.uuid4().hex, job_id=uuid.uuid4().hex, owner_id=owner, document_id=doc.document_id)
        job = JobRecord(job_id=run.job_id, owner_id=owner, document_id=doc.document_id, file_name="f.pdf", arm="A", latest_run_id=run.run_id,
                        experiment_id="exp")
        repo.put_run(run)
        repo.put_job(job)
        job_ids.append(job.job_id)
    page, cursor = repo.list_jobs(owner, status=None, cursor=None, limit=2)
    assert len(page) == 2 and cursor
    page2, cursor2 = repo.list_jobs(owner, status=None, cursor=cursor, limit=2)
    assert len(page2) == 1 and cursor2 is None
    assert page[0].created_at >= page[1].created_at  # newest first
    assert len(repo.list_jobs_by_experiment(owner, "exp")) == 3
    assert repo.get_job(owner, job_ids[0]).job_id == job_ids[0]

    run = repo.get_run(page[0].job_id, page[0].latest_run_id)
    assert run.etag
    stale = repo.get_run(run.job_id, run.run_id)
    run.status = JobStatus.PROCESSING
    repo.update_run(run)
    stale.status = JobStatus.FAILED
    with pytest.raises(LeaseConflictError):
        repo.update_run(stale)
    assert repo.get_run(run.job_id, run.run_id).status == JobStatus.PROCESSING
    assert any(r.run_id == run.run_id for r in repo.scan_runs(statuses=[JobStatus.PROCESSING], updated_before=None))

    repo.put_idempotency(owner, "key", {"payload_hash": "h", "jobs": []})
    assert repo.get_idempotency(owner, "key")["payload_hash"] == "h"


def test_blob_and_queue():
    blob = AzureBlobStore(CONN, "sdstest")
    path = f"test/{uuid.uuid4().hex}.bin"
    assert not blob.exists(path)
    blob.put(path, b"hello", "application/octet-stream")
    assert blob.exists(path) and blob.get(path) == b"hello"
    q = AzureQueue(CONN, "sds-test-" + uuid.uuid4().hex[:6])
    q.send('{"a": 1}')

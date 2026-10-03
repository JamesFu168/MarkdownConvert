import json

import pytest

from domain.errors import BadRequestError, ConflictError
from services.storage import BlobPaths
from tests.conftest import drain, upload


def test_review_versions_do_not_overwrite_machine_result(container):
    _, body = upload(container, arms=("B",))
    drain(container)
    job = container.repo.get_job("owner-1", body["job_id"])
    run = container.repo.get_run(body["job_id"], body["run_id"])
    machine_before = container.blob.get(BlobPaths.result(job.job_id, run.run_id))
    corrected = json.loads(machine_before)["extraction"]
    corrected["document"]["product_name"] = {"value": "Corrected", "raw_text": "Corrected", "status": "found", "evidence": []}

    review = container.reviews.save(job=job, run=run, reviewer_id="owner-1", corrected=corrected, expected_review_version=0, note="fix")
    assert review.version == 1 and review.changed_paths == ["document.product_name.raw_text", "document.product_name.status",
                                                            "document.product_name.value"]
    assert container.blob.get(BlobPaths.result(job.job_id, run.run_id)) == machine_before
    assert container.blob.exists(BlobPaths.review(job.job_id, run.run_id, 1))
    run = container.repo.get_run(job.job_id, run.run_id)
    assert run.review_version == 1

    with pytest.raises(ConflictError):
        container.reviews.save(job=job, run=run, reviewer_id="owner-1", corrected=corrected, expected_review_version=0, note=None)

    corrected["unexpected"] = True
    with pytest.raises(BadRequestError):
        container.reviews.save(job=job, run=run, reviewer_id="owner-1", corrected=corrected, expected_review_version=1, note=None)
    assert len(container.repo.list_reviews(run.run_id)) == 1

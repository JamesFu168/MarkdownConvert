import csv
import io

from services.experiments import csv_safe, export_rows, rows_to_csv, summarize_experiment
from tests.conftest import drain, upload


def test_csv_formula_injection_neutralised():
    assert csv_safe("=1+1") == "'=1+1"
    assert csv_safe("+cmd") == "'+cmd"
    assert csv_safe("-5") == "'-5"
    assert csv_safe("@x") == "'@x"
    assert csv_safe("normal") == "normal" and csv_safe(None) == ""


def test_summary_excludes_test_doubles_and_marks_unscored(container):
    upload(container, arms=("A", "B"), experiment_id="exp1")
    drain(container)
    jobs = container.repo.list_jobs_by_experiment("owner-1", "exp1")
    pairs = [(j, container.repo.get_run(j.job_id, j.latest_run_id)) for j in jobs]
    s = summarize_experiment("exp1", pairs, {}, container.arms)
    assert s["documents"] == 1
    a = s["arms"]["A"]
    assert a["documents_received"] == 1 and a["technically_completed"] == 1
    assert a["test_double_runs_excluded"] == 1 and a["tokens"]["real_calls"] == 0
    assert a["cost"]["status"] == "unknown"
    assert a["scoring"]["scored_documents"] == 0 and "未評分" in a["scoring"]["note"]
    assert s["arms"]["C"]["documents_received"] == 0

    docs = {j.document_id: container.repo.get_document("owner-1", j.document_id) for j in jobs}
    rows = export_rows("exp1", pairs, {}, {k: d.sha256 for k, d in docs.items()}, {k: d.format.value for k, d in docs.items()})
    text = rows_to_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 2 and parsed[0]["scored"] == "False" and parsed[0]["is_test_double"] == "True"
    assert parsed[0]["cost_amount"] == ""

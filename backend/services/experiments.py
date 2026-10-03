"""Experiment arm configuration, summaries and CSV export."""
from __future__ import annotations

import csv
import io
import json
import statistics
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from domain.models import ExperimentArm, JobRecord, JobStatus, RunRecord, Stage

from .cost import aggregate_costs

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def load_arms(path: str | Path) -> dict[str, ExperimentArm]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    arms = {a["arm"]: ExperimentArm.model_validate(a) for a in data["arms"]}
    if not arms:
        raise ValueError("experiment config has no arms")
    return arms


def csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formula injection for text coming from documents or models."""
    if value is None:
        return ""
    s = str(value)
    if s.startswith(FORMULA_PREFIXES):
        return "'" + s
    return s


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "max": None, "n": 0}
    vs = sorted(values)
    p50 = statistics.median(vs)
    idx = max(0, min(len(vs) - 1, round(0.95 * (len(vs) - 1))))
    return {"p50": p50, "p95": vs[idx], "max": vs[-1], "n": len(vs)}


def _stage_ms(run: RunRecord, stage: Stage) -> float | None:
    rec = run.stage_record(stage)
    return float(rec.elapsed_ms) if rec.elapsed_ms is not None else None


def _end_to_end_ms(run: RunRecord) -> float | None:
    if run.created_at and run.finished_at:
        return (run.finished_at - run.created_at).total_seconds() * 1000
    return None


def _queue_wait_ms(run: RunRecord) -> float | None:
    if run.created_at and run.started_at:
        return (run.started_at - run.created_at).total_seconds() * 1000
    return None


def summarize_arm(arm: str, pairs: list[tuple[JobRecord, RunRecord]], scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Summary for one arm. ``scores`` maps run_id -> scoring result (only runs with human gold)."""
    received = len(pairs)
    completed = [r for _, r in pairs if r.status in (JobStatus.SUCCEEDED, JobStatus.NEEDS_REVIEW)]
    failed = [r for _, r in pairs if r.status == JobStatus.FAILED]
    unsupported = [r for r in failed if (r.error or {}).get("code") == "unsupported_file_input"]
    needs_ocr = [r for r in failed if (r.error or {}).get("code") == "needs_ocr"]
    test_double_runs = [r for _, r in pairs if r.config and r.config.is_test_double]

    real_calls = [c for _, r in pairs for c in r.model_calls if not c.is_test_double]
    usage_in = sum(c.usage.input_tokens or 0 for c in real_calls if c.usage)
    usage_out = sum(c.usage.output_tokens or 0 for c in real_calls if c.usage)
    usage_cached = sum(c.usage.cached_tokens or 0 for c in real_calls if c.usage)
    calls_without_usage = sum(1 for c in real_calls if c.usage is None)
    cost = aggregate_costs([c.cost for c in real_calls])

    timings = {
        "queue_wait_ms": percentiles([v for v in (_queue_wait_ms(r) for _, r in pairs) if v is not None]),
        "convert_ms": percentiles([v for v in (_stage_ms(r, Stage.CONVERT) for _, r in pairs) if v is not None]),
        "extract_ms": percentiles([v for v in (_stage_ms(r, Stage.EXTRACT) for _, r in pairs) if v is not None]),
        "validate_result_ms": percentiles([v for v in (_stage_ms(r, Stage.VALIDATE_RESULT) for _, r in pairs) if v is not None]),
        "end_to_end_ms": percentiles([v for v in (_end_to_end_ms(r) for _, r in pairs) if v is not None]),
    }

    scored = [scores[r.run_id] for r in completed if r.run_id in scores and not (r.config and r.config.is_test_double)]
    scoring: dict[str, Any] = {"scored_documents": len(scored), "unscored_documents": len(completed) - len(scored)}
    if scored:
        def ratio(num_key: str, den_key: str) -> dict[str, Any]:
            num = sum(s["metrics"][num_key] for s in scored)
            den = sum(s["metrics"][den_key] for s in scored)
            return {"numerator": num, "denominator": den, "value": (num / den) if den else None}

        scoring.update({
            "valued_field_accuracy": ratio("valued_correct", "valued_total"),
            "absent_status_accuracy": ratio("absent_correct", "absent_total"),
            "ingredient_row_accuracy": ratio("ingredient_rows_correct", "ingredient_rows_gold"),
            "ingredient_rows_extra": sum(s["metrics"]["ingredient_rows_extra"] for s in scored),
            "set_fields": {
                k: {"tp": sum(s["metrics"]["sets"][k]["tp"] for s in scored),
                    "fp": sum(s["metrics"]["sets"][k]["fp"] for s in scored),
                    "fn": sum(s["metrics"]["sets"][k]["fn"] for s in scored)}
                for k in scored[0]["metrics"]["sets"].keys()
            },
            "critical_fields": {
                k: ratio_dict(sum(s["metrics"]["critical"][k]["correct"] for s in scored), sum(s["metrics"]["critical"][k]["total"] for s in scored))
                for k in scored[0]["metrics"]["critical"].keys()
            },
            "document_pass": {"passed": sum(1 for s in scored if s["metrics"]["document_pass"]), "total": len(scored)},
        })
        for v in scoring["set_fields"].values():
            tp, fp, fn = v["tp"], v["fp"], v["fn"]
            v["precision"] = tp / (tp + fp) if (tp + fp) else None
            v["recall"] = tp / (tp + fn) if (tp + fn) else None
    else:
        scoring["note"] = "未評分：沒有人工核對 gold，或只有測試替身結果"

    return {
        "arm": arm,
        "documents_received": received,
        "technically_completed": len(completed),
        "technical_completion_rate": (len(completed) / received) if received else None,
        "failed": len(failed),
        "failed_unsupported_file_input": len(unsupported),
        "failed_needs_ocr": len(needs_ocr),
        "test_double_runs_excluded": len(test_double_runs),
        "tokens": {"input": usage_in, "cached_input": usage_cached, "output": usage_out, "real_calls": len(real_calls),
                   "calls_without_usage": calls_without_usage},
        "cost": cost,
        "timings": timings,
        "scoring": scoring,
    }


def ratio_dict(num: int, den: int) -> dict[str, Any]:
    return {"numerator": num, "denominator": den, "value": (num / den) if den else None}


def summarize_experiment(experiment_id: str, pairs: Iterable[tuple[JobRecord, RunRecord]], scores: dict[str, dict[str, Any]],
                         arms: dict[str, ExperimentArm]) -> dict[str, Any]:
    by_arm: dict[str, list[tuple[JobRecord, RunRecord]]] = {a: [] for a in arms}
    for job, run in pairs:
        by_arm.setdefault(job.arm, []).append((job, run))
    arm_summaries = {a: summarize_arm(a, p, scores) for a, p in by_arm.items()}
    docs = {job.document_id for job, _ in pairs}
    return {
        "experiment_id": experiment_id,
        "documents": len(docs),
        "arms": arm_summaries,
        "caveats": [
            "只有真實文件與真實 API 的結果列入 tokens／cost；測試替身結果已排除。",
            "正確率只在有人工核對 gold 的文件上計算；unscored_documents 不代表正確或錯誤。",
            "cost.status=partial/unknown 表示部分或全部呼叫沒有價格表或 usage，不可當成完整成本。",
            "新指標與既有 85% 口徑不同，不可直接比較。",
        ],
    }


EXPORT_COLUMNS = [
    "experiment_id", "job_id", "run_id", "arm", "document_id", "document_sha256", "file_name", "format", "input_strategy",
    "model", "provider", "is_test_double", "status", "error_code", "input_tokens", "cached_tokens", "output_tokens",
    "cost_status", "cost_amount", "currency", "convert_ms", "extract_ms", "end_to_end_ms", "scored", "valued_correct",
    "valued_total", "ingredient_rows_correct", "ingredient_rows_gold", "review_version",
]


def export_rows(experiment_id: str, pairs: Iterable[tuple[JobRecord, RunRecord]], scores: dict[str, dict[str, Any]],
                sha_by_doc: dict[str, str], fmt_by_doc: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for job, run in pairs:
        calls = [c for c in run.model_calls if not c.is_test_double]
        cost = aggregate_costs([c.cost for c in calls]) if calls else {"status": "unknown", "measured_amount": None, "currency": None}
        sc = scores.get(run.run_id)
        rows.append({
            "experiment_id": experiment_id, "job_id": job.job_id, "run_id": run.run_id, "arm": job.arm,
            "document_id": job.document_id, "document_sha256": sha_by_doc.get(job.document_id), "file_name": job.file_name,
            "format": fmt_by_doc.get(job.document_id),
            "input_strategy": run.config.input_strategy.value if run.config else None,
            "model": run.config.model if run.config else None, "provider": run.config.provider if run.config else None,
            "is_test_double": run.config.is_test_double if run.config else None, "status": run.status.value,
            "error_code": (run.error or {}).get("code"),
            "input_tokens": sum(c.usage.input_tokens or 0 for c in calls if c.usage) if calls else None,
            "cached_tokens": sum(c.usage.cached_tokens or 0 for c in calls if c.usage) if calls else None,
            "output_tokens": sum(c.usage.output_tokens or 0 for c in calls if c.usage) if calls else None,
            "cost_status": cost["status"], "cost_amount": cost["measured_amount"] if cost["status"] != "unknown" else None,
            "currency": cost.get("currency"), "convert_ms": _stage_ms(run, Stage.CONVERT), "extract_ms": _stage_ms(run, Stage.EXTRACT),
            "end_to_end_ms": _end_to_end_ms(run), "scored": bool(sc),
            "valued_correct": sc["metrics"]["valued_correct"] if sc else None, "valued_total": sc["metrics"]["valued_total"] if sc else None,
            "ingredient_rows_correct": sc["metrics"]["ingredient_rows_correct"] if sc else None,
            "ingredient_rows_gold": sc["metrics"]["ingredient_rows_gold"] if sc else None, "review_version": run.review_version,
        })
    return rows


def rows_to_csv(rows: list[dict[str, Any]], columns: list[str] = EXPORT_COLUMNS) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([csv_safe(r.get(c)) for c in columns])
    return buf.getvalue()

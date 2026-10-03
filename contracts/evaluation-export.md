# 評估匯出格式

## CSV（`GET /experiments/{id}/export`）

UTF-8 with BOM；外部文字若以 `=`, `+`, `-`, `@`, Tab, CR 開頭會前置 `'` 以避免試算表公式注入。

| 欄位 | 說明 |
| --- | --- |
| experiment_id, job_id, run_id, arm | 定位 |
| document_id, document_sha256, file_name, format | 文件（檔名只是 metadata） |
| input_strategy, model, provider, is_test_double | 設定快照；`is_test_double=True` 的列不得列入成效 |
| status, error_code | run 終態與失敗原因 |
| input_tokens, cached_tokens, output_tokens | 只加總真實 provider 呼叫（含失敗重試）；cached 為 input 子集 |
| cost_status, cost_amount, currency | `measured` / `partial` / `unknown`；非 measured 時 amount 為空，不是 0 |
| convert_ms, extract_ms, end_to_end_ms | 階段耗時與端到端（建立到完成） |
| scored, valued_correct, valued_total, ingredient_rows_correct, ingredient_rows_gold | 只有 final gold 的文件才有值 |
| review_version | 人工修正版本數（不影響機器結果） |

## JSON（`?format=json`）

`{"experiment_id", "rows": [同 CSV 欄位], "scores": {run_id: scoring result}}`；scoring result 結構見 `backend/services/scoring.py` docstring（fields / ingredient_rows / metrics）。

## Summary（`GET /experiments/{id}/summary`）

每個 arm：`documents_received`、`technically_completed`、`failed`（含 `failed_unsupported_file_input`、`failed_needs_ocr`）、`test_double_runs_excluded`、`tokens`、`cost`、`timings`（p50/p95/max）、`scoring`（含分子分母；無 gold 時為 `scored_documents: 0` 與說明）。`caveats` 必須與數字一起呈現。

# API

正式契約見 [`contracts/openapi.yaml`](../contracts/openapi.yaml)。prefix `/api`；錯誤格式 `{code, message, request_id, retryable, details?}`；所有結果與下載皆檢查 owner。

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| GET | `/health` | 匿名；provider、是否測試替身、轉換器版本 |
| GET | `/config` | 允許的 arms、上限、模型名稱（server 控制）、輪詢間隔、owner |
| POST | `/jobs` | multipart `file` + `arms`（逗號）+ `experiment_id`；`Idempotency-Key` header；202／503 |
| GET | `/jobs?status=&cursor=&limit=` | 自己的任務，游標分頁 |
| GET | `/jobs/{job_id}` | 狀態、stage、warnings、最新 run、run 歷史 |
| GET | `/jobs/{job_id}/runs/{run_id}/result` | 202 未就緒；200 `ready:true` 含 result／validation／reviews，或 `ready:false, status:failed` |
| GET | `/jobs/{job_id}/runs/{run_id}/artifacts/{kind}` | `original|markdown|model-input|result|validation`（白名單） |
| PATCH | `/jobs/{job_id}/runs/{run_id}/review` | `{expected_review_version, corrected, note}` → 新版本；409 版本衝突 |
| POST | `/jobs/{job_id}/retry` | 只對 failed／needs_review，受 `MAX_RETRIES_PER_JOB` 限制 |
| GET | `/experiments/{id}/summary` | 各 arm 技術完成率、tokens、cost、耗時、評分（僅有 gold） |
| GET | `/experiments/{id}/export?format=csv|json` | 匯出；CSV 防公式注入 |
| POST | `/internal/reconcile` | authLevel=admin（master key）；重送 pending／過期 lease |

## 範例（本機）

```powershell
curl.exe -i -X POST http://localhost:7071/api/jobs -H "Idempotency-Key: demo-1" -F "file=@C:\path\to\your.pdf" -F "arms=A,B" -F "experiment_id=pilot-1"
curl.exe http://localhost:7071/api/jobs/<job_id>
curl.exe http://localhost:7071/api/jobs/<job_id>/runs/<run_id>/result
curl.exe -o result.json http://localhost:7071/api/jobs/<job_id>/runs/<run_id>/artifacts/result
curl.exe http://localhost:7071/api/experiments/pilot-1/summary
```

本機身分：`AUTH_MODE=local_dev` 只接受 Host 為 localhost／127.0.0.1 且無 `X-Forwarded-For` 的請求，owner 固定為 `LOCAL_DEV_USER_ID`；在 Azure（偵測到 `WEBSITE_INSTANCE_ID`）會直接拒絕此模式。

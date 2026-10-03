# Runbook

## 本機啟動（三個終端機）

```powershell
# 1. Azurite（已全域安裝 3.37.0；或 docker compose -f infra/docker-compose.azurite.yml up -d）
azurite --silent --location .azurite --skipApiVersionCheck

# 2. 後端
cd backend
uv venv --python 3.12 .venv            # 或 python -m venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
copy local.settings.json.example local.settings.json   # 預設 EXTRACTION_PROVIDER=mock
.\.venv\Scripts\Activate.ps1
func start --port 7071

# 3. 前端
cd frontend
npm ci
npm start                              # http://localhost:4200 → API http://localhost:7071/api
```

## 切換真實 OpenAI

在 `backend/local.settings.json` 設定：`EXTRACTION_PROVIDER=openai`、`OPENAI_API_KEY`、`OPENAI_MODEL_BASELINE`、`OPENAI_MODEL_CANDIDATE`（可選 `OPENAI_FILE_DETAIL=auto|low|high`）。缺任何一項時 `/api/jobs` 回 500 `provider_not_configured`，不會悄悄退回 mock。
在 `evaluation/pricing.json` 填入兩個模型的官方單價，否則費用顯示「未知」。

單份文件 smoke test（會產生費用，需人工決定）：

```powershell
curl.exe -i -X POST http://localhost:7071/api/jobs -H "Idempotency-Key: smoke-1" -F "file=@<你的 SDS>.pdf" -F "arms=B"
```

## 測試

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q                       # 69 項（realdocs／azurite 自動跳過）
$env:SDS_TEST_AZURITE="1"; .\.venv\Scripts\python.exe -m pytest tests/test_azurite_integration.py -q
$env:TEST_SDS_DIR="C:\path\to\sds"; .\.venv\Scripts\python.exe -m pytest tests/test_real_documents.py -q -s
.\.venv\Scripts\python.exe -m ruff check .

cd ..\frontend
npm run build
npx ng test --watch=false            # 4 項（vitest）
```

## 評估流程

```powershell
$env:TEST_SDS_DIR="C:\path\to\sds"
python evaluation/cli.py manifest --split dev          # 文件清單、文字層、可處理性
python evaluation/cli.py inspect-conversion            # MarkItDown 輸出供人工檢查
python evaluation/cli.py gold-template                 # data/gold/<sha>.json 空白骨架 → 人工填寫、status=final
python evaluation/cli.py submit --experiment pilot-1 --arms A,B,C,D [--confirm-real-cost]
python evaluation/cli.py summary --experiment pilot-1
# 匯出：GET /api/experiments/pilot-1/export?format=json → evaluation/cli.py score --export ... --results-dir ...
```

## 維運

| 情境 | 處置 |
| --- | --- |
| 回 503 `enqueue_failed` | 記錄已保留；用相同 Idempotency-Key 重送，或 `POST /api/internal/reconcile`（本機 admin 路由不需 key） |
| run 卡在 processing | 等 lease（600 秒）過期後 reconcile 會重送；查 `warnings` 的 `lease_expired_requeued` |
| poison queue | `process_poison` 將 run 標 `failed(poison_queue)`；查 `error.details.last_error` 與 `model-response-*.json` |
| 429 連續 | `model_calls` 看 `rate_limited` 與 Retry-After；調低 `maximumInstanceCount` 或增加 `OPENAI_TIMEOUT_SECONDS` 不會解決配額，應降低併行 |
| 候選模型不吃檔案 | arm C 會 `failed(unsupported_file_input)`，摘要單獨計數；不要把 C 改成文字輸入 |
| 掃描 PDF | `failed(needs_ocr)`；列入「收到文件」但不偽造答案；階段 6 加 OCR arm |

日誌只含 ID、錯誤分類與指標；不要在 log 輸出 SDS 內容或金鑰。

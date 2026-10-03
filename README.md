# SDS 文件解析 POC（MarkItDown × OpenAI × Azure Functions Flex Consumption）

比較「原始 PDF／DOCX 直接交給 OpenAI」與「先用 MarkItDown 轉成文字再交給 OpenAI」，搭配現行／候選模型形成 A/B/C/D 四組。前端 Angular 22，後端 Python 3.12 Azure Functions（v2 模型），長時間解析走 Storage Queue Trigger，HTTP 上傳只建立任務並回 202。

規劃文件：[`docs/sds-poc-codex-development-plan.md`](docs/sds-poc-codex-development-plan.md)。完成報告與未驗證清單：[`docs/poc-report.md`](docs/poc-report.md)、[`docs/limitations.md`](docs/limitations.md)。

> **重要**：本 repo 不含任何範例 SDS、合成測試文件或預填成效。沒有人工 gold 與真實 API usage 前，所有「正確率／成本」皆為 **未驗證**；轉檔成功、Schema 通過或 mock 結果都不代表抽取正確。

## 採用版本

| 元件 | 版本 |
| --- | --- |
| Python | 3.12.10（uv 0.11 建立 venv） |
| azure-functions / Core Tools | 1.25.0 / 4.8.0（runtime 4.1046） |
| openai | 3.24.0（Responses API + Structured Outputs，`max_retries=0`） |
| markitdown[pdf,docx] | 0.1.8（plugins 停用） |
| azure-storage-blob / data-tables / storage-queue / identity | 12.31.0 / 12.7.0 / 12.18.0 / 1.26.0 |
| pydantic / pypdf / pytest / ruff | 2.13.5 / 6.19.0 / 9.1.1 / 0.16.10 |
| Node / Angular / TypeScript / vitest | 22.23.2 / 22.2.x / 6.0 / 5.0 |
| Azurite | 3.37.0 |
| Bicep CLI | 0.47.16（僅 `az bicep build`） |

完整鎖定：`backend/requirements.lock.txt`、`frontend/package-lock.json`。

## 快速啟動（本機，無金鑰、mock provider）

```powershell
# 終端機 1：Azurite
azurite --silent --location .azurite --skipApiVersionCheck
# （沒有全域 azurite 時：docker compose -f infra/docker-compose.azurite.yml up -d）

# 終端機 2：後端
cd backend
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
copy local.settings.json.example local.settings.json
.\.venv\Scripts\Activate.ps1
func start --port 7071

# 終端機 3：前端
cd frontend
npm ci
npm start      # http://localhost:4200
```

`GET http://localhost:7071/api/health` 應回 `"provider": "mock", "is_test_double": true`。UI 會在頂端顯示「測試替身，未呼叫真實模型」。

## 設定

範本：[`.env.example`](.env.example)、[`backend/local.settings.json.example`](backend/local.settings.json.example)。重點變數：

| 變數 | 說明 |
| --- | --- |
| `EXTRACTION_PROVIDER` | `mock`（不建立 OpenAI client）或 `openai`（缺設定即明確失敗，不回退 mock） |
| `OPENAI_API_KEY`、`OPENAI_MODEL_BASELINE`、`OPENAI_MODEL_CANDIDATE` | 模型名稱只能由環境提供，程式不預設 |
| `OPENAI_TIMEOUT_SECONDS`=180、`OPENAI_MAX_ATTEMPTS`=2、`OPENAI_FILE_DETAIL`、`OPENAI_MAX_OUTPUT_TOKENS` | 呼叫預算與 PDF detail（空=不送，記為 default） |
| `MAX_UPLOAD_BYTES`=20 MiB、`MIN_TEXT_CHARS_PER_PAGE`=40、`PROCESSING_LEASE_SECONDS`=600 | 文件與處理限制 |
| `AzureWebJobsStorage`（或 `AzureWebJobsStorage__accountName` + Managed Identity） | 儲存 |
| `AUTH_MODE` | `local_dev`（僅 loopback，Azure 上拒絕）／`easyauth` |
| `TEST_SDS_DIR` | 使用者既有 PDF／DOCX 目錄（評估 CLI 與 realdocs 測試使用） |
| `GOLD_DIR` | 人工 gold 目錄，預設 `data/gold`（git-ignored） |
| `DOCINTEL_ENDPOINT` | 階段 6 OCR adapter 預留 |

價格表：[`evaluation/pricing.json`](evaluation/pricing.json)（出廠為空；填入官方單價前費用一律顯示「未知」）。實驗組定義：[`evaluation/experiments/pilot-v1.json`](evaluation/experiments/pilot-v1.json)。

## 測試

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q                                   # 69 passed（realdocs／azurite 依條件跳過）
$env:SDS_TEST_AZURITE="1"; .\.venv\Scripts\python.exe -m pytest tests/test_azurite_integration.py -q   # 2 passed（需 Azurite）
$env:TEST_SDS_DIR="C:\path\to\your\sds"; .\.venv\Scripts\python.exe -m pytest tests/test_real_documents.py -q -s
.\.venv\Scripts\python.exe -m ruff check .

cd ..\frontend
npm run build                      # production build
npx ng test --watch=false          # 4 passed（vitest）

az bicep build --file infra/main.bicep
```

## 使用者文件與評估

```powershell
$env:TEST_SDS_DIR="C:\path\to\your\sds"
python evaluation/cli.py manifest --split dev        # 格式、頁數、文字層、可處理性
python evaluation/cli.py inspect-conversion          # MarkItDown 輸出供人工檢查
python evaluation/cli.py gold-template               # 空白 gold 骨架（data/gold/<sha256>.json）
python evaluation/cli.py submit --experiment pilot-1 --arms A,B,C,D   # 真實 provider 時需 --confirm-real-cost
python evaluation/cli.py summary --experiment pilot-1
```

Gold 建立流程：[`evaluation/gold/README.md`](evaluation/gold/README.md)。

## 部署（未執行）

[`infra/main.bicep`](infra/main.bicep)、[`infra/deploy.md`](infra/deploy.md)：Storage（Blob/Queue/Table，identity-based）、Flex Consumption Python 3.12 Function App（`maximumInstanceCount` 預設 1、2048 MB）、Key Vault reference、Application Insights、CORS、Easy Auth。前端以 Azure Static Web Apps 靜態託管並呼叫獨立 Function App。本 POC 沒有建立任何雲端資源。

## 文件

- [docs/architecture.md](docs/architecture.md) — 流程、儲存、狀態機、時間預算、來源定位
- [docs/api.md](docs/api.md)、[contracts/openapi.yaml](contracts/openapi.yaml)
- [docs/runbook.md](docs/runbook.md) — 啟動、切換真實 provider、維運
- [docs/limitations.md](docs/limitations.md) — 未驗證項目與設計限制
- [docs/poc-report.md](docs/poc-report.md) — 完成報告
- [contracts/extraction-schema.pilot-v1.json](contracts/extraction-schema.pilot-v1.json)、[contracts/gold.schema.json](contracts/gold.schema.json)、[contracts/evaluation-export.md](contracts/evaluation-export.md)

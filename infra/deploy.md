# Azure 部署說明（Flex Consumption）

> 本 POC **沒有**執行任何 Azure 部署；以下步驟經 `az bicep build` 語法／schema 驗證，但尚未在真實訂閱上執行。部署會產生費用，請人工審核後再執行。

## 架構對應

| 元件 | 資源 | 備註 |
| --- | --- | --- |
| Blob／Queue／Table | `Microsoft.Storage/storageAccounts`（`allowSharedKeyAccess=false`） | 容器 `sds`、`app-package`；佇列 `sds-jobs`、`sds-jobs-poison` |
| Function App | `Microsoft.Web/sites`（`functionapp,linux`）＋ `serverfarms` sku `FC1/FlexConsumption` | Python 3.12，`maximumInstanceCount` 預設 1（官方文件：最低可設 1、最高 1000），`instanceMemoryMB` 2048 |
| 身分 | System-assigned Managed Identity | Storage Blob Data Owner／Queue Data Contributor／Table Data Contributor、Key Vault Secrets User |
| 密鑰 | Key Vault `openai-api-key` → App Setting Key Vault reference | 程式不接受前端傳入 key |
| 觀測 | Log Analytics ＋ Application Insights | 日誌只輸出 ID、錯誤分類、指標 |
| 認證 | App Service Authentication（Entra ID，`Return401`，`/api/health` 例外） | 後端讀 `X-MS-CLIENT-PRINCIPAL-ID` 決定 owner |
| CORS | `siteConfig.cors.allowedOrigins` | 只放前端來源 |

`functionTimeout=00:10:00` 與 queue `batchSize=1 / newBatchThreshold=0 / maxDequeueCount=3` 在 `backend/host.json`。Flex Consumption 需要 extension bundle `[4.*, 5.0.0)`（已設定）。Flex 不使用自訂 Docker image。

### 併行限制說明

每個 Function 在 Flex 上是獨立的 scale group；`maximumInstanceCount` 套用到**每個** group。POC 設為 1，加上 `batchSize=1`，等於同時最多 1 份文件在解析、HTTP group 也最多 1 個執行個體（HTTP 每執行個體預設併行足夠 POC 使用）。要提高吞吐時調高 `maximumInstanceCount` 並注意 OpenAI 速率限制。

## 步驟

```powershell
# 0. 登入並選訂閱
az login
az account set --subscription <SUBSCRIPTION_ID>
az functionapp list-flexconsumption-locations --query "sort_by(@, &name)[].name" -o table

# 1. 驗證範本（已在本機執行過，無部署）
az bicep build --file infra/main.bicep

# 2. 建立資源群組並 what-if（不建立資源）
az group create -n rg-sds-poc -l <REGION>
copy infra\main.bicepparam.example infra\main.bicepparam   # 編輯參數；main.bicepparam 已被 .gitignore 忽略（*.local.json 不含它，請勿提交含金鑰的檔案）
$env:OPENAI_API_KEY = "<key>"; $env:OPENAI_MODEL_BASELINE = "<model>"; $env:OPENAI_MODEL_CANDIDATE = "<model>"
az deployment group what-if -g rg-sds-poc -f infra/main.bicep -p infra/main.bicepparam

# 3. 部署（付費動作，需人工確認）
az deployment group create -g rg-sds-poc -f infra/main.bicep -p infra/main.bicepparam

# 4. 發布程式（Python 必須 remote build）
cd backend
func azure functionapp publish <FUNCTION_APP_NAME> --python
#   或：zip 專案根目錄後 az functionapp deployment source config-zip -g rg-sds-poc -n <APP> --src backend.zip --build-remote true

# 5. Entra ID
#   建立 API 的 App registration，設定 Application ID URI (api://<clientId>) 與 scope（例如 user_impersonation），
#   將 clientId / audience 填入參數後重新部署 authsettingsV2。
#   前端 SPA registration：redirect URI 為 Static Web App 網址；前端取得 api://<clientId>/user_impersonation token 並以 Bearer 送出。
```

## 前端（Azure Static Web Apps，靜態託管）

```powershell
cd frontend
npm ci
# 編輯 src/environments/environment.prod.ts 的 apiBaseUrl 為 https://<functionAppHostName>/api
npm run build
# 以 SWA CLI 或 GitHub Action 上傳 dist/frontend/browser；不要使用 SWA managed API（架構採獨立 Function App）
```

## 內部維運

- `POST /api/internal/reconcile`（authLevel=admin，需 master key）重送 `enqueue_pending` 與 lease 過期的 run。可用 Logic App／定時排程每 5 分鐘呼叫；本 POC 未自動建立。
- Poison queue 由 `process_poison` 函式處理並將 run 標為 failed（`poison_queue`）。

## 已知未驗證

- Bicep 僅通過 `az bicep build`；`what-if`／實際部署未執行。
- `allowSharedKeyAccess=false` 搭配 Flex 的 identity-based host storage 依官方文件支援，但未在本專案實測；若部署失敗可先改為 `true` 並改用連線字串設定，再回頭收緊。
- Easy Auth 與前端 MSAL token 流程未端到端實測（前端提供 token provider 介面，見 `frontend/src/app/core/auth.ts`）。

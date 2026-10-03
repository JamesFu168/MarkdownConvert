# SDS 解析 POC 完成報告（階段 1–5 程式；階段 6 僅介面）

日期：2026-10-03。依據 `docs/sds-poc-codex-development-plan.md`。本報告**不**宣稱任何正確率、85% 重現、95% 目標或 30% 省費；這些都需要使用者文件、人工 gold 與真實 API usage。

## 1. 已完成

| 階段 | 內容 | 驗證方式 |
| --- | --- | --- |
| 1 契約與骨架 | Angular 22 + Python Functions v2、Azurite、pilot-v1 Pydantic schema 與 strict JSON Schema、可注入 provider／converter／storage、上傳→Blob→Table→Queue→202、查詢、Idempotency-Key、入列部分失敗恢復 | 69 項 pytest；真實 Functions host + Azurite 端到端 smoke（見 §3） |
| 2 MarkItDown 與來源 | pypdf 文字層檢查（掃描／部分文字／加密／損毀／DOC 拒絕）、MarkItDown 0.1.8（plugins 停用）、原始 Markdown 與 blocks.json 保存、`[[bNNNN]]` 區塊 ID、`ws-v1` quote 核對、同文件轉檔重用 | 單元測試；**使用者文件尚未載入**（TEST_SDS_DIR 未設定） |
| 3 OpenAI adapter | Responses API + Structured Outputs（`input_file` base64 data URL／`input_text`、`strict:true`、`store:false`、SDK `max_retries=0`）、180 秒 deadline、每次 invocation 最多 2 次呼叫、Retry-After（上限 30 秒）、usage（cached／reasoning 子集）、request/response ID、錯誤分類（429／5xx／逾時／401／404／400 不支援檔案／refusal／incomplete） | 以假 client 的契約測試（`tests/test_openai_adapter.py`）；**真實呼叫未執行** |
| 4 比較與人工核對 | A/B/C/D 版本化設定、每 run 設定快照、gold 載入（只接受 `status=final`）、評分（純量正規化比對、集合一對一 TP/FP/FN、成分列配對、關鍵欄位分列、文件通過）、成本（價格表、measured／partial／unknown）、p50/p95/max 耗時、CSV（BOM、公式注入防護）、Angular 比較頁、版本化 review（不覆寫機器結果、409 衝突） | pytest + vitest（4 項）+ `ng build` |
| 5 恢復與部署文件 | 重複訊息、live lease 丟棄、過期 lease 接手、暫時性錯誤回 queued 並由 Queue 重試、checkpoint 續跑（轉檔只做一次）、poison queue → failed、reconcile 補送、>230 秒背景作業仍可查詢（執行緒 + 假時鐘）、Bicep（Flex FC1、Python 3.12、identity-based storage、Key Vault、Easy Auth、CORS） | pytest；`az bicep build` 通過；**未部署** |
| 6 擴充介面 | `adapters/ocr_interface.py`（Document Intelligence 預留）、`needs_ocr` 終態、`DOCINTEL_ENDPOINT` 設定；章節裁切／局部回退留為新 arm | 介面與測試而已 |

## 2. 命令與結果

```text
backend> python -m pytest -q                                   69 passed（realdocs、azurite 條件跳過）
backend> SDS_TEST_AZURITE=1 pytest tests/test_azurite_integration.py   2 passed（Azurite 3.37.0）
backend> ruff check .                                           All checks passed
frontend> npm run build                                         成功（initial 299.7 kB raw）
frontend> npx ng test --watch=false                             4 passed（vitest 5.0.3）
> az bicep build --file infra/main.bicep                        成功（無錯誤）
```

### 3. 真實 Functions host 端到端（Azurite、mock provider）

`func start` 成功載入全部 HTTP 路由與兩個 Queue Trigger。以**非文件**的 `%PDF` 檔頭探針（不是合成 SDS）驗證架構：

| 步驟 | 結果 |
| --- | --- |
| `GET /api/health` | 200，`provider=mock, is_test_double=true` |
| `POST /api/jobs` legacy `.doc` | 415 `unsupported_format` |
| `POST /api/jobs` arms=A,B + Idempotency-Key | 202，兩個 job 皆 `queued`，Queue 訊息只含 ID |
| 同 key 重送 | 202 + `Idempotent-Replayed: true`；同 key 不同 payload → 409 |
| Queue Trigger 處理後 `GET /api/jobs/{id}` | `failed`，`error.code=corrupt_document`（stage `validate_document`）——探針不是 PDF，正確拒絕，沒有偽成功 |
| `GET …/result` | 200 `ready:false, status:failed` |
| `GET /api/experiments/e2e/summary` ／ `export` | A、B 各 1 份收到、1 份失敗、0 份評分；CSV 含 BOM 且 `is_test_double=True` |
| `POST /api/internal/reconcile` | 200（補送了 Azurite 內殘留的測試 run；已改為測試專用 table prefix 並清理） |
| `Host: evil.example.com` | 403（local_dev 僅 loopback） |

這證明 HTTP 與解析解耦、Queue 編碼／觸發設定正確、錯誤分類可查詢。它**不**證明任何模型效果。

## 4. 工程取捨紀錄

- **一次上傳多組**：`POST /jobs` 可帶 `arms=A,B,C,D`，同一文件只存一份、每組一個 job/run；比較頁以 `document_id` 成對呈現。
- **Mock 輸出全部 `unreadable`**：避免替身結果看起來像真實抽取；UI、摘要、匯出全部標示並排除。
- **Schema 錯誤重試**：模型輸出非 JSON／不符 schema 時在 2 次呼叫預算內重試一次，再失敗即 `failed(schema_violation)`，不跨 Queue 重試。
- **轉檔重用**：Markdown 以 `documents/{id}/markdown/{converter_version}/` 快取，不同 arm／retry 共用；模型回應各自保存。
- **原始文件策略的 quote 核對**：PDF 用 pypdf 文字層、DOCX 用 document.xml 文字；屬本機免費動作，不影響模型輸入。
- **Flex 併行**：`maximumInstanceCount=1`（官方文件最低值為 1）+ `batchSize=1`；要擴大時需顧及 OpenAI 配額。
- **Table 設計**：jobs RowKey 用反向毫秒時間戳取得最新在前的游標分頁；runs 以 ETag 條件更新作 lease。
- **前端身分**：提供 `TOKEN_PROVIDER` 注入點，MSAL 未內建（避免在無 App registration 下寫無法驗證的流程）。
- **Markdown 顯示**：以 `<pre>` 純文字呈現，不做 HTML 渲染，天然排除 script／遠端資源。
- **評分不用模型**：所有比對皆為確定性規則；`accepted_values`／`accepted_names` 由人工 gold 提供同義寫法。

## 5. 尚未驗證（詳見 `docs/limitations.md`）

1. **使用者 PDF／DOCX**：`TEST_SDS_DIR` 未設定，常見目錄未找到 SDS 資料夾 → 儲存／檢查／轉檔對真實文件的表現未執行。設定後跑 `pytest tests/test_real_documents.py -s` 與 `evaluation/cli.py manifest / inspect-conversion`。
2. **真實 OpenAI 呼叫**：環境有 `OPENAI_API_KEY`，但 `OPENAI_MODEL_BASELINE`／`OPENAI_MODEL_CANDIDATE` 未設定且規劃禁止臆測模型與自動付費批次 → 未呼叫。DOCX 是否被 `input_file` 接受、候選模型是否接受檔案，皆待實測。
3. **成效數字**：無 gold、無 usage → 正確率、關鍵欄位、成本一律「未評分／未知」。
4. **Azure**：未建立任何資源；Bicep 只過編譯；Easy Auth + MSAL、identity-based host storage、10 分鐘 timeout 行為待部署觀測。
5. **價格表**：`evaluation/pricing.json` 刻意為空。

## 6. 下一步建議

1. 設定 `TEST_SDS_DIR`，用 10–15 份不同版面文件跑 `manifest` 與 `inspect-conversion`，人工檢查成分表／符號／原文。
2. 填入模型名稱與價格表，對 1 份文件執行 arm B smoke test（人工確認費用）。
3. 依 `evaluation/gold/README.md` 建立 gold（dev／eval 分開），再跑 A/B/C/D 批次並以 summary／export／score 觀察分子分母。
4. 真實文件證明需求後，才以**新 arm** 加入 Document Intelligence、章節裁切、局部回退（階段 6）。

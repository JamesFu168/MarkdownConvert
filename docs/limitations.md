# 限制與尚未驗證項目

## 尚未實際驗證（需要使用者文件／金鑰／訂閱）

| 項目 | 狀態 | 需要什麼 |
| --- | --- | --- |
| 真實 OpenAI 呼叫（Responses API + Structured Outputs，input_file／input_text） | **未執行**。adapter 依 openai 3.24.0 實際簽章撰寫並以假 client 做契約測試 | `OPENAI_API_KEY`、`OPENAI_MODEL_BASELINE`、`OPENAI_MODEL_CANDIDATE`；一次 smoke test 會產生費用 |
| DOCX 作為 `input_file` 是否被模型接受 | 未知 | 同上；被拒絕時 run 標 `unsupported_file_input` |
| 使用者 PDF／DOCX 的儲存、檢查、MarkItDown 轉換 | **未執行**（`TEST_SDS_DIR` 未設定；本機常見目錄未找到 SDS 資料夾） | 設定 `TEST_SDS_DIR` 後執行 `pytest tests/test_real_documents.py -s` 與 `evaluation/cli.py manifest / inspect-conversion` |
| 抽取正確率、關鍵欄位正確率、成本降幅 | **無任何數字**。無 gold、無真實 API usage | 人工 gold（見 `evaluation/gold/README.md`）＋真實批次 |
| Azure 部署 | Bicep 僅 `az bicep build` 通過；what-if／部署未執行 | 訂閱與人工核准 |
| Easy Auth + 前端 MSAL token 流程 | 後端讀取 `X-MS-CLIENT-PRINCIPAL-ID` 已測；前端只有 `TOKEN_PROVIDER` 介面 | App registration 與 MSAL 設定 |
| `functionTimeout=10 分鐘` 在 Flex 上的實際行為 | 依官方文件設定；未在雲端觀測 | 部署後觀測 |
| Flex 上 identity-based host storage（`allowSharedKeyAccess=false`） | 官方文件支援；未實測 | 部署後確認，失敗時暫改連線字串 |

## 已知設計限制

- **不是 exactly-once**：模型呼叫成功但保存前程序中斷，重試會再付費一次。所有嘗試都記錄，不只算最後成功那次。
- **掃描偵測**：以每頁文字層字數 < `MIN_TEXT_CHARS_PER_PAGE`（40）判定；少量文字的掃描頁可能被當成有文字層，混合 PDF 標 `partial_text` 並進 needs_review。
- **MarkItDown 頁碼**：Markdown 模式輸出沒有可驗證的頁面對應，`page` 填 null；只有原始 PDF 模式能用 pypdf 文字層核對頁碼。
- **DOCX 頁碼**：不猜測 Word 頁碼，只用區塊 ID。
- **Markdown 區塊切分**：以空行／表格列／標題為單位的簡單切分；跨頁重複頁首頁尾、章節裁切、長文件分段都留到階段 6，以免 A/B 同時改變多個因素。
- **候選模型參數**：各 arm 固定相同 `max_output_tokens`、prompt、schema、PDF detail；未針對不同模型套用其不支援的參數。若候選模型不支援檔案輸入，arm C 標 unsupported，不改成文字輸入。
- **併行**：Flex `maximumInstanceCount` 以每個 function group 計；設 1 + `batchSize=1` 讓同時只處理 1 份。要更高吞吐須同時考慮 OpenAI 速率限制。
- **成本**：只有價格表有條目且 API 回傳 usage 才算 `measured`；OCR／雲端執行成本無帳務前不納入，`cost.status` 會是 partial／unknown，不以模型費用冒充完整成本。
- **評分**：純量用正規化精確比對（NFKC、casefold、空白、邊緣標點），集合用穩定一對一比對，成分列以 CAS→名稱→濃度配對；沒有語意相似度，也不用模型評分。新指標與既有「85%」不同口徑，不能直接比較。
- **人工修正不是 gold**：review 版本只供追查與 UI 顯示；gold 必須另外依 `evaluation/gold/README.md` 建立。
- **測試替身**：mock 輸出所有欄位皆 `unreadable`，標示 `is_test_double`，摘要與匯出一律排除，不計 token／費用／正確率。
- **前端 Markdown**：以 `<pre>` 純文字呈現，不渲染 HTML、不載入遠端資源；PDF 以 blob URL iframe 預覽。
- **Legacy .doc**：回 415 `unsupported_format`，不自動轉換。
- **reconcile 排程**：提供 admin 路由，但未建立定時觸發（可用 Logic App／Timer trigger 補上）。
- **Table 掃描**：`scan_runs` 以 status 屬性全表過濾，POC 規模可接受，大量資料需二級索引。

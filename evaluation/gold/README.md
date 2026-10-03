# Gold 建立流程（人工）

`data/gold/<document_sha256>.json`（git-ignored）是唯一的正確答案來源。系統不會自動產生 gold，模型輸出與人工修正（review）也不會自動變成 gold。

1. `python evaluation/cli.py manifest --dir <TEST_SDS_DIR>`：列出文件、格式、頁數、文字層字數與可處理性。
2. `python evaluation/cli.py inspect-conversion`：輸出 MarkItDown 結果供人工檢查成分表、符號與原文（先用 10–15 份不同版面）。
3. `python evaluation/cli.py gold-template`：為每份文件建立空白 gold 骨架（以 SHA-256 命名）。
4. 審核者對照**原始文件**填寫：
   - `fields.*.status`：`found` / `not_reported` / `no_data` / `not_applicable`；`value` 為正規化值；`accepted_values` 列可接受的同義寫法。
   - `physical.flash_point.value`：`{"comparator","min","max","unit"}`，單位 `C`／`F`，不要換算。
   - `sets.*`：文件明列的代碼或分類原文；未列者留空陣列。
   - `ingredients[]`：名稱、`accepted_names`、CAS 陣列、濃度（保留比較符號與範圍）。
5. 完成後設定 `status: "final"` 與 `reviewed_at`；未完成（draft）的 gold 不參與評分。
6. 開發／調整用與評估用文件請以 `manifest --split dev|eval` 分開，避免同供應商同模板跨兩組。

格式契約見 `contracts/gold.schema.json`。

import { FieldStatus, JobStatus, Stage } from '../core/models';

export const STATUS_LABELS: Record<JobStatus, string> = {
  enqueue_pending: '待入列',
  queued: '已入列',
  processing: '處理中',
  succeeded: '已完成',
  needs_review: '需人工核對',
  failed: '失敗',
};

export const STAGE_LABELS: Record<Stage, string> = {
  validate_document: '文件檢查',
  convert: 'MarkItDown 轉換',
  extract: '模型抽取',
  validate_result: '結果驗證',
  persist: '保存',
};

export const FIELD_STATUS_LABELS: Record<FieldStatus, string> = {
  found: '已找到',
  not_reported: '文件未列載',
  no_data: '無資料',
  not_applicable: '不適用',
  unreadable: '無法讀取',
  conflict: '矛盾',
};

export const STRATEGY_LABELS: Record<string, string> = {
  original_document: '原始文件',
  markdown: 'MarkItDown 全文',
};

export const ERROR_LABELS: Record<string, string> = {
  unsupported_format: '不支援的格式（僅 PDF／DOCX；舊式 DOC 請另存 DOCX）',
  document_too_large: '文件超過大小上限',
  encrypted_document: 'PDF 已加密',
  corrupt_document: '文件損毀或無法讀取',
  needs_ocr: '沒有文字層（掃描檔），第一階段不執行 OCR',
  unsupported_file_input: '候選模型不接受檔案輸入（此組標記為 unsupported）',
  schema_violation: '模型輸出不符合 Schema',
  provider_not_configured: '模型服務未設定',
  poison_queue: '重試多次後仍失敗（poison queue）',
  model_refusal: '模型拒絕回應',
  output_incomplete: '模型輸出被截斷',
  empty_markdown: '轉換結果為空',
};

export function statusLabel(s: JobStatus): string {
  return STATUS_LABELS[s] ?? s;
}

export function errorLabel(code: string | undefined | null): string {
  return (code && ERROR_LABELS[code]) || code || '';
}

export function formatNumeric(v: { comparator: string | null; min: number | null; max: number | null; unit: string | null } | null): string {
  if (!v) return '';
  const unit = v.unit ? ` ${v.unit}` : '';
  switch (v.comparator) {
    case 'range':
      return `${v.min ?? '?'} – ${v.max ?? '?'}${unit}`;
    case 'lt':
      return `< ${v.max}${unit}`;
    case 'lte':
      return `≤ ${v.max}${unit}`;
    case 'gt':
      return `> ${v.min}${unit}`;
    case 'gte':
      return `≥ ${v.min}${unit}`;
    case 'approx':
      return `≈ ${v.min}${unit}`;
    default:
      return `${v.min ?? v.max ?? ''}${unit}`;
  }
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

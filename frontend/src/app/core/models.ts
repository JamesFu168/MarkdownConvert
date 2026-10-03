// API contracts (field names in English, mirroring the backend).
export type JobStatus = 'enqueue_pending' | 'queued' | 'processing' | 'succeeded' | 'needs_review' | 'failed';
export type Stage = 'validate_document' | 'convert' | 'extract' | 'validate_result' | 'persist';
export type FieldStatus = 'found' | 'not_reported' | 'no_data' | 'not_applicable' | 'unreadable' | 'conflict';

export const TERMINAL_STATUSES: JobStatus[] = ['succeeded', 'needs_review', 'failed'];
export function isTerminal(status: JobStatus | null | undefined): boolean {
  return !!status && TERMINAL_STATUSES.includes(status);
}

export interface ExperimentArm {
  arm: string;
  input_strategy: 'original_document' | 'markdown';
  model_role: 'baseline' | 'candidate';
  description: string;
}

export interface AppConfig {
  allowed_arms: ExperimentArm[];
  max_upload_bytes: number;
  accepted_formats: string[];
  provider: string;
  is_test_double: boolean;
  models: { baseline: string | null; candidate: string | null };
  auth_mode: string;
  owner_id: string;
  poll_interval_ms: number;
}

export interface ApiError {
  code: string;
  message: string;
  request_id: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export interface CreateJobsResponse {
  document_id: string;
  job_id: string;
  run_id: string;
  status_url: string;
  idempotent_replay: boolean;
  jobs: { job_id: string; run_id: string; arm: string; status: JobStatus; status_url: string }[];
  error?: ApiError;
}

export interface StageView {
  stage: Stage;
  status: 'pending' | 'running' | 'done' | 'skipped' | 'failed';
  elapsed_ms: number | null;
  detail: Record<string, unknown>;
}

export interface ModelCall {
  attempt: number;
  provider: string;
  model: string;
  is_test_double: boolean;
  success: boolean;
  error_code: string | null;
  elapsed_ms: number | null;
  response_id: string | null;
  usage: { input_tokens: number | null; output_tokens: number | null; cached_tokens: number | null; reasoning_tokens: number | null } | null;
  cost: { status: string; amount: number | null; currency?: string; reason?: string } | null;
}

export interface RunView {
  run_id: string;
  status: JobStatus;
  stage: Stage | null;
  attempt_no: number;
  previous_run_id: string | null;
  retry_reason: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
  stages: StageView[];
  warnings: string[];
  error: (ApiError & { stage?: string; will_retry?: boolean }) | null;
  config: {
    arm: string;
    input_strategy: string;
    model_role: string;
    model: string;
    provider: string;
    is_test_double: boolean;
    prompt_version: string;
    schema_version: string;
    converter: string | null;
    converter_version: string | null;
  } | null;
  metrics: Record<string, unknown> & {
    tokens?: { input: number; cached_input: number; output: number; real_calls: number; test_double_calls: number };
    cost?: { status: string; measured_amount: number; currency: string | null; calls_unknown: number };
    stage_elapsed_ms?: Record<string, number | null>;
  };
  review_version: number;
  model_calls: ModelCall[];
}

export interface JobView {
  job_id: string;
  document_id: string;
  file_name: string;
  arm: string;
  experiment_id: string | null;
  status: JobStatus;
  run_count: number;
  created_at: string;
  updated_at: string;
  latest_run: RunView | null;
  runs?: RunView[];
}

export interface JobList {
  items: JobView[];
  next_cursor: string | null;
}

export interface Evidence {
  section: string | null;
  page: number | null;
  block_id: string | null;
  quote: string | null;
}

export interface FieldEnvelope<T> {
  value: T;
  raw_text: string | null;
  status: FieldStatus;
  evidence: Evidence[];
}

export interface NumericValue {
  comparator: string | null;
  min: number | null;
  max: number | null;
  unit: string | null;
  basis?: string | null;
  conditions?: string | null;
}

export interface Ingredient {
  name: FieldEnvelope<string | null>;
  cas_numbers: FieldEnvelope<string[]>;
  concentration: FieldEnvelope<NumericValue | null>;
}

export interface Extraction {
  schema_version: 'pilot-v1';
  document: {
    product_name: FieldEnvelope<string | null>;
    revision_date: FieldEnvelope<string | null>;
    supplier_name: FieldEnvelope<string | null>;
  };
  ingredients: Ingredient[];
  hazards: {
    classifications: FieldEnvelope<string[]>;
    signal_word: FieldEnvelope<string | null>;
    h_codes: FieldEnvelope<string[]>;
    p_codes: FieldEnvelope<string[]>;
  };
  physical: { flash_point: FieldEnvelope<NumericValue | null> };
  extraction_notes: string | null;
}

export interface Finding {
  code: string;
  severity: 'info' | 'warning' | 'error';
  path: string;
  message: string;
  review_required: boolean;
}

export interface Validation {
  schema_valid: boolean;
  findings: Finding[];
  evidence_checks: { path: string; status: string; matched_block_id: string | null; matched_page: number | null }[];
  review_required: boolean;
}

export interface Review {
  run_id: string;
  version: number;
  reviewer_id: string;
  created_at: string;
  corrected: Extraction;
  note: string | null;
  changed_paths: string[];
}

export interface ResultResponse {
  ready: boolean;
  status: JobStatus;
  stage?: Stage | null;
  job_id?: string;
  run_id?: string;
  arm?: string;
  is_test_double?: boolean;
  result?: { extraction: Extraction; model: string; provider: string; response_id: string | null };
  validation?: Validation;
  reviews?: Review[];
  error?: ApiError;
  run: RunView;
}

export interface ArmSummary {
  arm: string;
  documents_received: number;
  technically_completed: number;
  technical_completion_rate: number | null;
  failed: number;
  failed_unsupported_file_input: number;
  failed_needs_ocr: number;
  test_double_runs_excluded: number;
  tokens: { input: number; cached_input: number; output: number; real_calls: number; calls_without_usage: number };
  cost: { status: string; measured_amount: number; currency: string | null; calls_unknown: number };
  timings: Record<string, { p50: number | null; p95: number | null; max: number | null; n: number }>;
  scoring: {
    scored_documents: number;
    unscored_documents: number;
    note?: string;
    valued_field_accuracy?: { numerator: number; denominator: number; value: number | null };
    ingredient_row_accuracy?: { numerator: number; denominator: number; value: number | null };
    document_pass?: { passed: number; total: number };
  };
}

export interface ExperimentSummary {
  experiment_id: string;
  documents: number;
  arms: Record<string, ArmSummary>;
  caveats: string[];
  documents_detail: { document_id: string; file_name: string; format: string; sha256: string; has_gold: boolean }[];
  jobs: { job_id: string; run_id: string; arm: string; document_id: string; status: JobStatus; scored: boolean; is_test_double: boolean }[];
}

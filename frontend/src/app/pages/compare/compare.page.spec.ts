import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it } from 'vitest';
import { ExperimentSummary } from '../../core/models';
import { ComparePage } from './compare.page';

const SUMMARY: ExperimentSummary = {
  experiment_id: 'e1',
  documents: 1,
  caveats: ['只有真實文件與真實 API 的結果列入 tokens／cost'],
  documents_detail: [{ document_id: 'd1', file_name: 'x.pdf', format: 'pdf', sha256: 'abc', has_gold: false }],
  jobs: [{ job_id: 'j1', run_id: 'r1', arm: 'A', document_id: 'd1', status: 'succeeded', scored: false, is_test_double: true }],
  arms: {
    A: {
      arm: 'A', documents_received: 1, technically_completed: 1, technical_completion_rate: 1, failed: 0, failed_unsupported_file_input: 0,
      failed_needs_ocr: 0, test_double_runs_excluded: 1,
      tokens: { input: 0, cached_input: 0, output: 0, real_calls: 0, calls_without_usage: 0 },
      cost: { status: 'unknown', measured_amount: 0, currency: null, calls_unknown: 0 },
      timings: { convert_ms: { p50: null, p95: null, max: null, n: 0 }, extract_ms: { p50: 12, p95: 12, max: 12, n: 1 }, end_to_end_ms: { p50: 1500, p95: 1500, max: 1500, n: 1 } },
      scoring: { scored_documents: 0, unscored_documents: 1, note: '未評分' },
    },
  },
};

describe('ComparePage', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({ imports: [ComparePage], providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
  });

  it('renders unscored state, excluded test doubles and unknown cost instead of fake numbers', async () => {
    const fixture = TestBed.createComponent(ComparePage);
    const http = TestBed.inject(HttpTestingController);
    fixture.componentInstance.experimentId = 'e1';
    fixture.componentInstance.load();
    http.expectOne((r) => r.url.endsWith('/experiments/e1/summary')).flush(SUMMARY);
    await fixture.whenStable();
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('未評分');
    expect(text).toContain('替身');
    expect(text).toContain('未知');
    expect(text).not.toContain('100.0%（');
    expect(text).toContain('無真實呼叫');
  });
});

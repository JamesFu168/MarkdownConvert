import { DecimalPipe, PercentPipe } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiFailure, ApiService } from '../../core/api.service';
import { ExperimentSummary, JobStatus } from '../../core/models';
import { formatMs, statusLabel } from '../../shared/labels';

@Component({
  selector: 'app-compare-page',
  imports: [FormsModule, RouterLink, DecimalPipe, PercentPipe],
  templateUrl: './compare.page.html',
})
export class ComparePage {
  private readonly api = inject(ApiService);
  experimentId = '';
  readonly summary = signal<ExperimentSummary | null>(null);
  readonly error = signal<string | null>(null);
  readonly loading = signal(false);
  readonly statusLabel = statusLabel;
  readonly formatMs = formatMs;

  readonly arms = computed(() => Object.keys(this.summary()?.arms ?? {}).sort());

  readonly matrix = computed(() => {
    const s = this.summary();
    if (!s) return [];
    const byDoc = new Map<string, { file_name: string; has_gold: boolean; cells: Record<string, { job_id: string; status: JobStatus; scored: boolean; is_test_double: boolean }> }>();
    for (const d of s.documents_detail) byDoc.set(d.document_id, { file_name: d.file_name, has_gold: d.has_gold, cells: {} });
    for (const j of s.jobs) {
      const row = byDoc.get(j.document_id);
      if (row) row.cells[j.arm] = { job_id: j.job_id, status: j.status, scored: j.scored, is_test_double: j.is_test_double };
    }
    return [...byDoc.entries()].map(([id, r]) => ({ document_id: id, ...r }));
  });

  load() {
    const id = this.experimentId.trim();
    if (!id) return;
    this.loading.set(true);
    this.error.set(null);
    this.api.experimentSummary(id).subscribe({
      next: (s) => {
        this.summary.set(s);
        this.loading.set(false);
      },
      error: (e: ApiFailure) => {
        this.error.set(`${e.error.code}: ${e.error.message}`);
        this.loading.set(false);
      },
    });
  }

  exportUrl(): string {
    return this.api.experimentExportUrl(this.experimentId.trim());
  }

  ratio(r: { numerator: number; denominator: number; value: number | null } | undefined): string {
    if (!r || r.value === null) return '未評分';
    return `${(r.value * 100).toFixed(1)}%（${r.numerator}/${r.denominator}）`;
  }
}

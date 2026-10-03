import { DatePipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiFailure, ApiService } from '../../core/api.service';
import { JobStatus, JobView } from '../../core/models';
import { formatMs, STATUS_LABELS, statusLabel, STRATEGY_LABELS } from '../../shared/labels';

@Component({
  selector: 'app-jobs-page',
  imports: [FormsModule, RouterLink, DatePipe],
  templateUrl: './jobs.page.html',
})
export class JobsPage {
  private readonly api = inject(ApiService);
  readonly items = signal<JobView[]>([]);
  readonly nextCursor = signal<string | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  status: JobStatus | '' = '';
  readonly statuses = Object.keys(STATUS_LABELS) as JobStatus[];
  readonly statusLabel = statusLabel;
  readonly strategyLabels = STRATEGY_LABELS;
  readonly formatMs = formatMs;

  constructor() {
    this.load(null);
  }

  load(cursor: string | null) {
    this.loading.set(true);
    this.api.listJobs(this.status || null, cursor).subscribe({
      next: (r) => {
        this.items.set(cursor ? [...this.items(), ...r.items] : r.items);
        this.nextCursor.set(r.next_cursor);
        this.loading.set(false);
      },
      error: (e: ApiFailure) => {
        this.error.set(e.message);
        this.loading.set(false);
      },
    });
  }

  processingMs(j: JobView): number | null {
    const r = j.latest_run;
    if (!r?.started_at) return null;
    const end = r.finished_at ? new Date(r.finished_at).getTime() : Date.now();
    return end - new Date(r.started_at).getTime();
  }

  badgeClass(s: JobStatus): string {
    if (s === 'succeeded') return 'badge badge-ok';
    if (s === 'needs_review') return 'badge badge-warn';
    if (s === 'failed') return 'badge badge-error';
    return 'badge badge-info';
  }
}

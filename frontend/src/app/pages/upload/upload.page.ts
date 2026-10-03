import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiFailure, ApiService } from '../../core/api.service';
import { AppConfig, CreateJobsResponse } from '../../core/models';
import { formatBytes, statusLabel, STRATEGY_LABELS } from '../../shared/labels';

@Component({
  selector: 'app-upload-page',
  imports: [FormsModule, RouterLink],
  templateUrl: './upload.page.html',
})
export class UploadPage {
  private readonly api = inject(ApiService);
  readonly config = signal<AppConfig | null>(null);
  readonly file = signal<File | null>(null);
  readonly selectedArms = signal<Set<string>>(new Set(['A', 'B']));
  experimentId = '';
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly result = signal<CreateJobsResponse | null>(null);
  readonly formatBytes = formatBytes;
  readonly statusLabel = statusLabel;
  readonly strategyLabels = STRATEGY_LABELS;

  readonly fileProblem = computed(() => {
    const f = this.file();
    const c = this.config();
    if (!f || !c) return null;
    const ext = f.name.split('.').pop()?.toLowerCase() ?? '';
    if (ext === 'doc') return '舊式 .doc 不支援，請先另存為 .docx';
    if (!c.accepted_formats.includes(ext)) return `只接受 ${c.accepted_formats.join(' / ')} 格式`;
    if (f.size > c.max_upload_bytes) return `檔案 ${formatBytes(f.size)} 超過上限 ${formatBytes(c.max_upload_bytes)}`;
    if (f.size === 0) return '檔案為空';
    return null;
  });

  readonly canSubmit = computed(() => !!this.file() && !this.fileProblem() && this.selectedArms().size > 0 && !this.busy());

  constructor() {
    this.api.config().subscribe({ next: (c) => this.config.set(c), error: (e: ApiFailure) => this.error.set(e.message) });
  }

  onFile(event: Event) {
    const input = event.target as HTMLInputElement;
    this.file.set(input.files?.[0] ?? null);
    this.result.set(null);
    this.error.set(null);
  }

  toggleArm(arm: string, checked: boolean) {
    const next = new Set(this.selectedArms());
    if (checked) next.add(arm);
    else next.delete(arm);
    this.selectedArms.set(next);
  }

  submit() {
    const f = this.file();
    if (!f || !this.canSubmit()) return;
    this.busy.set(true);
    this.error.set(null);
    const key = `${crypto.randomUUID()}`;
    this.api.createJobs(f, [...this.selectedArms()].sort(), this.experimentId.trim() || null, key).subscribe({
      next: (r) => {
        this.result.set(r);
        this.busy.set(false);
      },
      error: (e: ApiFailure) => {
        // 503 enqueue_failed still carries the created jobs
        const body = e.error as unknown as CreateJobsResponse;
        if (e.status === 503 && body?.jobs) {
          this.result.set(body);
        }
        this.error.set(`${e.error.code}: ${e.error.message}${e.error.request_id ? `（request_id ${e.error.request_id}）` : ''}`);
        this.busy.set(false);
      },
    });
  }
}

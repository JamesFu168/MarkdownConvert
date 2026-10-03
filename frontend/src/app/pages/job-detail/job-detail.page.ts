import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { Component, DestroyRef, OnDestroy, computed, effect, inject, input, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';
import { environment } from '../../../environments/environment';
import { ApiFailure, ApiService } from '../../core/api.service';
import { Extraction, Ingredient, JobView, ResultResponse, RunView, isTerminal } from '../../core/models';
import { pollUntil } from '../../core/polling';
import { ERROR_LABELS, FIELD_STATUS_LABELS, STAGE_LABELS, STRATEGY_LABELS, errorLabel, formatMs, formatNumeric, statusLabel } from '../../shared/labels';

interface IngredientDraft {
  name: string;
  cas: string;
  conc_raw: string;
  comparator: string;
  min: string;
  max: string;
  unit: string;
}

@Component({
  selector: 'app-job-detail-page',
  imports: [FormsModule, RouterLink, DatePipe, NgTemplateOutlet],
  templateUrl: './job-detail.page.html',
  styleUrl: './job-detail.page.scss',
})
export class JobDetailPage implements OnDestroy {
  readonly jobId = input.required<string>();
  private readonly api = inject(ApiService);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly destroyRef = inject(DestroyRef);

  readonly job = signal<JobView | null>(null);
  readonly result = signal<ResultResponse | null>(null);
  readonly markdown = signal<string | null>(null);
  readonly pdfUrl = signal<SafeResourceUrl | null>(null);
  readonly error = signal<string | null>(null);
  readonly polling = signal(false);
  readonly tab = signal<'fields' | 'markdown' | 'original' | 'validation' | 'review' | 'runs'>('fields');
  readonly draft = signal<Extraction | null>(null);
  readonly ingredientDrafts = signal<IngredientDraft[]>([]);
  reviewNote = '';
  readonly saving = signal(false);
  readonly saveMessage = signal<string | null>(null);

  readonly run = computed<RunView | null>(() => this.job()?.latest_run ?? null);
  readonly isTerminal = computed(() => isTerminal(this.run()?.status));
  readonly extraction = computed<Extraction | null>(() => this.result()?.result?.extraction ?? null);
  readonly latestReview = computed(() => {
    const rs = this.result()?.reviews ?? [];
    return rs.length ? rs[rs.length - 1] : null;
  });

  readonly statusLabel = statusLabel;
  readonly stageLabels = STAGE_LABELS;
  readonly strategyLabels = STRATEGY_LABELS;
  readonly fieldStatusLabels = FIELD_STATUS_LABELS;
  fieldStatusLabel(s: string): string {
    return (FIELD_STATUS_LABELS as Record<string, string>)[s] ?? s;
  }
  readonly errorLabels = ERROR_LABELS;
  readonly errorLabel = errorLabel;
  readonly formatMs = formatMs;
  readonly formatNumeric = formatNumeric;

  private pollSub: Subscription | null = null;
  private objectUrl: string | null = null;

  constructor() {
    effect(() => {
      const id = this.jobId();
      this.reset();
      this.startPolling(id);
    });
  }

  private reset() {
    this.pollSub?.unsubscribe();
    this.job.set(null);
    this.result.set(null);
    this.markdown.set(null);
    this.revokePdf();
    this.error.set(null);
    this.draft.set(null);
    this.saveMessage.set(null);
  }

  private startPolling(id: string) {
    this.polling.set(true);
    this.pollSub = pollUntil(() => this.api.getJob(id), (j) => isTerminal(j.latest_run?.status ?? j.status), environment.pollIntervalMs)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (j) => {
          this.job.set(j);
          if (isTerminal(j.latest_run?.status ?? j.status)) {
            this.polling.set(false);
            this.loadResult(j);
          }
        },
        error: (e: ApiFailure) => {
          this.polling.set(false);
          this.error.set(`${e.error.code}: ${e.error.message}`);
        },
      });
  }

  private loadResult(j: JobView) {
    const run = j.latest_run;
    if (!run) return;
    this.api.getResult(j.job_id, run.run_id).subscribe({
      next: (r) => {
        this.result.set(r);
        if (r.ready && r.result) this.initDraft(r.result.extraction, r.reviews?.length ? r.reviews[r.reviews.length - 1].corrected : null);
        if (run.config?.input_strategy === 'markdown') {
          this.api.getArtifactText(j.job_id, run.run_id, 'markdown').subscribe({ next: (md) => this.markdown.set(md), error: () => this.markdown.set(null) });
        }
      },
      error: (e: ApiFailure) => this.error.set(`${e.error.code}: ${e.error.message}`),
    });
  }

  loadOriginal() {
    const j = this.job();
    const run = this.run();
    if (!j || !run || this.pdfUrl()) return;
    this.api.getArtifactBlob(j.job_id, run.run_id, 'original').subscribe({
      next: (blob) => {
        if (j.file_name.toLowerCase().endsWith('.pdf')) {
          this.objectUrl = URL.createObjectURL(blob);
          this.pdfUrl.set(this.sanitizer.bypassSecurityTrustResourceUrl(this.objectUrl));
        } else {
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = j.file_name;
          a.click();
          URL.revokeObjectURL(a.href);
        }
      },
      error: (e: ApiFailure) => this.error.set(e.message),
    });
  }

  downloadArtifact(kind: 'result' | 'validation' | 'model-input' | 'markdown') {
    const j = this.job();
    const run = this.run();
    if (!j || !run) return;
    this.api.getArtifactBlob(j.job_id, run.run_id, kind).subscribe({
      next: (blob) => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${j.job_id}-${kind}`;
        a.click();
        URL.revokeObjectURL(a.href);
      },
      error: (e: ApiFailure) => this.error.set(e.message),
    });
  }

  retry() {
    const j = this.job();
    if (!j) return;
    this.api.retry(j.job_id, '人工重試').subscribe({
      next: () => {
        this.reset();
        this.startPolling(j.job_id);
      },
      error: (e: ApiFailure) => this.error.set(`${e.error.code}: ${e.error.message}`),
    });
  }

  // ---- review editing
  private initDraft(machine: Extraction, corrected: Extraction | null) {
    const base: Extraction = structuredClone(corrected ?? machine);
    this.draft.set(base);
    this.ingredientDrafts.set(base.ingredients.map((i) => this.toDraft(i)));
  }

  private toDraft(i: Ingredient): IngredientDraft {
    return {
      name: i.name.value ?? '',
      cas: i.cas_numbers.value.join(', '),
      conc_raw: i.concentration.raw_text ?? '',
      comparator: i.concentration.value?.comparator ?? '',
      min: i.concentration.value?.min?.toString() ?? '',
      max: i.concentration.value?.max?.toString() ?? '',
      unit: i.concentration.value?.unit ?? '',
    };
  }

  addIngredient() {
    this.ingredientDrafts.set([...this.ingredientDrafts(), { name: '', cas: '', conc_raw: '', comparator: '', min: '', max: '', unit: '' }]);
  }

  removeIngredient(idx: number) {
    this.ingredientDrafts.set(this.ingredientDrafts().filter((_, i) => i !== idx));
  }

  setScalar(path: 'product_name' | 'revision_date' | 'supplier_name' | 'signal_word', value: string) {
    const d = this.draft();
    if (!d) return;
    const env = path === 'signal_word' ? d.hazards.signal_word : d.document[path];
    env.value = value.trim() || null;
    env.status = value.trim() ? 'found' : env.status === 'found' ? 'unreadable' : env.status;
    this.draft.set({ ...d });
  }

  setList(path: 'classifications' | 'h_codes' | 'p_codes', value: string) {
    const d = this.draft();
    if (!d) return;
    const items = value.split(/[,，;\n]/).map((s) => s.trim()).filter(Boolean);
    d.hazards[path].value = items;
    d.hazards[path].status = items.length ? 'found' : 'not_reported';
    this.draft.set({ ...d });
  }

  setFlashPointRaw(value: string) {
    const d = this.draft();
    if (!d) return;
    d.physical.flash_point.raw_text = value.trim() || null;
    this.draft.set({ ...d });
  }

  setFlashPointNumeric(key: 'comparator' | 'min' | 'max' | 'unit', value: string) {
    const d = this.draft();
    if (!d) return;
    const v = d.physical.flash_point.value ?? { comparator: null, min: null, max: null, unit: null, conditions: null };
    if (key === 'comparator') v.comparator = value || null;
    else if (key === 'unit') v.unit = value || null;
    else v[key] = value.trim() === '' ? null : Number(value);
    d.physical.flash_point.value = v;
    d.physical.flash_point.status = 'found';
    this.draft.set({ ...d });
  }

  private buildCorrected(): Extraction | null {
    const d = this.draft();
    if (!d) return null;
    const out: Extraction = structuredClone(d);
    const machine = this.extraction();
    out.ingredients = this.ingredientDrafts().map((dr, idx) => {
      const base: Ingredient = machine?.ingredients[idx]
        ? structuredClone(machine.ingredients[idx])
        : {
            name: { value: null, raw_text: null, status: 'unreadable', evidence: [] },
            cas_numbers: { value: [], raw_text: null, status: 'unreadable', evidence: [] },
            concentration: { value: null, raw_text: null, status: 'unreadable', evidence: [] },
          };
      base.name.value = dr.name.trim() || null;
      base.name.status = dr.name.trim() ? 'found' : 'unreadable';
      base.cas_numbers.value = dr.cas.split(/[,，;\s]+/).map((s) => s.trim()).filter(Boolean);
      base.cas_numbers.status = base.cas_numbers.value.length ? 'found' : 'not_reported';
      base.concentration.raw_text = dr.conc_raw.trim() || null;
      const hasNum = dr.comparator || dr.min !== '' || dr.max !== '';
      base.concentration.value = hasNum
        ? {
            comparator: dr.comparator || null,
            min: dr.min === '' ? null : Number(dr.min),
            max: dr.max === '' ? null : Number(dr.max),
            unit: dr.unit || null,
            basis: base.concentration.value?.basis ?? null,
          }
        : null;
      base.concentration.status = hasNum || dr.conc_raw.trim() ? 'found' : 'unreadable';
      return base;
    });
    return out;
  }

  saveReview() {
    const j = this.job();
    const run = this.run();
    const corrected = this.buildCorrected();
    if (!j || !run || !corrected) return;
    this.saving.set(true);
    this.saveMessage.set(null);
    this.api.saveReview(j.job_id, run.run_id, run.review_version, corrected, this.reviewNote.trim() || null).subscribe({
      next: (r) => {
        this.saving.set(false);
        this.saveMessage.set(`已保存人工修正版本 v${r.review_version}（變更 ${r.changed_paths.length} 個欄位）。機器抽取結果保持不變。`);
        this.reviewNote = '';
        this.api.getJob(j.job_id).subscribe((jj) => {
          this.job.set(jj);
          this.loadResult(jj);
        });
      },
      error: (e: ApiFailure) => {
        this.saving.set(false);
        this.saveMessage.set(
          e.status === 409 ? '版本衝突：此 run 已有較新的修正版本，請重新載入後再修改。' : `${e.error.code}: ${e.error.message}`,
        );
      },
    });
  }

  evidenceChecks(path: string) {
    return (this.result()?.validation?.evidence_checks ?? []).filter((c) => c.path.startsWith(path + '.'));
  }

  statusClass(s: string): string {
    if (s === 'succeeded' || s === 'done') return 'badge badge-ok';
    if (s === 'needs_review' || s === 'running' || s === 'skipped') return 'badge badge-warn';
    if (s === 'failed') return 'badge badge-error';
    return 'badge badge-info';
  }

  private revokePdf() {
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
    this.objectUrl = null;
    this.pdfUrl.set(null);
  }

  ngOnDestroy() {
    this.pollSub?.unsubscribe();
    this.revokePdf();
  }
}

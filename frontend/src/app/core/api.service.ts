import { HttpClient, HttpErrorResponse, HttpHeaders, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import {
  ApiError,
  AppConfig,
  CreateJobsResponse,
  ExperimentSummary,
  Extraction,
  JobList,
  JobStatus,
  JobView,
  ResultResponse,
} from './models';

export class ApiFailure extends Error {
  constructor(public readonly status: number, public readonly error: ApiError) {
    super(error.message);
  }
}

function toFailure(err: HttpErrorResponse): Observable<never> {
  const body = (err.error && typeof err.error === 'object' ? err.error : null) as ApiError | null;
  const apiError: ApiError = body?.code
    ? body
    : { code: err.status === 0 ? 'network_error' : 'http_error', message: err.status === 0 ? '無法連線到 API' : err.message, request_id: '' };
  return throwError(() => new ApiFailure(err.status, apiError));
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  readonly base = environment.apiBaseUrl.replace(/\/$/, '');

  config(): Observable<AppConfig> {
    return this.http.get<AppConfig>(`${this.base}/config`).pipe(catchError(toFailure));
  }

  createJobs(file: File, arms: string[], experimentId: string | null, idempotencyKey: string): Observable<CreateJobsResponse> {
    const form = new FormData();
    form.append('file', file, file.name);
    form.append('arms', arms.join(','));
    if (experimentId) form.append('experiment_id', experimentId);
    const headers = new HttpHeaders({ 'Idempotency-Key': idempotencyKey });
    return this.http.post<CreateJobsResponse>(`${this.base}/jobs`, form, { headers }).pipe(catchError(toFailure));
  }

  listJobs(status: JobStatus | null, cursor: string | null, limit = 20): Observable<JobList> {
    let params = new HttpParams().set('limit', limit);
    if (status) params = params.set('status', status);
    if (cursor) params = params.set('cursor', cursor);
    return this.http.get<JobList>(`${this.base}/jobs`, { params }).pipe(catchError(toFailure));
  }

  getJob(jobId: string): Observable<JobView> {
    return this.http.get<JobView>(`${this.base}/jobs/${encodeURIComponent(jobId)}`).pipe(catchError(toFailure));
  }

  getResult(jobId: string, runId: string): Observable<ResultResponse> {
    return this.http
      .get<ResultResponse>(`${this.base}/jobs/${encodeURIComponent(jobId)}/runs/${encodeURIComponent(runId)}/result`)
      .pipe(catchError(toFailure));
  }

  artifactUrl(jobId: string, runId: string, kind: 'original' | 'markdown' | 'model-input' | 'result' | 'validation'): string {
    return `${this.base}/jobs/${encodeURIComponent(jobId)}/runs/${encodeURIComponent(runId)}/artifacts/${kind}`;
  }

  getArtifactBlob(jobId: string, runId: string, kind: 'original' | 'markdown' | 'model-input' | 'result' | 'validation'): Observable<Blob> {
    return this.http.get(this.artifactUrl(jobId, runId, kind), { responseType: 'blob' }).pipe(catchError(toFailure));
  }

  getArtifactText(jobId: string, runId: string, kind: 'markdown' | 'model-input'): Observable<string> {
    return this.http.get(this.artifactUrl(jobId, runId, kind), { responseType: 'text' }).pipe(catchError(toFailure));
  }

  saveReview(jobId: string, runId: string, expectedVersion: number, corrected: Extraction, note: string | null) {
    return this.http
      .patch<{ run_id: string; review_version: number; changed_paths: string[] }>(
        `${this.base}/jobs/${encodeURIComponent(jobId)}/runs/${encodeURIComponent(runId)}/review`,
        { expected_review_version: expectedVersion, corrected, note },
      )
      .pipe(catchError(toFailure));
  }

  retry(jobId: string, reason: string | null) {
    return this.http
      .post<{ job_id: string; run_id: string; status: JobStatus }>(`${this.base}/jobs/${encodeURIComponent(jobId)}/retry`, { reason })
      .pipe(catchError(toFailure));
  }

  experimentSummary(experimentId: string): Observable<ExperimentSummary> {
    return this.http
      .get<ExperimentSummary>(`${this.base}/experiments/${encodeURIComponent(experimentId)}/summary`)
      .pipe(catchError(toFailure));
  }

  experimentExportUrl(experimentId: string): string {
    return `${this.base}/experiments/${encodeURIComponent(experimentId)}/export`;
  }
}

import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', redirectTo: 'upload', pathMatch: 'full' },
  { path: 'upload', loadComponent: () => import('./pages/upload/upload.page').then((m) => m.UploadPage), title: '上傳 SDS' },
  { path: 'jobs', loadComponent: () => import('./pages/jobs/jobs.page').then((m) => m.JobsPage), title: '任務列表' },
  { path: 'jobs/:jobId', loadComponent: () => import('./pages/job-detail/job-detail.page').then((m) => m.JobDetailPage), title: '任務詳情' },
  { path: 'compare', loadComponent: () => import('./pages/compare/compare.page').then((m) => m.ComparePage), title: 'A/B/C/D 比較' },
  { path: '**', redirectTo: 'upload' },
];

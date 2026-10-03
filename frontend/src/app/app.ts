import { Component, inject, signal } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { ApiService } from './core/api.service';
import { AppConfig } from './core/models';

@Component({
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  selector: 'app-root',
  styleUrl: './app.scss',
  templateUrl: './app.html',
})
export class App {
  private readonly api = inject(ApiService);
  readonly config = signal<AppConfig | null>(null);
  readonly configError = signal<string | null>(null);

  constructor() {
    this.api.config().subscribe({
      next: (c) => this.config.set(c),
      error: (e) => this.configError.set(e?.message ?? '無法載入設定'),
    });
  }
}

import { of } from 'rxjs';
import { describe, expect, it } from 'vitest';
import { isTerminal } from './models';
import { pollUntil } from './polling';

describe('pollUntil', () => {
  it('stops polling once the terminal predicate is true and emits the final value', async () => {
    const statuses = ['queued', 'processing', 'succeeded', 'succeeded'] as const;
    let calls = 0;
    const fetch = () => of({ status: statuses[Math.min(calls++, statuses.length - 1)] });
    const seen: string[] = [];
    await new Promise<void>((resolve) => {
      pollUntil(fetch, (v) => isTerminal(v.status), 1).subscribe({ next: (v) => seen.push(v.status), complete: resolve });
    });
    expect(seen).toEqual(['queued', 'processing', 'succeeded']);
    expect(calls).toBe(3);
  });
});

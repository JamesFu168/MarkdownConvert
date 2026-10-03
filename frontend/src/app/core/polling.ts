import { Observable, defer, timer } from 'rxjs';
import { exhaustMap, takeWhile } from 'rxjs/operators';

/**
 * Poll `fetch` every `intervalMs` while `isDone` is false. Emits each result (including the
 * final one) and completes. Callers unsubscribe on navigation so background polling stops.
 */
export function pollUntil<T>(fetch: () => Observable<T>, isDone: (value: T) => boolean, intervalMs: number): Observable<T> {
  return defer(() => timer(0, intervalMs).pipe(exhaustMap(() => fetch()))).pipe(takeWhile((v) => !isDone(v), true));
}

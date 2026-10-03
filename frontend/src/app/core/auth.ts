import { HttpInterceptorFn } from '@angular/common/http';
import { InjectionToken, inject } from '@angular/core';

/**
 * Supplies the bearer token sent to the Function App. Locally no token is needed (loopback
 * `local_dev` identity on the server). On Azure, plug in MSAL (acquireTokenSilent for
 * `api://<clientId>/user_impersonation`) by providing a different TOKEN_PROVIDER.
 * The browser never holds OpenAI keys, storage connection strings or Function keys.
 */
export interface TokenProvider {
  getToken(): Promise<string | null>;
}

export const TOKEN_PROVIDER = new InjectionToken<TokenProvider>('TOKEN_PROVIDER', {
  providedIn: 'root',
  factory: () => ({ getToken: async () => null }),
});

export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const provider = inject(TOKEN_PROVIDER);
  // Interceptors are synchronous here; a token provider resolving asynchronously must cache its token.
  const token = (provider as TokenProvider & { cachedToken?: string | null }).cachedToken ?? null;
  if (token) {
    return next(req.clone({ setHeaders: { Authorization: `Bearer ${token}` } }));
  }
  return next(req);
};

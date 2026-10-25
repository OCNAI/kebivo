/** Same-origin API boundary. CSRF material is intentionally held only in memory. */
export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly fields: { field: string; message: string }[] = [], public readonly code = '') {
    super(message);
    this.name = 'ApiError';
  }
}

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(`/api/v1/${path.replace(/^\/+/, '')}`, {
    ...init,
    method,
    headers,
    credentials: 'same-origin',
  });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('session-expired'));
    let message = `Request failed (${response.status})`;
    let fields: { field: string; message: string }[] = [];
    let code = '';
    try {
      const body = await response.json() as { detail?: string | { msg?: string }[]; error?: { message?: string; code?: string; details?: { fields?: { field?: string; message?: string }[] } } };
      const detail = Array.isArray(body.detail) ? body.detail.map((item: { msg?: string }) => item.msg).join('; ') : body.detail;
      message = body.error?.message ?? detail ?? message;
      fields = (body.error?.details?.fields ?? []).map((field) => ({ field: field.field ?? '', message: field.message ?? 'Invalid value' }));
      code = body.error?.code ?? '';
    } catch { /* Keep the useful status message when the response is not JSON. */ }
    throw new ApiError(response.status, message, fields, code);
  }
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
}

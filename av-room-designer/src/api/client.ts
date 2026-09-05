// Thin fetch wrapper against the existing QTcal backend. No new backend is
// introduced here — this talks to the same api/auth.py (login) and
// api/av_rooms.py (Phase 1 CRUD) routes as everything else in QTcal, using
// the same bearer-JWT pattern (see index.html's own `api()` helper, which
// this deliberately mirrors).
//
// API_BASE is read from VITE_API_BASE at build/dev time so this same bundle
// can point at localhost during development and the real QTcal deployment
// (staging/production) once built — see .env.example. Left as '' (relative)
// by default so that once this app is deployed alongside index.html on the
// same Vercel project/domain, no configuration is needed at all.
const API_BASE = import.meta.env.VITE_API_BASE || '';

const TOKEN_KEY = 'avrd_token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // Non-JSON response (rare — e.g. a gateway error page). Fall through
    // to the generic status-based error below.
  }

  if (!res.ok) {
    const message =
      (data && typeof data === 'object' && 'error' in (data as any) && (data as any).error) ||
      `Request failed (${res.status})`;
    // A 401 here means the JWT expired or was revoked server-side -- there's
    // no refresh-token flow, so the only correct move is to force a clean
    // logout rather than let the caller show a raw "unauthorized" error
    // inline (which is what used to happen: see the "can't create a room"
    // bug report). client.ts has no React context, so it can't navigate
    // directly -- it clears the stored token and fires a DOM event that
    // App.tsx listens for to drop back to the login screen with an
    // explanatory message. Only fires once per token (SESSION_EXPIRED_FLAG)
    // so a burst of already-in-flight requests doesn't spam the event.
    if (res.status === 401 && token && !sessionExpiredNotified) {
      sessionExpiredNotified = true;
      setToken(null);
      window.dispatchEvent(new CustomEvent(SESSION_EXPIRED_EVENT));
    }
    throw new ApiError(String(message), res.status);
  }
  return data as T;
}

export const SESSION_EXPIRED_EVENT = 'avrd:session-expired';
let sessionExpiredNotified = false;

// Reset the "already notified" guard on every successful login so a later
// expiry (in a new session) can trigger the flow again.
export function resetSessionExpiredGuard() {
  sessionExpiredNotified = false;
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
};

// ── Auth ─────────────────────────────────────────────────────────────────
export interface LoginResponse {
  token: string;
  user: { id: string; name: string; email: string; role: string };
  company: { id: string; name: string; slug: string };
}

export async function login(email: string, password: string): Promise<LoginResponse> {
  const res = await api.post<LoginResponse>('/api/auth/login', { email, password });
  setToken(res.token);
  resetSessionExpiredGuard();
  return res;
}

export function logout() {
  setToken(null);
}

// ── Products (catalog search, for device-to-product mapping) ───────────────
// Reuses the existing, already-live api/products.py search endpoint as-is --
// no backend changes needed. That route only requires a valid JWT (no
// avRoomDesigner feature/RBAC gate), so any logged-in user of this app can
// search the shared product catalog, same as the main QTcal app does.
export interface ProductSearchResult {
  id: string;
  brand: string;
  model: string;
  description: string | null;
  sku: string | null;
  category: string | null;
  default_cost: number;
  cost_currency: string;
  image_url: string | null;
}

export async function searchProducts(query: string, limit = 20): Promise<ProductSearchResult[]> {
  const params = new URLSearchParams();
  if (query.trim()) params.set('search', query.trim());
  params.set('limit', String(limit));
  const res = await api.get<{ products: ProductSearchResult[]; total: number }>(
    `/api/products?${params.toString()}`
  );
  return res.products || [];
}

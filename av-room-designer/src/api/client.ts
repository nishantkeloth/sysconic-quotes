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
    throw new ApiError(String(message), res.status);
  }
  return data as T;
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
  return res;
}

export function logout() {
  setToken(null);
}

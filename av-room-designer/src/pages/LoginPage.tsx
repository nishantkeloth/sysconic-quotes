import { useState } from 'react';
import { login, ApiError } from '../api/client';

export default function LoginPage({
  onLoggedIn,
  notice,
}: {
  onLoggedIn: () => void;
  notice?: string | null;
}) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(notice || null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
      onLoggedIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Login failed. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="avrd-center">
      <h1>AV Room Designer</h1>
      {error && <div className="avrd-error">{error}</div>}
      <form onSubmit={submit}>
        <div className="avrd-field">
          <label>Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
            required
          />
        </div>
        <div className="avrd-field">
          <label>Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>
        <button className="avrd-btn primary" type="submit" disabled={busy} style={{ width: '100%' }}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p style={{ fontSize: 12, color: 'var(--gray-500)', marginTop: 16 }}>
        Use your existing QTcal account. AV Room Designer must be enabled for your company.
      </p>
    </div>
  );
}

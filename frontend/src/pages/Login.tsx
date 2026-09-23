import { useState } from "react";
import { Button, ErrorBox, Field, inputClass } from "../components/ui";
import { post, setToken } from "../lib/api";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form
        className="w-full max-w-sm space-y-3 rounded-lg border border-line bg-surface p-6"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            const res = await post<{ token: string }>("/auth/login", { login, password, totp });
            setToken(res.token);
            onLogin();
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <h1 className="text-lg font-semibold">📈 Tradebot — вход</h1>
        <Field label="Логин">
          <input className={inputClass} value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="username" required />
        </Field>
        <Field label="Пароль">
          <input className={inputClass} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </Field>
        <Field label="Код 2FA" hint="6 цифр из приложения-аутентификатора">
          <input className={inputClass} value={totp} onChange={(e) => setTotp(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={6} />
        </Field>
        <ErrorBox error={error} />
        <Button variant="primary" className="w-full" disabled={busy}>
          {busy ? "Вход…" : "Войти"}
        </Button>
      </form>
    </div>
  );
}

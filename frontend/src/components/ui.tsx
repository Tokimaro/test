import { type ReactNode, useState } from "react";

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-line bg-surface p-4 ${className}`}>
      {(title || actions) && (
        <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="text-sm font-semibold text-ink-2">{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, hint, tone }: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: string;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone ?? "text-ink"}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-ink-2">{hint}</div>}
    </div>
  );
}

type Variant = "default" | "primary" | "danger";
const VARIANTS: Record<Variant, string> = {
  default: "border border-line bg-surface-2 text-ink hover:brightness-110",
  primary: "bg-accent text-white hover:brightness-110",
  danger: "bg-bad text-white hover:brightness-110",
};

export function Button({ variant = "default", className = "", ...props }:
  React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...props}
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    />
  );
}

export function Badge({ children, tone = "neutral" }: {
  children: ReactNode;
  tone?: "neutral" | "good" | "bad" | "warn" | "accent";
}) {
  const tones = {
    neutral: "bg-surface-2 text-ink-2",
    good: "bg-surface-2 text-good",
    bad: "bg-surface-2 text-bad",
    warn: "bg-surface-2 text-warn",
    accent: "bg-surface-2 text-accent",
  };
  return <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>{children}</span>;
}

export function Direction({ value }: { value: string | null }) {
  if (!value) return <span className="text-muted">—</span>;
  return value === "long" ? <Badge tone="good">▲ LONG</Badge> : <Badge tone="bad">▼ SHORT</Badge>;
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="rounded-md border border-bad/40 bg-surface-2 p-3 text-sm text-bad">⚠ {error}</div>;
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full min-w-max border-collapse text-sm [&_td]:border-t [&_td]:border-line [&_td]:px-2 [&_td]:py-1.5 [&_th]:px-2 [&_th]:py-1.5 [&_th]:text-left [&_th]:text-xs [&_th]:font-medium [&_th]:text-muted">
        {children}
      </table>
    </div>
  );
}

export function ConfirmButton({ label, confirmText, onConfirm, variant = "danger", disabled }: {
  label: string;
  confirmText: string;
  onConfirm: () => Promise<void> | void;
  variant?: Variant;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <Button variant={variant} disabled={disabled} onClick={() => setOpen(true)}>{label}</Button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" role="dialog">
          <div className="w-full max-w-sm rounded-lg border border-line bg-surface p-5">
            <p className="mb-4 text-sm">{confirmText}</p>
            <div className="flex justify-end gap-2">
              <Button onClick={() => setOpen(false)} disabled={busy}>Отмена</Button>
              <Button
                variant={variant}
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await onConfirm();
                  } finally {
                    setBusy(false);
                    setOpen(false);
                  }
                }}
              >
                Подтвердить
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-xs text-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "w-full rounded-md border border-line bg-surface-2 px-2 py-1.5 text-sm text-ink outline-none focus:border-accent";

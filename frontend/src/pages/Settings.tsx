import { useEffect, useState } from "react";
import { Badge, Button, Card, ErrorBox, Field, inputClass } from "../components/ui";
import { put } from "../lib/api";
import { useApi } from "../lib/useApi";

type Obj = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const PROFILES = [
  ["conservative", "Консервативный (0.5%)"],
  ["moderate", "Умеренный (1%)"],
  ["aggressive", "Агрессивный (2%)"],
  ["custom", "Свой"],
] as const;

const RISK_FIELDS: [string, string, number, number, number][] = [
  ["risk_per_trade_pct", "Риск на сделку, %", 0.1, 3, 0.1],
  ["max_open_positions", "Макс. позиций", 1, 20, 1],
  ["max_total_open_risk_pct", "Макс. суммарный риск, %", 0.5, 20, 0.5],
  ["daily_loss_limit_pct", "Дневной лимит убытка, %", 0.5, 20, 0.5],
  ["weekly_loss_limit_pct", "Недельный лимит убытка, %", 1, 40, 0.5],
  ["max_drawdown_stop_pct", "Остановка при просадке, %", 2, 50, 1],
  ["max_leverage", "Макс. плечо", 1, 20, 1],
  ["confidence_threshold", "Порог уверенности, %", 50, 95, 1],
];

const STOP_FIELDS: [string, string, number][] = [
  ["sl_atr_min", "SL мин., ATR", 0.1],
  ["sl_atr_max", "SL макс., ATR", 0.1],
  ["tp1_r", "TP1, R", 0.1],
  ["tp1_close_pct", "Закрыть на TP1, %", 5],
  ["tp2_r", "TP2, R", 0.1],
  ["trailing_atr", "Трейлинг, ATR", 0.1],
  ["min_rr", "Мин. R:R", 0.1],
  ["time_stop_bars", "Тайм-стоп, свечей", 1],
];

export default function SettingsPage() {
  const remote = useApi<Obj>("/settings");
  const secrets = useApi<{ encryption_enabled: boolean; bybit_api_key: string | null; env_bybit_api_key: string | null }>("/secrets");
  const [cfg, setCfg] = useState<Obj | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [keys, setKeys] = useState({ api_key: "", api_secret: "" });

  useEffect(() => {
    if (remote.data) setCfg(structuredClone(remote.data));
  }, [remote.data]);
  if (!cfg) return <ErrorBox error={remote.error} />;

  const custom = cfg.risk.profile === "custom";
  const setPath = (path: string[], value: unknown) =>
    setCfg((c) => {
      const next = structuredClone(c!);
      let o = next;
      for (const k of path.slice(0, -1)) o = o[k];
      o[path.at(-1)!] = value;
      return next;
    });

  const save = async () => {
    setMsg(null);
    try {
      const res = await put<{ config: Obj; restart_required: boolean }>("/settings", cfg);
      remote.setData(res.config);
      setMsg({ ok: true, text: res.restart_required ? "Сохранено. Изменения инструментов вступят в силу после перезапуска." : "Сохранено и применено." });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <>
      <Card title="Риск">
        <div className="mb-4 flex flex-wrap gap-2">
          {PROFILES.map(([id, label]) => (
            <Button key={id} variant={cfg.risk.profile === id ? "primary" : "default"} onClick={() => setPath(["risk", "profile"], id)}>
              {label}
            </Button>
          ))}
        </div>
        {!custom && <p className="mb-3 text-xs text-muted">Значения задаёт выбранный профиль. Чтобы менять их вручную, выберите «Свой».</p>}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {RISK_FIELDS.map(([key, label, min, max, step]) => (
            <Field key={key} label={label} hint={`${min}…${max}`}>
              <input className={inputClass} type="number" min={min} max={max} step={step} disabled={!custom}
                value={cfg.risk[key]} onChange={(e) => setPath(["risk", key], Number(e.target.value))} />
            </Field>
          ))}
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={cfg.risk.scale_by_confidence} onChange={(e) => setPath(["risk", "scale_by_confidence"], e.target.checked)} />
            Масштабировать риск по уверенности
          </label>
        </div>
      </Card>

      <Card title="Стопы и цели">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {STOP_FIELDS.map(([key, label, step]) => (
            <Field key={key} label={label}>
              <input className={inputClass} type="number" step={step} value={cfg.strategy.stops[key]}
                onChange={(e) => setPath(["strategy", "stops", key], Number(e.target.value))} />
            </Field>
          ))}
        </div>
      </Card>

      <Card title="Веса стратегий по режимам (сумма = 1)">
        <div className="grid gap-3 md:grid-cols-2">
          {(["trend", "range"] as const).map((regime) => (
            <div key={regime} className="grid grid-cols-3 gap-2">
              {(["trend", "mean_reversion", "breakout"] as const).map((k) => (
                <Field key={k} label={`${regime === "trend" ? "Тренд" : "Флэт"}: ${k}`}>
                  <input className={inputClass} type="number" step={0.05} min={0} max={1} value={cfg.strategy.weights[regime][k]}
                    onChange={(e) => setPath(["strategy", "weights", regime, k], Number(e.target.value))} />
                </Field>
              ))}
            </div>
          ))}
        </div>
      </Card>

      <Card title="Инструменты">
        {Object.entries(cfg.markets as Record<string, Obj>).map(([name, m]) => (
          <div key={name} className="mb-3 grid gap-2 md:grid-cols-[10rem_1fr]">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={m.enabled} onChange={(e) => setPath(["markets", name, "enabled"], e.target.checked)} />
              {name} <Badge>{m.category}</Badge>
            </label>
            <input className={inputClass} value={m.symbols.join(" ")}
              onChange={(e) => setPath(["markets", name, "symbols"], e.target.value.toUpperCase().split(/[\s,]+/).filter(Boolean))} />
          </div>
        ))}
      </Card>

      <div className="flex items-center gap-3">
        <Button variant="primary" onClick={save}>Сохранить настройки</Button>
        {msg && <span className={`text-sm ${msg.ok ? "text-good" : "text-bad"}`}>{msg.ok ? "✓" : "⚠"} {msg.text}</span>}
      </div>

      <Card title="API-ключи Bybit">
        <p className="mb-3 text-xs text-muted">
          Права ключа: только торговля, без вывода средств, с привязкой к IP сервера. Ключи хранятся зашифрованными
          (TB_MASTER_KEY); переменные окружения TB_BYBIT_API_KEY имеют приоритет. Применяются после перезапуска.
        </p>
        <p className="mb-3 text-sm">
          Сохранённый ключ: {secrets.data?.bybit_api_key ?? "нет"} · из окружения: {secrets.data?.env_bybit_api_key ?? "нет"}
        </p>
        {secrets.data && !secrets.data.encryption_enabled ? (
          <ErrorBox error="TB_MASTER_KEY не задан — хранение ключей в БД отключено" />
        ) : (
          <div className="flex flex-wrap items-end gap-3">
            <Field label="API key"><input className={inputClass} value={keys.api_key} onChange={(e) => setKeys({ ...keys, api_key: e.target.value })} autoComplete="off" /></Field>
            <Field label="API secret"><input className={inputClass} type="password" value={keys.api_secret} onChange={(e) => setKeys({ ...keys, api_secret: e.target.value })} autoComplete="off" /></Field>
            <Button onClick={async () => { await put("/secrets/bybit", keys); setKeys({ api_key: "", api_secret: "" }); await secrets.refresh(); }} disabled={!keys.api_key || !keys.api_secret}>
              Сохранить ключи
            </Button>
          </div>
        )}
      </Card>
    </>
  );
}

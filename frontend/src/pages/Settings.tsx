import { useEffect, useState } from "react";
import { Badge, Button, Card, ErrorBox, Field, inputClass } from "../components/ui";
import { put } from "../lib/api";
import { useApi } from "../lib/useApi";

type Obj = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const PROFILES = [
  ["conservative", "Консервативный (волатильность 15%)"],
  ["moderate", "Умеренный (25%, проверенный)"],
  ["aggressive", "Агрессивный (40%)"],
  ["custom", "Свой"],
] as const;

const WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];

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
        <p className="mb-3 text-xs text-muted">
          Размер позиций задаёт целевая волатильность: чем она выше, тем большую долю капитала стратегия держит в
          монетах (не больше 100% — спот без плеча). В проверке использовалось 25%.
        </p>
        <div className="mb-4 flex flex-wrap gap-2">
          {PROFILES.map(([id, label]) => (
            <Button key={id} variant={cfg.risk.profile === id ? "primary" : "default"} onClick={() => setPath(["risk", "profile"], id)}>
              {label}
            </Button>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          <Field label="Целевая волатильность, %" hint={custom ? "5…100" : "задаёт профиль"}>
            <input className={inputClass} type="number" min={5} max={100} step={1} disabled={!custom}
              value={cfg.risk.target_vol_pct} onChange={(e) => setPath(["risk", "target_vol_pct"], Number(e.target.value))} />
          </Field>
          <Field label="Макс. доля одной монеты, %" hint="100 — без ограничения">
            <input className={inputClass} type="number" min={1} max={100} step={1}
              value={cfg.risk.max_weight_pct} onChange={(e) => setPath(["risk", "max_weight_pct"], Number(e.target.value))} />
          </Field>
          <Field label="Всё в USDT при просадке, %" hint="0 — выключено (в проверке не использовалось)">
            <input className={inputClass} type="number" min={0} max={90} step={1}
              value={cfg.risk.max_drawdown_stop_pct} onChange={(e) => setPath(["risk", "max_drawdown_stop_pct"], Number(e.target.value))} />
          </Field>
        </div>
      </Card>

      <Card title="Стратегия: дневной тренд «лонг или кэш»">
        <p className="mb-3 text-xs text-muted">
          Значения по умолчанию — проверенная конфигурация (docs/strategy-selection.md). Изменения действуют с ближайшего
          расчёта; подбор параметров под историю повышает риск переобучения.
        </p>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          <Field label="Горизонты тренда, дней" hint="через пробел, по возрастанию">
            <input className={inputClass} value={cfg.strategy.lookbacks.join(" ")}
              onChange={(e) => setPath(["strategy", "lookbacks"], e.target.value.split(/[\s,]+/).filter(Boolean).map(Number))} />
          </Field>
          <Field label="Окно волатильности, дней">
            <input className={inputClass} type="number" min={5} max={365} value={cfg.strategy.vol_lookback}
              onChange={(e) => setPath(["strategy", "vol_lookback"], Number(e.target.value))} />
          </Field>
          <Field label="День ребалансировки (UTC)">
            <select className={inputClass} value={cfg.strategy.rebalance_weekday}
              onChange={(e) => setPath(["strategy", "rebalance_weekday"], Number(e.target.value))}>
              {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
            </select>
          </Field>
          <Field label="Мин. изменение доли, % капитала" hint="меньшие изменения не торгуются (экономия комиссий)">
            <input className={inputClass} type="number" min={0} max={20} step={0.5} value={cfg.strategy.min_trade_pct}
              onChange={(e) => setPath(["strategy", "min_trade_pct"], Number(e.target.value))} />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={cfg.strategy.btc_filter} onChange={(e) => setPath(["strategy", "btc_filter"], e.target.checked)} />
            Держать монеты только когда BTC выше средней (нужен BTCUSDT в списке)
          </label>
          <Field label="Средняя BTC, дней">
            <input className={inputClass} type="number" min={20} max={400} value={cfg.strategy.btc_ma_days} disabled={!cfg.strategy.btc_filter}
              onChange={(e) => setPath(["strategy", "btc_ma_days"], Number(e.target.value))} />
          </Field>
        </div>
      </Card>

      <Card title="Инструменты">
        {Object.entries(cfg.markets as Record<string, Obj>).map(([name, m]) => (
          <div key={name} className="mb-3 grid gap-2 md:grid-cols-[10rem_1fr]">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={m.enabled} onChange={(e) => setPath(["markets", name, "enabled"], e.target.checked)} />
              {name} <Badge>{m.category}</Badge>
              {m.category !== "spot" && <Badge tone="warn">не проверялся</Badge>}
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

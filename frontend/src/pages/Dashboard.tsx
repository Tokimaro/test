import { useState } from "react";
import { EquityChart } from "../components/charts";
import { Badge, Button, Card, ConfirmButton, ErrorBox, Stat } from "../components/ui";
import { post } from "../lib/api";
import { dateTime, num, pnlClass, price, signed } from "../lib/format";
import type { BusEvent, Status } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useEvents } from "../lib/useEvents";

const RANGES = [
  ["24ч", 86_400_000],
  ["7д", 7 * 86_400_000],
  ["30д", 30 * 86_400_000],
  ["Всё", 0],
] as const;

function describe(e: BusEvent): string {
  const d = e.data as Record<string, unknown>;
  switch (e.type) {
    case "trade_opened":
      return `Открыта ${d.symbol} ${String(d.direction).toUpperCase()} @ ${price(d.entry as number)} (уверенность ${num(d.confidence as number, 0)}%)`;
    case "trade_closed":
      return `Закрыта ${d.symbol}: ${signed(d.pnl as number)} USDT (${signed(d.r_multiple as number)}R), ${d.reason}`;
    case "trade_updated":
      return `${d.symbol}: ${d.tp1_done ? "TP1 исполнен" : `стоп → ${price(d.stop as number)} (${d.kind})`}`;
    case "signal":
      return `Сигнал ${d.symbol}: ${d.direction ?? "нет"} ${num(d.confidence as number, 0)}%${d.acted ? " → сделка" : d.reason ? ` (${d.reason})` : ""}`;
    case "alert":
      return `⚠ ${d.kind}`;
    case "bot_status":
      return `Статус: ${d.paused ? "пауза" : "работает"}${d.halted ? `, остановлен (${d.halt_reason})` : ""}`;
    default:
      return e.type;
  }
}

export default function Dashboard() {
  const status = useApi<Status>("/status", 15_000);
  const [range, setRange] = useState<number>(7 * 86_400_000);
  const from = range ? Date.now() - range : undefined;
  const equity = useApi<{ ts: number; equity: number | null }[]>(
    `/equity${from ? `?from_ms=${Math.floor(from / 60_000) * 60_000}` : ""}`,
    60_000,
  );
  const { events, connected } = useEvents((e) => {
    if (["trade_opened", "trade_closed", "bot_status"].includes(e.type)) void status.refresh();
  });
  const s = status.data;
  const eng = s?.engine;

  return (
    <>
      <ErrorBox error={status.error} />
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={s?.mode === "live" ? "bad" : "accent"}>
          {s?.mode === "live" ? "LIVE — реальные деньги" : `${s?.mode ?? "…"}${s?.testnet ? " · testnet" : ""}`}
        </Badge>
        {eng ? (
          eng.halted ? (
            <Badge tone="bad">⛔ Остановлен: {eng.halt_reason}</Badge>
          ) : eng.paused ? (
            <Badge tone="warn">⏸ Пауза{eng.circuit_breaker ? " (circuit breaker)" : ""}</Badge>
          ) : (
            <Badge tone="good">● Работает</Badge>
          )
        ) : (
          <Badge>Движок не запущен</Badge>
        )}
        <Badge tone={connected ? "good" : "warn"}>{connected ? "● Онлайн" : "○ Нет связи"}</Badge>
        <div className="ml-auto flex gap-2">
          {eng?.paused || eng?.halted ? (
            <Button onClick={async () => { await post("/control/resume"); await status.refresh(); }}>▶ Возобновить</Button>
          ) : (
            <Button disabled={!eng} onClick={async () => { await post("/control/pause"); await status.refresh(); }}>⏸ Пауза</Button>
          )}
          <ConfirmButton
            label="🛑 KILL SWITCH"
            disabled={!eng}
            confirmText="Отменить все ордера, закрыть ВСЕ позиции по рынку и остановить торговлю?"
            onConfirm={async () => {
              await post("/control/kill", { confirm: "KILL" });
              await status.refresh();
            }}
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Капитал, USDT" value={num(s?.equity)} hint={`обновлено ${dateTime(s?.equity_ts)}`} />
        <Stat label="PnL сегодня" value={signed(s?.day_pnl_pct, 2, "%")} tone={pnlClass(s?.day_pnl_pct)} hint={`неделя: ${signed(s?.week_pnl_pct, 2, "%")}`} />
        <Stat label="Просадка от пика" value={`${num(s?.drawdown_pct)}%`} hint={`нереализовано: ${signed(s?.unrealized)} USDT`} />
        <Stat label="Открытый риск" value={num(s?.open_risk)} hint={`позиций: ${eng?.open_positions ?? 0} · риск/сделка ${num(eng?.risk_pct, 2)}%`} />
      </div>

      <Card
        title="Капитал"
        actions={
          <div className="flex gap-1">
            {RANGES.map(([label, ms]) => (
              <button
                key={label}
                onClick={() => setRange(ms)}
                className={`rounded px-2 py-0.5 text-xs ${range === ms ? "bg-surface-2 text-ink" : "text-muted"}`}
              >
                {label}
              </button>
            ))}
          </div>
        }
      >
        <EquityChart points={equity.data ?? []} />
      </Card>

      <Card title="События в реальном времени">
        {events.length === 0 ? (
          <p className="text-sm text-muted">Ожидание событий…</p>
        ) : (
          <ul className="max-h-80 space-y-1 overflow-y-auto text-sm">
            {events.map((e, i) => (
              <li key={`${e.ts}-${i}`} className="flex gap-3">
                <span className="shrink-0 text-muted">{new Date(e.ts).toLocaleTimeString("ru-RU")}</span>
                <span className={e.type === "alert" ? "text-bad" : ""}>{describe(e)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

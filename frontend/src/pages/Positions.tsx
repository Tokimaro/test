import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { TrendBreakdown } from "../components/TrendBreakdown";
import { Badge, Card, ConfirmButton, ErrorBox, Table } from "../components/ui";
import { post } from "../lib/api";
import { dateTime, num, pct, pnlClass, price, signed } from "../lib/format";
import type { Holding } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useEvents } from "../lib/useEvents";

const SKIP_TEXT: Record<string, string> = {
  targets_pending: "доли по новому списку монет ещё не рассчитаны — дождитесь дневного расчёта или перезапустите бота",
  paused: "торговля на паузе",
  halted: "торговля остановлена (kill switch или просадка) — возобновите её",
  broker_error: "ошибка биржи, подробности в журнале",
};

function rebalanceText(results: Record<string, number | string>): string {
  return Object.values(results)
    .map((r) =>
      typeof r === "number"
        ? r > 0
          ? `отправлено ордеров: ${r}`
          : "портфель уже соответствует долям (изменения меньше порога) — сделок нет"
        : SKIP_TEXT[r] ?? r,
    )
    .join("; ");
}

/** Текущая доля монеты против целевой. */
function WeightBar({ current, target }: { current: number | null; target: number }) {
  const max = Math.max(0.25, current ?? 0, target);
  return (
    <div className="w-40" aria-label="Доля в портфеле">
      <div className="relative h-2 rounded bg-surface-2">
        <div className="absolute inset-y-0 left-0 rounded bg-accent" style={{ width: `${((current ?? 0) / max) * 100}%` }} />
        <div className="absolute -inset-y-1 w-0.5 bg-ink" style={{ left: `${(target / max) * 100}%` }} title={`Цель ${pct(target)}`} />
      </div>
      <div className="mt-1 text-xs text-muted">сейчас {pct(current)} · цель {pct(target)}</div>
    </div>
  );
}

export default function Positions() {
  const { data, error, refresh } = useApi<Holding[]>("/positions", 15_000);
  const [open, setOpen] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  useEvents((e) => {
    if (e.type.startsWith("trade_") || e.type === "rebalance" || e.type === "signal") void refresh();
  });
  const next = data?.[0]?.next_rebalance_ts;

  return (
    <Card
      title="Портфель"
      actions={
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted">следующая ребалансировка: {dateTime(next)}</span>
          <ConfirmButton
            label="Ребалансировать сейчас"
            variant="default"
            confirmText="Привести портфель к последним рассчитанным долям по рынку?"
            onConfirm={async () => {
              try {
                const r = await post<{ results: Record<string, number | string> }>("/control/rebalance");
                setNote(`Ребалансировка: ${rebalanceText(r.results)}`);
              } catch (e) {
                setNote(`Ребалансировка не выполнена: ${e instanceof Error ? e.message : String(e)}`);
              }
              await refresh();
            }}
          />
        </div>
      }
    >
      <p className="mb-3 text-xs text-muted">
        Стратегия держит монеты в растущем тренде и продаёт их, когда тренд пропадает. Доли пересчитываются каждый
        день по закрытию дневной свечи, сделки — раз в неделю. Стоп-лоссов нет: выход — по сигналу тренда.
      </p>
      {note && <p className="mb-3 rounded border border-line bg-surface-2 px-3 py-2 text-sm">{note}</p>}
      <ErrorBox error={error} />
      {data && data.length === 0 && <p className="text-sm text-muted">Движок не запущен</p>}
      {data && data.length > 0 && (
        <Table>
          <thead>
            <tr><th>Монета</th><th>Доля</th><th>Объём</th><th>Средняя цена</th><th>Цена</th><th>Стоимость</th><th>Результат</th><th>С</th><th /></tr>
          </thead>
          <tbody>
            {data.map((h) => (
              <Fragment key={h.symbol}>
                <tr className="cursor-pointer hover:bg-surface-2" onClick={() => setOpen(open === h.symbol ? null : h.symbol)}>
                  <td>
                    {h.trade_id ? <Link className="font-medium hover:text-accent" to={`/trades/${h.trade_id}`} onClick={(e) => e.stopPropagation()}>{h.symbol}</Link> : <span className="font-medium">{h.symbol}</span>}
                    <div className="mt-1">{h.target_weight > 0 ? <Badge tone="good">тренд вверх</Badge> : <Badge>кэш</Badge>}</div>
                  </td>
                  <td><WeightBar current={h.weight} target={h.target_weight} /></td>
                  <td>{h.qty ? num(h.qty, 4) : "—"}</td>
                  <td>{price(h.entry)}</td>
                  <td>{price(h.price)}</td>
                  <td>{h.value ? num(h.value) : "—"}</td>
                  <td className={pnlClass(h.unrealized)}>
                    {signed(h.unrealized)}<div className="text-xs">{signed(h.unrealized_pct, 2, "%")}</div>
                  </td>
                  <td className="text-xs">{dateTime(h.opened_ts)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {h.qty > 0 && (
                      <ConfirmButton label="Продать" confirmText={`Продать весь ${h.symbol} по рынку? На следующей ребалансировке стратегия решит заново.`} onConfirm={async () => { await post(`/positions/${h.symbol}/close`); await refresh(); }} />
                    )}
                  </td>
                </tr>
                {open === h.symbol && (
                  <tr><td colSpan={9} className="bg-surface-2"><div className="max-w-md p-2"><TrendBreakdown components={{ ...h.components, weight: h.target_weight, score: h.score }} /></div></td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

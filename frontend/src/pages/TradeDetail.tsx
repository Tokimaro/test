import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { CandleChart, type Level } from "../components/charts";
import { TrendBreakdown } from "../components/TrendBreakdown";
import { Card, ErrorBox, Stat, Table } from "../components/ui";
import { dateTime, num, pnlClass, price, REASONS, signed } from "../lib/format";
import type { Candle, TradeDetail as TD } from "../lib/types";
import { useApi } from "../lib/useApi";

const DAY = 86_400_000;

export default function TradeDetail() {
  const { id } = useParams();
  const { data: t, error } = useApi<TD>(`/trades/${id}`);
  const end = t ? (t.closed_ts ?? Date.now()) + 30 * DAY : null;
  const candles = useApi<Candle[]>(t && end ? `/candles/${t.symbol}?tf=D&limit=365&end_ms=${end}` : null);

  const levels = useMemo<Level[]>(() => {
    if (!t) return [];
    const out: Level[] = [];
    if (t.entry) out.push({ price: t.entry, title: "Средняя цена", tone: "accent" });
    if (t.exit) out.push({ price: t.exit, title: "Продажа", tone: (t.pnl ?? 0) > 0 ? "good" : "bad" });
    return out;
  }, [t]);
  const markers = useMemo(() => {
    if (!t?.opened_ts) return [];
    const m = [{ ts: t.opened_ts, position: "belowBar", text: "Покупка", tone: "accent" } as const];
    return t.closed_ts
      ? [...m, { ts: t.closed_ts, position: "aboveBar", text: "Продажа", tone: (t.pnl ?? 0) > 0 ? "good" : "bad" } as const]
      : [...m];
  }, [t]);

  if (error) return <ErrorBox error={error} />;
  if (!t) return <p className="text-sm text-muted">Загрузка…</p>;

  return (
    <>
      <div className="flex items-center gap-3">
        <Link to="/trades" className="text-sm text-muted hover:text-ink">← Сделки</Link>
        <h1 className="text-lg font-semibold">{t.symbol} #{t.id}</h1>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Результат" value={t.status === "closed" ? signed(t.pnl) : "держим"} tone={pnlClass(t.pnl)} hint={signed(t.return_pct, 2, "%")} />
        <Stat label="Средняя цена → продажа" value={`${price(t.entry)} → ${price(t.exit)}`} hint={t.close_reason ? REASONS[t.close_reason] ?? t.close_reason : undefined} />
        <Stat label="Вложено" value={`${num(t.invested)} USDT`} hint={`объём ${num(t.qty, 4)}`} />
        <Stat label="Целевая доля" value={t.target_weight !== null ? `${num(t.target_weight * 100, 1)}%` : "—"} hint={`дней: ${t.bars_held || "—"}`} />
      </div>
      <Card title="График (1D)">
        <CandleChart candles={candles.data ?? []} levels={levels} markers={markers} />
      </Card>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Сигнал при покупке">
          {t.signal ? <TrendBreakdown components={t.signal.components} /> : <p className="text-sm text-muted">Нет данных сигнала</p>}
        </Card>
        <Card title="Ордера">
          <Table>
            <thead><tr><th>Назначение</th><th>Тип</th><th>Объём</th><th>Цена</th><th>Статус</th><th>Время</th></tr></thead>
            <tbody>
              {t.orders.map((o) => (
                <tr key={o.link_id}>
                  <td>{o.purpose}</td><td>{o.type} {o.side}</td><td>{num(o.qty, 4)}</td>
                  <td>{price(o.price)}</td><td>{o.status}</td><td className="text-xs">{dateTime(o.ts)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      </div>
    </>
  );
}

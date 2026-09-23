import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { CandleChart, type Level } from "../components/charts";
import { SignalBreakdown } from "../components/SignalBreakdown";
import { Card, Direction, ErrorBox, Stat, Table } from "../components/ui";
import { dateTime, num, pnlClass, price, REASONS, signed } from "../lib/format";
import type { Candle, TradeDetail as TD } from "../lib/types";
import { useApi } from "../lib/useApi";

const H = 3_600_000;

export default function TradeDetail() {
  const { id } = useParams();
  const { data: t, error } = useApi<TD>(`/trades/${id}`);
  const end = t ? (t.closed_ts ?? Date.now()) + 24 * H : null;
  const candles = useApi<Candle[]>(t && end ? `/candles/${t.symbol}?tf=60&limit=300&end_ms=${end}` : null);

  const levels = useMemo<Level[]>(() => {
    if (!t) return [];
    const out: Level[] = [];
    if (t.entry) out.push({ price: t.entry, title: "Вход", tone: "accent" });
    out.push({ price: t.initial_stop, title: "SL", tone: "bad" });
    if (t.stop !== t.initial_stop) out.push({ price: t.stop, title: "SL тек.", tone: "warn" });
    if (t.tp1) out.push({ price: t.tp1, title: "TP1", tone: "good" });
    if (t.tp2) out.push({ price: t.tp2, title: "TP2", tone: "good" });
    return out;
  }, [t]);
  const markers = useMemo(() => {
    if (!t?.opened_ts) return [];
    const long = t.direction === "long";
    const m = [{ ts: t.opened_ts, position: long ? "belowBar" : "aboveBar", text: "Вход", tone: "accent" } as const];
    return t.closed_ts
      ? [...m, { ts: t.closed_ts, position: long ? "aboveBar" : "belowBar", text: "Выход", tone: (t.pnl ?? 0) > 0 ? "good" : "bad" } as const]
      : [...m];
  }, [t]);

  if (error) return <ErrorBox error={error} />;
  if (!t) return <p className="text-sm text-muted">Загрузка…</p>;

  return (
    <>
      <div className="flex items-center gap-3">
        <Link to="/trades" className="text-sm text-muted hover:text-ink">← Сделки</Link>
        <h1 className="text-lg font-semibold">{t.symbol} #{t.id}</h1>
        <Direction value={t.direction} />
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Результат" value={t.status === "closed" ? signed(t.pnl) : t.status} tone={pnlClass(t.pnl)} hint={signed(t.r_multiple, 2, "R")} />
        <Stat label="Вход → выход" value={`${price(t.entry)} → ${price(t.exit)}`} hint={t.close_reason ? REASONS[t.close_reason] ?? t.close_reason : undefined} />
        <Stat label="Риск" value={`${num(t.risk_amount)} USDT`} hint={`объём ${num(t.qty, 4)} · плечо ${t.leverage ?? "1"}×`} />
        <Stat label="Уверенность" value={`${num(t.confidence, 0)}%`} hint={`${t.strategy} · ${t.regime ?? ""}`} />
      </div>
      <Card title="График (1h)">
        <CandleChart candles={candles.data ?? []} levels={levels} markers={markers} />
      </Card>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Почему бот вошёл">
          {t.signal ? <SignalBreakdown components={t.signal.components} /> : <p className="text-sm text-muted">Нет данных сигнала</p>}
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

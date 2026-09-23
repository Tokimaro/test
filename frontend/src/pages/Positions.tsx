import { Link } from "react-router-dom";
import { Badge, Button, Card, ConfirmButton, Direction, ErrorBox, Table } from "../components/ui";
import { post } from "../lib/api";
import { dateTime, num, pnlClass, price, signed } from "../lib/format";
import type { OpenPosition } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useEvents } from "../lib/useEvents";

/** Шкала «стоп — вход — цели» с текущим положением цены. */
function LevelScale({ p }: { p: OpenPosition }) {
  const sign = p.direction === "long" ? 1 : -1;
  const risk = Math.abs(p.entry - p.stop) || 1;
  const current = p.unrealized_r !== null ? p.entry + sign * p.unrealized_r * risk : null;
  const lo = Math.min(p.stop, p.tp2, current ?? p.entry);
  const hi = Math.max(p.stop, p.tp2, current ?? p.entry);
  const pos = (v: number) => `${((v - lo) / (hi - lo || 1)) * 100}%`;
  const marks: [number, string, string][] = [
    [p.stop, "SL", "var(--bad)"],
    [p.entry, "Вход", "var(--text-2)"],
    ...(p.tp1 !== null ? [[p.tp1, "TP1", "var(--good)"] as [number, string, string]] : []),
    [p.tp2, "TP2", "var(--good)"],
  ];
  return (
    <div className="relative h-8 w-48" aria-label="Уровни позиции">
      <div className="absolute top-3 right-0 left-0 h-px bg-line" />
      {marks.map(([v, label, color]) => (
        <div key={label} className="absolute top-1 -translate-x-1/2 text-center text-[10px]" style={{ left: pos(v) }} title={`${label}: ${price(v)}`}>
          <div className="mx-auto h-4 w-0.5" style={{ background: color }} />
          <span className="text-muted">{label}</span>
        </div>
      ))}
      {current !== null && (
        <div className="absolute top-1.5 size-3 -translate-x-1/2 rounded-full border-2 border-surface bg-accent" style={{ left: pos(current) }} title={`Цена ≈ ${price(current)}`} />
      )}
    </div>
  );
}

export default function Positions() {
  const { data, error, refresh } = useApi<OpenPosition[]>("/positions", 10_000);
  useEvents((e) => {
    if (e.type.startsWith("trade_")) void refresh();
  });

  return (
    <Card title="Открытые позиции">
      <ErrorBox error={error} />
      {data && data.length === 0 && <p className="text-sm text-muted">Открытых позиций нет</p>}
      {data && data.length > 0 && (
        <Table>
          <thead>
            <tr>
              <th>Инструмент</th><th>Направление</th><th>Вход</th><th>Объём</th><th>Уровни</th>
              <th>PnL</th><th>Уверенность</th><th>Открыта</th><th />
            </tr>
          </thead>
          <tbody>
            {data.map((p) => (
              <tr key={p.trade_id}>
                <td>
                  <Link className="font-medium hover:text-accent" to={`/trades/${p.trade_id}`}>{p.symbol}</Link>
                  <div className="text-xs text-muted">{p.strategy} · {p.regime}</div>
                </td>
                <td><Direction value={p.direction} />{!p.confirmed && <div className="mt-1"><Badge tone="warn">не подтверждена</Badge></div>}</td>
                <td>{price(p.entry)}</td>
                <td>{num(p.remaining, 4)}{p.tp1_done && <div className="text-xs text-good">TP1 ✓</div>}</td>
                <td>
                  <LevelScale p={p} />
                  <div className="text-xs text-muted">SL {price(p.stop)} ({p.stop_kind})</div>
                </td>
                <td className={pnlClass(p.unrealized)}>
                  {signed(p.unrealized)}<div className="text-xs">{signed(p.unrealized_r, 2, "R")}</div>
                </td>
                <td>{num(p.confidence, 0)}%</td>
                <td className="text-xs">{dateTime(p.opened_ts)}<div className="text-muted">{p.bars_held} св.</div></td>
                <td className="space-x-1 whitespace-nowrap">
                  <Button onClick={async () => { await post(`/positions/${p.symbol}/breakeven`); await refresh(); }} disabled={!p.confirmed}>В БУ</Button>
                  <ConfirmButton label="Закрыть" confirmText={`Закрыть ${p.symbol} по рынку?`} disabled={!p.confirmed} onConfirm={async () => { await post(`/positions/${p.symbol}/close`); await refresh(); }} />
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

import { useState } from "react";
import { EquityChart } from "../components/charts";
import { Button, Card, ErrorBox, Field, inputClass, Stat, Table } from "../components/ui";
import { api, post } from "../lib/api";
import { dateTime, num, pnlClass, signed } from "../lib/format";
import type { GroupStats } from "../lib/types";
import { useApi } from "../lib/useApi";

interface Run {
  id: number;
  created_ts: number;
  symbols: string[];
  period_start: number;
  period_end: number;
  summary: GroupStats | null;
}
interface Detail {
  id: number;
  metrics: { summary: GroupStats };
  equity_curve: [number, number][];
}

export default function Backtests() {
  const list = useApi<{ jobs: Record<string, { status: string; error?: string }>; runs: Run[] }>("/backtests", 5_000);
  const [symbols, setSymbols] = useState("BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT TRXUSDT BCHUSDT");
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const running = Object.values(list.data?.jobs ?? {}).some((j) => j.status === "running");
  const failed = Object.values(list.data?.jobs ?? {}).filter((j) => j.status === "failed");

  return (
    <>
      <Card title="Новый бэктест">
        <p className="mb-3 text-xs text-muted">
          Нужны дневные свечи в БД: <code>python -m app.market.backfill --symbols … --days 1500</code> (стратегии
          нужно 120+ дней истории). Результат на истории не гарантирует будущую прибыль; параметры стратегии
          проверялись вне выборки на 2022–2026 (docs/research-strategies.md).
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Инструменты через пробел">
            <input className={`${inputClass} w-80`} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
          </Field>
          <Button
            variant="primary"
            disabled={running}
            onClick={async () => {
              setError(null);
              try {
                await post("/backtests", { symbols: symbols.split(/\s+/).filter(Boolean) });
                await list.refresh();
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
              }
            }}
          >
            {running ? "Выполняется…" : "Запустить"}
          </Button>
        </div>
        <div className="mt-2"><ErrorBox error={error ?? failed.at(-1)?.error ?? null} /></div>
      </Card>

      <Card title="Прогоны">
        <Table>
          <thead><tr><th>#</th><th>Когда</th><th>Монеты</th><th>Период</th><th>Доходность</th><th>CAGR</th><th>Sharpe</th><th>Макс. DD</th><th /></tr></thead>
          <tbody>
            {list.data?.runs.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td><td className="text-xs">{dateTime(r.created_ts)}</td><td>{r.symbols?.join(", ")}</td>
                <td className="text-xs">{dateTime(r.period_start)} — {dateTime(r.period_end)}</td>
                <td className={pnlClass(r.summary?.total_return_pct)}>{signed(r.summary?.total_return_pct, 2, "%")}</td>
                <td className={pnlClass(r.summary?.cagr_pct)}>{signed(r.summary?.cagr_pct, 1, "%")}</td>
                <td>{num(r.summary?.sharpe)}</td>
                <td>{num(r.summary?.max_drawdown_pct)}%</td>
                <td><Button onClick={async () => setDetail(await api<Detail>(`/backtests/${r.id}`))}>Открыть</Button></td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>

      {detail && (
        <Card title={`Прогон #${detail.id}`} actions={<Button onClick={() => setDetail(null)}>✕</Button>}>
          <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Доходность" value={signed(detail.metrics.summary.total_return_pct, 2, "%")} tone={pnlClass(detail.metrics.summary.total_return_pct)} />
            <Stat label="CAGR / Sharpe" value={signed(detail.metrics.summary.cagr_pct, 1, "%")} hint={`Sharpe ${num(detail.metrics.summary.sharpe)} · Sortino ${num(detail.metrics.summary.sortino)}`} />
            <Stat label="Макс. просадка" value={`${num(detail.metrics.summary.max_drawdown_pct)}%`} hint={`Calmar ${detail.metrics.summary.calmar ?? "—"}`} />
            <Stat label="Владений" value={detail.metrics.summary.trades} hint={`PF ${detail.metrics.summary.profit_factor ?? "—"} · средняя ${signed(detail.metrics.summary.avg_return_pct, 1, "%")}`} />
          </div>
          <EquityChart points={detail.equity_curve.map(([ts, equity]) => ({ ts, equity }))} />
        </Card>
      )}
    </>
  );
}

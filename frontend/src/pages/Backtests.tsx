import { useState } from "react";
import { EquityChart } from "../components/charts";
import { Badge, Button, Card, ErrorBox, Field, inputClass, Stat, Table } from "../components/ui";
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
  metrics: { summary: GroupStats; walk_forward?: { summary: GroupStats }; monte_carlo?: Record<string, number> };
  equity_curve: [number, number][];
}

export default function Backtests() {
  const list = useApi<{ jobs: Record<string, { status: string; error?: string }>; runs: Run[] }>("/backtests", 5_000);
  const [symbols, setSymbols] = useState("BTCUSDT ETHUSDT SOLUSDT");
  const [wf, setWf] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const running = Object.values(list.data?.jobs ?? {}).some((j) => j.status === "running");
  const failed = Object.values(list.data?.jobs ?? {}).filter((j) => j.status === "failed");

  return (
    <>
      <Card title="Новый бэктест">
        <p className="mb-3 text-xs text-muted">
          Нужна история в БД: <code>python -m app.market.backfill --symbols … --days 730</code>. Результат на
          истории не гарантирует будущую прибыль — ориентируйтесь на walk-forward (out-of-sample).
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Инструменты через пробел">
            <input className={`${inputClass} w-80`} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={wf} onChange={(e) => setWf(e.target.checked)} /> walk-forward
          </label>
          <Button
            variant="primary"
            disabled={running}
            onClick={async () => {
              setError(null);
              try {
                await post("/backtests", { symbols: symbols.split(/\s+/).filter(Boolean), walk_forward: wf });
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
          <thead><tr><th>#</th><th>Когда</th><th>Инструменты</th><th>Период</th><th>Сделок</th><th>Доходность</th><th>PF</th><th>Макс. DD</th><th /></tr></thead>
          <tbody>
            {list.data?.runs.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td><td className="text-xs">{dateTime(r.created_ts)}</td><td>{r.symbols?.join(", ")}</td>
                <td className="text-xs">{dateTime(r.period_start)} — {dateTime(r.period_end)}</td>
                <td>{r.summary?.trades ?? 0}</td>
                <td className={pnlClass(r.summary?.total_return_pct)}>{signed(r.summary?.total_return_pct, 2, "%")}</td>
                <td>{r.summary?.profit_factor ?? "—"}</td>
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
            <Stat label="Profit Factor" value={detail.metrics.summary.profit_factor ?? "—"} hint={`ожидание ${signed(detail.metrics.summary.expectancy_r, 3, "R")}`} />
            <Stat label="Макс. просадка" value={`${num(detail.metrics.summary.max_drawdown_pct)}%`} hint={detail.metrics.monte_carlo ? `Monte Carlo p95: ${num(detail.metrics.monte_carlo.dd_p95_pct)}%` : undefined} />
            <Stat
              label="Walk-forward (OOS)"
              value={detail.metrics.walk_forward ? signed(detail.metrics.walk_forward.summary.expectancy_r, 3, "R") : "—"}
              hint={detail.metrics.walk_forward ? <Badge tone={(detail.metrics.walk_forward.summary.expectancy_r ?? 0) > 0 ? "good" : "bad"}>{detail.metrics.walk_forward.summary.trades} сделок out-of-sample</Badge> : "не запускался"}
            />
          </div>
          <EquityChart points={detail.equity_curve.map(([ts, equity]) => ({ ts, equity }))} />
        </Card>
      )}
    </>
  );
}

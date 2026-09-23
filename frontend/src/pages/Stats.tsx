import { RHistogram, SignedBars } from "../components/charts";
import { Card, ErrorBox, Stat, Table } from "../components/ui";
import { num, pnlClass, REASONS, signed } from "../lib/format";
import type { GroupStats, Stats } from "../lib/types";
import { useApi } from "../lib/useApi";

const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

function Breakdown({ title, data, labels }: { title: string; data: Record<string, GroupStats>; labels?: Record<string, string> }) {
  return (
    <Card title={title}>
      <Table>
        <thead><tr><th>Группа</th><th>Сделок</th><th>Win rate</th><th>PF</th><th>Ожидание, R</th><th>PnL</th></tr></thead>
        <tbody>
          {Object.entries(data).map(([k, s]) => (
            <tr key={k}>
              <td>{labels?.[k] ?? k}</td><td>{s.trades}</td><td>{num(s.win_rate, 1)}%</td>
              <td>{s.profit_factor ?? "—"}</td>
              <td className={pnlClass(s.expectancy_r)}>{signed(s.expectancy_r, 3)}</td>
              <td className={pnlClass(s.net_pnl)}>{signed(s.net_pnl)}</td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

export default function StatsPage() {
  const { data, error } = useApi<Stats>("/stats");
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p className="text-sm text-muted">Загрузка…</p>;
  const s = data.summary;
  if (!s.trades) return <Card title="Статистика"><p className="text-sm text-muted">Закрытых сделок пока нет</p></Card>;

  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Сделок" value={s.trades} hint={`win rate ${num(s.win_rate, 1)}%`} />
        <Stat label="Profit Factor" value={s.profit_factor ?? "—"} hint={`ожидание ${signed(s.expectancy_r, 3, "R")}`} />
        <Stat label="Чистый PnL" value={signed(s.net_pnl)} tone={pnlClass(s.net_pnl)} hint={`комиссии ${num(s.fees)}`} />
        <Stat label="Макс. просадка" value={`${num(s.max_drawdown_pct)}%`} hint={`Sharpe ${num(s.sharpe)} · Sortino ${num(s.sortino)}`} />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Распределение результатов, R"><RHistogram values={data.r_distribution} /></Card>
        <Card title="Калибровка уверенности">
          <p className="mb-2 text-xs text-muted">Растёт ли фактический винрейт вместе с уверенностью — можно ли ей доверять.</p>
          <Table>
            <thead><tr><th>Уверенность</th><th>Сделок</th><th>Win rate</th><th>Ожидание, R</th></tr></thead>
            <tbody>
              {data.calibration.map((c) => (
                <tr key={c.bucket}><td>{c.bucket}%</td><td>{c.trades}</td><td>{num(c.win_rate, 1)}%</td><td className={pnlClass(c.expectancy_r)}>{signed(c.expectancy_r, 3)}</td></tr>
              ))}
            </tbody>
          </Table>
        </Card>
        <Card title="PnL по дням недели (по времени входа, UTC)">
          <SignedBars rows={WEEKDAYS.map((d, i) => ({ label: d, value: data.pnl_by_weekday[String(i)] ?? 0 }))} />
        </Card>
        <Breakdown title="По стратегиям" data={data.by_strategy} />
        <Breakdown title="По инструментам" data={data.by_symbol} />
        <Breakdown title="По режимам рынка" data={data.by_regime} />
        <Breakdown title="По причинам закрытия" data={data.by_close_reason} labels={REASONS} />
        <Breakdown title="По направлению" data={data.by_direction} />
      </div>
    </>
  );
}

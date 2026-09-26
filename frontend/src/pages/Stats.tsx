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
        <thead><tr><th>Группа</th><th>Владений</th><th>Прибыльных</th><th>PF</th><th>Ср. доходность</th><th>PnL</th></tr></thead>
        <tbody>
          {Object.entries(data).map(([k, s]) => (
            <tr key={k}>
              <td>{labels?.[k] ?? k}</td><td>{s.trades}</td><td>{num(s.win_rate, 1)}%</td>
              <td>{s.profit_factor ?? "—"}</td>
              <td className={pnlClass(s.avg_return_pct)}>{signed(s.avg_return_pct, 2, "%")}</td>
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
  if (!s.trades) return <Card title="Статистика"><p className="text-sm text-muted">Завершённых владений пока нет</p></Card>;

  return (
    <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Завершённых владений" value={s.trades} hint={`прибыльных ${num(s.win_rate, 1)}% · в среднем ${num(s.avg_days_held, 0)} дн.`} />
        <Stat label="Profit Factor" value={s.profit_factor ?? "—"} hint={`средняя доходность ${signed(s.avg_return_pct, 2, "%")}`} />
        <Stat label="Чистый PnL" value={signed(s.net_pnl)} tone={pnlClass(s.net_pnl)} hint={`комиссии ${num(s.fees)}`} />
        <Stat label="Макс. просадка" value={`${num(s.max_drawdown_pct)}%`} hint={`Sharpe ${num(s.sharpe)} · Sortino ${num(s.sortino)}`} />
      </div>
      <p className="text-xs text-muted">
        У трендовой стратегии прибыльных владений обычно меньше половины: она часто выходит с небольшим убытком,
        а зарабатывает на редких длинных трендах. Главное — profit factor и средняя доходность.
      </p>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Доходность владений, %">
          <RHistogram values={data.return_distribution} step={10} lo={-50} hi={100} unit="%" />
        </Card>
        <Card title="PnL по дням недели (по дате покупки, UTC)">
          <SignedBars rows={WEEKDAYS.map((d, i) => ({ label: d, value: data.pnl_by_weekday[String(i)] ?? 0 }))} />
        </Card>
        <Breakdown title="По монетам" data={data.by_symbol} />
        <Breakdown title="По причинам продажи" data={data.by_close_reason} labels={REASONS} />
      </div>
    </>
  );
}

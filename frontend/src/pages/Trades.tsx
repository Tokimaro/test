import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, Direction, ErrorBox, inputClass, Table } from "../components/ui";
import { download } from "../lib/api";
import { dateTime, num, pnlClass, price, REASONS, signed } from "../lib/format";
import type { Trade } from "../lib/types";
import { useApi } from "../lib/useApi";

const PAGE = 50;

export default function Trades() {
  const [filters, setFilters] = useState({ status: "", symbol: "", direction: "", strategy: "", result: "" });
  const [page, setPage] = useState(0);
  const query = useMemo(() => {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
    Object.entries(filters).forEach(([k, v]) => v && p.set(k, v));
    return p.toString();
  }, [filters, page]);
  const { data, error } = useApi<{ total: number; items: Trade[] }>(`/trades?${query}`);
  const set = (k: keyof typeof filters) => (e: React.ChangeEvent<HTMLSelectElement | HTMLInputElement>) => {
    setPage(0);
    setFilters((f) => ({ ...f, [k]: e.target.value }));
  };
  const pages = data ? Math.ceil(data.total / PAGE) : 0;

  return (
    <Card
      title={`История сделок${data ? ` (${data.total})` : ""}`}
      actions={
        <Button onClick={() => download(`/trades.csv?${new URLSearchParams(Object.entries(filters).filter(([k, v]) => v && ["status", "symbol"].includes(k)))}`, "trades.csv")}>
          ⬇ CSV
        </Button>
      }
    >
      <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
        <select className={inputClass} value={filters.status} onChange={set("status")}>
          <option value="">Все статусы</option><option value="open">Открытые</option><option value="closed">Закрытые</option><option value="cancelled">Отменённые</option>
        </select>
        <input className={inputClass} placeholder="Инструмент" value={filters.symbol} onChange={set("symbol")} />
        <select className={inputClass} value={filters.direction} onChange={set("direction")}>
          <option value="">Оба направления</option><option value="long">Long</option><option value="short">Short</option>
        </select>
        <select className={inputClass} value={filters.strategy} onChange={set("strategy")}>
          <option value="">Все стратегии</option><option value="trend">Тренд</option><option value="mean_reversion">Возврат к среднему</option><option value="breakout">Пробой</option>
        </select>
        <select className={inputClass} value={filters.result} onChange={set("result")}>
          <option value="">Любой результат</option><option value="win">Прибыльные</option><option value="loss">Убыточные</option>
        </select>
      </div>
      <ErrorBox error={error} />
      <Table>
        <thead>
          <tr><th>#</th><th>Инструмент</th><th>Напр.</th><th>Стратегия</th><th>Вход</th><th>Выход</th><th>PnL</th><th>R</th><th>Причина</th><th>Уверен.</th><th>Открыта</th></tr>
        </thead>
        <tbody>
          {data?.items.map((t) => (
            <tr key={t.id}>
              <td><Link className="text-accent" to={`/trades/${t.id}`}>{t.id}</Link></td>
              <td className="font-medium">{t.symbol}</td>
              <td><Direction value={t.direction} /></td>
              <td>{t.strategy}</td>
              <td>{price(t.entry)}</td>
              <td>{price(t.exit)}</td>
              <td className={pnlClass(t.pnl)}>{t.status === "closed" ? signed(t.pnl) : t.status}</td>
              <td className={pnlClass(t.r_multiple)}>{signed(t.r_multiple)}</td>
              <td>{t.close_reason ? REASONS[t.close_reason] ?? t.close_reason : "—"}</td>
              <td>{num(t.confidence, 0)}%</td>
              <td className="text-xs">{dateTime(t.opened_ts)}</td>
            </tr>
          ))}
        </tbody>
      </Table>
      {pages > 1 && (
        <div className="mt-3 flex items-center gap-2 text-sm">
          <Button disabled={page === 0} onClick={() => setPage(page - 1)}>←</Button>
          <span className="text-muted">{page + 1} / {pages}</span>
          <Button disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>→</Button>
        </div>
      )}
    </Card>
  );
}

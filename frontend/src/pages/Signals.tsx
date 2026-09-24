import { Fragment, useState } from "react";
import { TrendBreakdown } from "../components/TrendBreakdown";
import { Badge, Card, ErrorBox, inputClass, Table } from "../components/ui";
import { dateTime, num, rejectLabel } from "../lib/format";
import type { SignalRow } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useEvents } from "../lib/useEvents";

export default function Signals() {
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const query = filter === "long" ? "&with_direction=true" : filter === "rebalance" ? "&acted=true" : "";
  const { data, error, refresh } = useApi<SignalRow[]>(`/signals?limit=400${query}`);
  useEvents((e) => e.type === "signal" && void refresh());

  return (
    <Card
      title="Целевые доли (расчёт каждый день)"
      actions={
        <select className={`${inputClass} w-auto`} value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">Все</option>
          <option value="long">Только с долей &gt; 0</option>
          <option value="rebalance">Дни ребалансировки</option>
        </select>
      }
    >
      <ErrorBox error={error} />
      <Table>
        <thead><tr><th>Дата</th><th>Монета</th><th>Целевая доля</th><th>Сила тренда</th><th>Сделки</th></tr></thead>
        <tbody>
          {data?.map((s) => (
            <Fragment key={s.id}>
              <tr className="cursor-pointer hover:bg-surface-2" onClick={() => setOpen(open === s.id ? null : s.id)}>
                <td className="text-xs">{dateTime(s.ts)}</td>
                <td className="font-medium">{s.symbol}</td>
                <td>
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 w-16 rounded bg-surface-2"><div className="h-full rounded bg-accent" style={{ width: `${Math.min(100, s.weight_pct * 2)}%` }} /></div>
                    {num(s.weight_pct, 1)}%
                  </div>
                </td>
                <td>{s.score !== null ? `${num(s.score * 100, 0)}%` : "—"}</td>
                <td>{s.acted ? <Badge tone="good">ребалансировка</Badge> : <span className="text-xs text-ink-2">{rejectLabel(s.reject_reason)}</span>}</td>
              </tr>
              {open === s.id && (
                <tr><td colSpan={5} className="bg-surface-2"><div className="max-w-md p-2"><TrendBreakdown components={s.components} /></div></td></tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

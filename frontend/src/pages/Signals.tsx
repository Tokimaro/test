import { Fragment, useState } from "react";
import { SignalBreakdown } from "../components/SignalBreakdown";
import { Badge, Card, Direction, ErrorBox, inputClass, Table } from "../components/ui";
import { dateTime, num, rejectLabel } from "../lib/format";
import type { SignalRow } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useEvents } from "../lib/useEvents";

export default function Signals() {
  const [acted, setActed] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const { data, error, refresh } = useApi<SignalRow[]>(`/signals?limit=300${acted ? `&acted=${acted}` : ""}`);
  useEvents((e) => e.type === "signal" && void refresh());

  return (
    <Card
      title="Сигналы (включая отклонённые)"
      actions={
        <select className={`${inputClass} w-auto`} value={acted} onChange={(e) => setActed(e.target.value)}>
          <option value="">Все</option><option value="true">Исполненные</option><option value="false">Отклонённые</option>
        </select>
      }
    >
      <ErrorBox error={error} />
      <Table>
        <thead><tr><th>Время</th><th>Инструмент</th><th>Напр.</th><th>Уверенность</th><th>Режим</th><th>Итог</th></tr></thead>
        <tbody>
          {data?.map((s) => (
            <Fragment key={s.id}>
              <tr className="cursor-pointer hover:bg-surface-2" onClick={() => setOpen(open === s.id ? null : s.id)}>
                <td className="text-xs">{dateTime(s.ts)}</td>
                <td className="font-medium">{s.symbol}</td>
                <td><Direction value={s.direction} /></td>
                <td>
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 w-16 rounded bg-surface-2"><div className="h-full rounded bg-accent" style={{ width: `${s.confidence}%` }} /></div>
                    {num(s.confidence, 0)}%
                  </div>
                </td>
                <td>{s.regime}</td>
                <td>{s.acted ? <Badge tone="good">✓ сделка</Badge> : <span className="text-xs text-ink-2">{rejectLabel(s.reject_reason)}</span>}</td>
              </tr>
              {open === s.id && (
                <tr><td colSpan={6} className="bg-surface-2"><div className="max-w-xl p-2"><SignalBreakdown components={s.components} /></div></td></tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

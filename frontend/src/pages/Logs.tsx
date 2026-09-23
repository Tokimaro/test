import { Card, ErrorBox, Table } from "../components/ui";
import { dateTime } from "../lib/format";
import { useApi } from "../lib/useApi";

export default function Logs() {
  const { data, error } = useApi<{ id: number; ts: number; type: string; details: Record<string, unknown> }[]>(
    "/risk-events",
    30_000,
  );
  return (
    <Card title="События риска и сбои">
      <ErrorBox error={error} />
      {data?.length === 0 && <p className="text-sm text-muted">Событий нет</p>}
      <Table>
        <thead><tr><th>Время</th><th>Событие</th><th>Детали</th></tr></thead>
        <tbody>
          {data?.map((e) => (
            <tr key={e.id}>
              <td className="text-xs whitespace-nowrap">{dateTime(e.ts)}</td>
              <td className="font-medium">{e.type}</td>
              <td className="font-mono text-xs text-ink-2">{JSON.stringify(e.details)}</td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

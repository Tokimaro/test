import { num, signed } from "../lib/format";

/** Почему у монеты такая доля: доходность по горизонтам тренда, волатильность, фильтр BTC. */
export function TrendBreakdown({ components }: { components: Record<string, unknown> }) {
  const horizons = Object.entries(components)
    .filter(([k]) => /^ret_\d+d$/.test(k))
    .map(([k, v]) => [Number(k.slice(4, -1)), v as number | null] as const)
    .sort((a, b) => a[0] - b[0]);
  const weight = components.weight as number | undefined;
  const score = components.score as number | undefined;
  return (
    <div className="space-y-2 text-sm">
      {horizons.map(([days, ret]) => (
        <div key={days} className="flex justify-between gap-4">
          <span>Доходность за {days} дн.</span>
          <span className={ret === null ? "text-muted" : ret > 0 ? "text-good" : "text-bad"}>
            {ret === null ? "мало истории" : `${ret > 0 ? "▲" : "▼"} ${signed(ret, 2, "%")}`}
          </span>
        </div>
      ))}
      <div className="flex justify-between gap-4">
        <span>Волатильность (годовая)</span>
        <span>{num(components.vol_pct as number | null, 1)}%</span>
      </div>
      {"btc_above_ma" in components && (
        <div className="flex justify-between gap-4">
          <span>BTC выше средней</span>
          <span className={components.btc_above_ma ? "text-good" : "text-bad"}>{components.btc_above_ma ? "да" : "нет → кэш"}</span>
        </div>
      )}
      <div className="border-t border-line pt-2 text-xs text-ink-2">
        Сила тренда {num((score ?? 0) * 100, 0)}% (доля горизонтов с ростом) → целевая доля{" "}
        {weight !== undefined ? `${num(weight * 100, 1)}%` : "—"} капитала. Чем выше волатильность монеты,
        тем меньше её доля.
      </div>
    </div>
  );
}

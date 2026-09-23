/** Разбор уверенности сигнала: вклад каждой стратегии, множители MTF и фильтров. */
export function SignalBreakdown({ components }: { components: Record<string, unknown> }) {
  const scores = (components.scores ?? {}) as Record<string, number>;
  const weights = (components.weights ?? {}) as Record<string, number>;
  const reasons = (components.reasons ?? {}) as Record<string, Record<string, unknown>>;
  const filters = (components.filters ?? {}) as Record<string, number>;
  const names: Record<string, string> = { trend: "Тренд", mean_reversion: "Возврат к среднему", breakout: "Пробой" };
  return (
    <div className="space-y-2 text-sm">
      {Object.keys(scores).map((k) => {
        const score = scores[k] ?? 0;
        const parts = { ...((reasons[k]?.long ?? {}) as object), ...((reasons[k]?.short ?? {}) as object) };
        return (
          <div key={k}>
            <div className="flex justify-between">
              <span>{names[k] ?? k} <span className="text-muted">(вес {weights[k] ?? 0})</span></span>
              <span className={score > 0 ? "text-good" : score < 0 ? "text-bad" : "text-muted"}>
                {score > 0 ? "▲ " : score < 0 ? "▼ " : ""}{score.toFixed(2)}
              </span>
            </div>
            <div className="relative mt-1 h-1.5 rounded bg-surface-2">
              <div
                className="absolute inset-y-0 rounded"
                style={{
                  width: `${Math.abs(score) * 50}%`,
                  left: score >= 0 ? "50%" : `${50 - Math.abs(score) * 50}%`,
                  background: score >= 0 ? "var(--good)" : "var(--bad)",
                }}
              />
            </div>
            {Object.keys(parts).length > 0 && (
              <div className="mt-0.5 text-xs text-muted">{Object.keys(parts).join(", ")}</div>
            )}
          </div>
        );
      })}
      <div className="border-t border-line pt-2 text-xs text-ink-2">
        Итог до множителей: {String(components.raw ?? "—")} · старший ТФ ×{String(components.mtf ?? "—")}
        {Object.keys(filters).length > 0 && ` · фильтры: ${Object.entries(filters).map(([k, v]) => `${k} ×${v}`).join(", ")}`}
      </div>
    </div>
  );
}

export const num = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : v.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const price = (v: number | null | undefined): string => {
  if (v === null || v === undefined) return "—";
  const abs = Math.abs(v);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return v.toLocaleString("ru-RU", { maximumFractionDigits: digits });
};

export const signed = (v: number | null | undefined, digits = 2, suffix = ""): string =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${num(v, digits)}${suffix}`;

export const dateTime = (ms: number | null | undefined): string =>
  ms ? new Date(ms).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";

export const pnlClass = (v: number | null | undefined): string =>
  v === null || v === undefined || v === 0 ? "text-ink-2" : v > 0 ? "text-good" : "text-bad";

export const REASONS: Record<string, string> = {
  schedule: "Ребалансировка",
  signal: "Сигнал тренда",
  manual: "Вручную",
  kill: "Kill switch",
  drawdown_stop: "Стоп по просадке",
  external: "Продано вне бота",
  end: "Конец данных",
};

export const REJECTS: Record<string, string> = {
  not_rebalance_day: "не день ребалансировки",
  paused: "торговля на паузе",
};

export const rejectLabel = (r: string | null): string => {
  if (!r) return "";
  if (r.startsWith("halted")) return "бот остановлен";
  return REJECTS[r] ?? r;
};

export const WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"];

export const pct = (v: number | null | undefined, digits = 1): string =>
  v === null || v === undefined ? "—" : `${num(v * 100, digits)}%`;

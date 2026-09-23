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
  sl: "Стоп-лосс",
  be: "Безубыток",
  trailing: "Трейлинг",
  tp1: "TP1",
  tp2: "TP2",
  time: "Тайм-стоп",
  manual: "Вручную",
  kill: "Kill switch",
  end: "Конец данных",
};

export const REJECTS: Record<string, string> = {
  below_threshold: "уверенность ниже порога",
  position_open: "позиция уже открыта",
  position_exists: "позиция уже открыта",
  exchange_position_exists: "на бирже есть неучтённая позиция",
  max_open_positions: "лимит числа позиций",
  max_total_open_risk: "лимит суммарного риска",
  correlated_exposure: "коррелированные позиции",
  daily_loss_limit: "дневной лимит убытка",
  weekly_loss_limit: "недельный лимит убытка",
  paused: "торговля на паузе",
  market_closed: "рынок закрыт",
  order_uncertain: "исход ордера неизвестен",
  short_not_allowed: "шорт запрещён на рынке",
};

export const rejectLabel = (r: string | null): string => {
  if (!r) return "";
  if (r.startsWith("halted")) return "бот остановлен";
  if (r.startsWith("regime_")) return `режим ${r.slice(7)}`;
  if (r.startsWith("rr_too_low")) return "мало R:R";
  if (r.startsWith("size_")) return `объём: ${r.slice(5)}`;
  return REJECTS[r] ?? r;
};

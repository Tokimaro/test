import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,

  type IChartApi,
  LineStyle,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { Candle } from "../lib/types";

/** Цвета графиков берутся из CSS-токенов темы, поэтому светлая/тёмная темы согласованы. */
function tokens() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return {
    surface: v("--surface"),
    text: v("--muted"),
    grid: v("--grid"),
    axis: v("--axis"),
    accent: v("--accent"),
    good: v("--good"),
    bad: v("--bad"),
    warn: v("--warn"),
    ink2: v("--text-2"),
  };
}

function useThemeVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const bump = () => setVersion((x) => x + 1);
    mq.addEventListener("change", bump);
    return () => mq.removeEventListener("change", bump);
  }, []);
  return version;
}

const toTime = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;

function baseChart(el: HTMLElement, height: number): IChartApi {
  const t = tokens();
  return createChart(el, {
    height,
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: t.surface },
      textColor: t.text,
      fontSize: 11,
      attributionLogo: false,
    },
    grid: { vertLines: { visible: false }, horzLines: { color: t.grid } },
    rightPriceScale: { borderColor: t.axis },
    timeScale: { borderColor: t.axis, timeVisible: true },
    crosshair: { mode: 0 },
  });
}

export function EquityChart({ points, height = 260 }: {
  points: { ts: number; equity: number | null }[];
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  useEffect(() => {
    if (!ref.current) return;
    const t = tokens();
    const chart = baseChart(ref.current, height);
    const series = chart.addSeries(AreaSeries, {
      lineColor: t.accent,
      lineWidth: 2,
      topColor: `${t.accent}33`,
      bottomColor: `${t.accent}05`,
      priceLineVisible: false,
    });
    const seen = new Set<number>();
    series.setData(
      points
        .filter((p) => p.equity !== null)
        .map((p) => ({ time: toTime(p.ts), value: p.equity as number }))
        .filter((p) => (seen.has(p.time) ? false : (seen.add(p.time), true))),
    );
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, height, theme]);
  if (points.length < 2) {
    return <div className="flex h-40 items-center justify-center text-sm text-muted">Пока нет истории капитала</div>;
  }
  return <div ref={ref} aria-label="График капитала" />;
}

export interface Level {
  price: number;
  title: string;
  tone: "good" | "bad" | "warn" | "accent";
}

export function CandleChart({ candles, levels = [], markers = [], height = 420 }: {
  candles: Candle[];
  levels?: Level[];
  markers?: { ts: number; position: "aboveBar" | "belowBar"; text: string; tone: "good" | "bad" | "accent" }[];
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  useEffect(() => {
    if (!ref.current || candles.length === 0) return;
    const t = tokens();
    const chart = baseChart(ref.current, height);
    const series = chart.addSeries(CandlestickSeries, {
      upColor: t.good,
      downColor: t.bad,
      wickUpColor: t.good,
      wickDownColor: t.bad,
      borderVisible: false,
    });
    series.setData(
      candles.map((c) => ({ time: toTime(c.ts), open: c.open, high: c.high, low: c.low, close: c.close })),
    );
    for (const l of levels) {
      series.createPriceLine({
        price: l.price,
        color: t[l.tone],
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: l.title,
      });
    }
    const first = candles[0]!.ts;
    const ms: SeriesMarker<Time>[] = markers
      .filter((m) => m.ts >= first)
      .sort((a, b) => a.ts - b.ts)
      .map((m) => ({
        time: toTime(m.ts),
        position: m.position,
        color: t[m.tone],
        shape: m.position === "belowBar" ? "arrowUp" : "arrowDown",
        text: m.text,
      }));
    createSeriesMarkers(series, ms);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [candles, levels, markers, height, theme]);
  if (candles.length === 0) {
    return <div className="flex h-40 items-center justify-center text-sm text-muted">Нет свечей в БД</div>;
  }
  return <div ref={ref} aria-label="Свечной график" />;
}

/** Гистограмма результатов: прибыльные/убыточные корзины — статусными цветами. */
export function RHistogram({ values, height = 200, step = 0.5, lo = -3, hi = 4, unit = "R" }: {
  values: number[];
  height?: number;
  step?: number;
  lo?: number;
  hi?: number;
  unit?: string;
}) {
  if (values.length === 0) return <div className="text-sm text-muted">Нет закрытых сделок</div>;
  const buckets = new Map<number, number>();
  for (const v of values) {
    const b = Math.max(lo, Math.min(hi, Math.floor(v / step) * step));
    buckets.set(b, (buckets.get(b) ?? 0) + 1);
  }
  // все корзины между минимумом и максимумом, включая пустые — иначе ось X неравномерна
  const present = [...buckets.keys()];
  const keys: number[] = [];
  for (let k = Math.min(...present); k <= Math.max(...present) + 1e-9; k += step) {
    keys.push(Math.round(k / step) * step);
  }
  const max = Math.max(...buckets.values());
  const fmt = (x: number) => (Number.isInteger(step) ? x.toFixed(0) : x.toFixed(1));
  return (
    <div>
      <div className="flex items-end gap-0.5" style={{ height }} role="img" aria-label={`Распределение, ${unit}`}>
        {keys.map((k) => {
          const count = buckets.get(k) ?? 0;
          return (
            <div key={k} className="group relative flex h-full flex-1 flex-col justify-end">
              <div
                className="rounded-t-[4px]"
                style={{
                  height: `${(count / max) * 100}%`,
                  background: k >= 0 ? "var(--good)" : "var(--bad)",
                }}
              />
              <div className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-1 hidden -translate-x-1/2 whitespace-nowrap rounded border border-line bg-surface px-2 py-1 text-xs group-hover:block">
                {fmt(k)}…{fmt(k + step)}{unit}: {count}
              </div>
            </div>
          );
        })}
      </div>
      <div className="mt-1 flex gap-0.5 text-[10px] text-muted">
        {keys.map((k) => (
          <div key={k} className="flex-1 text-center">{fmt(k)}</div>
        ))}
      </div>
      <div className="mt-2 flex gap-4 text-xs text-ink-2">
        <span><span className="mr-1 inline-block size-2 rounded-sm" style={{ background: "var(--good)" }} />≥ 0{unit} (прибыль)</span>
        <span><span className="mr-1 inline-block size-2 rounded-sm" style={{ background: "var(--bad)" }} />&lt; 0{unit} (убыток)</span>
      </div>
    </div>
  );
}

/** Горизонтальные столбики со знаком (PnL по дням недели и т.п.) — с подписью значения. */
export function SignedBars({ rows }: { rows: { label: string; value: number }[] }) {
  const max = Math.max(1e-9, ...rows.map((r) => Math.abs(r.value)));
  return (
    <div className="space-y-1 text-xs">
      {rows.map((r) => (
        <div key={r.label} className="grid grid-cols-[3rem_1fr_5rem] items-center gap-2">
          <span className="text-ink-2">{r.label}</span>
          <div className="relative h-3">
            <div className="absolute inset-y-0 left-1/2 w-px bg-line" />
            <div
              className="absolute inset-y-0 rounded-[4px]"
              style={{
                background: r.value >= 0 ? "var(--good)" : "var(--bad)",
                width: `${(Math.abs(r.value) / max) * 50}%`,
                left: r.value >= 0 ? "50%" : undefined,
                right: r.value < 0 ? "50%" : undefined,
              }}
            />
          </div>
          <span className={`text-right ${r.value >= 0 ? "text-good" : "text-bad"}`}>
            {r.value >= 0 ? "+" : ""}{r.value.toFixed(2)}
          </span>
        </div>
      ))}
    </div>
  );
}


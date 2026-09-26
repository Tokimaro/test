export interface EngineStatus {
  paused: boolean;
  halted: boolean;
  halt_reason: string | null;
  open_positions: number;
  drawdown_pct: number;
  circuit_breaker: boolean;
  last_rebalance_ts: number | null;
  next_rebalance_ts: number;
  target_vol_pct: number;
}

export interface Status {
  mode: string;
  testnet: boolean;
  running: boolean;
  engine: EngineStatus | null;
  equity: number | null;
  unrealized: number | null;
  invested: number | null;
  equity_ts: number | null;
  drawdown_pct: number | null;
  markets: Record<string, { symbols: string[]; enabled: boolean; type: string }>;
}

/** Монета стратегии: текущее владение и целевая доля. */
export interface Holding {
  symbol: string;
  trade_id: number | null;
  qty: number;
  entry: number | null;
  price: number | null;
  value: number;
  weight: number | null;
  target_weight: number;
  score: number | null;
  unrealized: number | null;
  unrealized_pct: number | null;
  realized: number | null;
  opened_ts: number | null;
  components: Record<string, unknown>;
  next_rebalance_ts: number;
}

/** Владение монетой: от первой покупки до полной продажи. */
export interface Trade {
  id: number;
  symbol: string;
  direction: "long" | "short";
  strategy: string;
  status: string;
  entry: number | null;
  exit: number | null;
  qty: number;
  invested: number | null;
  return_pct: number | null;
  target_weight: number | null;
  pnl: number | null;
  close_reason: string | null;
  bars_held: number;
  opened_ts: number | null;
  closed_ts: number | null;
}

export interface SignalRow {
  id: number;
  ts: number;
  symbol: string;
  direction: "long" | null;
  weight_pct: number;
  score: number | null;
  rebalance: boolean;
  acted: boolean;
  reject_reason: string | null;
  components: Record<string, unknown>;
}

export interface TradeDetail extends Trade {
  orders: {
    link_id: string;
    purpose: string;
    side: string;
    type: string;
    qty: number;
    price: number | null;
    status: string;
    ts: number;
  }[];
  signal: SignalRow | null;
}

export interface Candle {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface GroupStats {
  trades: number;
  win_rate?: number;
  profit_factor?: number | null;
  avg_return_pct?: number;
  avg_win_return_pct?: number;
  avg_loss_return_pct?: number;
  best_return_pct?: number;
  worst_return_pct?: number;
  net_pnl?: number;
  fees?: number;
  avg_days_held?: number;
  max_drawdown_pct?: number;
  sharpe?: number;
  sortino?: number;
  calmar?: number | null;
  total_return_pct?: number;
  cagr_pct?: number;
}

export interface Stats {
  summary: GroupStats;
  by_symbol: Record<string, GroupStats>;
  by_close_reason: Record<string, GroupStats>;
  return_distribution: number[];
  pnl_by_weekday: Record<string, number>;
  pnl_by_hour: Record<string, number>;
}

export interface BusEvent {
  type: string;
  ts: number;
  data: Record<string, unknown>;
}

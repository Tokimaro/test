export interface EngineStatus {
  paused: boolean;
  halted: boolean;
  halt_reason: string | null;
  open_positions: number;
  risk_pct: number;
  drawdown_pct: number;
  circuit_breaker: boolean;
}

export interface Status {
  mode: string;
  testnet: boolean;
  running: boolean;
  engine: EngineStatus | null;
  equity: number | null;
  unrealized: number | null;
  open_risk: number | null;
  equity_ts: number | null;
  day_pnl_pct: number | null;
  week_pnl_pct: number | null;
  drawdown_pct: number | null;
  markets: Record<string, { symbols: string[]; enabled: boolean; type: string }>;
}

export interface OpenPosition {
  trade_id: number;
  symbol: string;
  direction: "long" | "short";
  strategy: string;
  entry: number;
  qty: number;
  remaining: number;
  stop: number;
  stop_kind: string;
  tp1: number | null;
  tp1_done: boolean;
  tp2: number;
  confidence: number;
  regime: string;
  opened_ts: number;
  bars_held: number;
  unrealized: number | null;
  unrealized_r: number | null;
  confirmed: boolean;
}

export interface Trade {
  id: number;
  symbol: string;
  direction: "long" | "short";
  strategy: string;
  status: string;
  regime: string | null;
  confidence: number;
  entry: number | null;
  exit: number | null;
  qty: number;
  initial_stop: number;
  stop: number;
  tp1: number | null;
  tp2: number | null;
  tp1_done: boolean;
  risk_amount: number;
  pnl: number | null;
  r_multiple: number | null;
  close_reason: string | null;
  bars_held: number;
  opened_ts: number | null;
  closed_ts: number | null;
  leverage: string | null;
}

export interface SignalRow {
  id: number;
  ts: number;
  symbol: string;
  direction: "long" | "short" | null;
  confidence: number;
  regime: string;
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
  expectancy_r?: number;
  net_pnl?: number;
  avg_r_win?: number;
  avg_r_loss?: number;
  max_drawdown_pct?: number;
  sharpe?: number;
  sortino?: number;
  calmar?: number | null;
  total_return_pct?: number;
  fees?: number;
  best_r?: number;
  worst_r?: number;
  avg_bars_held?: number;
}

export interface Stats {
  summary: GroupStats;
  by_strategy: Record<string, GroupStats>;
  by_symbol: Record<string, GroupStats>;
  by_regime: Record<string, GroupStats>;
  by_close_reason: Record<string, GroupStats>;
  by_direction: Record<string, GroupStats>;
  calibration: { bucket: string; trades: number; win_rate: number; expectancy_r: number }[];
  r_distribution: number[];
  pnl_by_weekday: Record<string, number>;
  pnl_by_hour: Record<string, number>;
}

export interface BusEvent {
  type: string;
  ts: number;
  data: Record<string, unknown>;
}

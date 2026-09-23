import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { getToken, setToken, subscribeUnauthorized } from "./lib/api";
import Backtests from "./pages/Backtests";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import Logs from "./pages/Logs";
import Positions from "./pages/Positions";
import SettingsPage from "./pages/Settings";
import Signals from "./pages/Signals";
import StatsPage from "./pages/Stats";
import TradeDetail from "./pages/TradeDetail";
import Trades from "./pages/Trades";

const NAV = [
  ["/", "Обзор"],
  ["/positions", "Позиции"],
  ["/trades", "Сделки"],
  ["/signals", "Сигналы"],
  ["/stats", "Статистика"],
  ["/backtests", "Бэктест"],
  ["/settings", "Настройки"],
  ["/logs", "Журнал"],
] as const;

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()));
  useEffect(() => subscribeUnauthorized(() => setAuthed(false)), []);

  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-line bg-surface/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-4 overflow-x-auto px-4 py-2">
          <span className="font-semibold whitespace-nowrap">📈 Tradebot</span>
          <nav className="flex gap-1 text-sm">
            {NAV.map(([to, label]) => (
              <NavLink
                key={to}
                to={to}
                end={to === "/"}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-md px-2.5 py-1 ${isActive ? "bg-surface-2 text-ink" : "text-ink-2 hover:text-ink"}`
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
          <button
            className="ml-auto text-sm whitespace-nowrap text-muted hover:text-ink"
            onClick={() => {
              setToken(null);
              setAuthed(false);
            }}
          >
            Выйти
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-7xl space-y-4 px-4 py-4">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/trades/:id" element={<TradeDetail />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="/backtests" element={<Backtests />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  );
}

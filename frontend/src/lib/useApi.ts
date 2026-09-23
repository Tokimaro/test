import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

/** Загрузка данных с перезапросом по refresh() и опциональным интервалом. */
export function useApi<T>(path: string | null, intervalMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    void refresh();
    if (!intervalMs) return;
    const t = setInterval(() => void refresh(), intervalMs);
    return () => clearInterval(t);
  }, [refresh, intervalMs]);

  return { data, error, loading, refresh, setData };
}

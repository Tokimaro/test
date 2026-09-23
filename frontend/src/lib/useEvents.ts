import { useEffect, useRef, useState } from "react";
import { getToken } from "./api";
import type { BusEvent } from "./types";

/** Поток событий движка по WebSocket с автоматическим переподключением. */
export function useEvents(onEvent?: (e: BusEvent) => void, keep = 50) {
  const [events, setEvents] = useState<BusEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;

    const connect = () => {
      const token = getToken();
      if (!token) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      // токен — первым сообщением, а не в URL (URL попадает в журналы сервера)
      ws = new WebSocket(`${proto}://${location.host}/api/ws`);
      ws.onopen = () => {
        ws?.send(JSON.stringify({ type: "auth", token }));
        retry = 1000;
      };
      ws.onmessage = (msg) => {
        const e = JSON.parse(msg.data) as BusEvent;
        if (e.type === "auth_ok") {
          setConnected(true);
          return;
        }
        if (e.type === "ping") return;
        handler.current?.(e);
        setEvents((prev) => [e, ...prev].slice(0, keep));
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) {
          timer = setTimeout(connect, retry);
          retry = Math.min(retry * 2, 30_000);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, [keep]);

  return { events, connected };
}

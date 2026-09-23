"""WebSocket /api/ws — поток событий движка для панели (раздел 9.2 плана).

Токен передаётся первым сообщением {"type": "auth", "token": ...}, а не в URL:
query-строки попадают в журналы веб-сервера и прокси. Срок действия токена
проверяется и после подключения — истёкший токен закрывает соединение.
"""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.api.context import AppContext

router = APIRouter()

AUTH_TIMEOUT_S = 5
PING_S = 25


@router.websocket("/api/ws")
async def events(ws: WebSocket) -> None:
    ctx: AppContext = ws.app.state.ctx
    await ws.accept()
    try:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=AUTH_TIMEOUT_S)
    except (TimeoutError, WebSocketDisconnect, ValueError):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    token = str(msg.get("token", "")) if isinstance(msg, dict) else ""
    if msg.get("type") != "auth" or ctx.tokens.verify(token) is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.send_json({"type": "auth_ok"})
    queue = ctx.bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=PING_S)
            except TimeoutError:
                event = None
            if ctx.tokens.verify(token) is None:
                await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            if event is None:
                await ws.send_json({"type": "ping"})  # держим соединение через прокси
            else:
                await ws.send_json({"type": event.type, "ts": event.ts, "data": event.data})
    except WebSocketDisconnect:
        pass
    finally:
        ctx.bus.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await ws.close()

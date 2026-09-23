"""WebSocket /api/ws — поток событий движка для панели (раздел 9.2 плана)."""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.api.context import AppContext

router = APIRouter()


@router.websocket("/api/ws")
async def events(ws: WebSocket) -> None:
    ctx: AppContext = ws.app.state.ctx
    # браузер не умеет ставить заголовки для WebSocket — токен приходит в query
    token = ws.query_params.get("token", "")
    if ctx.tokens.verify(token) is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    queue = ctx.bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
            except TimeoutError:
                await ws.send_json({"type": "ping"})  # держим соединение через прокси
                continue
            await ws.send_json({"type": event.type, "ts": event.ts, "data": event.data})
    except WebSocketDisconnect:
        pass
    finally:
        ctx.bus.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await ws.close()

"""Telegram: уведомления о сделках и алертах + команды управления (раздел 9 плана).

Команды принимаются ТОЛЬКО из чата TB_TELEGRAM_CHAT_ID:
/status, /positions, /pause, /resume, /kill CONFIRM
"""

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from app.config import Settings
from app.core.events import Event, EventBus

if TYPE_CHECKING:
    from app.api.context import AppContext

log = structlog.get_logger()

API = "https://api.telegram.org"
ALERT_ICONS = {"error": "🚨", "warning": "⚠️"}


def format_event(event: Event) -> str | None:
    d = event.data
    if event.type in ("trade_opened", "trade_updated"):
        icon = "🟢" if d.get("side") == "buy" else "🔻"
        verb = "Покупка" if d.get("side") == "buy" else "Продажа"
        return (
            f"{icon} {verb} {d.get('symbol')}: {d.get('qty', 0):.6g} @ {d.get('price', 0):.6g}\n"
            f"Целевая доля {float(d.get('weight') or 0) * 100:.1f}%"
        )
    if event.type == "trade_closed":
        pnl = float(d.get("pnl") or 0)
        icon = "✅" if pnl > 0 else "❌"
        return (
            f"{icon} Продано всё {d.get('symbol')}: {pnl:+.2f} USDT "
            f"({float(d.get('return_pct') or 0):+.2f}%) · {d.get('reason')}"
        )
    if event.type == "rebalance" and not d.get("skipped"):
        orders = d.get("orders") or []
        if not orders:
            return None
        lines = [f"{o['side']} {o['symbol']} {o['qty']:.6g} @ {o['price']:.6g}" for o in orders]
        return "🔄 Ребалансировка:\n" + "\n".join(lines)
    if event.type == "alert":
        icon = ALERT_ICONS.get(str(d.get("level")), "ℹ️")
        details = {k: v for k, v in d.items() if k not in ("level", "kind")}
        return f"{icon} {d.get('kind')}: {details}" if details else f"{icon} {d.get('kind')}"
    return None


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        bus: EventBus,
        ctx: "AppContext",
        transport: httpx.AsyncBaseTransport | None = None,
        admin_ids: list[int] | None = None,
    ) -> None:
        self._chat_id = str(chat_id)
        self._admins = {int(a) for a in admin_ids or []}
        self._bus = bus
        self._ctx = ctx
        self._http = httpx.AsyncClient(
            base_url=f"{API}/bot{token}", timeout=httpx.Timeout(35.0), transport=transport
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._offset = 0

    @classmethod
    def from_settings(
        cls, settings: Settings, bus: EventBus, ctx: "AppContext"
    ) -> "TelegramNotifier | None":
        token = settings.telegram_bot_token.get_secret_value()
        if not token or not settings.telegram_chat_id:
            return None
        return cls(
            token, settings.telegram_chat_id, bus, ctx, admin_ids=settings.telegram_admin_ids
        )

    async def start(self) -> None:
        queue = self._bus.subscribe()
        self._tasks = [
            asyncio.create_task(self._forward(queue)),
            asyncio.create_task(self._poll_commands()),
        ]
        await self.send("🤖 Бот запущен")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self._http.aclose()

    async def send(self, text: str) -> None:
        try:
            resp = await self._http.post(
                "/sendMessage", json={"chat_id": self._chat_id, "text": text[:4000]}
            )
            if resp.status_code != 200:
                log.warning("telegram.send_failed", status=resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("telegram.send_error", error=str(exc))

    async def _forward(self, queue: "asyncio.Queue[Event]") -> None:
        while True:
            event = await queue.get()
            text = format_event(event)
            if text:
                await self.send(text)

    def authorized(self, msg: dict[str, Any]) -> bool:
        """Команды — только из заданного чата; в группе — только от админов из списка."""
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != self._chat_id:
            return False
        if chat.get("type", "private") == "private":
            return True
        sender = (msg.get("from") or {}).get("id")
        return sender is not None and int(sender) in self._admins

    async def _poll_commands(self) -> None:
        while True:
            try:
                resp = await self._http.get(
                    "/getUpdates", params={"offset": self._offset, "timeout": 25}
                )
                body = resp.json() if resp.status_code == 200 else {}
                updates = body.get("result", []) if isinstance(body, dict) else []
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telegram.poll_error", error=str(exc))
                await asyncio.sleep(5)
                continue
            for upd in updates if isinstance(updates, list) else []:
                try:
                    self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message") or {}
                    if not self.authorized(msg):
                        continue
                    reply = await self.handle_command(str(msg.get("text", "")).strip())
                except Exception as exc:  # ошибка команды не должна отключать управление
                    log.exception("telegram.command_failed")
                    reply = f"⚠ Ошибка выполнения команды: {exc}"
                if reply:
                    await self.send(reply)

    async def handle_command(self, text: str) -> str | None:
        runtime = self._ctx.runtime
        engine = runtime.engine if runtime is not None else None
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/status":
            if engine is None:
                return "Движок не запущен"
            st: dict[str, Any] = engine.status()
            return (
                f"Режим: {self._ctx.settings.mode.value}\n"
                f"Пауза: {st['paused']} · Остановлен: {st['halted']} ({st['halt_reason']})\n"
                f"Монет в портфеле: {st['open_positions']} · "
                f"Целевая волатильность: {st['target_vol_pct']}%\n"
                f"Просадка: {st['drawdown_pct']}%"
            )
        if engine is None:
            return None if not cmd.startswith("/") else "Движок не запущен"
        if cmd == "/positions":
            if not engine.holdings:
                return "Портфель в кэше: монет нет"
            return "\n".join(
                f"{s}: {h.qty:g} @ {h.avg_entry:.6g}, цель {engine.targets.get(s, 0.0) * 100:.1f}%"
                for s, h in engine.holdings.items()
            )
        if cmd == "/pause":
            engine.pause()
            return "⏸ Новые входы приостановлены"
        if cmd == "/resume":
            engine.resume()
            return "▶️ Торговля возобновлена"
        if cmd == "/kill":
            if arg.strip() != "CONFIRM":
                return "Для аварийной остановки: /kill CONFIRM"
            await engine.kill_switch()
            return "🛑 Kill switch: ордера отменены, позиции закрыты, торговля остановлена"
        if cmd.startswith("/"):
            return "Команды: /status /positions /pause /resume /kill CONFIRM"
        return None

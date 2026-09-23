"""REST API панели (раздел 9 плана)."""

import asyncio
import csv
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Select, desc, func, select

from app.api.context import Ctx, User
from app.api.security import mask, verify_password, verify_totp
from app.backtest.engine import ClosedTrade
from app.backtest.metrics import breakdown, confidence_calibration, equity_stats, trade_stats
from app.core.runner import TRADING_CONFIG_KEY
from app.db.candles import SqlCandleStore, dt_to_ms
from app.db.models import (
    BacktestRunRow,
    EquitySnapshotRow,
    InstrumentRow,
    OrderRow,
    RiskEventRow,
    SecretRow,
    SettingRow,
    SignalRow,
    TradeRow,
    UserRow,
)
from app.domain import Timeframe
from app.trading_config import TradingConfig

router = APIRouter(prefix="/api")

BYBIT_KEY_SECRET = "bybit_api_key"
BYBIT_SECRET_SECRET = "bybit_api_secret"


def _num(v: Decimal | float | None) -> float | None:
    return float(v) if v is not None else None


def _ts(dt: datetime | None) -> int | None:
    return dt_to_ms(dt) if dt is not None else None


def _parse_time(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms is not None else None


# ---------------------------------------------------------------------- auth
class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str = Field(default="", max_length=10)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, ctx: Ctx) -> dict[str, Any]:
    ip = request.client.host if request.client else "?"
    keys = (f"login:{body.login}", f"ip:{ip}")
    if ctx.limiter.blocked(*keys):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "слишком много попыток, подождите")
    async with ctx.sm() as s:
        user = await s.scalar(select(UserRow).where(UserRow.login == body.login))
    ok = user is not None and verify_password(user.password_hash, body.password)
    if ok and user is not None and user.totp_secret:
        ok = verify_totp(user.totp_secret, body.totp)
    if not ok:
        ctx.limiter.fail(*keys)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "неверный логин, пароль или код 2FA")
    ctx.limiter.reset(*keys)
    return {"token": ctx.tokens.issue(body.login), "login": body.login}


@router.get("/auth/me")
async def me(user: User) -> dict[str, str]:
    return {"login": user}


# ---------------------------------------------------------------------- статус
@router.get("/status")
async def get_status(ctx: Ctx, user: User) -> dict[str, Any]:
    rt = ctx.runtime
    engine = rt.engine if rt is not None else None
    async with ctx.sm() as s:
        eq = await s.scalar(
            select(EquitySnapshotRow)
            .where(EquitySnapshotRow.mode == ctx.repo.mode)
            .order_by(desc(EquitySnapshotRow.ts))
            .limit(1)
        )
    risk = engine.risk if engine is not None else None
    return {
        "mode": ctx.settings.mode.value,
        "testnet": ctx.settings.bybit_testnet,
        "running": engine is not None,
        "engine": engine.status() if engine is not None else None,
        "equity": _num(eq.equity) if eq else None,
        "unrealized": _num(eq.unrealized_pnl) if eq else None,
        "open_risk": _num(eq.open_risk) if eq else None,
        "equity_ts": _ts(eq.ts) if eq else None,
        "day_pnl_pct": round(risk.day_pnl_pct(), 3) if risk else None,
        "week_pnl_pct": round(risk.week_pnl_pct(), 3) if risk else None,
        "drawdown_pct": round(risk.drawdown_pct(), 3) if risk else None,
        "markets": {
            name: {"symbols": m.symbols, "enabled": m.enabled, "type": m.market_type.value}
            for name, m in ctx.config.markets.items()
        },
    }


# ---------------------------------------------------------------------- позиции
@router.get("/positions")
async def positions(ctx: Ctx, user: User) -> list[dict[str, Any]]:
    engine = ctx.runtime.engine if ctx.runtime else None
    if engine is None:
        return []
    live: dict[str, Any] = {}
    for broker in engine.brokers.values():
        try:
            for p in await broker.get_positions():
                live[p.symbol] = p
        except Exception:  # панель не должна падать из-за биржи
            pass
    out = []
    for symbol, tr in engine.tracked.items():
        pos = tr.pos
        ex = live.get(symbol)
        risk = pos.risk_amount
        unreal = float(ex.unrealized_pnl) if ex is not None else None
        out.append(
            {
                "trade_id": tr.trade_id,
                "symbol": symbol,
                "direction": pos.direction.value,
                "strategy": pos.strategy,
                "entry": pos.entry,
                "qty": pos.qty,
                "remaining": pos.remaining,
                "stop": pos.stop,
                "stop_kind": pos.stop_kind.value,
                "tp1": pos.tp1,
                "tp1_done": pos.tp1_done,
                "tp2": pos.tp2,
                "confidence": pos.confidence,
                "regime": pos.regime,
                "opened_ts": pos.opened_ts,
                "bars_held": pos.bars_held,
                "unrealized": unreal,
                "unrealized_r": unreal / risk if unreal is not None and risk > 0 else None,
                "confirmed": tr.confirmed,
            }
        )
    return out


def _engine_or_409(ctx: Ctx) -> Any:
    engine = ctx.runtime.engine if ctx.runtime else None
    if engine is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "торговый движок не запущен")
    return engine


@router.post("/positions/{symbol}/close")
async def close_position(symbol: str, ctx: Ctx, user: User) -> dict[str, str]:
    engine = _engine_or_409(ctx)
    if symbol not in engine.tracked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет открытой позиции")
    await engine.close_manually(symbol)
    return {"status": "closing"}


@router.post("/positions/{symbol}/breakeven")
async def breakeven(symbol: str, ctx: Ctx, user: User) -> dict[str, str]:
    engine = _engine_or_409(ctx)
    if symbol not in engine.tracked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет открытой позиции")
    await engine.move_to_breakeven(symbol)
    return {"status": "ok"}


# ---------------------------------------------------------------------- сделки
def _trade_query(
    ctx: Ctx,
    status_: str | None,
    symbol: str | None,
    direction: str | None,
    strategy: str | None,
    result: str | None,
    from_ms: int | None,
    to_ms: int | None,
) -> Select[tuple[TradeRow, str]]:
    q = (
        select(TradeRow, InstrumentRow.symbol)
        .join(InstrumentRow, InstrumentRow.id == TradeRow.instrument_id)
        .where(TradeRow.mode == ctx.repo.mode)
    )
    if status_:
        q = q.where(TradeRow.status == status_)
    if symbol:
        q = q.where(InstrumentRow.symbol == symbol)
    if direction:
        q = q.where(TradeRow.direction == direction)
    if strategy:
        q = q.where(TradeRow.strategy == strategy)
    if result == "win":
        q = q.where(TradeRow.realized_pnl > 0)
    elif result == "loss":
        q = q.where(TradeRow.realized_pnl <= 0, TradeRow.status == "closed")
    if from_ms is not None:
        q = q.where(TradeRow.opened_at >= _parse_time(from_ms))
    if to_ms is not None:
        q = q.where(TradeRow.opened_at <= _parse_time(to_ms))
    return q


def _trade_dict(t: TradeRow, symbol: str) -> dict[str, Any]:
    extra = t.extra or {}
    return {
        "id": t.id,
        "symbol": symbol,
        "direction": t.direction,
        "strategy": t.strategy,
        "status": t.status,
        "regime": extra.get("regime"),
        "confidence": t.confidence,
        "entry": _num(t.entry_price),
        "exit": _num(t.exit_price),
        "qty": _num(t.qty),
        "initial_stop": _num(t.initial_stop),
        "stop": _num(t.stop_loss),
        "tp1": _num(t.tp1),
        "tp2": _num(t.tp2),
        "tp1_done": t.tp1_done,
        "risk_amount": _num(t.risk_amount),
        "pnl": _num(t.realized_pnl),
        "r_multiple": t.r_multiple,
        "close_reason": t.close_reason,
        "bars_held": t.bars_held,
        "opened_ts": _ts(t.opened_at),
        "closed_ts": _ts(t.closed_at),
        "leverage": extra.get("leverage"),
    }


@router.get("/trades")
async def trades(
    ctx: Ctx,
    user: User,
    status_: str | None = Query(None, alias="status"),
    symbol: str | None = None,
    direction: str | None = None,
    strategy: str | None = None,
    result: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    q = _trade_query(ctx, status_, symbol, direction, strategy, result, from_ms, to_ms)
    async with ctx.sm() as s:
        total = await s.scalar(select(func.count()).select_from(q.subquery()))
        rows = (await s.execute(q.order_by(desc(TradeRow.id)).limit(limit).offset(offset))).all()
    return {"total": total or 0, "items": [_trade_dict(t, sym) for t, sym in rows]}


@router.get("/trades.csv")
async def trades_csv(
    ctx: Ctx,
    user: User,
    status_: str | None = Query(None, alias="status"),
    symbol: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> StreamingResponse:
    q = _trade_query(ctx, status_, symbol, None, None, None, from_ms, to_ms)
    async with ctx.sm() as s:
        rows = (await s.execute(q.order_by(TradeRow.id))).all()
    buf = io.StringIO()
    items = [_trade_dict(t, sym) for t, sym in rows]
    fields = list(items[0].keys()) if items else ["id"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(items)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


@router.get("/trades/{trade_id}")
async def trade_detail(trade_id: int, ctx: Ctx, user: User) -> dict[str, Any]:
    async with ctx.sm() as s:
        row = (
            await s.execute(
                select(TradeRow, InstrumentRow.symbol)
                .join(InstrumentRow, InstrumentRow.id == TradeRow.instrument_id)
                .where(TradeRow.id == trade_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "сделка не найдена")
        trade, symbol = row
        orders = (
            await s.scalars(
                select(OrderRow).where(OrderRow.trade_id == trade_id).order_by(OrderRow.id)
            )
        ).all()
        signal = await s.get(SignalRow, trade.signal_id) if trade.signal_id else None
    return {
        **_trade_dict(trade, symbol),
        "orders": [
            {
                "link_id": o.link_id,
                "purpose": o.purpose,
                "side": o.side,
                "type": o.order_type,
                "qty": _num(o.qty),
                "price": _num(o.price),
                "status": o.status,
                "exchange_order_id": o.exchange_order_id,
                "ts": _ts(o.created_at),
            }
            for o in orders
        ],
        "signal": _signal_dict(signal, symbol) if signal else None,
    }


# ---------------------------------------------------------------------- сигналы
def _signal_dict(sg: SignalRow, symbol: str) -> dict[str, Any]:
    return {
        "id": sg.id,
        "ts": _ts(sg.ts),
        "symbol": symbol,
        "direction": sg.direction,
        "confidence": sg.confidence,
        "regime": sg.regime,
        "acted": sg.acted,
        "reject_reason": sg.reject_reason,
        "components": sg.components,
    }


@router.get("/signals")
async def signals(
    ctx: Ctx,
    user: User,
    symbol: str | None = None,
    acted: bool | None = None,
    with_direction: bool = True,
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    q = (
        select(SignalRow, InstrumentRow.symbol)
        .join(InstrumentRow, InstrumentRow.id == SignalRow.instrument_id)
        .where(SignalRow.mode == ctx.repo.mode)
    )
    if symbol:
        q = q.where(InstrumentRow.symbol == symbol)
    if acted is not None:
        q = q.where(SignalRow.acted == acted)
    if with_direction:
        q = q.where(SignalRow.direction.is_not(None))
    async with ctx.sm() as s:
        rows = (await s.execute(q.order_by(desc(SignalRow.id)).limit(limit))).all()
    return [_signal_dict(sg, sym) for sg, sym in rows]


# ---------------------------------------------------------------------- капитал и статистика
@router.get("/equity")
async def equity(
    ctx: Ctx, user: User, from_ms: int | None = None, to_ms: int | None = None
) -> list[dict[str, Any]]:
    q = select(EquitySnapshotRow).where(EquitySnapshotRow.mode == ctx.repo.mode)
    if from_ms is not None:
        q = q.where(EquitySnapshotRow.ts >= _parse_time(from_ms))
    if to_ms is not None:
        q = q.where(EquitySnapshotRow.ts <= _parse_time(to_ms))
    async with ctx.sm() as s:
        rows = (await s.scalars(q.order_by(EquitySnapshotRow.ts))).all()
    # прореживаем до ~2000 точек, чтобы график не тормозил
    step = max(1, len(rows) // 2000)
    sampled = list(rows[::step])
    if rows and sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return [
        {"ts": _ts(r.ts), "equity": _num(r.equity), "unrealized": _num(r.unrealized_pnl)}
        for r in sampled
    ]


def _as_closed(t: TradeRow, symbol: str) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        direction=t.direction,
        strategy=t.strategy,
        regime=str((t.extra or {}).get("regime", "")),
        confidence=t.confidence,
        entry_ts=_ts(t.opened_at) or 0,
        exit_ts=_ts(t.closed_at) or 0,
        entry=float(t.entry_price or 0),
        exit=float(t.exit_price or 0),
        qty=float(t.qty),
        pnl=float(t.realized_pnl),
        fees=float(t.fees),
        funding=0.0,
        risk_amount=float(t.risk_amount),
        r_multiple=float(t.r_multiple or 0.0),
        bars_held=t.bars_held,
        close_reason=t.close_reason or "",
    )


@router.get("/stats")
async def stats(
    ctx: Ctx, user: User, from_ms: int | None = None, to_ms: int | None = None
) -> dict[str, Any]:
    q = _trade_query(ctx, "closed", None, None, None, None, from_ms, to_ms)
    async with ctx.sm() as s:
        rows = (await s.execute(q.order_by(TradeRow.closed_at))).all()
        eq_rows = (
            await s.scalars(
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.mode == ctx.repo.mode)
                .order_by(EquitySnapshotRow.ts)
            )
        ).all()
    closed = [_as_closed(t, sym) for t, sym in rows]
    curve = [(_ts(r.ts) or 0, float(r.equity)) for r in eq_rows]
    eq_stats = equity_stats(curve[1:], curve[0][1], 60_000) if len(curve) > 2 else {}
    weekday: dict[str, float] = {}
    hour: dict[str, float] = {}
    for c in closed:
        dt = datetime.fromtimestamp(c.entry_ts / 1000, tz=UTC)
        weekday[str(dt.weekday())] = weekday.get(str(dt.weekday()), 0.0) + c.pnl
        hour[str(dt.hour)] = hour.get(str(dt.hour), 0.0) + c.pnl
    return {
        "summary": {**eq_stats, **trade_stats(closed)},
        "by_strategy": breakdown(closed, "strategy"),
        "by_symbol": breakdown(closed, "symbol"),
        "by_regime": breakdown(closed, "regime"),
        "by_close_reason": breakdown(closed, "close_reason"),
        "by_direction": breakdown(closed, "direction"),
        "calibration": confidence_calibration(closed),
        "r_distribution": [round(c.r_multiple, 3) for c in closed],
        "pnl_by_weekday": weekday,
        "pnl_by_hour": hour,
    }


# ---------------------------------------------------------------------- свечи для графика
@router.get("/candles/{symbol}")
async def candles(
    symbol: str,
    ctx: Ctx,
    user: User,
    tf: Timeframe = Timeframe.H1,
    limit: int = Query(500, ge=10, le=5000),
    end_ms: int | None = None,
) -> list[dict[str, float]]:
    store = SqlCandleStore(ctx.sm, broker="bybit")
    try:
        rows = await store.get_candles(symbol, tf, end_ms=end_ms, limit=limit)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет данных по инструменту") from exc
    return [
        {
            "ts": c.ts,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in rows
    ]


@router.get("/risk-events")
async def risk_events(
    ctx: Ctx, user: User, limit: int = Query(200, ge=1, le=1000)
) -> list[dict[str, Any]]:
    async with ctx.sm() as s:
        rows = (
            await s.scalars(select(RiskEventRow).order_by(desc(RiskEventRow.id)).limit(limit))
        ).all()
    return [{"id": r.id, "ts": _ts(r.ts), "type": r.type, "details": r.details} for r in rows]


# ---------------------------------------------------------------------- настройки
@router.get("/settings")
async def get_settings_(ctx: Ctx, user: User) -> dict[str, Any]:
    return ctx.config.model_dump(mode="json")


@router.put("/settings")
async def put_settings(body: dict[str, Any], ctx: Ctx, user: User) -> dict[str, Any]:
    try:
        cfg = TradingConfig.from_dict(body)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    dumped = cfg.model_dump(mode="json")
    await ctx.repo.set_setting(TRADING_CONFIG_KEY, dumped, updated_by=user)
    restart = False
    engine = ctx.runtime.engine if ctx.runtime else None
    if engine is not None:
        restart = await engine.apply_config(cfg)
    ctx.config = cfg
    return {"config": dumped, "restart_required": restart}


@router.get("/settings/history")
async def settings_history(
    ctx: Ctx, user: User, limit: int = Query(50, ge=1, le=500)
) -> list[dict[str, Any]]:
    async with ctx.sm() as s:
        rows = (
            await s.scalars(select(SettingRow).order_by(desc(SettingRow.id)).limit(limit))
        ).all()
    return [
        {
            "id": r.id,
            "key": r.key,
            "updated_by": r.updated_by,
            "ts": _ts(r.updated_at),
            "value": r.value,
        }
        for r in rows
    ]


class BybitKeysIn(BaseModel):
    api_key: str = Field(min_length=4, max_length=128)
    api_secret: str = Field(min_length=4, max_length=256)


@router.get("/secrets")
async def get_secrets(ctx: Ctx, user: User) -> dict[str, Any]:
    async with ctx.sm() as s:
        row = await s.get(SecretRow, BYBIT_KEY_SECRET)
    key = ctx.secrets.decrypt(row.ciphertext) if row else None
    env_key = ctx.settings.bybit_api_key.get_secret_value()
    return {
        "encryption_enabled": ctx.secrets.enabled,
        "bybit_api_key": mask(key) if key else None,
        "env_bybit_api_key": mask(env_key) if env_key else None,
    }


@router.put("/secrets/bybit")
async def put_bybit_keys(body: BybitKeysIn, ctx: Ctx, user: User) -> dict[str, Any]:
    if not ctx.secrets.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "задайте TB_MASTER_KEY для хранения ключей")
    async with ctx.sm() as s, s.begin():
        for name, value in (
            (BYBIT_KEY_SECRET, body.api_key),
            (BYBIT_SECRET_SECRET, body.api_secret),
        ):
            row = await s.get(SecretRow, name)
            ciphertext = ctx.secrets.encrypt(value)
            if row is None:
                s.add(SecretRow(name=name, ciphertext=ciphertext))
            else:
                row.ciphertext = ciphertext
    return {"bybit_api_key": mask(body.api_key), "restart_required": True}


# ---------------------------------------------------------------------- управление
class KillIn(BaseModel):
    confirm: str


@router.post("/control/pause")
async def pause(ctx: Ctx, user: User) -> dict[str, Any]:
    engine = _engine_or_409(ctx)
    engine.pause()
    return dict(engine.status())


@router.post("/control/resume")
async def resume(ctx: Ctx, user: User) -> dict[str, Any]:
    engine = _engine_or_409(ctx)
    engine.resume()
    return dict(engine.status())


@router.post("/control/kill")
async def kill(body: KillIn, ctx: Ctx, user: User) -> dict[str, Any]:
    if body.confirm != "KILL":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'для подтверждения передайте "KILL"')
    engine = _engine_or_409(ctx)
    await engine.kill_switch()
    return dict(engine.status())


# ---------------------------------------------------------------------- бэктесты
class BacktestIn(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=20)
    market: str = "crypto"
    equity: float = Field(10_000, gt=0)
    walk_forward: bool = False


@router.get("/backtests")
async def backtests(ctx: Ctx, user: User) -> dict[str, Any]:
    async with ctx.sm() as s:
        rows = (
            await s.scalars(select(BacktestRunRow).order_by(desc(BacktestRunRow.id)).limit(50))
        ).all()
    return {
        "jobs": ctx.jobs,
        "runs": [
            {
                "id": r.id,
                "created_ts": _ts(r.created_at),
                "symbols": (r.params or {}).get("symbols"),
                "period_start": _ts(r.period_start),
                "period_end": _ts(r.period_end),
                "summary": (r.metrics or {}).get("summary"),
            }
            for r in rows
        ],
    }


@router.get("/backtests/{run_id}")
async def backtest_detail(run_id: int, ctx: Ctx, user: User) -> dict[str, Any]:
    async with ctx.sm() as s:
        r = await s.get(BacktestRunRow, run_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "прогон не найден")
    return {
        "id": r.id,
        "params": r.params,
        "metrics": r.metrics,
        "equity_curve": r.equity_curve,
        "trades": r.trades,
    }


@router.post("/backtests", status_code=status.HTTP_202_ACCEPTED)
async def start_backtest(body: BacktestIn, ctx: Ctx, user: User) -> dict[str, str]:
    import argparse

    from app.backtest import cli

    if any(j["status"] == "running" for j in ctx.jobs.values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "бэктест уже выполняется")
    job_id = uuid.uuid4().hex[:8]
    ctx.jobs[job_id] = {"status": "running", "symbols": body.symbols}
    args = argparse.Namespace(
        symbols=body.symbols, market=body.market, equity=body.equity, walk_forward=body.walk_forward
    )

    async def job() -> None:
        try:
            # вычисления тяжёлые — в отдельном потоке со своим циклом, чтобы не мешать торговле
            await asyncio.to_thread(asyncio.run, cli.main_async(args))
            ctx.jobs[job_id]["status"] = "done"
        except (Exception, SystemExit) as exc:  # SystemExit из CLI — понятная ошибка данных
            ctx.jobs[job_id].update(status="failed", error=str(exc))

    task = asyncio.create_task(job())
    ctx.tasks.add(task)
    task.add_done_callback(ctx.tasks.discard)
    return {"job_id": job_id}

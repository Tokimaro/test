"""Схема БД (раздел 8 плана). Перечисления хранятся строками — без миграций на каждое значение."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Price = Numeric(28, 10)
Json = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InstrumentRow(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("broker", "symbol", name="uq_instruments_broker_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    market_type: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(16))
    tick_size: Mapped[Decimal] = mapped_column(Price)
    qty_step: Mapped[Decimal] = mapped_column(Price)
    min_qty: Mapped[Decimal] = mapped_column(Price)
    max_qty: Mapped[Decimal] = mapped_column(Price)
    min_notional: Mapped[Decimal] = mapped_column(Price, default=Decimal(0))
    max_leverage: Mapped[Decimal] = mapped_column(Price, default=Decimal(1))
    taker_fee: Mapped[Decimal] = mapped_column(Price)
    maker_fee: Mapped[Decimal] = mapped_column(Price)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = _ts()


class CandleRow(Base):
    """Свечи. В PostgreSQL с TimescaleDB таблица превращается в hypertable по ts."""

    __tablename__ = "candles"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    tf: Mapped[str] = mapped_column(String(4), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    turnover: Mapped[float] = mapped_column(Float, default=0.0)


class SignalRow(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_instrument_ts", "instrument_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    mode: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(16))
    components: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    acted: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str | None] = mapped_column(String(128))


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_status", "status"),
        Index("ix_trades_opened_at", "opened_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    mode: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))  # pending/open/closed/cancelled
    entry_price: Mapped[Decimal | None] = mapped_column(Price)
    qty: Mapped[Decimal] = mapped_column(Price)
    remaining_qty: Mapped[Decimal] = mapped_column(Price)
    initial_stop: Mapped[Decimal] = mapped_column(Price)
    stop_loss: Mapped[Decimal] = mapped_column(Price)
    tp1: Mapped[Decimal | None] = mapped_column(Price)
    tp2: Mapped[Decimal | None] = mapped_column(Price)
    tp1_done: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_amount: Mapped[Decimal] = mapped_column(Price)
    confidence: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[Decimal] = mapped_column(Price, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(Price, default=Decimal(0))
    r_multiple: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[Decimal | None] = mapped_column(Price)
    close_reason: Mapped[str | None] = mapped_column(String(16))
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime] = _ts()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    link_id: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(16))  # entry/tp1/close/...
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal | None] = mapped_column(Price)
    qty: Mapped[Decimal] = mapped_column(Price)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = _ts()


class ExecutionRow(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    exec_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    price: Mapped[Decimal] = mapped_column(Price)
    qty: Mapped[Decimal] = mapped_column(Price)
    fee: Mapped[Decimal] = mapped_column(Price)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    equity: Mapped[Decimal] = mapped_column(Price)
    balance: Mapped[Decimal] = mapped_column(Price)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Price)
    open_risk: Mapped[Decimal] = mapped_column(Price)


class SettingRow(Base):
    """История изменений настроек: актуальное значение — последняя запись по key."""

    __tablename__ = "settings"
    __table_args__ = (Index("ix_settings_key_id", "key", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict[str, Any]] = mapped_column(Json)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = _ts()


class RiskEventRow(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = _ts()
    type: Mapped[str] = mapped_column(String(32), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _ts()
    params: Mapped[dict[str, Any]] = mapped_column(Json)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(Json)
    equity_curve: Mapped[list[Any]] = mapped_column(Json)
    trades: Mapped[list[Any]] = mapped_column(Json, default=list)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    totp_secret: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class SecretRow(Base):
    """Зашифрованные (Fernet) секреты: API-ключи бирж и т.п."""

    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = _ts()


class RuntimeStateRow(Base):
    """Служебное состояние, которое должно переживать рестарт (риск-менеджер, paper-счёт)."""

    __tablename__ = "runtime_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(Json)
    updated_at: Mapped[datetime] = _ts()

"""holdings without stops

Стратегия дневного тренда держит монеты без стоп-лосса и без риска на сделку: колонки
стопа и риска становятся необязательными.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("initial_stop", "stop_loss", "risk_amount")


def upgrade() -> None:
    for col in COLUMNS:
        op.alter_column("trades", col, existing_type=sa.Numeric(28, 10), nullable=True)


def downgrade() -> None:
    for col in COLUMNS:
        op.execute(f"UPDATE trades SET {col} = 0 WHERE {col} IS NULL")
        op.alter_column("trades", col, existing_type=sa.Numeric(28, 10), nullable=False)

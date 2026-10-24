"""Add member position rank storage.

Revision ID: 20260831_0003
Revises: 20260730_0001
Create Date: 2026-08-31

Parented directly onto 20260730_0001 because 20260807_0002 (exchange-rule
storage) is still pending explicit authorization and must not be applied as
a side effect. When 20260807_0002 is authorized, add an Alembic merge
revision to reunify the heads.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0003"
down_revision: Union[str, None] = "20260730_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_position_ranks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("exchange", sa.String(length=8), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("product_id", sa.String(length=8), nullable=False),
        sa.Column("instrument_id", sa.String(length=16), nullable=False),
        sa.Column("metric", sa.String(length=8), nullable=False),
        sa.Column("rank_no", sa.SmallInteger(), nullable=False),
        sa.Column("member_id", sa.String(length=16)),
        sa.Column("member_name", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("change_qty", sa.BigInteger()),
        sa.Column("basis", sa.String(length=8)),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "exchange",
            "trading_day",
            "product_id",
            "instrument_id",
            "metric",
            "rank_no",
            "member_name",
            name="uq_member_ranks_day_instrument_metric_rank_member",
        ),
        sa.CheckConstraint(
            "metric IN ('volume', 'long', 'short')", name="ck_member_ranks_metric"
        ),
        sa.CheckConstraint("rank_no BETWEEN 0 AND 20", name="ck_member_ranks_rank_no"),
        sa.CheckConstraint("quantity >= 0", name="ck_member_ranks_quantity"),
    )
    op.create_index(
        "ix_member_ranks_exchange_day",
        "member_position_ranks",
        ["exchange", "trading_day"],
    )
    op.create_index(
        "ix_member_ranks_day_product",
        "member_position_ranks",
        ["trading_day", "product_id"],
    )
    op.create_index(
        "ix_member_ranks_member",
        "member_position_ranks",
        ["trading_day", "member_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_member_ranks_member", table_name="member_position_ranks")
    op.drop_index("ix_member_ranks_day_product", table_name="member_position_ranks")
    op.drop_index("ix_member_ranks_exchange_day", table_name="member_position_ranks")
    op.drop_table("member_position_ranks")

"""Create PostgreSQL storage for reference, run, report, and bar data.

Revision ID: 20260730_0001
Revises:
Create Date: 2026-07-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260730_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reference_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False, unique=True),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("raw_json", sa.LargeBinary(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_path", sa.Text()),
        sa.Column("calendar_valid_from", sa.Date(), nullable=False),
        sa.Column("calendar_valid_through", sa.Date(), nullable=False),
        sa.Column(
            "broker_markup", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "reference_products",
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("reference_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("code", sa.String(length=16), primary_key=True),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("main_symbol", sa.Text(), nullable=False),
        sa.Column("research_contract", sa.Text(), nullable=False),
        sa.Column("last_trading_day", sa.Date(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_table(
        "reference_closed_dates",
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("reference_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("closed_date", sa.Date(), primary_key=True),
    )
    op.create_table(
        "strategy_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market_symbol", sa.Text()),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text()),
        sa.Column("alembic_revision", sa.String(length=64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "strategy_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("strategy_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text()),
        sa.Column("source_line", sa.Integer()),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "run_id", "sequence_no", name="uq_strategy_events_run_sequence"
        ),
    )
    op.create_index(
        "ix_strategy_events_event_type", "strategy_events", ["event_type"]
    )
    op.create_table(
        "run_reports",
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("strategy_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text()),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "preopen_reports",
        sa.Column("report_id", sa.String(length=64), primary_key=True),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "reference_snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("reference_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("json_path", sa.Text()),
        sa.Column("markdown_path", sa.Text()),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "ingestion_batches",
        sa.Column("batch_id", sa.String(length=32), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rows_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "null_trading_days", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "kind", "source_sha256", name="uq_ingestion_batches_kind_source"
        ),
    )
    op.create_table(
        "market_bars",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("datetime_nano", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trading_day", sa.Date()),
        sa.Column("trading_day_source", sa.String(length=32), nullable=False),
        sa.Column("open", sa.Numeric(precision=28, scale=10)),
        sa.Column("high", sa.Numeric(precision=28, scale=10)),
        sa.Column("low", sa.Numeric(precision=28, scale=10)),
        sa.Column("close", sa.Numeric(precision=28, scale=10)),
        sa.Column("volume", sa.Numeric(precision=28, scale=8)),
        sa.Column("open_oi", sa.Numeric(precision=28, scale=8)),
        sa.Column("close_oi", sa.Numeric(precision=28, scale=8)),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "batch_id",
            sa.String(length=32),
            sa.ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "symbol",
            "duration_seconds",
            "datetime_nano",
            name="uq_market_bars_symbol_duration_nano",
        ),
    )
    op.create_index(
        "ix_market_bars_symbol_duration_ts",
        "market_bars",
        ["symbol", "duration_seconds", "ts"],
    )
    op.create_index("ix_market_bars_trading_day", "market_bars", ["trading_day"])


def downgrade() -> None:
    op.drop_index("ix_market_bars_trading_day", table_name="market_bars")
    op.drop_index("ix_market_bars_symbol_duration_ts", table_name="market_bars")
    op.drop_table("market_bars")
    op.drop_table("ingestion_batches")
    op.drop_table("preopen_reports")
    op.drop_table("run_reports")
    op.drop_index("ix_strategy_events_event_type", table_name="strategy_events")
    op.drop_table("strategy_events")
    op.drop_table("strategy_runs")
    op.drop_table("reference_closed_dates")
    op.drop_table("reference_products")
    op.drop_table("reference_snapshots")

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

reference_snapshots = Table(
    "reference_snapshots",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("content_sha256", String(64), nullable=False, unique=True),
    Column("schema_version", SmallInteger, nullable=False),
    Column("raw_json", LargeBinary, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("source_path", Text),
    Column("calendar_valid_from", Date, nullable=False),
    Column("calendar_valid_through", Date, nullable=False),
    Column("broker_markup", JSONB, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

reference_products = Table(
    "reference_products",
    metadata,
    Column(
        "snapshot_id",
        BigInteger,
        ForeignKey("reference_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("code", String(16), primary_key=True),
    Column("exchange", String(16), nullable=False),
    Column("main_symbol", Text, nullable=False),
    Column("research_contract", Text, nullable=False),
    Column("last_trading_day", Date, nullable=False),
    Column("payload", JSONB, nullable=False),
)

reference_closed_dates = Table(
    "reference_closed_dates",
    metadata,
    Column(
        "snapshot_id",
        BigInteger,
        ForeignKey("reference_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("closed_date", Date, primary_key=True),
)

strategy_runs = Table(
    "strategy_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("mode", String(16), nullable=False),
    Column("symbol", Text, nullable=False),
    Column("market_symbol", Text),
    Column("manifest", JSONB, nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("source_path", Text),
    Column("alembic_revision", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

strategy_events = Table(
    "strategy_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "run_id",
        String(64),
        ForeignKey("strategy_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("sequence_no", Integer, nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("source_path", Text),
    Column("source_line", Integer),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("run_id", "sequence_no", name="uq_strategy_events_run_sequence"),
)

run_reports = Table(
    "run_reports",
    metadata,
    Column(
        "run_id",
        String(64),
        ForeignKey("strategy_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("lifecycle_state", String(32), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("source_path", Text),
    Column("stored_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

preopen_reports = Table(
    "preopen_reports",
    metadata,
    Column("report_id", String(64), primary_key=True),
    Column("trading_day", Date, nullable=False),
    Column("status", String(32), nullable=False),
    Column(
        "reference_snapshot_id",
        BigInteger,
        ForeignKey("reference_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("payload", JSONB, nullable=False),
    Column("markdown", Text, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("json_path", Text),
    Column("markdown_path", Text),
    Column("stored_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

ingestion_batches = Table(
    "ingestion_batches",
    metadata,
    Column("batch_id", String(32), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("source", Text, nullable=False),
    Column("source_sha256", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("rows_read", Integer, nullable=False, server_default="0"),
    Column("rows_written", Integer, nullable=False, server_default="0"),
    Column("null_trading_days", Integer, nullable=False, server_default="0"),
    Column("details", JSONB, nullable=False, server_default="{}"),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("kind", "source_sha256", name="uq_ingestion_batches_kind_source"),
)

market_bars = Table(
    "market_bars",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("symbol", Text, nullable=False),
    Column("duration_seconds", Integer, nullable=False),
    Column("datetime_nano", BigInteger, nullable=False),
    # PostgreSQL timestamps are microsecond precision; datetime_nano is authoritative.
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("trading_day", Date),
    Column("trading_day_source", String(32), nullable=False),
    Column("open", Numeric(28, 10)),
    Column("high", Numeric(28, 10)),
    Column("low", Numeric(28, 10)),
    Column("close", Numeric(28, 10)),
    Column("volume", Numeric(28, 8)),
    Column("open_oi", Numeric(28, 8)),
    Column("close_oi", Numeric(28, 8)),
    Column("source", Text, nullable=False),
    Column(
        "batch_id",
        String(32),
        ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "symbol",
        "duration_seconds",
        "datetime_nano",
        name="uq_market_bars_symbol_duration_nano",
    ),
)

member_position_ranks = Table(
    "member_position_ranks",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("exchange", String(8), nullable=False),
    Column("trading_day", Date, nullable=False),
    Column("product_id", String(8), nullable=False),
    Column("instrument_id", String(16), nullable=False),
    # volume | long | short
    Column("metric", String(8), nullable=False),
    # 1..20 for disclosed ranks; 0 marks the aggregate/total row
    Column("rank_no", SmallInteger, nullable=False),
    Column("member_id", String(16)),
    Column("member_name", String(64), nullable=False),
    Column("quantity", BigInteger, nullable=False),
    Column("change_qty", BigInteger),
    # 'double' when the source double counts volume; NULL when single/unknown
    Column("basis", String(8)),
    Column("source_url", Text, nullable=False),
    Column(
        "collected_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "exchange",
        "trading_day",
        "product_id",
        "instrument_id",
        "metric",
        "rank_no",
        "member_name",
        name="uq_member_ranks_day_instrument_metric_rank_member",
    ),
)

exchange_rule_source_contents = Table(
    "exchange_rule_source_contents",
    metadata,
    Column("content_sha256", String(64), primary_key=True),
    Column("content", LargeBinary, nullable=False),
    Column("content_path", Text),
    Column(
        "stored_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

exchange_rule_source_documents = Table(
    "exchange_rule_source_documents",
    metadata,
    Column("document_id", String(64), primary_key=True),
    Column("exchange", String(16), nullable=False),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column(
        "content_sha256",
        String(64),
        ForeignKey("exchange_rule_source_contents.content_sha256", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("content_path", Text, nullable=False),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    Column("publication_date", Date),
    Column("effective_date", Date),
    Column("payload", JSONB, nullable=False),
    Column(
        "stored_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

exchange_rule_snapshots = Table(
    "exchange_rule_snapshots",
    metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("target_trading_day", Date, nullable=False),
    Column("collected_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("origin", String(64), nullable=False),
    Column("review_reason", Text),
    Column(
        "parent_snapshot_id",
        String(64),
        ForeignKey("exchange_rule_snapshots.snapshot_id", ondelete="RESTRICT"),
    ),
    Column("payload", JSONB, nullable=False),
    Column(
        "stored_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

exchange_rule_items = Table(
    "exchange_rule_items",
    metadata,
    Column("snapshot_id", String(64), nullable=False),
    Column("item_key", String(64), nullable=False),
    Column("exchange", String(16), nullable=False),
    Column("product", String(32), nullable=False),
    Column("contract", Text),
    Column("category", String(32), nullable=False),
    Column("field", String(64), nullable=False),
    Column("value_kind", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("value_text", Text),
    Column("unit", String(64)),
    Column("applies_to", Text, nullable=False),
    Column("effective_from", Date),
    Column("effective_through", Date),
    Column("source_document_id", String(64)),
    Column("source_url", Text),
    Column("excerpt", Text, nullable=False),
    Column("note", Text),
    Column("payload", JSONB, nullable=False),
    Column(
        "stored_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["exchange_rule_snapshots.snapshot_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["source_document_id"],
        ["exchange_rule_source_documents.document_id"],
        ondelete="RESTRICT",
    ),
    PrimaryKeyConstraint("snapshot_id", "item_key"),
)

exchange_rule_runs = Table(
    "exchange_rule_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("target_trading_day", Date, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("source_registry_sha256", String(64)),
    Column("snapshot_id", String(64)),
    Column("previous_snapshot_id", String(64)),
    Column("payload", JSONB, nullable=False),
    Column(
        "stored_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["exchange_rule_snapshots.snapshot_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["previous_snapshot_id"],
        ["exchange_rule_snapshots.snapshot_id"],
        ondelete="RESTRICT",
    ),
)

exchange_rule_approvals = Table(
    "exchange_rule_approvals",
    metadata,
    Column("record_id", String(64), primary_key=True),
    Column("candidate_snapshot_id", String(64), nullable=False),
    Column("approved_snapshot_id", String(64)),
    Column("decision", String(16), nullable=False),
    Column("reviewer", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column(
        "stored_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    ForeignKeyConstraint(
        ["candidate_snapshot_id"],
        ["exchange_rule_snapshots.snapshot_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["approved_snapshot_id"],
        ["exchange_rule_snapshots.snapshot_id"],
        ondelete="RESTRICT",
    ),
)

exchange_rule_current = Table(
    "exchange_rule_current",
    metadata,
    Column("singleton_id", SmallInteger, primary_key=True),
    Column("snapshot_id", String(64), nullable=False),
    Column("approval_record_id", String(64), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["exchange_rule_snapshots.snapshot_id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["approval_record_id"],
        ["exchange_rule_approvals.record_id"],
        ondelete="RESTRICT",
    ),
)

Index("ix_exchange_rule_documents_content", exchange_rule_source_documents.c.content_sha256)
Index("ix_exchange_rule_snapshots_target_day", exchange_rule_snapshots.c.target_trading_day)
Index("ix_exchange_rule_snapshots_status", exchange_rule_snapshots.c.status)
Index("ix_exchange_rule_items_lookup", exchange_rule_items.c.exchange, exchange_rule_items.c.product, exchange_rule_items.c.category, exchange_rule_items.c.status)
Index("ix_exchange_rule_runs_target_day", exchange_rule_runs.c.target_trading_day)
Index("ix_exchange_rule_runs_status", exchange_rule_runs.c.status)
Index("ix_exchange_rule_approvals_candidate", exchange_rule_approvals.c.candidate_snapshot_id)


Index("ix_market_bars_symbol_duration_ts", market_bars.c.symbol, market_bars.c.duration_seconds, market_bars.c.ts)
Index("ix_market_bars_trading_day", market_bars.c.trading_day)
Index("ix_member_ranks_exchange_day", member_position_ranks.c.exchange, member_position_ranks.c.trading_day)
Index("ix_member_ranks_day_product", member_position_ranks.c.trading_day, member_position_ranks.c.product_id)
Index("ix_member_ranks_member", member_position_ranks.c.trading_day, member_position_ranks.c.member_name)
Index("ix_strategy_events_event_type", strategy_events.c.event_type)

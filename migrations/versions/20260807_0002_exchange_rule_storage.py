"""Add immutable exchange-rule evidence and review storage.

Revision ID: 20260807_0002
Revises: 20260730_0001
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260807_0002"
down_revision: Union[str, None] = "20260730_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "exchange_rule_source_contents",
        sa.Column("content_sha256", sa.String(length=64), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_path", sa.Text()),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "exchange_rule_source_documents",
        sa.Column("document_id", sa.String(length=64), primary_key=True),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column(
            "content_sha256",
            sa.String(length=64),
            sa.ForeignKey(
                "exchange_rule_source_contents.content_sha256", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("content_path", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publication_date", sa.Date()),
        sa.Column("effective_date", sa.Date()),
        sa.Column("payload", _JSONB, nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_exchange_rule_documents_content",
        "exchange_rule_source_documents",
        ["content_sha256"],
    )
    op.create_table(
        "exchange_rule_snapshots",
        sa.Column("snapshot_id", sa.String(length=64), primary_key=True),
        sa.Column("target_trading_day", sa.Date(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=64), nullable=False),
        sa.Column("review_reason", sa.Text()),
        sa.Column(
            "parent_snapshot_id",
            sa.String(length=64),
            sa.ForeignKey(
                "exchange_rule_snapshots.snapshot_id", ondelete="RESTRICT"
            ),
        ),
        sa.Column("payload", _JSONB, nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_exchange_rule_snapshots_target_day",
        "exchange_rule_snapshots",
        ["target_trading_day"],
    )
    op.create_index(
        "ix_exchange_rule_snapshots_status",
        "exchange_rule_snapshots",
        ["status"],
    )
    op.create_table(
        "exchange_rule_items",
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("item_key", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("product", sa.String(length=32), nullable=False),
        sa.Column("contract", sa.Text()),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("value_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("value_text", sa.Text()),
        sa.Column("unit", sa.String(length=64)),
        sa.Column("applies_to", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_through", sa.Date()),
        sa.Column("source_document_id", sa.String(length=64)),
        sa.Column("source_url", sa.Text()),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("payload", _JSONB, nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["exchange_rule_snapshots.snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["exchange_rule_source_documents.document_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "item_key"),
    )
    op.create_index(
        "ix_exchange_rule_items_lookup",
        "exchange_rule_items",
        ["exchange", "product", "category", "status"],
    )
    op.create_table(
        "exchange_rule_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("target_trading_day", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_registry_sha256", sa.String(length=64)),
        sa.Column("snapshot_id", sa.String(length=64)),
        sa.Column("previous_snapshot_id", sa.String(length=64)),
        sa.Column("payload", _JSONB, nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["exchange_rule_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_snapshot_id"],
            ["exchange_rule_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_exchange_rule_runs_target_day",
        "exchange_rule_runs",
        ["target_trading_day"],
    )
    op.create_index(
        "ix_exchange_rule_runs_status", "exchange_rule_runs", ["status"]
    )
    op.create_table(
        "exchange_rule_approvals",
        sa.Column("record_id", sa.String(length=64), primary_key=True),
        sa.Column("candidate_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("approved_snapshot_id", sa.String(length=64)),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", _JSONB, nullable=False),
        sa.Column(
            "stored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_snapshot_id"],
            ["exchange_rule_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_snapshot_id"],
            ["exchange_rule_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_exchange_rule_approvals_candidate",
        "exchange_rule_approvals",
        ["candidate_snapshot_id"],
    )
    op.create_table(
        "exchange_rule_current",
        sa.Column("singleton_id", sa.SmallInteger(), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("approval_record_id", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["exchange_rule_snapshots.snapshot_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_record_id"],
            ["exchange_rule_approvals.record_id"],
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    op.drop_table("exchange_rule_current")
    op.drop_index(
        "ix_exchange_rule_approvals_candidate",
        table_name="exchange_rule_approvals",
    )
    op.drop_table("exchange_rule_approvals")
    op.drop_index("ix_exchange_rule_runs_status", table_name="exchange_rule_runs")
    op.drop_index(
        "ix_exchange_rule_runs_target_day", table_name="exchange_rule_runs"
    )
    op.drop_table("exchange_rule_runs")
    op.drop_index("ix_exchange_rule_items_lookup", table_name="exchange_rule_items")
    op.drop_table("exchange_rule_items")
    op.drop_index(
        "ix_exchange_rule_snapshots_status", table_name="exchange_rule_snapshots"
    )
    op.drop_index(
        "ix_exchange_rule_snapshots_target_day", table_name="exchange_rule_snapshots"
    )
    op.drop_table("exchange_rule_snapshots")
    op.drop_index(
        "ix_exchange_rule_documents_content",
        table_name="exchange_rule_source_documents",
    )
    op.drop_table("exchange_rule_source_documents")
    op.drop_table("exchange_rule_source_contents")

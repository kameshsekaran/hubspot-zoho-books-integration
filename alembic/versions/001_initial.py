"""Initial schema: deal_links, integration_logs, integration_jobs, zoho_tokens

Revision ID: 001_initial
Revises:
Create Date: 2026-10-30 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── deal_links ────────────────────────────────────────────────────────────
    op.create_table(
        "deal_links",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("hs_deal_id", sa.String(64), nullable=False, unique=True),
        sa.Column("zoho_customer_id", sa.String(64), nullable=True),
        sa.Column("zoho_so_id", sa.String(64), nullable=True),
        sa.Column("zoho_so_number", sa.String(64), nullable=True),
        sa.Column("zoho_invoice_ids", sa.Text, nullable=True),
        sa.Column("zoho_payment_ids", sa.Text, nullable=True),
        sa.Column("sync_state", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("hs_deal_id", name="uq_deal_links_hs_deal_id"),
    )
    op.create_index("ix_deal_links_hs_deal_id", "deal_links", ["hs_deal_id"])

    # ── integration_logs ──────────────────────────────────────────────────────
    op.create_table(
        "integration_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("deal_id", sa.String(64), nullable=True),
        sa.Column("direction", sa.String(32), nullable=True),
        sa.Column("system", sa.String(32), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("request_json", sa.Text, nullable=True),
        sa.Column("response_json", sa.Text, nullable=True),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("success", sa.Boolean, nullable=False, default=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_integration_logs_deal_id", "integration_logs", ["deal_id"])
    op.create_index("ix_integration_logs_system", "integration_logs", ["system"])
    op.create_index("ix_integration_logs_created_at", "integration_logs", ["created_at"])

    # ── integration_jobs ──────────────────────────────────────────────────────
    op.create_table(
        "integration_jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("deal_id", sa.String(64), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer, nullable=False, default=0),
        sa.Column("max_retries", sa.Integer, nullable=False, default=6),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("metadata_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("next_retry_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_integration_jobs_deal_id", "integration_jobs", ["deal_id"])
    op.create_index("ix_integration_jobs_status", "integration_jobs", ["status"])

    # ── zoho_tokens ───────────────────────────────────────────────────────────
    op.create_table(
        "zoho_tokens",
        sa.Column("id", sa.Integer, primary_key=True, default=1),
        sa.Column("access_token", sa.Text, nullable=False),
        sa.Column("refresh_token", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("zoho_tokens")
    op.drop_index("ix_integration_jobs_status", table_name="integration_jobs")
    op.drop_index("ix_integration_jobs_deal_id", table_name="integration_jobs")
    op.drop_table("integration_jobs")
    op.drop_index("ix_integration_logs_created_at", table_name="integration_logs")
    op.drop_index("ix_integration_logs_system", table_name="integration_logs")
    op.drop_index("ix_integration_logs_deal_id", table_name="integration_logs")
    op.drop_table("integration_logs")
    op.drop_index("ix_deal_links_hs_deal_id", table_name="deal_links")
    op.drop_table("deal_links")


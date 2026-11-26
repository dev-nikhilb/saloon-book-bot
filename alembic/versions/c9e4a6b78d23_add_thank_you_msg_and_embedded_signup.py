"""Add thank_you_message to tenant_settings and waba_business_account_id to tenants

Revision ID: c9e4a6b78d23
Revises: b8d3f5a67c12
Create Date: 2026-11-26
"""
import sqlalchemy as sa

from alembic import op

revision = "c9e4a6b78d23"
down_revision = "b8d3f5a67c12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_settings", sa.Column("thank_you_message", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("waba_business_account_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "waba_business_account_id")
    op.drop_column("tenant_settings", "thank_you_message")

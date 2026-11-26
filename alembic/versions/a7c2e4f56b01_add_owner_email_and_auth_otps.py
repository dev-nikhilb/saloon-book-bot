"""Add owner_email to tenants and Postgres-backed auth_otps table

Revision ID: a7c2e4f56b01
Revises: f6b1c3d45e90
Create Date: 2026-11-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a7c2e4f56b01"
down_revision = "f6b1c3d45e90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("owner_email", sa.Text(), nullable=True))
    op.create_index("ix_tenants_owner_email", "tenants", ["owner_email"])

    op.create_table(
        "auth_otps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("identifier", sa.Text(), nullable=False, index=True),  # normalized email or phone
        sa.Column("purpose", sa.Text(), nullable=False, server_default="login"),  # login, signup
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("auth_otps")
    op.drop_index("ix_tenants_owner_email", table_name="tenants")
    op.drop_column("tenants", "owner_email")

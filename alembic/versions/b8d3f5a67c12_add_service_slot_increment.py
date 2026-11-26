"""Add per-service slot_increment_minutes to services table

Revision ID: b8d3f5a67c12
Revises: a7c2e4f56b01
Create Date: 2026-11-26
"""
import sqlalchemy as sa

from alembic import op

revision = "b8d3f5a67c12"
down_revision = "a7c2e4f56b01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("slot_increment_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("services", "slot_increment_minutes")

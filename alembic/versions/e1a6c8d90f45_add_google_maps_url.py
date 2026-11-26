"""Add google_maps_url to tenant_settings for booking confirmation location links

Revision ID: e1a6c8d90f45
Revises: d0f5b7c89e34
Create Date: 2026-11-26
"""
import sqlalchemy as sa

from alembic import op

revision = "e1a6c8d90f45"
down_revision = "d0f5b7c89e34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_settings", sa.Column("google_maps_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_settings", "google_maps_url")

"""Add notes and reason columns to bookings

Revision ID: e5a9b2c34d78
Revises: d4e8f1a23b56
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "e5a9b2c34d78"
down_revision = "d4e8f1a23b56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("notes", sa.Text(), nullable=True))
    op.add_column("bookings", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "reason")
    op.drop_column("bookings", "notes")

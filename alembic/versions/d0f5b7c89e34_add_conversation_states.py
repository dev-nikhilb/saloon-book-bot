"""Add conversation_states table for serverless-safe multi-turn WhatsApp state

Revision ID: d0f5b7c89e34
Revises: c9e4a6b78d23
Create Date: 2026-11-26
"""
import sqlalchemy as sa

from alembic import op

revision = "d0f5b7c89e34"
down_revision = "c9e4a6b78d23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_states",
        sa.Column("state_key", sa.Text(), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_conversation_states_expires_at", "conversation_states", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_conversation_states_expires_at", table_name="conversation_states")
    op.drop_table("conversation_states")

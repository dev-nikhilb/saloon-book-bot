"""Add tenant slug for subdomain routing

Revision ID: d4e8f1a23b56
Revises: c3f7a0d12e89
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e8f1a23b56"
down_revision = "c3f7a0d12e89"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add slug column as nullable first so we can backfill
    op.add_column("tenants", sa.Column("slug", sa.Text(), nullable=True))

    # Backfill existing tenants: slug = lower(replace(business_name, ' ', '-'))
    op.execute(
        "UPDATE tenants SET slug = lower(replace(business_name, ' ', '-')) WHERE slug IS NULL"
    )

    # Now make it NOT NULL and add unique constraint
    op.alter_column("tenants", "slug", nullable=False)
    op.create_unique_constraint("uq_tenants_slug", "tenants", ["slug"])


def downgrade() -> None:
    op.drop_constraint("uq_tenants_slug", "tenants", type_="unique")
    op.drop_column("tenants", "slug")

"""Add service hierarchy and tenant settings

Revision ID: c3f7a0d12e89
Revises: 8ab07b5112a7
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c3f7a0d12e89"
down_revision = "8ab07b5112a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Service hierarchy columns ---
    op.add_column("services", sa.Column("parent_service_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("services", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("services", sa.Column("image_url", sa.Text(), nullable=True))
    op.create_index("ix_services_parent_service_id", "services", ["parent_service_id"])
    op.create_foreign_key(
        "fk_services_parent_service_id_services",
        "services", "services",
        ["parent_service_id"], ["id"],
        ondelete="SET NULL",
    )

    # --- Tenant settings table ---
    op.create_table(
        "tenant_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, unique=True),
        # Booking rules
        sa.Column("advance_booking_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("min_cancel_hours", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("slot_increment_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("max_bookings_per_customer", sa.Integer(), nullable=False, server_default="5"),
        # Notifications
        sa.Column("send_reminders", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("reminder_hours_before", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("send_feedback_request", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("feedback_delay_hours", sa.Integer(), nullable=False, server_default="2"),
        # Display
        sa.Column("welcome_message", sa.Text(), nullable=True),
        sa.Column("booking_confirmed_message", sa.Text(), nullable=True),
        sa.Column("google_review_url", sa.Text(), nullable=True),
        # Integrations
        sa.Column("google_calendar_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("razorpay_enabled", sa.Boolean(), nullable=False, server_default="false"),
        # Timestamps
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_settings_tenant_id", "tenant_settings", ["tenant_id"])

    # Create default settings for existing tenants
    op.execute("""
        INSERT INTO tenant_settings (id, tenant_id, created_at, updated_at)
        SELECT gen_random_uuid(), id, now(), now()
        FROM tenants
        WHERE id NOT IN (SELECT tenant_id FROM tenant_settings)
    """)


def downgrade() -> None:
    op.drop_table("tenant_settings")
    op.drop_constraint("fk_services_parent_service_id_services", "services", type_="foreignkey")
    op.drop_index("ix_services_parent_service_id", table_name="services")
    op.drop_column("services", "image_url")
    op.drop_column("services", "description")
    op.drop_column("services", "parent_service_id")

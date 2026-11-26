"""Add subscription lifecycle, invoices, and WhatsApp deposit payment fields

Revision ID: f6b1c3d45e90
Revises: e5a9b2c34d78
Create Date: 2026-11-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "f6b1c3d45e90"
down_revision = "e5a9b2c34d78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Subscription lifecycle & Razorpay subscription metadata
    op.add_column("subscriptions", sa.Column("razorpay_subscription_id", sa.Text(), nullable=True))
    op.add_column("subscriptions", sa.Column("razorpay_plan_id", sa.Text(), nullable=True))
    op.add_column("subscriptions", sa.Column("razorpay_customer_id", sa.Text(), nullable=True))
    op.add_column("subscriptions", sa.Column("grace_period_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "subscriptions",
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("subscriptions", sa.Column("warning_sent_3d_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("warning_sent_1d_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscriptions", sa.Column("expiry_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_subscriptions_razorpay_sub_id", "subscriptions", ["razorpay_subscription_id"])

    # 2. Subscription invoices ledger
    op.create_table(
        "subscription_invoices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("subscription_id", UUID(as_uuid=True), nullable=True),
        sa.Column("plan_tier", sa.Text(), nullable=False),
        sa.Column("billing_cycle", sa.Text(), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),  # paid, failed, pending, refunded
        sa.Column("payment_provider", sa.Text(), nullable=False, server_default="razorpay"),
        sa.Column("razorpay_payment_id", sa.Text(), nullable=True),
        sa.Column("razorpay_invoice_id", sa.Text(), nullable=True),
        sa.Column("razorpay_order_id", sa.Text(), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # 3. Booking deposit payment links, UPI manual UTR, hold expiry & verification audit
    op.add_column(
        "payments",
        sa.Column("payment_method", sa.Text(), nullable=False, server_default="razorpay"),
    )
    op.add_column("payments", sa.Column("razorpay_payment_link_id", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("payment_url", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("utr_reference", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("verified_by", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_payments_plink_id", "payments", ["razorpay_payment_link_id"])

    # 4. Tenant deposit collection settings (UPI ID, deposit mode, hold minutes)
    op.add_column("tenant_settings", sa.Column("upi_id", sa.Text(), nullable=True))
    op.add_column(
        "tenant_settings",
        sa.Column("deposit_mode", sa.Text(), nullable=False, server_default="auto"),
    )
    op.add_column(
        "tenant_settings",
        sa.Column("deposit_hold_minutes", sa.Integer(), nullable=False, server_default="15"),
    )


def downgrade() -> None:
    op.drop_column("tenant_settings", "deposit_hold_minutes")
    op.drop_column("tenant_settings", "deposit_mode")
    op.drop_column("tenant_settings", "upi_id")

    op.drop_index("ix_payments_plink_id", table_name="payments")
    op.drop_column("payments", "verified_at")
    op.drop_column("payments", "verified_by")
    op.drop_column("payments", "expires_at")
    op.drop_column("payments", "utr_reference")
    op.drop_column("payments", "payment_url")
    op.drop_column("payments", "razorpay_payment_link_id")
    op.drop_column("payments", "payment_method")

    op.drop_table("subscription_invoices")

    op.drop_index("ix_subscriptions_razorpay_sub_id", table_name="subscriptions")
    op.drop_column("subscriptions", "expiry_notified_at")
    op.drop_column("subscriptions", "warning_sent_1d_at")
    op.drop_column("subscriptions", "warning_sent_3d_at")
    op.drop_column("subscriptions", "cancel_at_period_end")
    op.drop_column("subscriptions", "cancelled_at")
    op.drop_column("subscriptions", "grace_period_ends_at")
    op.drop_column("subscriptions", "razorpay_customer_id")
    op.drop_column("subscriptions", "razorpay_plan_id")
    op.drop_column("subscriptions", "razorpay_subscription_id")

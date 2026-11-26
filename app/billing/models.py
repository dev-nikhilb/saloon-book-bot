import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    plan_tier: Mapped[str] = mapped_column(Text, nullable=False)
    billing_cycle: Mapped[str] = mapped_column(Text, nullable=False)  # monthly, annual
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # trial, active, past_due, cancelled, expired
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Razorpay SaaS subscription lifecycle fields
    razorpay_subscription_id: Mapped[str | None] = mapped_column(Text, index=True)
    razorpay_plan_id: Mapped[str | None] = mapped_column(Text)
    razorpay_customer_id: Mapped[str | None] = mapped_column(Text)
    grace_period_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Proactive renewal warning tracking (idempotency)
    warning_sent_3d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warning_sent_1d_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiry_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionInvoice(Base):
    __tablename__ = "subscription_invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    plan_tier: Mapped[str] = mapped_column(Text, nullable=False)
    billing_cycle: Mapped[str] = mapped_column(Text, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # paid, failed, pending, refunded
    payment_provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="razorpay")
    razorpay_payment_id: Mapped[str | None] = mapped_column(Text)
    razorpay_invoice_id: Mapped[str | None] = mapped_column(Text)
    razorpay_order_id: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MessageCreditLedger(Base):
    __tablename__ = "message_credit_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

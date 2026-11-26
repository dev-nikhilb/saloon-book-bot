import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payment_method: Mapped[str] = mapped_column(Text, nullable=False, server_default="razorpay")
    razorpay_order_id: Mapped[str | None] = mapped_column(Text)
    razorpay_payment_id: Mapped[str | None] = mapped_column(Text)
    razorpay_payment_link_id: Mapped[str | None] = mapped_column(Text, index=True)
    payment_url: Mapped[str | None] = mapped_column(Text)
    utr_reference: Mapped[str | None] = mapped_column(Text)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # created, submitted_utr, authorized, captured, failed, expired, refunded
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[str | None] = mapped_column(Text)  # webhook, api_poll, owner_whatsapp, owner_dashboard
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

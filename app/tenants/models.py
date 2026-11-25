import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_name: Mapped[str] = mapped_column(Text, nullable=False)
    business_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_phone: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    owner_name: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # subdomain: slug.bookbot.com
    primary_language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Asia/Kolkata")
    waba_phone_number_id: Mapped[str | None] = mapped_column(Text)
    waba_access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    plan_tier: Mapped[str] = mapped_column(Text, nullable=False, server_default="solo")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    branches: Mapped[list["Branch"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    staff: Mapped[list["Staff"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    services: Mapped[list["Service"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    settings: Mapped["TenantSettings"] = relationship(back_populates="tenant", uselist=False, cascade="all, delete-orphan")


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    operating_hours: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="branches")


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str | None] = mapped_column(Text)
    bio: Mapped[str | None] = mapped_column(Text)  # "5 yrs experience, color specialist"
    specialty: Mapped[str | None] = mapped_column(Text)  # "Hair Color, Bridal"
    skills: Mapped[dict | None] = mapped_column(JSONB)
    working_hours: Mapped[dict | None] = mapped_column(JSONB)
    google_calendar_id: Mapped[str | None] = mapped_column(Text)
    google_oauth_refresh_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="staff")


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    parent_service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # gap after each booking
    price_paise: Mapped[int | None] = mapped_column(Integer)
    requires_deposit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deposit_amount_paise: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="services")
    parent: Mapped["Service | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["Service"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )


class TenantSettings(Base):
    __tablename__ = "tenant_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )

    # Booking rules
    advance_booking_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="14")
    min_cancel_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    slot_increment_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="15")
    max_bookings_per_customer: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")

    # Notifications
    send_reminders: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    reminder_hours_before: Mapped[int] = mapped_column(Integer, nullable=False, server_default="24")
    send_feedback_request: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    feedback_delay_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")

    # Display
    welcome_message: Mapped[str | None] = mapped_column(Text)
    booking_confirmed_message: Mapped[str | None] = mapped_column(Text)
    google_review_url: Mapped[str | None] = mapped_column(Text)

    # Integrations
    google_calendar_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    razorpay_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="settings")

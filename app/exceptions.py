class BookingSaaSError(Exception):
    """Base exception for all application errors."""


class SlotConflictError(BookingSaaSError):
    """Raised when a booking conflicts with an existing one."""


class SlotNotAvailableError(BookingSaaSError):
    """Raised when a requested slot is outside working hours or blocked."""


class TenantNotFoundError(BookingSaaSError):
    """Raised when a tenant lookup fails."""


class CustomerNotFoundError(BookingSaaSError):
    """Raised when a customer lookup fails."""


class BookingNotFoundError(BookingSaaSError):
    """Raised when a booking lookup fails."""


class InvalidStateTransitionError(BookingSaaSError):
    """Raised when a booking state transition is not allowed."""

    def __init__(self, current_status: str, target_status: str):
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(f"Cannot transition from {current_status} to {target_status}")


class PlanLimitExceededError(BookingSaaSError):
    """Raised when a tenant exceeds their plan limits."""

    def __init__(self, limit_name: str, current: int, maximum: int):
        self.limit_name = limit_name
        self.current = current
        self.maximum = maximum
        super().__init__(f"{limit_name}: {current}/{maximum}")


class SubscriptionExpiredError(BookingSaaSError):
    """Raised when a tenant's trial or subscription has expired past any grace period."""

    def __init__(self, status: str = "expired", message: str | None = None):
        self.status = status
        self.message = message or (
            "Your BookBot subscription has expired. Renew your plan in Settings → Billing to resume accepting bookings."
        )
        super().__init__(self.message)


class FeatureNotAvailableError(BookingSaaSError):
    """Raised when a tenant tries to use a feature locked behind a higher plan tier."""

    def __init__(self, feature: str, required_plan: str = "growth"):
        self.feature = feature
        self.required_plan = required_plan
        super().__init__(
            f"'{feature}' requires the {required_plan.capitalize()} plan or higher."
        )


class DowngradeConflictError(BookingSaaSError):
    """Raised when downgrading to a lower plan conflicts with current active usage."""

    def __init__(self, message: str, conflicts: list[dict] | None = None):
        self.conflicts = conflicts or []
        super().__init__(message)


class CrossTenantAccessError(BookingSaaSError):
    """Raised when a query attempts to access data outside its tenant scope.
    This is a critical security error."""

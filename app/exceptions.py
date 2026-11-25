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


class CrossTenantAccessError(BookingSaaSError):
    """Raised when a query attempts to access data outside its tenant scope.
    This is a critical security error."""

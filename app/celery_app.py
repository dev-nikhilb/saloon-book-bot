from celery import Celery

from app.config import settings

celery = Celery(
    "booking_saas",
    broker=settings.celery_broker_url,
    backend=settings.redis_url,
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Retry policy for transient failures
    task_default_retry_delay=60,
    task_max_retries=5,
    # Beat schedule
    beat_schedule={
        "scan-reminders-every-5min": {
            "task": "reminders.scan_and_send",
            "schedule": 300.0,  # every 5 minutes
        },
        "scan-feedback-every-30min": {
            "task": "feedback.scan_and_send",
            "schedule": 1800.0,  # every 30 minutes
        },
        "scan-rebooking-daily": {
            "task": "rebooking.scan_and_nudge",
            "schedule": 86400.0,  # daily
        },
    },
)

# Auto-discover tasks from feature modules
celery.autodiscover_tasks([
    "app.messaging",
    "app.calendar_sync",
    "app.reminders",
    "app.payments",
    "app.rebooking",
    "app.feedback",
    "app.billing",
    "app.analytics",
])

"""Celery tasks for asynchronous inbound WhatsApp webhook processing."""
from __future__ import annotations

import asyncio

import structlog

from app.celery_app import celery
from app.messaging.whatsapp.webhook import process_webhook_payload

logger = structlog.get_logger()


@celery.task(name="messaging.process_webhook", bind=True, max_retries=3, default_retry_delay=15)
def process_webhook_task(self, payload: dict):
    """Process a Meta WhatsApp webhook payload asynchronously in a Celery worker."""
    try:
        asyncio.run(process_webhook_payload(payload))
    except Exception as exc:
        logger.error("webhook_task_failed", error=str(exc))
        raise self.retry(exc=exc)

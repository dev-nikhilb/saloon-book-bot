"""Onboarding API — simulate onboarding flow for dev/testing.

In production, this would be triggered by messages to a central onboarding WABA number.
For dev, we expose it as a REST endpoint.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.onboarding.flow import handle_onboarding_message

router = APIRouter(prefix="/dev/onboard", tags=["onboarding"])


class OnboardRequest(BaseModel):
    phone: str  # owner's phone
    body: str   # message text


class OnboardResponse(BaseModel):
    reply: str


@router.post("/message", response_model=OnboardResponse)
async def onboard_message(req: OnboardRequest, db: AsyncSession = Depends(get_db)):
    """Simulate onboarding conversation."""
    reply = await handle_onboarding_message(db, req.phone, req.body)
    return OnboardResponse(reply=reply)

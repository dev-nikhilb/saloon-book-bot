"""Dev-only endpoint to simulate WhatsApp messages without Meta webhook.
This mimics what happens when a customer or owner texts the business number.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/dev", tags=["dev-simulate"])


class SimulateMessageRequest(BaseModel):
    from_phone: str  # customer/owner phone e.g. "+919876543210"
    body: str  # message text e.g. "I want to book a haircut"
    to_tenant_phone: str | None = None  # owner phone of the tenant (optional, uses first tenant)


class SimulateMessageResponse(BaseModel):
    received: bool
    sender_type: str  # "owner", "customer", "unknown_tenant"
    tenant_id: str | None
    customer_id: str | None
    message: str  # what the bot would reply
    debug: dict


@router.post("/simulate", response_model=SimulateMessageResponse)
async def simulate_message(req: SimulateMessageRequest, db: AsyncSession = Depends(get_db)):
    """Simulate an inbound WhatsApp message. No Meta account needed."""

    # 1. Find tenant — either by to_tenant_phone or use first active tenant
    if req.to_tenant_phone:
        result = await db.execute(
            select_tenant_by_phone(req.to_tenant_phone)
        )
        tenant = result.first()
    else:
        result = await db.execute(
            text("SELECT id, business_name, business_type, owner_phone, primary_language, timezone FROM tenants WHERE is_active = true ORDER BY created_at DESC LIMIT 1")
        )
        tenant = result.first()

    if not tenant:
        return SimulateMessageResponse(
            received=True,
            sender_type="unknown_tenant",
            tenant_id=None,
            customer_id=None,
            message="No tenant found. Create a tenant first via POST /dev/seed",
            debug={"hint": "Call POST /dev/seed to create a demo salon with services and staff"},
        )

    tenant_id = str(tenant.id)
    tz_name = getattr(tenant, "timezone", None) or "Asia/Kolkata"
    btype = getattr(tenant, "business_type", None) or "salon"

    # 2. Check if sender is the owner
    is_owner = req.from_phone == tenant.owner_phone

    if is_owner:
        from app.owner_console.handler import handle_owner_message
        reply = await handle_owner_message(
            db, tenant_id, req.body, tenant.business_name, tz_name=tz_name, business_type=btype
        )
        return SimulateMessageResponse(
            received=True,
            sender_type="owner",
            tenant_id=tenant_id,
            customer_id=None,
            message=reply,
            debug={"owner_phone": tenant.owner_phone, "command": req.body},
        )

    # 3. Customer message — find or create customer
    from app.conversation.menu_flow import handle_customer_message

    cust_result = await db.execute(
        text("SELECT id, name, preferred_language, total_bookings FROM customers WHERE tenant_id = :tid AND phone = :phone"),
        {"tid": tenant.id, "phone": req.from_phone},
    )
    customer = cust_result.first()

    if not customer:
        cust_id = uuid.uuid4()
        await db.execute(
            text("INSERT INTO customers (id, tenant_id, phone, first_seen_at) VALUES (:id, :tid, :phone, :now)"),
            {"id": cust_id, "tid": tenant.id, "phone": req.from_phone, "now": datetime.now(timezone.utc)},
        )
        await db.commit()
        is_new = True
        cust_lang = getattr(tenant, "primary_language", None) or "en"
    else:
        cust_id = customer.id
        is_new = False
        cust_lang = customer.preferred_language or getattr(tenant, "primary_language", None) or "en"

    reply = await handle_customer_message(
        db=db,
        tenant_id=tenant_id,
        customer_id=str(cust_id),
        customer_phone=req.from_phone,
        customer_name=customer.name if customer else "",
        body=req.body,
        business_name=tenant.business_name,
        tz_name=tz_name,
        business_type=btype,
        lang=cust_lang,
    )

    # Log the message
    await db.execute(
        text("INSERT INTO messages (id, tenant_id, customer_id, direction, body, status, created_at) VALUES (:id, :tid, :cid, 'inbound', :body, 'received', :now)"),
        {"id": uuid.uuid4(), "tid": tenant.id, "cid": cust_id, "body": req.body, "now": datetime.now(timezone.utc)},
    )
    await db.commit()

    # Extract text from structured response
    if hasattr(reply, 'type'):
        if reply.type == "text":
            msg_text = reply.text
        elif reply.type == "buttons":
            btn_labels = " | ".join(f"[{b['title']}]" for b in reply.buttons)
            msg_text = f"{reply.body}\n\n{btn_labels}"
        elif reply.type == "list":
            items = []
            for s in reply.sections:
                for r in s["rows"]:
                    desc = f" — {r['description']}" if r.get('description') else ""
                    items.append(f"  • {r['title']}{desc}")
            msg_text = f"{reply.body}\n\n" + "\n".join(items) + f"\n\n[{reply.button_text}]"
        else:
            msg_text = str(reply)
    else:
        msg_text = str(reply)

    return SimulateMessageResponse(
        received=True,
        sender_type="new_customer" if is_new else "returning_customer",
        tenant_id=tenant_id,
        customer_id=str(cust_id),
        message=msg_text,
        debug={"is_new_customer": is_new, "raw_input": req.body},
    )


def select_tenant_by_phone(phone: str):
    return text("SELECT id, business_name, business_type, owner_phone, primary_language, timezone FROM tenants WHERE owner_phone = :phone AND is_active = true LIMIT 1").bindparams(phone=phone)


class SeedResponse(BaseModel):
    tenant_id: str
    owner_phone: str
    branch_id: str
    staff_ids: list[str]
    service_ids: list[str]
    message: str


@router.post("/seed", response_model=SeedResponse)
async def seed_demo_tenant(db: AsyncSession = Depends(get_db)):
    """Create a demo salon tenant with branch, staff, and services for testing."""

    # Check if demo tenant already exists
    existing = await db.execute(
        text("SELECT id FROM tenants WHERE owner_phone = '+919999900000'")
    )
    row = existing.first()
    if row:
        # Delete dependents first, then tenant
        tid = row.id
        await db.execute(text("DELETE FROM tenant_settings WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM feedback WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM messages WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM bookings WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM customers WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM services WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM staff WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM branches WHERE tenant_id = :tid"), {"tid": tid})
        await db.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": tid})
        await db.commit()

    tenant_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    staff1_id = uuid.uuid4()
    staff2_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # Tenant
    await db.execute(
        text("""INSERT INTO tenants (id, business_name, business_type, owner_phone, owner_name, slug, primary_language, timezone, plan_tier, is_active, created_at, updated_at)
                VALUES (:id, 'Style Studio', 'salon', '+919999900000', 'Priya', 'style-studio', 'en', 'Asia/Kolkata', 'growth', true, :now, :now)"""),
        {"id": tenant_id, "now": now},
    )

    # Branch
    operating_hours = {
        "mon": [{"open": "09:00", "close": "20:00"}],
        "tue": [{"open": "09:00", "close": "20:00"}],
        "wed": [{"open": "09:00", "close": "20:00"}],
        "thu": [{"open": "09:00", "close": "20:00"}],
        "fri": [{"open": "09:00", "close": "21:00"}],
        "sat": [{"open": "10:00", "close": "21:00"}],
        "sun": [{"open": "10:00", "close": "18:00"}],
    }
    import json
    await db.execute(
        text("""INSERT INTO branches (id, tenant_id, name, address, is_primary, operating_hours, created_at)
                VALUES (:id, :tid, 'Main Branch', '42 Anna Nagar, Chennai', true, :hours, :now)"""),
        {"id": branch_id, "tid": tenant_id, "hours": json.dumps(operating_hours), "now": now},
    )

    # Services
    services = [
        ("Haircut", 30, 50000, False, 0, 1),         # ₹500
        ("Beard Trim", 15, 20000, False, 0, 2),       # ₹200
        ("Hair Color", 90, 200000, True, 50000, 3),   # ₹2000, deposit ₹500
        ("Facial", 60, 150000, False, 0, 4),           # ₹1500
        ("Head Massage", 30, 40000, False, 0, 5),     # ₹400
    ]
    service_ids = []
    for name, dur, price, deposit, dep_amt, sort in services:
        sid = uuid.uuid4()
        service_ids.append(sid)
        await db.execute(
            text("""INSERT INTO services (id, tenant_id, name, duration_minutes, price_paise, requires_deposit, deposit_amount_paise, is_active, sort_order, created_at)
                    VALUES (:id, :tid, :name, :dur, :price, :dep, :dep_amt, true, :sort, :now)"""),
            {"id": sid, "tid": tenant_id, "name": name, "dur": dur, "price": price, "dep": deposit, "dep_amt": dep_amt, "sort": sort, "now": now},
        )

    # Staff (with working hours so slot calculator works)
    staff_hours = {
        "mon": [{"open": "09:00", "close": "20:00"}],
        "tue": [{"open": "09:00", "close": "20:00"}],
        "wed": [{"open": "09:00", "close": "20:00"}],
        "thu": [{"open": "09:00", "close": "20:00"}],
        "fri": [{"open": "09:00", "close": "21:00"}],
        "sat": [{"open": "10:00", "close": "21:00"}],
        "sun": [{"open": "10:00", "close": "18:00"}],
    }
    await db.execute(
        text("""INSERT INTO staff (id, tenant_id, branch_id, name, role, skills, working_hours, is_active, created_at)
                VALUES (:id, :tid, :bid, 'Ravi', 'stylist', :skills, :hours, true, :now)"""),
        {"id": staff1_id, "tid": tenant_id, "bid": branch_id,
         "skills": json.dumps([str(s) for s in service_ids[:3]]),
         "hours": json.dumps(staff_hours), "now": now},
    )
    await db.execute(
        text("""INSERT INTO staff (id, tenant_id, branch_id, name, role, skills, working_hours, is_active, created_at)
                VALUES (:id, :tid, :bid, 'Meena', 'stylist', :skills, :hours, true, :now)"""),
        {"id": staff2_id, "tid": tenant_id, "bid": branch_id,
         "skills": json.dumps([str(s) for s in service_ids]),
         "hours": json.dumps(staff_hours), "now": now},
    )

    # Default tenant settings
    await db.execute(
        text("""INSERT INTO tenant_settings (id, tenant_id, created_at, updated_at)
                VALUES (:id, :tid, :now, :now)"""),
        {"id": uuid.uuid4(), "tid": tenant_id, "now": now},
    )

    await db.commit()

    return SeedResponse(
        tenant_id=str(tenant_id),
        owner_phone="+919999900000",
        branch_id=str(branch_id),
        staff_ids=[str(staff1_id), str(staff2_id)],
        service_ids=[str(s) for s in service_ids],
        message="Demo salon 'Style Studio' created. Owner: Priya (+919999900000). 2 staff, 5 services. Try: POST /dev/simulate with from_phone=+919876543210",
    )

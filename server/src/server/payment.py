import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.engine import Connection

from server.accounts import calendar_month_end, touch_session, utc_now
from server.auth import (
    account_document,
    begin,
    current_account,
    error_response,
    find_stored_period,
    origin_rejected,
    unauthenticated,
)
from server.bots import email_unconfirmed
from server.config import payment_required
from server.models import quota_counters, subscription_periods

router = APIRouter()


def _latest_period(connection: Connection, account_id: uuid.UUID):
    return (
        connection.execute(
            select(subscription_periods)
            .where(subscription_periods.c.account_id == account_id)
            .order_by(
                subscription_periods.c.started_at.desc(),
                subscription_periods.c.id.desc(),
            )
            .limit(1)
        )
        .mappings()
        .first()
    )


def _deactivate_latest_at_end(
    connection: Connection, account_id: uuid.UUID, at: datetime
) -> None:
    period = _latest_period(connection, account_id)
    if period is None or at < period["ends_at"]:
        return
    connection.execute(
        subscription_periods.update()
        .where(subscription_periods.c.id == period["id"])
        .values(active=False)
    )


def record_cancellation(
    connection: Connection, account_id: uuid.UUID, at: datetime
) -> None:
    _deactivate_latest_at_end(connection, account_id, at)


def record_renewal_failed(
    connection: Connection, account_id: uuid.UUID, at: datetime
) -> None:
    _deactivate_latest_at_end(connection, account_id, at)


def apply_payment_event(
    connection: Connection,
    account_id: uuid.UUID,
    kind: str,
    at: datetime,
) -> None:
    if kind == "success":
        record_success(connection, account_id, at)
    elif kind == "cancellation":
        record_cancellation(connection, account_id, at)
    elif kind == "renewal_failed":
        record_renewal_failed(connection, account_id, at)


def record_success(connection: Connection, account_id: uuid.UUID, at: datetime) -> None:
    connection.execute(
        subscription_periods.update()
        .where(subscription_periods.c.account_id == account_id)
        .values(active=False)
    )
    period_id = uuid.uuid4()
    connection.execute(
        subscription_periods.insert().values(
            id=period_id,
            account_id=account_id,
            started_at=at,
            ends_at=calendar_month_end(at),
            active=True,
        )
    )
    connection.execute(
        quota_counters.insert().values(
            id=uuid.uuid4(),
            period_id=period_id,
            used=0,
        )
    )


async def read_payment_body(request: Request) -> JSONResponse | None:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", "Нужен JSON-объект.")
    if not isinstance(body, dict) or "password" in body or "cookie" in body:
        return error_response(400, "invalid_request", "Нужен JSON-объект.")
    return None


@router.post("/api/payment")
async def pay(request: Request) -> JSONResponse:
    rejected = origin_rejected(request)
    if rejected is not None:
        return rejected
    refusal = await read_payment_body(request)
    if refusal is not None:
        return refusal
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            document = None
        else:
            session, account = current
            if account["email_confirmed_at"] is None:
                document = "unconfirmed"
            elif not payment_required():
                document = "unset"
            else:
                touch_session(connection, session["id"], now)
                apply_payment_event(connection, account["id"], "success", now)
                document = account_document(
                    account["id"],
                    account["email"],
                    account["name"],
                    account["email_confirmed_at"],
                    find_stored_period(connection, account["id"]),
                    now,
                )
    if document is None:
        return unauthenticated()
    if document == "unconfirmed":
        return email_unconfirmed()
    if document == "unset":
        return error_response(400, "invalid_request", "Оплата не задана.")
    return JSONResponse(document)

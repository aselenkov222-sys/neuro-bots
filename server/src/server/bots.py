from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.accounts import utc_now
from server.auth import (
    begin,
    current_account,
    error_response,
    origin_rejected,
    unauthenticated,
)

router = APIRouter()


def email_unconfirmed() -> JSONResponse:
    return error_response(403, "email_unconfirmed", "Почта ещё не подтверждена.")


def refuse_bots(request: Request, *, mutating: bool) -> JSONResponse:
    if mutating:
        rejected = origin_rejected(request)
        if rejected is not None:
            return rejected
    now = utc_now()
    with begin() as connection:
        current = current_account(connection, request, now)
        if current is None:
            outcome = "anonymous"
        elif current[1]["email_confirmed_at"] is None:
            outcome = "unconfirmed"
        else:
            outcome = "confirmed"
    if outcome == "anonymous":
        return unauthenticated()
    if outcome == "unconfirmed":
        return email_unconfirmed()
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@router.get("/api/bots")
def list_bots(request: Request) -> JSONResponse:
    return refuse_bots(request, mutating=False)


@router.post("/api/bots")
def create_bot(request: Request) -> JSONResponse:
    return refuse_bots(request, mutating=True)


@router.get("/api/bots/{bot_id}")
def read_bot(request: Request, bot_id: str) -> JSONResponse:
    return refuse_bots(request, mutating=False)


@router.patch("/api/bots/{bot_id}")
def update_bot(request: Request, bot_id: str) -> JSONResponse:
    return refuse_bots(request, mutating=True)


@router.get("/api/bots/{bot_id}/messages")
def list_messages(request: Request, bot_id: str) -> JSONResponse:
    return refuse_bots(request, mutating=False)


@router.post("/api/bots/{bot_id}/messages")
def create_message(request: Request, bot_id: str) -> JSONResponse:
    return refuse_bots(request, mutating=True)

import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.engine import RowMapping

from server.accounts import format_utc, touch_session
from server.auth import error_response
from server.bots import open_confirmed
from server.browser_action import named_address
from server.models import catalog_apps

router = APIRouter()

NEED_FIELDS = "Нужны название, адрес и короткое описание."
NEED_ADDRESS = "Нужен адрес сайта."


def item_document(row: RowMapping) -> dict[str, str]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "address": row["address"],
        "description": row["description"],
        "created_at": format_utc(row["created_at"]),
    }


def _plain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


async def _parsed_item(
    request: Request,
) -> tuple[str, str, str] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_request", NEED_FIELDS)
    if not isinstance(body, dict):
        return error_response(400, "invalid_request", NEED_FIELDS)
    name = _plain(body.get("name"))
    address = _plain(body.get("address"))
    description = _plain(body.get("description"))
    if name is None or address is None or description is None:
        return error_response(400, "invalid_request", NEED_FIELDS)
    stored = named_address(address)
    if stored is None:
        return error_response(400, "invalid_request", NEED_ADDRESS)
    return name, stored, description


@router.get("/api/catalog")
def list_catalog(request: Request) -> JSONResponse:
    with open_confirmed(request, mutating=False) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        rows = (
            opened.connection.execute(
                select(catalog_apps)
                .where(catalog_apps.c.account_id == opened.account["id"])
                .order_by(
                    catalog_apps.c.created_at.asc(),
                    catalog_apps.c.id.asc(),
                )
            )
            .mappings()
            .all()
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse({"items": [item_document(row) for row in rows]})


@router.post("/api/catalog")
async def create_catalog_item(request: Request) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed = await _parsed_item(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        name, address, description = parsed
        item_id = uuid.uuid4()
        opened.connection.execute(
            catalog_apps.insert().values(
                id=item_id,
                account_id=opened.account["id"],
                name=name,
                address=address,
                description=description,
                created_at=opened.now,
            )
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        row = (
            opened.connection.execute(
                select(catalog_apps).where(catalog_apps.c.id == item_id)
            )
            .mappings()
            .one()
        )
        return JSONResponse(item_document(row))

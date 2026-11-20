import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from server.accounts import format_utc, touch_session
from server.auth import error_response
from server.bots import not_found, open_confirmed, parse_id
from server.browser_action import named_address
from server.models import bots, catalog_app_bots, catalog_apps

router = APIRouter()

NEED_FIELDS = "Нужны название, адрес и короткое описание."
NEED_ADDRESS = "Нужен адрес сайта."
INVALID_CONNECTION = "Подключение задаётся всем ботам или выбранным."
ITEM_NOT_FOUND = "Элемент каталога не найден."


def item_document(row: RowMapping, bot_ids: list[str]) -> dict[str, object]:
    everyone = bool(row["everyone"])
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "address": row["address"],
        "description": row["description"],
        "created_at": format_utc(row["created_at"]),
        "everyone": everyone,
        "bot_ids": [] if everyone else bot_ids,
    }


def _plain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped == "":
        return None
    return stripped


def item_not_found() -> JSONResponse:
    return error_response(404, "not_found", ITEM_NOT_FOUND)


def _bot_ids_by_item(
    connection: Connection, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    grouped = {item_id: [] for item_id in item_ids}
    if not item_ids:
        return grouped
    rows = connection.execute(
        select(
            catalog_app_bots.c.catalog_app_id,
            catalog_app_bots.c.bot_id,
        )
        .where(catalog_app_bots.c.catalog_app_id.in_(item_ids))
        .order_by(
            catalog_app_bots.c.catalog_app_id.asc(),
            catalog_app_bots.c.bot_id.asc(),
        )
    ).all()
    for catalog_app_id, bot_id in rows:
        grouped[catalog_app_id].append(str(bot_id))
    return grouped


def _load_item(
    connection: Connection, item_id: uuid.UUID, account_id: uuid.UUID
) -> RowMapping | None:
    return (
        connection.execute(
            select(catalog_apps).where(
                catalog_apps.c.id == item_id,
                catalog_apps.c.account_id == account_id,
            )
        )
        .mappings()
        .one_or_none()
    )


def _stored_document(connection: Connection, row: RowMapping) -> dict[str, object]:
    links = _bot_ids_by_item(connection, [row["id"]])
    return item_document(row, links.get(row["id"], []))


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


def _invalid_connection() -> JSONResponse:
    return error_response(400, "invalid_request", INVALID_CONNECTION)


async def _parsed_connection(
    request: Request,
) -> tuple[bool, list[object]] | JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid_connection()
    if not isinstance(body, dict) or not isinstance(body.get("everyone"), bool):
        return _invalid_connection()
    everyone = body["everyone"]
    if everyone:
        if "bot_ids" not in body or body["bot_ids"] == []:
            return True, []
        raw_ids = body["bot_ids"]
        if isinstance(raw_ids, list) and len(raw_ids) > 0:
            return _invalid_connection()
        return _invalid_connection()
    raw_ids = body.get("bot_ids")
    if not isinstance(raw_ids, list):
        return _invalid_connection()
    return False, raw_ids


def _selected_bots(
    connection: Connection, account_id: uuid.UUID, raw_ids: list[object]
) -> list[uuid.UUID] | JSONResponse:
    parsed: list[uuid.UUID] = []
    for raw in raw_ids:
        if not isinstance(raw, str):
            return not_found()
        bot_id = parse_id(raw)
        if bot_id is None:
            return not_found()
        parsed.append(bot_id)
    unique = list(dict.fromkeys(parsed))
    if not unique:
        return []
    found = set(
        connection.execute(
            select(bots.c.id).where(
                bots.c.id.in_(unique),
                bots.c.account_id == account_id,
            )
        )
        .scalars()
        .all()
    )
    if found != set(unique):
        return not_found()
    return unique


def _replace_links(
    connection: Connection,
    item_id: uuid.UUID,
    *,
    everyone: bool,
    bot_ids: list[uuid.UUID],
) -> None:
    connection.execute(
        catalog_apps.update()
        .where(catalog_apps.c.id == item_id)
        .values(everyone=everyone)
    )
    connection.execute(
        catalog_app_bots.delete().where(
            catalog_app_bots.c.catalog_app_id == item_id
        )
    )
    if bot_ids:
        connection.execute(
            catalog_app_bots.insert(),
            [
                {"catalog_app_id": item_id, "bot_id": bot_id}
                for bot_id in bot_ids
            ],
        )


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
        links = _bot_ids_by_item(opened.connection, [row["id"] for row in rows])
        touch_session(opened.connection, opened.session["id"], opened.now)
        return JSONResponse(
            {"items": [item_document(row, links.get(row["id"], [])) for row in rows]}
        )


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
                everyone=False,
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
        return JSONResponse(item_document(row, []))


@router.put("/api/catalog/{item_id}/connection")
async def connect_catalog_item(request: Request, item_id: str) -> JSONResponse:
    with open_confirmed(request, mutating=True) as opened:
        if isinstance(opened, JSONResponse):
            return opened
        parsed_id = parse_id(item_id)
        if parsed_id is None:
            return item_not_found()
        row = _load_item(opened.connection, parsed_id, opened.account["id"])
        if row is None:
            return item_not_found()
        parsed = await _parsed_connection(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        everyone, raw_ids = parsed
        if everyone:
            bot_ids: list[uuid.UUID] = []
        else:
            selected = _selected_bots(
                opened.connection, opened.account["id"], raw_ids
            )
            if isinstance(selected, JSONResponse):
                return selected
            bot_ids = selected
        _replace_links(
            opened.connection,
            parsed_id,
            everyone=everyone,
            bot_ids=bot_ids,
        )
        touch_session(opened.connection, opened.session["id"], opened.now)
        stored = _load_item(opened.connection, parsed_id, opened.account["id"])
        if stored is None:
            return item_not_found()
        return JSONResponse(_stored_document(opened.connection, stored))

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from server.models import bots, chats, messages

_LEADS = (
    "сохранить правила в сценарий",
    "сохрани правила в сценарий",
    "сохранить задачу в сценарий",
    "сохрани задачу в сценарий",
    "сохранить в сценарий",
    "сохрани в сценарий",
    "сохранить правила",
    "сохрани правила",
)
_SEPARATORS = ":.—-;,."

PROPOSAL_CODE = "scenario_proposal"
CONFIRMED_CODE = "scenario_confirmed"


def proposed_scenario(text: str) -> str | None:
    stripped = text.strip()
    lowered = stripped.casefold()
    for lead in _LEADS:
        if not lowered.startswith(lead):
            continue
        if len(lowered) == len(lead):
            return None
        if lowered[len(lead)].isalnum() or lowered[len(lead)] == "_":
            return None
        body = stripped[len(lead) :].lstrip()
        if body[:1] in _SEPARATORS:
            body = body[1:].lstrip()
        if body == "":
            return None
        return body
    return None


def confirm_proposal(
    connection: Connection,
    bot_id: uuid.UUID,
    message_id: uuid.UUID,
    now: datetime,
) -> tuple[str, uuid.UUID | None]:
    row = (
        connection.execute(
            select(
                messages.c.text,
                messages.c.code,
                messages.c.chat_id,
            )
            .join(chats, chats.c.id == messages.c.chat_id)
            .where(
                messages.c.id == message_id,
                chats.c.bot_id == bot_id,
                messages.c.role == "assistant",
                messages.c.code.in_((PROPOSAL_CODE, CONFIRMED_CODE)),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return "missing", None
    if row["code"] == CONFIRMED_CODE:
        return "already", row["chat_id"]
    updated = connection.execute(
        messages.update()
        .where(
            messages.c.id == message_id,
            messages.c.code == PROPOSAL_CODE,
        )
        .values(code=CONFIRMED_CODE)
    )
    if updated.rowcount != 1:
        return "already", row["chat_id"]
    connection.execute(
        bots.update()
        .where(bots.c.id == bot_id)
        .values(scenario=row["text"], updated_at=now)
    )
    return "applied", row["chat_id"]

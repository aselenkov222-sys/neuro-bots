import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from server.dangerous import write_message
from server.models import bots

REPORT_CODE = "chief_report"
ASSIGNMENT_CODE = "chief_assignment"
SKIPPED = "skipped"
WAITING = "waiting"
COMPLETED = "completed"


def report_sentence(kind: str, name: str) -> str:
    if kind == SKIPPED:
        return f"Рутина бота «{name}» пропущена."
    if kind == WAITING:
        return f"Рутина бота «{name}» ждёт «Да»."
    return f"Рутина бота «{name}» завершилась."


def report_routine(
    connection: Connection,
    bot_id: uuid.UUID,
    kind: str,
    now: datetime,
) -> None:
    if kind not in {SKIPPED, WAITING, COMPLETED}:
        return
    actor = (
        connection.execute(
            select(bots.c.account_id, bots.c.name, bots.c.chief).where(
                bots.c.id == bot_id
            )
        )
        .mappings()
        .one()
    )
    if actor["chief"] is True:
        return
    chief_id = connection.execute(
        select(bots.c.id).where(
            bots.c.account_id == actor["account_id"],
            bots.c.chief.is_(True),
        )
    ).scalar_one_or_none()
    if chief_id is None:
        return
    write_message(
        connection,
        chief_id,
        "assistant",
        report_sentence(kind, str(actor["name"])),
        REPORT_CODE,
        now,
    )

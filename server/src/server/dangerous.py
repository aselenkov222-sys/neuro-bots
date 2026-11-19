import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from server.models import (
    bots,
    chats,
    dangerous_steps,
    messages,
    routine_checks,
    routine_runs,
)

DANGEROUS_CODE = "dangerous_action"
YES_TEXT = "Да"
NO_TEXT = "Нет"
_GATE = re.compile(r"отправ|заплат|оплат|удал|опублик")


def is_dangerous(line: str) -> bool:
    text = line.lower()
    if _GATE.search(text):
        return True
    return "парол" in text and "смен" in text


def scenario_lines(scenario: str) -> list[str]:
    return [line.strip() for line in scenario.splitlines() if line.strip()]


def first_gate(scenario: str, after: int = -1) -> tuple[int, str] | None:
    for index, line in enumerate(scenario_lines(scenario)):
        if index <= after:
            continue
        if is_dangerous(line):
            return index, line
    return None


def not_done_text(action: str) -> str:
    return f"Не сделано: {action}"


def routine_is_awaiting(connection: Connection, routine_id: uuid.UUID) -> bool:
    return (
        connection.execute(
            select(routine_runs.c.id)
            .where(
                routine_runs.c.routine_id == routine_id,
                routine_runs.c.status == "awaiting",
            )
            .limit(1)
        ).first()
        is not None
    )


def _chat_id(connection: Connection, bot_id: uuid.UUID) -> uuid.UUID:
    return connection.execute(
        select(chats.c.id).where(
            chats.c.bot_id == bot_id,
            chats.c.kind == "main",
        )
    ).scalar_one()


def write_message(
    connection: Connection,
    bot_id: uuid.UUID,
    role: str,
    text: str,
    code: str | None,
    updated_at: datetime,
) -> uuid.UUID:
    chat_id = _chat_id(connection, bot_id)
    message_id = uuid.uuid4()
    connection.execute(
        messages.insert().values(
            id=message_id,
            chat_id=chat_id,
            role=role,
            text=text,
            code=code,
            updated_at=updated_at,
        )
    )
    connection.execute(
        chats.update().where(chats.c.id == chat_id).values(updated_at=updated_at)
    )
    return message_id


def open_gate(
    connection: Connection,
    bot_id: uuid.UUID,
    action: str,
    position: int,
    now: datetime,
    *,
    run_id: uuid.UUID | None,
    check_id: uuid.UUID | None,
) -> uuid.UUID:
    message_id = write_message(
        connection, bot_id, "assistant", action, DANGEROUS_CODE, now
    )
    step_id = uuid.uuid4()
    connection.execute(
        dangerous_steps.insert().values(
            id=step_id,
            run_id=run_id,
            check_id=check_id,
            bot_id=bot_id,
            message_id=message_id,
            action=action,
            position=position,
        )
    )
    return step_id


def _parent_awaiting(connection: Connection, step: RowMapping) -> bool:
    if step["run_id"] is not None:
        status = connection.execute(
            select(routine_runs.c.status).where(routine_runs.c.id == step["run_id"])
        ).scalar_one()
        return status == "awaiting"
    if step["check_id"] is not None:
        status = connection.execute(
            select(routine_checks.c.status).where(
                routine_checks.c.id == step["check_id"]
            )
        ).scalar_one()
        return status == "awaiting"
    return False


def _owned_step(
    connection: Connection,
    account_id: uuid.UUID,
    *,
    message_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
    bot_id: uuid.UUID | None = None,
) -> RowMapping | None:
    query = select(dangerous_steps).join(
        bots, bots.c.id == dangerous_steps.c.bot_id
    )
    if message_id is not None:
        query = query.where(dangerous_steps.c.message_id == message_id)
    if step_id is not None:
        query = query.where(dangerous_steps.c.id == step_id)
    if bot_id is not None:
        query = query.where(dangerous_steps.c.bot_id == bot_id)
    return (
        connection.execute(query.where(bots.c.account_id == account_id))
        .mappings()
        .one_or_none()
    )


def take_answer(
    connection: Connection,
    account_id: uuid.UUID,
    bot_id: uuid.UUID,
    message_id: uuid.UUID,
    answer: str,
    now: datetime,
) -> RowMapping | None:
    step = _owned_step(
        connection, account_id, message_id=message_id, bot_id=bot_id
    )
    if step is None or step["answer"] is not None:
        return None
    if not _parent_awaiting(connection, step):
        return None
    write_message(
        connection,
        bot_id,
        "user",
        YES_TEXT if answer == "yes" else NO_TEXT,
        None,
        now,
    )
    connection.execute(
        dangerous_steps.update()
        .where(
            dangerous_steps.c.id == step["id"],
            dangerous_steps.c.answer.is_(None),
        )
        .values(answer=answer, answered_at=now)
    )
    return step


def take_perform(
    connection: Connection,
    account_id: uuid.UUID,
    step_id: uuid.UUID,
    now: datetime,
) -> RowMapping | None:
    step = _owned_step(connection, account_id, step_id=step_id)
    if step is None or step["answer"] != "yes" or step["performed_at"] is not None:
        return None
    if not _parent_awaiting(connection, step):
        return None
    updated = connection.execute(
        dangerous_steps.update()
        .where(
            dangerous_steps.c.id == step["id"],
            dangerous_steps.c.answer == "yes",
            dangerous_steps.c.performed_at.is_(None),
        )
        .values(performed_at=now)
    )
    if updated.rowcount != 1:
        return None
    return step


def released_step(
    connection: Connection, account_id: uuid.UUID
) -> RowMapping | None:
    rows = (
        connection.execute(
            select(dangerous_steps)
            .join(bots, bots.c.id == dangerous_steps.c.bot_id)
            .where(
                bots.c.account_id == account_id,
                dangerous_steps.c.answer == "yes",
                dangerous_steps.c.performed_at.is_(None),
            )
            .order_by(
                dangerous_steps.c.answered_at.asc(),
                dangerous_steps.c.id.asc(),
            )
        )
        .mappings()
        .all()
    )
    for step in rows:
        if _parent_awaiting(connection, step):
            return step
    return None


def gate_state(
    connection: Connection, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, object]]:
    if not message_ids:
        return {}
    rows = (
        connection.execute(
            select(
                dangerous_steps.c.message_id,
                dangerous_steps.c.answer,
                routine_runs.c.status.label("run_status"),
                routine_checks.c.status.label("check_status"),
            )
            .outerjoin(
                routine_runs, routine_runs.c.id == dangerous_steps.c.run_id
            )
            .outerjoin(
                routine_checks,
                routine_checks.c.id == dangerous_steps.c.check_id,
            )
            .where(dangerous_steps.c.message_id.in_(message_ids))
        )
        .mappings()
        .all()
    )
    return {
        row["message_id"]: {
            "answer": row["answer"],
            "pending": row["answer"] is None
            and (
                row["run_status"] == "awaiting" or row["check_status"] == "awaiting"
            ),
        }
        for row in rows
    }

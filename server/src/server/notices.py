import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Connection

from server.models import bots, desktop_notices

COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"
KINDS = (COMPLETED, FAILED, SKIPPED)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def record_notice(
    connection: Connection,
    bot_id: uuid.UUID,
    kind: str,
    now: datetime,
) -> None:
    if kind not in KINDS:
        return
    account_id = connection.execute(
        select(bots.c.account_id).where(bots.c.id == bot_id)
    ).scalar_one()
    connection.execute(
        desktop_notices.insert().values(
            id=uuid.uuid4(),
            account_id=account_id,
            bot_id=bot_id,
            kind=kind,
            created_at=_aware(now),
        )
    )


def pending_notices(connection: Connection, account_id: uuid.UUID) -> list[dict[str, str]]:
    rows = (
        connection.execute(
            select(
                desktop_notices.c.id,
                desktop_notices.c.bot_id,
                desktop_notices.c.kind,
            )
            .where(
                desktop_notices.c.account_id == account_id,
                desktop_notices.c.delivered_at.is_(None),
            )
            .order_by(desktop_notices.c.created_at.asc(), desktop_notices.c.id.asc())
        )
        .mappings()
        .all()
    )
    return [
        {"id": str(row["id"]), "bot_id": str(row["bot_id"]), "kind": row["kind"]}
        for row in rows
    ]


def mark_delivered(
    connection: Connection,
    account_id: uuid.UUID,
    notice_ids: list[uuid.UUID],
    now: datetime,
) -> None:
    if not notice_ids:
        return
    connection.execute(
        desktop_notices.update()
        .where(
            desktop_notices.c.account_id == account_id,
            desktop_notices.c.id.in_(notice_ids),
            desktop_notices.c.delivered_at.is_(None),
        )
        .values(delivered_at=_aware(now))
    )

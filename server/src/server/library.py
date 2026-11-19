import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection

from server.models import library_files


def store_library_file(
    connection: Connection,
    *,
    bot_id: uuid.UUID,
    chat_id: uuid.UUID,
    message_id: uuid.UUID,
    name: str,
    content: bytes,
    created_at: datetime,
) -> None:
    connection.execute(
        library_files.insert().values(
            id=uuid.uuid4(),
            bot_id=bot_id,
            chat_id=chat_id,
            message_id=message_id,
            name=name,
            content=content,
            created_at=created_at,
        )
    )


def attach_files(
    connection: Connection,
    documents: list[dict[str, object]],
    message_ids: list[uuid.UUID],
) -> None:
    if not message_ids:
        return
    rows = (
        connection.execute(
            select(
                library_files.c.message_id,
                library_files.c.id,
                library_files.c.name,
            ).where(library_files.c.message_id.in_(message_ids))
        )
        .mappings()
        .all()
    )
    by_message = {
        row["message_id"]: {"id": str(row["id"]), "name": row["name"]}
        for row in rows
    }
    for document, message_id in zip(documents, message_ids, strict=True):
        file = by_message.get(message_id)
        if file is not None:
            document["file"] = file

"""Client publication editor, explicit preview and per-part history."""

from sqlalchemy import select
from ama import publications as p
from ama.errors import AccessDenied, BusinessError
from ama.models import Work, ClientPublication
from ama.ui import business
from ama.ui.controller import show, draft_screen, paginate, navigation
from ama.ui.engine import button as B


def dispatch(db, actor, screen, action, data):
    if action == "pub_cancel":
        obj = db.get(ClientPublication, data["id"])
        if not obj:
            raise BusinessError("Публикация не найдена.")
        p.cancel(db, actor, obj.id, data["expected"])
        return dispatch(db, actor, screen, "pub_list", {"work_id": obj.work_id})
    w = db.get(Work, data["work_id"])
    p.authorize(db, actor, w)
    if action == "pub_new":
        p.recipient(db, w)
        draft = business.make_draft(
            db,
            actor,
            "publication:send",
            "Отправка выбранного комплекта клиенту",
            {"mode": "reply", "attachment_ids": []},
            w,
        )
        return draft_screen(db, actor, screen, {"draft_id": draft.id})
    if action == "pub_list":
        rows = list(
            db.scalars(
                select(ClientPublication)
                .where(ClientPublication.work_id == w.id, ClientPublication.bot_id == p.settings().bot_id)
                .order_by(ClientPublication.created_at.desc())
            )
        )
        items, page = paginate(rows, data.get("page", 0))
        actions = []
        lines = []
        for item in items:
            for ident in item.data["parts"]:
                from ama.models import Outbox

                p.refresh(db, db.get(Outbox, ident))
                break
            labels = {
                "queued": "В очереди",
                "pending": "Ожидает отправки",
                "sent": "Доставлено",
                "partial": "Частично доставлено",
                "blocked": "Заблокировано",
                "failed": "Ошибка доставки",
                "uncertain": "Исход требует сверки",
                "sending": "Отправка / требуется сверка",
                "cancelled": "Отменено",
            }
            parts = []
            for i, ident in enumerate(item.data["parts"]):
                part = db.get(Outbox, ident)
                filename = "Текст" if i == 0 else item.data["snapshot"]["files"][i - 1]["filename"]
                parts.append(filename + ": " + labels.get(part.status, part.status))
            lines.append(
                item.created_at.isoformat()
                + " · "
                + labels.get(item.status, item.status)
                + "\n"
                + "; ".join(parts)
                + "\nID: "
                + item.id
            )
            if item.status not in {"sent", "cancelled"}:
                actions.append(
                    B("Отменить неотправленное · " + item.id[:8], "pub_cancel", id=item.id, expected=item.version)
                )
        return show(
            db,
            actor,
            screen,
            "Публикации выбранных комплектов. Доставка не означает прочтение или приёмку.\n" + "\n".join(lines),
            actions + navigation("pub_list", page, len(rows), work_id=w.id) + [B("К работе", "work", work_id=w.id)],
            "pub_list",
            work_id=w.id,
            page=page,
        )
    raise AccessDenied("Неизвестное действие публикации.")

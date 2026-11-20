"""Durable draft/unassigned uploads and checked Reply resolution. No unsolicited forwarding."""

from sqlalchemy import select
from ama.auth import require, roles_for
from ama.config import settings
from ama.errors import AccessDenied, BusinessError
from ama.models import IntakeFile, Attachment, Outbox, Work, Binding, Audit
from ama.service import attach


def reply_work(db, actor, event):
    ident = getattr(event, "reply_message_id", None)
    if not ident:
        return None
    # Only authoritative sent messages created by this bot, in the same current route.
    rows = list(
        db.scalars(
            select(Outbox).where(
                Outbox.platform == event.platform,
                Outbox.chat_id == event.chat_id,
                Outbox.topic_key == event.topic,
                Outbox.external_id == str(ident),
                Outbox.status == "sent",
                Outbox.work_id.is_not(None),
            )
        )
    )
    eligible = []
    for row in rows:
        source = db.get(Binding, row.binding_id)
        w = db.get(Work, row.work_id)
        if source and source.bot_id == settings().bot_id and source.id == actor.binding_id and w:
            try:
                if roles_for(db, actor, w):
                    eligible.append(w.id)
            except AccessDenied:
                pass
    if len(set(eligible)) == 1:
        return eligible[0]
    return None


def own(db, actor, ident):
    obj = db.scalar(
        select(IntakeFile).where(IntakeFile.id == ident).with_for_update().execution_options(populate_existing=True)
    )
    b = db.get(Binding, actor.binding_id)
    if (
        not obj
        or obj.bot_id != settings().bot_id
        or obj.person_id != actor.person_id
        or obj.binding_id != actor.binding_id
        or obj.binding_version != b.version
        or not b.active
        or not b.verified
    ):
        raise AccessDenied("Файл принадлежит другому человеку / маршруту или привязка изменилась.")
    return obj


def capture(db, actor, event, dialog=None):
    require(
        db,
        actor,
        {"partner", "observer", "curator", "specialist", "marketing_manager", "contract", "consultant", "support_head"},
    )
    b = db.get(Binding, actor.binding_id)
    result = []
    for i, item in enumerate(event.files):
        key = event.key + ":" + str(i)
        old = db.scalar(select(IntakeFile).where(IntakeFile.source_key == key))
        if old:
            result.append(old)
            continue
        f = IntakeFile(
            source_key=key,
            bot_id=settings().bot_id,
            person_id=actor.person_id,
            binding_id=b.id,
            binding_version=b.version,
            dialog_id=dialog.id if dialog else None,
            platform=event.platform,
            file_id=item["file_id"],
            filename=item["filename"][:250],
            album_id=item.get("album_id"),
            data={"establishment_id": dialog.data.get("establishment_id") if dialog else None, "draft": bool(dialog)},
        )
        db.add(f)
        db.flush()
        result.append(f)
        db.add(
            Audit(
                actor_id=actor.person_id, object_id=f.id, action="FILE_CONTEXT_PENDING", data={"dialog_id": f.dialog_id}
            )
        )
    if dialog:
        dialog.data = {
            **dialog.data,
            "file_ids": list(dict.fromkeys([*dialog.data.get("file_ids", []), *[f.id for f in result]])),
        }
        dialog.version += 1
    return result


def link(db, actor, ident, work_id):
    f = own(db, actor, ident)
    w = db.get(Work, work_id)
    if not w:
        raise BusinessError("Пункт недоступен.")
    require(
        db, actor, {"partner", "observer", "curator", "specialist", "marketing_manager", "contract", "support_head"}, w
    )
    if f.work_id:
        if f.work_id != w.id:
            raise BusinessError("Файл уже связан с исходной карточкой.")
        return db.scalar(select(Attachment).where(Attachment.source_key == f.source_key))
    if f.status == "cancelled":
        raise BusinessError("Выбор файла отменён.")
    if f.dialog_id and f.data.get("establishment_id") != w.establishment_id:
        raise BusinessError("Предприятие черновика изменилось. Уберите прежние файлы явно и приложите нужные заново.")
    a = attach(db, actor, w.id, f.platform, f.file_id, f.filename, f.source_key, f.album_id)
    if f.status == "stored":
        a.storage_path, a.size, a.sha256 = f.storage_path, f.size, f.sha256
        a.status = "stored"
        # Attachment worker also routes already saved files once, using its durable delivery key.
        a.status = "pending_local"
    if f.album_id:
        from hashlib import sha256
        from ama.models import MediaGroup

        binding = db.get(Binding, f.binding_id)
        key = sha256(f"{f.platform}:{binding.chat_id}:{binding.topic_key}:{f.album_id}".encode()).hexdigest()
        group = db.get(MediaGroup, key)
        if group and (
            group.work_id != w.id or group.author_id != actor.person_id or group.binding_id != actor.binding_id
        ):
            raise AccessDenied("Другой исходный контекст альбома.")
        if not group:
            db.add(MediaGroup(id=key, work_id=w.id, author_id=actor.person_id, binding_id=actor.binding_id))
    f.work_id = w.id
    f.status = "linked"
    f.version += 1
    db.add(Audit(actor_id=actor.person_id, object_id=f.id, action="FILE_CONTEXT_ASSIGNED", data={"work_id": w.id}))
    return a


def confirm_dialog(db, actor, dialog, w):
    for ident in dialog.data.get("file_ids", []):
        link(db, actor, ident, w.id)


def worker_step(transport, session):
    from ama.files import download
    from ama.storage_policy import check_attachment, current

    with session.begin() as db:
        f = db.scalar(
            select(IntakeFile)
            .where(IntakeFile.status == "pending", IntakeFile.work_id.is_(None))
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not f:
            return False
        try:
            b = db.get(Binding, f.binding_id)
            if (
                not b
                or b.bot_id != settings().bot_id
                or not b.active
                or not b.verified
                or b.version != f.binding_version
            ):
                f.status = "blocked"
                return True
            check_attachment(db, f)
            policy = current(db)
            f.storage_path, f.size, f.sha256 = download(
                f, transport.client, **({"max_bytes": policy.data["max_file_bytes"]} if policy else {})
            )
            f.status = "stored"
        except Exception as error:
            f.status = "failed"
            f.data = {**f.data, "error_type": type(error).__name__}
            source = db.get(Binding, f.binding_id)
            if source and source.active and source.verified and source.version == f.binding_version:
                from ama.service import queue

                queue(
                    db,
                    source,
                    "intake-save-error:" + f.id,
                    "Не удалось сохранить файл «"
                    + f.filename
                    + "». Он не принят. Прикрепите его заново или выберите другой файл.",
                )
        f.version += 1
        return True


def choices(db, actor):
    result = []
    for w in db.scalars(select(Work).where(Work.status != "done").order_by(Work.created_at.desc())):
        try:
            if roles_for(db, actor, w) & {
                "partner",
                "observer",
                "curator",
                "specialist",
                "marketing_manager",
                "contract",
                "support_head",
            }:
                result.append(w)
        except AccessDenied:
            pass
    return result


def chooser(db, actor, screen, files, page=0):
    from ama.ui.controller import show, paginate, navigation
    from ama.ui.engine import button as B

    rows = choices(db, actor)
    items, page = paginate(rows, page)
    ids = [f.id for f in files]
    return show(
        db,
        actor,
        screen,
        "Файлы получены отдельно и сохраняются. Они пока никому не передаются. Выберите исходный пункт.",
        [B(w.title, "up_assign", file_ids=ids, work_id=w.id) for w in items]
        + navigation("up_choose", page, len(rows), file_ids=ids)
        + [B("Оставить до выбора", "home")],
        "up_choose",
        file_ids=ids,
        page=page,
    )


def dispatch(db, actor, screen, action, data):
    files = [own(db, actor, x) for x in data["file_ids"]]
    if action == "up_choose":
        return chooser(db, actor, screen, files, data.get("page", 0))
    if action == "up_assign":
        for f in files:
            link(db, actor, f.id, data["work_id"])
        from ama.ui.controller import show
        from ama.ui.engine import button as B

        return show(
            db,
            actor,
            screen,
            "Файлы связаны с выбранным пунктом. Сохранение/доставка и приёмка отслеживаются отдельно.",
            [B("Открыть пункт", "work", work_id=data["work_id"])],
        )
    raise BusinessError("Неизвестное действие с файлом.")

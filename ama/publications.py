"""Explicit, scoped publication of an immutable selection; internal files stay internal."""

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from sqlalchemy import select
from ama.auth import Actor, active_person_role
from ama.config import settings
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.models import Attachment, Binding, ClientPublication, Work, Entity, EstablishmentBrand, Outbox, Audit, now
from ama.service import queue, versioned
from ama.ui import access

MAX_FILES = 10


def authorize(db, actor, w):
    access.require_staff(db, actor)
    b = db.get(Binding, actor.binding_id)
    if not w or b.purpose == "client" or not access.can_work(db, actor, w, detail=True):
        raise AccessDenied("Внутренний материал доступен только в назначенной рабочей очереди.")
    if actor.person_id not in {w.owner_id, w.reviewer_id} or actor.binding_id not in {
        w.binding_id,
        w.review_binding_id,
    }:
        raise AccessDenied("Публикацию выполняет текущий ответственный / принимающий.")
    if w.kind.startswith("finance_") or w.kind in {"sales_expert", "sales_contract"}:
        raise BusinessError(
            "Договорные документы и ответы экспертов передаются через владельца общения в профильном процессе."
        )
    if w.kind.startswith("marketing") and not active_person_role(db, actor.person_id, {"marketing_manager"}, w):
        raise AccessDenied("Маркетинговый ответ публикует назначенный менеджер.")
    return b


def recipient(db, w):
    b = db.get(Binding, w.client_binding_id)
    if (
        not b
        or b.bot_id != settings().bot_id
        or b.purpose != "client"
        or not b.active
        or not b.verified
        or b.partner_id != w.partner_id
        or not b.brand_id
        or w.brand_id
        and b.brand_id != w.brand_id
        or b.establishment_id
        and b.establishment_id != w.establishment_id
    ):
        raise BusinessError("Нет текущего проверенного клиентского маршрута этой работы.")
    if not w.partner_person_id or not active_person_role(db, w.partner_person_id, {"partner"}, w):
        raise BusinessError("Не назначен действующий ФП — получатель ответа.")
    if w.establishment_id:
        e = db.get(Entity, w.establishment_id)
        if (
            not e
            or e.partner_id != w.partner_id
            or not db.scalar(
                select(EstablishmentBrand.id).where(
                    EstablishmentBrand.establishment_id == e.id,
                    EstablishmentBrand.brand_id == b.brand_id,
                    EstablishmentBrand.active.is_(True),
                )
            )
        ):
            raise BusinessError("Предприятие не связано с брендом адресата.")
    return b


def file_path(a, expected=None):
    if not a or a.status != "stored" or not a.storage_path or not a.sha256 or a.size is None:
        raise BusinessError("Файл ещё не сохранён или не имеет контрольной суммы.")
    root = Path(settings().file_root).resolve()
    path = Path(a.storage_path)
    if path.is_symlink() or root not in path.resolve().parents or not path.is_file():
        raise BusinessError("Файл отсутствует в разрешённом хранилище.")
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > settings().max_file_bytes:
                raise BusinessError("Файл превышает разрешённый размер.")
            digest.update(chunk)
    if size != a.size or digest.hexdigest() != a.sha256:
        raise BusinessError("Содержимое файла изменилось; повторите проверку и предпросмотр.")
    if expected and any(getattr(a, k) != expected[k] for k in ("sha256", "size", "filename")):
        raise Conflict("Выбранная редакция файла изменилась.")
    return path


def fingerprint(db, actor, w, data):
    source = authorize(db, actor, w)
    target = recipient(db, w)
    text = (data.get("text") or "").strip()
    mode = data.get("mode", "reply")
    if mode not in {"reply", "request"} or not text or len(text) > 2500:
        raise BusinessError("Укажите ответ / запрос и текст до 2500 символов.")
    request_data = {}
    if mode == "request":
        from ama.service import parse_date

        if actor.person_id != w.owner_id or not data.get("reason") or not data.get("next_check"):
            raise BusinessError("Запрос данных оформляет владелец: нужны причина и будущая дата контроля.")
        if parse_date(data["next_check"]) <= now():
            raise BusinessError("Назначьте будущую дату контроля.")
        request_data = {k: data[k] for k in ("reason", "next_check")}
    ids = data.get("attachment_ids", [])
    if not isinstance(ids, list) or len(ids) != len(set(ids)) or len(ids) > MAX_FILES:
        raise BusinessError("Выберите до 10 разных файлов.")
    files = []
    for ident in ids:
        a = db.get(Attachment, ident)
        if not a or a.work_id != w.id:
            raise AccessDenied("Файл относится к другой работе.")
        from ama.storage_policy import check_attachment

        check_attachment(db, a)
        file_path(a)
        files.append({k: getattr(a, k) for k in ("id", "filename", "sha256", "size")})
    context = {
        k: getattr(w, k)
        for k in ("partner_id", "brand_id", "establishment_id", "partner_person_id", "owner_id", "reviewer_id")
    }
    return {
        "work_id": w.id,
        "work_version": w.version,
        "source_id": source.id,
        "source_version": source.version,
        "target_id": target.id,
        "target_version": target.version,
        "target_platform": target.platform,
        "target_chat": target.chat_id,
        "target_topic": target.topic_key,
        "context": context,
        "grants": access.grants_hash(db, actor),
        "text": text,
        "mode": mode,
        "files": files,
        "request_data": request_data,
    }


def digest(snapshot):
    return sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare(db, actor, draft):
    w = db.get(Work, draft.target_id)
    versioned(w, draft.target_version)
    snapshot = fingerprint(db, actor, w, draft.data)
    draft.data = {
        **draft.data,
        "_snapshot": snapshot,
        "_digest": digest(snapshot),
        "_expires": (now() + timedelta(minutes=15)).isoformat(),
    }
    return snapshot


def send(db, actor, draft):
    w = db.scalar(
        select(Work).where(Work.id == draft.target_id).with_for_update().execution_options(populate_existing=True)
    )
    old = db.scalar(select(ClientPublication).where(ClientPublication.origin_key == "ui:" + draft.id).with_for_update())
    if old:
        return {"id": old.id, "status": old.status}
    versioned(w, draft.target_version)
    from datetime import datetime

    if not draft.data.get("_expires") or datetime.fromisoformat(draft.data["_expires"]) <= now():
        raise BusinessError("Предпросмотр истёк. Проверьте комплект заново.")
    snapshot = fingerprint(db, actor, w, draft.data)
    if digest(snapshot) != draft.data.get("_digest"):
        raise Conflict("Права, адресат или файлы изменились после предпросмотра.")
    request_context = {}
    request_row = None
    if snapshot["mode"] == "request":
        from ama.service import transition
        from ama.sla import case_for

        transition(
            db,
            actor,
            w.id,
            w.version,
            "wait_partner",
            {"party": "ФП", "required": snapshot["text"], **snapshot["request_data"]},
            "publication:" + draft.id,
        )
        c = case_for(db, w)
        if c and c.data.get("request"):
            request_row = db.scalar(
                select(Outbox).where(Outbox.dedupe_key == "sla-request:" + c.data["request"]["token"])
            )
            request_context = {"sla_request": c.data["request"]["token"], "sla_case": c.id}
    p = ClientPublication(
        bot_id=settings().bot_id,
        origin_key="ui:" + draft.id,
        work_id=w.id,
        author_id=actor.person_id,
        source_binding_id=actor.binding_id,
        target_binding_id=snapshot["target_id"],
        data={"snapshot": deepcopy(snapshot), "parts": [], "sla": request_context},
    )
    db.add(p)
    db.flush()
    target = db.get(Binding, p.target_binding_id)
    title = "Запрос данных" if snapshot["mode"] == "request" else "Ответ по обращению"
    text = (
        title
        + " · "
        + w.title
        + "\n"
        + snapshot["text"]
        + "\nПредприятие: "
        + (db.get(Entity, w.establishment_id).name if w.establishment_id else "Все применимые")
        + "\nПункт: "
        + w.id
    )
    if request_row:
        request_row.payload = {k: v for k, v in request_row.payload.items() if k not in {"sla_request", "sla_case"}}
        request_row.payload = {**request_row.payload, "text": text}
    rows = [request_row or queue(db, target, "publication:" + p.id + ":text", text, work=w)]
    for i, item in enumerate(snapshot["files"]):
        rows.append(
            queue(
                db,
                target,
                "publication:" + p.id + ":file:" + str(i),
                item["filename"],
                work=w,
                attachment_id=item["id"],
            )
        )
    db.flush()
    for i, row in enumerate(rows):
        row.payload = {**row.payload, "client_publication": p.id, "publication_part": i, "partner_window": True}
    p.data = {**p.data, "parts": [r.id for r in rows]}
    db.add(
        Audit(
            actor_id=actor.person_id,
            object_id=p.id,
            action="CLIENT_PUBLICATION_CONFIRMED",
            data={
                "work_id": w.id,
                "target_binding_id": target.id,
                "files": snapshot["files"],
                "digest": digest(snapshot),
            },
        )
    )
    return {
        "id": p.id,
        "status": p.status,
        "recipient": target.platform + ":" + target.chat_id + ":" + target.topic_key,
        "ФП": db.get(Entity, w.partner_id).name,
        "Бренд": db.get(Entity, target.brand_id).name,
        "Предприятие": db.get(Entity, w.establishment_id).name if w.establishment_id else "Все применимые",
        "Текст": snapshot["text"][:400],
        "Выбранные файлы": [f["filename"] for f in snapshot["files"]],
        "Полный комплект": "Откройте «Полный текст и поля»; остальные внутренние файлы не отправляются.",
    }


def context_valid(db, p):
    if not p or p.bot_id != settings().bot_id or p.status == "cancelled":
        return False
    w = db.get(Work, p.work_id)
    actor = Actor(p.author_id, p.source_binding_id)
    snapshot = p.data["snapshot"]
    try:
        current = fingerprint(
            db,
            actor,
            w,
            {
                "text": snapshot["text"],
                "mode": snapshot["mode"],
                "attachment_ids": [f["id"] for f in snapshot["files"]],
                **snapshot.get("request_data", {}),
            },
        )
        # Routine progress does not retract already approved content, but ownership/area/recipients must stay the same.
        if p.data.get("sla"):
            from ama.models import SLACase

            c = db.get(SLACase, p.data["sla"]["sla_case"])
            if (
                not c
                or c.data.get("request", {}).get("token") != p.data["sla"]["sla_request"]
                or w.status != "waiting_partner"
            ):
                return False
        current["work_version"] = snapshot["work_version"]
        return digest(current) == digest(snapshot)
    except BusinessError:
        return False


def deliverable(db, row):
    if not row.payload.get("client_publication"):
        if row.payload.get("attachment_id"):
            a = db.get(Attachment, row.payload["attachment_id"])
            b = db.get(Binding, row.binding_id)
            if b and b.purpose == "client" and a and a.visibility != "client":
                return False
        return True
    p = db.scalar(
        select(ClientPublication).where(ClientPublication.id == row.payload["client_publication"]).with_for_update()
    )
    if not context_valid(db, p) or row.id not in p.data["parts"] or row.binding_id != p.target_binding_id:
        return False
    index = row.payload["publication_part"]
    if index and any(db.get(Outbox, ident).status != "sent" for ident in p.data["parts"][:index]):
        return None  # Do not send files ahead of their text / an uncertain earlier part.
    if index:
        item = p.data["snapshot"]["files"][index - 1]
        if row.payload.get("attachment_id") != item["id"]:
            return False
        file_path(db.get(Attachment, item["id"]), item)
    return True


def refresh(db, row):
    ident = row.payload.get("client_publication")
    if not ident:
        return
    p = db.scalar(select(ClientPublication).where(ClientPublication.id == ident).with_for_update())
    if not p:
        return
    cancelled = p.status == "cancelled"
    rows = [db.get(Outbox, key) for key in p.data["parts"]]
    states = {r.status for r in rows}
    p.status = (
        "cancelled"
        if cancelled
        else (
            "sent"
            if states == {"sent"}
            else "uncertain"
            if states & {"uncertain", "sending"}
            else "partial"
            if "sent" in states
            else "blocked"
            if states & {"failed", "blocked", "cancelled"}
            else "queued"
        )
    )
    p.data = {
        **p.data,
        "delivery": {r.id: {"status": r.status, "external_id": r.external_id} for r in rows},
        **({"delivered_at": now().isoformat()} if p.status == "sent" and not p.data.get("delivered_at") else {}),
    }
    if p.status == "sent" and p.data.get("sla"):
        from types import SimpleNamespace
        from ama.sla import delivered

        delivered(db, SimpleNamespace(id=rows[0].id, status="sent", work_id=p.work_id, payload=p.data["sla"]))
    p.version += 1


def cancel(db, actor, ident, expected):
    p = db.scalar(select(ClientPublication).where(ClientPublication.id == ident).with_for_update())
    if not p or p.bot_id != settings().bot_id:
        raise AccessDenied("Публикация недоступна.")
    authorize(db, actor, db.get(Work, p.work_id))
    versioned(p, expected)
    for ident in p.data["parts"]:
        row = db.get(Outbox, ident)
        if row.status in {"pending", "failed", "blocked"}:
            row.status = "cancelled"
    p.status = "cancelled"
    p.version += 1
    db.add(
        Audit(
            actor_id=actor.person_id,
            object_id=p.id,
            action="CLIENT_PUBLICATION_CANCELLED",
            data={"delivered_parts_remain_history": True},
        )
    )

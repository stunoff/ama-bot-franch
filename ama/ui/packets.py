from copy import deepcopy
from datetime import timedelta
from sqlalchemy import select
from ama import packets as flow
from ama.models import (
    Packet,
    PacketRevision,
    PacketObligation,
    PacketPublication,
    PacketSchedule,
    PacketQuestion,
    Entity,
    Binding,
    Work,
)
from ama.config import settings
from ama.errors import AccessDenied, BusinessError
from ama.ui import access, business
from ama.ui.controller import show, paginate, navigation, draft_screen, new_draft
from ama.ui.engine import button as B, render
from ama.models import now

LABELS = {
    "delivery_issue": "Задержка доставки; нужен будущий срок",
    "pending": "Ожидается",
    "acknowledged": "Ознакомление подтверждено",
    "inherited": "Повтор не требуется по решению автора",
    "accepted": "Результат принят",
    "review": "На проверке",
    "rework": "Доработка",
    "overdue": "Срок истёк",
    "cancelled": "Отменено",
}


def staff_items(db, actor, r):
    from types import SimpleNamespace

    return [
        i
        for i in r.audience["included"]
        if access.scope(
            db,
            actor,
            SimpleNamespace(**{k: i.get(k) for k in ("partner_id", "brand_id", "establishment_id")}),
            {"curator", "support_head", "marketing_manager"},
        )
    ]


def begin(db, actor, s, kind, target, data=None):
    d = business.make_draft(db, actor, kind, "Рассылки ФП: " + kind.split(":")[1], data or {}, target=target)
    from ama.ui import schema

    if not schema.fields_for(kind):
        from ama.ui.controller import dispatch as controller

        return controller(db, actor, s, "preview", {"draft_id": d.id})
    return draft_screen(db, actor, s, {"draft_id": d.id})


def dispatch(db, actor, s, action, params):
    if action == "pk_list":
        b = db.get(Binding, actor.binding_id)
        client = b.purpose == "client"
        rows = []
        for p in db.scalars(
            select(Packet).where(Packet.bot_id == settings().bot_id).order_by(Packet.created_at.desc())
        ):
            r = flow.revision(db, p)
            if flow.visible_items(db, actor, r) if client else staff_items(db, actor, r):
                rows.append((p, r))
        items, page = paginate(rows, params.get("page", 0))
        buttons = [B(p.title, "pk_view", revision_id=r.id) for p, r in items]
        if not client:
            try:
                flow.publisher(db, actor)
                buttons += [
                    B("Создать пакет…", "pk_new"),
                    B("Регулярные отправки", "pk_schedules"),
                    B("Черновики", "drafts"),
                ]
            except AccessDenied:
                pass
        return show(
            db,
            actor,
            s,
            f"Рассылки ФП: {len(rows)}",
            buttons + navigation("pk_list", page, len(rows)) + [B("В меню", "home")],
            "pk_list",
            page=page,
        )
    if action == "pk_new":
        flow.publisher(db, actor)
        d = new_draft(
            db,
            actor,
            "packet:create",
            {
                "audience": "brands",
                "mode": "information",
                "scope": "partner_brand",
                "population": "all",
                "call_center": "all",
                "calendar": "support",
                "remind_days": 0,
                "escalate_days": 0,
            },
        )
        return draft_screen(db, actor, s, {"draft_id": d.id})
    if action in {"pk_asset", "pk_preview_audience"}:
        from ama.ui.engine import view_draft

        d = view_draft(db, actor, params["draft_id"])
        if action == "pk_asset":
            flow.publisher(db, actor)
            return render(
                db,
                actor,
                s,
                "Отправьте файлы/фото для внешнего пакета. Видео — ссылками. После сохранения проверьте пакет перед отправкой.",
                [B("← Черновик", "draft", draft_id=d.id)],
                prompt={
                    "type": "packet_asset",
                    "draft_id": d.id,
                    "expires": (now() + timedelta(minutes=15)).isoformat(),
                },
            )
        snap = d.before.get("_checked", {}).get("packet_audience")
        if not snap:
            raise BusinessError("Сначала откройте предпросмотр пакета.")
        rows = [(i, "Включено") for i in snap["included"]] + [(i, i["reason"]) for i in snap["excluded"]]
        items, page = paginate(rows, params.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Аудитория зафиксирована на момент предпросмотра:\n"
            + "\n".join(i["name"] + " · " + i["brand_id"] + " · " + reason for i, reason in items),
            navigation("pk_preview_audience", page, len(rows), draft_id=d.id)
            + [B("← Предпросмотр", "preview", draft_id=d.id)],
            "pk_preview_audience",
            draft_id=d.id,
            page=page,
        )
    if action == "pk_schedules":
        flow.publisher(db, actor)
        rows = list(
            db.scalars(
                select(PacketSchedule)
                .where(PacketSchedule.bot_id == settings().bot_id, PacketSchedule.author_id == actor.person_id)
                .order_by(PacketSchedule.next_at)
            )
        )
        items, page = paginate(rows, params.get("page", 0))
        buttons = [
            B(
                x.data["payload"]["title"] + " · " + ("активно" if x.enabled else "остановлено"),
                "pk_schedule",
                schedule_id=x.id,
            )
            for x in items
        ]
        return show(
            db,
            actor,
            s,
            "Регулярные пакеты: каждый цикл имеет свою редакцию и аудиторию.",
            buttons
            + [B("Новое расписание…", "pk_schedule_new")]
            + navigation("pk_schedules", page, len(rows))
            + [B("← Рассылки", "pk_list")],
            "pk_schedules",
            page=page,
        )
    if action == "pk_schedule_new":
        flow.publisher(db, actor)
        d = new_draft(
            db,
            actor,
            "packet:schedule",
            {
                "payload": {
                    "audience": "brands",
                    "mode": "information",
                    "scope": "partner_brand",
                    "population": "all",
                    "call_center": "all",
                    "calendar": "support",
                    "remind_days": 0,
                    "escalate_days": 0,
                }
            },
        )
        return draft_screen(db, actor, s, {"draft_id": d.id})
    if action in {"pk_schedule", "pk_schedule_stop"}:
        row = db.get(PacketSchedule, params["schedule_id"])
        if not row or row.author_id != actor.person_id or row.bot_id != settings().bot_id:
            raise AccessDenied("Расписание другого автора.")
        flow.publisher(db, actor)
        if action == "pk_schedule_stop":
            return begin(db, actor, s, "packet:stop", row, {})
        return show(
            db,
            actor,
            s,
            row.data["payload"]["title"]
            + f"\nСледующий запуск: {row.next_at} UTC\nКонец: {row.until_at} UTC\n"
            + str(row.data.get("error") or row.data.get("stop_reason") or ""),
            [B("Остановить…", "pk_schedule_stop", schedule_id=row.id), B("← Расписания", "pk_schedules")],
            "pk_schedule",
            schedule_id=row.id,
        )
    if action in {"pk_ob", "pk_ack", "pk_submit", "pk_upload", "pk_reschedule"}:
        ob = db.get(PacketObligation, params["obligation_id"])
        if not ob:
            raise BusinessError("Обязательство не найдено.")
        flow.authorize_ob(db, actor, ob)
        r = db.get(PacketRevision, ob.revision_id)
        p = flow.get(db, r.packet_id)
        if action == "pk_reschedule":
            return begin(db, actor, s, "obligation:reschedule", ob, {})
        if action == "pk_ack":
            flow.authorize_ob(db, actor, ob, True)
            return begin(db, actor, s, "obligation:ack", ob, {})
        if action == "pk_submit":
            flow.authorize_ob(db, actor, ob, True)
            w = db.get(Work, ob.work_id)
            return begin(db, actor, s, "obligation:submit", ob, {"work_version": w.version, "completed_items": []})
        if action == "pk_upload":
            flow.authorize_ob(db, actor, ob, True)
            w = db.get(Work, ob.work_id)
            return render(
                db,
                actor,
                s,
                "Отправьте доказательства для конкретного предприятия. Фото другого участника не подтверждает результат.",
                [B("← Обязательство", "pk_ob", obligation_id=ob.id)],
                prompt={
                    "type": "attachment",
                    "work_id": w.id,
                    "work_version": w.version,
                    "expires": (now() + timedelta(minutes=15)).isoformat(),
                },
            )
        text = (
            p.title
            + "\n"
            + obligation_label(db, ob)
            + "\n"
            + LABELS[flow.status(db, ob)]
            + "\nСрок: "
            + ob.due_at.isoformat()
            + " UTC"
        )
        text += "\n" + "\n".join("• " + x for x in r.data.get("checklist", []))
        if r.data.get("evidence_requirements"):
            text += "\nОжидаемый результат: " + r.data["evidence_requirements"]
        if r.data.get("evidence_mode") in {"photo", "file"}:
            text += "\nПриложите " + ("фото" if r.data["evidence_mode"] == "photo" else "файл") + " к этому пункту."
        buttons = []
        if (
            actor.person_id == ob.curator_id
            and access.scope(db, actor, ob, {"curator", "support_head"})
            and flow.status(db, ob) not in {"accepted", "acknowledged", "inherited", "cancelled"}
        ):
            buttons.append(B("Согласовать будущий срок…", "pk_reschedule", obligation_id=ob.id))
        if flow.ob_visible(db, actor, ob, acting=True) and flow.status(db, ob) not in {
            "acknowledged",
            "inherited",
            "accepted",
            "cancelled",
        }:
            if r.data["mode"] == "acknowledgement":
                buttons.append(B("Подтвердить ознакомление…", "pk_ack", obligation_id=ob.id))
            elif flow.status(db, ob) != "review":
                buttons += [
                    B("Сдать результат…", "pk_submit", obligation_id=ob.id),
                    B("Добавить доказательства", "pk_upload", obligation_id=ob.id),
                ]
        if ob.work_id:
            w = db.get(Work, ob.work_id)
            if access.can_work(db, actor, w, detail=True) and db.get(Binding, actor.binding_id).purpose != "client":
                buttons.append(B("Приёмка / рабочая карточка", "work", work_id=w.id))
        return show(
            db, actor, s, text, buttons + [B("← Пакет", "pk_view", revision_id=r.id)], "pk_ob", obligation_id=ob.id
        )
    r = db.get(PacketRevision, params["revision_id"])
    if not r:
        raise BusinessError("Редакция не найдена.")
    p = flow.get(db, r.packet_id)
    client = db.get(Binding, actor.binding_id).purpose == "client"
    items = flow.visible_items(db, actor, r) if client else staff_items(db, actor, r)
    if not items:
        raise AccessDenied("Пакет недоступен в этой области.")
    if action in {"pk_questions", "pk_question_card", "pk_answer"}:
        queries = list(db.scalars(select(PacketQuestion).where(PacketQuestion.revision_id == r.id)))
        queries = (
            [
                q
                for q in queries
                if q.person_id == actor.person_id and q.partner_id == db.get(Binding, actor.binding_id).partner_id
            ]
            if client
            else [q for q in queries if access.can_work(db, actor, db.get(Work, q.work_id))]
        )
        if action == "pk_questions":
            shown, page = paginate(queries, params.get("page", 0))
            return show(
                db,
                actor,
                s,
                "Индивидуальные вопросы по пакету",
                [B(q.scope_key, "pk_question_card", revision_id=r.id, question_id=q.id) for q in shown]
                + navigation("pk_questions", page, len(queries), revision_id=r.id)
                + [B("← Пакет", "pk_view", revision_id=r.id)],
                "pk_questions",
                revision_id=r.id,
                page=page,
            )
        q = next((q for q in queries if q.id == params["question_id"]), None)
        if not q:
            raise AccessDenied("Вопрос другого получателя.")
        w = db.get(Work, q.work_id)
        if action == "pk_answer":
            return begin(db, actor, s, "packet:answer", w, {})
        text = "Вопрос по пакету: " + r.data["title"]
        if client or access.can_work(db, actor, w, detail=True):
            text += "\n" + "\n".join("ФП: " + x["text"] for x in w.data.get("public_questions", []))
            text += "\n" + "\n".join("Ответ УК: " + x["text"] for x in w.data.get("public_answers", []))
        buttons = []
        if not client:
            buttons.append(B("Рабочая карточка / тема", "work", work_id=w.id))
            if (
                access.can_work(db, actor, w, detail=True)
                and w.owner_id == actor.person_id
                and w.status not in {"done", "review"}
            ):
                buttons.append(B("Ответить ФП…", "pk_answer", revision_id=r.id, question_id=q.id))
        return show(
            db,
            actor,
            s,
            text,
            buttons + [B("← Вопросы", "pk_questions", revision_id=r.id)],
            "pk_question_card",
            revision_id=r.id,
            question_id=q.id,
        )
    if action == "pk_question":
        if not client:
            raise AccessDenied("Вопрос создаёт ФП из клиентского чата.")
        return begin(db, actor, s, "packet:question", r, {})
    if action == "pk_edit":
        kind = params["kind"]
        if kind not in {"revise", "typo", "add"}:
            raise BusinessError("Неизвестное действие пакета.")
        if kind != "add":
            flow.authorize_edit(db, actor, p, r)
        else:
            flow.publisher(db, actor)
        data = (
            {"text": r.data["text"]}
            if kind == "typo"
            else {"payload": deepcopy(r.data), "repeat_required": False, "cancel_pending": False}
            if kind == "revise"
            else {}
        )
        return begin(db, actor, s, "packet:" + kind, p, data)
    if action in {"pk_delivery", "pk_retry"}:
        if client:
            raise AccessDenied("Диагностика доставки доступна сотрудникам.")
        pubs = [
            x
            for x in db.scalars(select(PacketPublication).where(PacketPublication.revision_id == r.id))
            if any(
                (i["partner_id"], i["establishment_id"], i["brand_id"])
                in {(j["partner_id"], j["establishment_id"], j["brand_id"]) for j in x.data["items"]}
                for i in items
            )
        ]
        if action == "pk_retry":
            pub = next((x for x in pubs if x.id == params["publication_id"]), None)
            if not pub:
                raise AccessDenied("Публикация не в вашей области.")
            flow.authorize_edit(db, actor, p, r)
            return begin(db, actor, s, "packet:retry", pub, {})
        rows, page = paginate(pubs, params.get("page", 0))
        buttons = [
            B(x.binding_id + " · " + flow.publication_state(db, x), "pk_retry", revision_id=r.id, publication_id=x.id)
            for x in rows
        ]
        return show(
            db,
            actor,
            s,
            "Доставка по частям: sent не означает прочтение. Неопределённые отправки сначала сверяются.",
            buttons
            + navigation("pk_delivery", page, len(pubs), revision_id=r.id)
            + [B("← Пакет", "pk_view", revision_id=r.id)],
            "pk_delivery",
            revision_id=r.id,
            page=page,
        )
    if action == "pk_history":
        rows = list(
            db.scalars(
                select(PacketRevision).where(PacketRevision.packet_id == p.id).order_by(PacketRevision.number.desc())
            )
        )
        rows = (
            [x for x in rows if flow.visible_items(db, actor, x)]
            if client
            else [x for x in rows if staff_items(db, actor, x)]
        )
        shown, page = paginate(rows, params.get("page", 0))
        return show(
            db,
            actor,
            s,
            "История редакций; старые подтверждения сохраняются.",
            [B(f"Редакция {x.number}", "pk_view", revision_id=x.id) for x in shown]
            + navigation("pk_history", page, len(rows), revision_id=r.id)
            + [B("← Пакет", "pk_view", revision_id=r.id)],
            "pk_history",
            revision_id=r.id,
            page=page,
        )
    if action == "pk_view":
        pub = (
            db.scalar(
                select(PacketPublication).where(
                    PacketPublication.revision_id == r.id, PacketPublication.binding_id == actor.binding_id
                )
            )
            if client
            else None
        )
        text = (
            flow.packet_text(db, p, r, type("View", (), {"data": {"items": items}})())
            if pub
            else p.title + f"\nРедакция {r.number}\n" + r.data["text"]
        )
        if r.number != p.current_revision:
            text += "\nАрхивная редакция. Новые действия по ней недоступны."
        allowed_keys = {flow.scope_key(r.data, i) for i in items}
        obs = [
            o
            for o in db.scalars(select(PacketObligation).where(PacketObligation.revision_id == r.id))
            if o.scope_key in allowed_keys
        ]
        text += f"\nПринято {sum(flow.status(db, o) in {'acknowledged', 'inherited', 'accepted'} for o in obs)} из {len(obs)}"
        shown, page = paginate(obs, params.get("page", 0))
        buttons = [
            B(obligation_label(db, o) + " · " + LABELS[flow.status(db, o)], "pk_ob", obligation_id=o.id) for o in shown
        ]
        buttons += navigation("pk_view", page, len(obs), revision_id=r.id)
        if client:
            buttons.append(B("Задать вопрос куратору…", "pk_question", revision_id=r.id))
        else:
            buttons += [
                B("Доставка / ошибки", "pk_delivery", revision_id=r.id),
                B("Отчёт CSV…", "pk_report", revision_id=r.id),
            ]
            if actor.person_id == p.author_id and r.number == p.current_revision:
                buttons += [
                    B("Опечатка…", "pk_edit", revision_id=r.id, kind="typo"),
                    B("Новая редакция…", "pk_edit", revision_id=r.id, kind="revise"),
                ]
            if r.number == p.current_revision:
                buttons.append(B("Добавить новые предприятия…", "pk_edit", revision_id=r.id, kind="add"))
        buttons += [B("История редакций", "pk_history", revision_id=r.id), B("← Рассылки", "pk_list")]
        return show(db, actor, s, text, buttons, "pk_view", revision_id=r.id, page=page)
    if action == "pk_report":
        if client:
            raise AccessDenied("Внутренний отчёт не публикуется ФП.")
        return begin(db, actor, s, "packet:report", p, {})
    raise BusinessError("Неизвестный экран рассылки.")


def obligation_label(db, ob):
    base = db.get(Entity, ob.establishment_id or ob.partner_id)
    brand = db.get(Entity, ob.brand_id) if ob.brand_id else None
    return (base.name if base else "Получатель") + (" · " + brand.name if brand else " · общий пакет")

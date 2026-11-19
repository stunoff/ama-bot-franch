"""Scoped learning screens, progress and content operations."""

from sqlalchemy import select
from ama import learning as l
from ama.models import LearningTrack, Training, Record, Binding, Entity, EstablishmentBrand
from ama.auth import active_person_role
from ama.errors import AccessDenied, BusinessError
from ama.ui.engine import button as B
from ama.ui.controller import show, draft_screen, paginate, navigation
from ama.ui import business
from ama.ui.schema import f, ref, choice


def fields(kind):
    a = kind.split(":")[1]
    if a == "prepare":
        return []
    if a == "assign":
        return [
            ref("person_id", "ФП", "partner_person", True),
            ref("binding_id", "Клиентский чат", "client_binding", True),
            choice("course_key", "Ознакомление", {"general": "Общее", "call_center": "КЦ"}, True),
            choice(
                "mode",
                "Основание",
                {
                    "new": "Назначить / продолжить",
                    "inherit": "Подтверждено ранее",
                    "updates": "Только обязательные обновления (с прежним подтверждением)",
                },
                True,
            ),
            f("evidence", "Основание наследования"),
            f("known_version", "Подтверждённая версия"),
        ]
    if a == "deadline":
        return [
            f("due_at", "Согласованная будущая дата", "datetime", True),
            f("reason", "Основание договорённости", required=True),
        ]
    if a == "archive":
        return [
            f("replacement_key", "Ключ проверенной замены (пусто — техническое ожидание)"),
            f("reason", "Причина", required=True),
        ]
    if a == "problem":
        return [f("reason", "Что не открывается / какая ошибка", required=True)]
    return []


def dispatch(db, actor, s, action, p):
    if action == "lr_home":
        b = db.get(Binding, actor.binding_id)
        if b.purpose == "client":
            return show(
                db,
                actor,
                s,
                "Персональное ознакомление. Прогресс общий для человека, без экзаменов и оценок.",
                [
                    B("Продолжить общую базу", "lr_course", course="general"),
                    B("Короткий курс КЦ", "lr_course", course="call_center"),
                    B("База знаний", "lr_materials"),
                ],
                action,
            )
        if not active_person_role(db, actor.person_id, {"curator"}, b):
            raise AccessDenied("Прогресс доступен куратору в сопровождении.")
        tracks = []
        for tr in db.scalars(select(LearningTrack).where(LearningTrack.bot_id == l.settings().bot_id)):
            target = db.get(Binding, tr.binding_id)
            if active_person_role(db, actor.person_id, {"curator"}, target):
                tracks.append(tr)
        rows, page = paginate(tracks, p.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Персональные назначения: " + str(len(tracks)),
            [B(tr.course_key + " · " + tr.status + " · " + tr.person_id, "lr_progress", track=tr.id) for tr in rows]
            + navigation(action, page, len(tracks))
            + [B("Назначить / подтвердить прежнее", "lr_begin", op="assign"), B("Готовность контента", "lr_inventory")],
            action,
            page=page,
        )
    if action == "lr_inventory":
        from ama.ui import access

        access.require_staff(db, actor)
        courses = list(db.scalars(select(Record).where(Record.kind == "course", Record.published.is_(True))))
        from ama.learning_catalog import REQUIREMENTS

        lines = []
        for item in REQUIREMENTS:
            try:
                l.material(db, item["key"])
                status = "Опубликован"
            except BusinessError:
                status = "ТРЕБУЕТ ССЫЛКУ / ПОДТВЕРЖДЕНИЕ"
            lines.append(item["title"] + ": " + status)
        for c in courses:
            for k in sorted({k for card in c.data.get("cards", []) for k in card.get("required_materials", [])}):
                try:
                    l.material(db, k)
                    status = "Опубликован"
                except BusinessError:
                    status = "ТРЕБУЕТ ССЫЛКУ / ПРОВЕРКУ"
                lines.append(c.key + " → " + k + ": " + status)
        items, page = paginate(lines, p.get("page", 0))
        from ama.ui.repository import is_admin

        extra = (
            [B("Подготовить черновики материалов", "lr_begin", op="prepare"), B("Настройки", "settings")]
            if is_admin(db, actor)
            else []
        )
        return show(
            db,
            actor,
            s,
            "Материалы обязательных карточек:\n"
            + "\n".join(items or ["Зависимости пока не настроены. Это не подтверждение наличия реальных материалов."]),
            navigation(action, page, len(lines)) + extra + [B("Назад", "lr_home")],
            action,
            page=page,
        )
    if action == "lr_course":
        course = p.get("course", "general")
        t = l.action(db, actor, p.get("course", "general"), p.get("op", "open"), p.get("expected"))
        tr = l.track_for(db, t)
        card = t.cards[t.position]
        boundary = t.position == len(t.cards) - 1 or t.cards[t.position + 1].get("module", 0) != card.get("module", 0)
        text = (
            card["title"]
            + "\n"
            + card["text"]
            + f"\nКарточка {t.position + 1}/{len(t.cards)} · раздел {card.get('module', 0)}"
        )
        buttons = [
            B("Назад", "lr_course", course=course, op="back", expected=t.version),
            B("Материалы этой карточки", "lr_course", course=course, op="materials", expected=t.version),
        ]
        if t.completed_at or tr.status == "inherited":
            buttons.append(B("Далее (повторный просмотр)", "lr_course", course=course, op="next", expected=t.version))
            text += "\nОзнакомление подтверждено. Повторный просмотр не сбрасывает результат."
        elif tr.data.get("await_finish"):
            buttons.append(
                B(
                    "Ознакомился / завершить ознакомление",
                    "lr_course",
                    course=course,
                    op="finish",
                    expected=t.version,
                )
            )
        else:
            buttons.append(
                B(
                    "Завершить раздел" if boundary else "Понятно, дальше",
                    "lr_course",
                    course=course,
                    op="finish_module" if boundary else "next",
                    expected=t.version,
                )
            )
        try:
            l.check_card(db, actor, card)
        except BusinessError as error:
            text += "\nТехническое ожидание: " + str(error)
            for key in card.get("required_materials", []):
                buttons.append(B("Сообщить о проблеме", "lr_begin", op="problem", key=key))
        buttons += [
            B("Продолжить позже", "lr_course", course=course, op="later", expected=t.version),
            B("База знаний", "lr_materials"),
        ]
        return show(db, actor, s, text, buttons, action, course=course)
    if action == "lr_materials":
        materials = []
        for rec in db.scalars(select(Record).where(Record.kind == "material", Record.published.is_(True))):
            try:
                if l.can_material(db, actor, rec, establishment_id=p.get("establishment_id")):
                    l.material(db, rec.key)
                    materials.append(rec)
            except BusinessError:
                pass
        rows, page = paginate(materials, p.get("page", 0))
        binding = db.get(Binding, actor.binding_id)
        establishments = (
            [
                e
                for e in db.scalars(
                    select(Entity).where(Entity.kind == "establishment", Entity.partner_id == binding.partner_id)
                )
                if db.scalar(
                    select(EstablishmentBrand.id).where(
                        EstablishmentBrand.establishment_id == e.id,
                        EstablishmentBrand.brand_id == binding.brand_id,
                        EstablishmentBrand.active.is_(True),
                    )
                )
            ]
            if binding.purpose == "client"
            else []
        )
        choices = [B("Материалы предприятия: " + e.name, "lr_materials", establishment_id=e.id) for e in establishments]

        return show(
            db,
            actor,
            s,
            "База знаний: только разрешённые действующие материалы.",
            [
                B(r.data.get("title", r.key), "lr_material", key=r.key, establishment_id=p.get("establishment_id"))
                for r in rows
            ]
            + navigation(action, page, len(materials), establishment_id=p.get("establishment_id"))
            + choices
            + [B("Ознакомление", "lr_home")],
            action,
            page=page,
        )
    if action == "lr_material":
        rec = l.resolve(db, actor, p["key"], establishment_id=p.get("establishment_id"))
        l.show_material(db, actor, rec.key, establishment_id=p.get("establishment_id"))
        return show(
            db,
            actor,
            s,
            "Материал поставлен в очередь. Переход по ссылке не считается ознакомлением.",
            [B("Сообщить о проблеме", "lr_begin", op="problem", key=rec.key), B("База знаний", "lr_materials")],
            action,
            **p,
        )
    if action == "lr_progress":
        tr = db.get(LearningTrack, p["track"])
        target = db.get(Binding, tr.binding_id) if tr else None
        if (
            not target
            or db.get(Binding, actor.binding_id).purpose != "support"
            or not active_person_role(db, actor.person_id, {"curator"}, target)
        ):
            raise AccessDenied("Прогресс другого куратора / области.")
        t = db.get(Training, tr.training_id)
        return show(
            db,
            actor,
            s,
            f"{tr.course_key}: {tr.status}\nПодтверждено карточек: {len(t.acknowledged) if t else 0}\nЗавершённые разделы: {tr.data.get('modules', [])}\nПоследнее действие: {tr.data.get('last_action')}\nТехническое ожидание: {tr.data.get('technical_blocker', 'нет')}\nНапоминаний доставлено: {tr.data.get('round', 0)}",
            [B("Согласовать индивидуальную дату", "lr_begin", op="deadline", track=tr.id), B("Назад", "lr_home")],
            action,
            **p,
        )
    if action == "lr_begin":
        op = p["op"]
        target = db.get(LearningTrack, p["track"]) if p.get("track") else None
        if op in {"problem", "archive"}:
            target = db.scalar(select(Record).where(Record.kind == "material", Record.key == p["key"]))
        draft = business.make_draft(db, actor, "learning:" + op, "Ознакомление и материалы", {}, target)
        if op == "prepare":
            from ama.ui.controller import dispatch

            return dispatch(db, actor, s, "preview", {"draft_id": draft.id})
        return draft_screen(db, actor, s, {"draft_id": draft.id})
    raise BusinessError("Неизвестное действие ознакомления.")

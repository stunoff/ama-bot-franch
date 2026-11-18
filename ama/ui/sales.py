"""Buying interests, knowledge search and chief-marketer workspaces."""

from sqlalchemy import select
from ama.models import SalesCase, SalesExpert, SalesEvent, Lead
from ama.errors import AccessDenied
from ama.ui import access, business, repository as repo
from ama.ui.engine import button as B
from ama.ui.controller import show, paginate, navigation, draft_screen
from ama import sale_flow, prospects

ACTIONS = {
    "take": "Взять в работу",
    "qualify": "Уточнить профиль",
    "assign_brand": "Назначить проект",
    "stage": "Этап продажи",
    "financial_review": "Финансовый разбор проведён",
    "reply": "Ответить лиду",
    "call": "Зафиксировать звонок",
    "call_schedule": "Назначить звонок",
    "contact": "Следующий контакт",
    "wait_lead": "Ждём лида",
    "not_now": "Не сейчас",
    "expert": "Вопрос Главному маркетологу",
    "ready": "Готов к договору",
    "contract": "Передать в договоры",
    "refuse": "Отказ",
    "not_target": "Не целевой",
    "reopen": "Возобновить с согласия",
    "note": "Внутренняя заметка",
    "reassign": "Переназначить консультанта",
}


def dispatch(db, actor, s, action, p):
    access.require_staff(db, actor)
    if action in {"sales_cases", "leads"}:
        # Existing people become one primary interest; no duplicate person or purchase.
        for lead in db.scalars(select(Lead)):
            if access.can_lead(db, actor, lead):
                sale_flow.primary(db, lead)
        mode, search = p.get("filter", "active"), p.get("search", "").casefold()
        rows = [
            c
            for c in db.scalars(select(SalesCase).order_by(SalesCase.created_at.desc()))
            if sale_flow.can_case(db, actor, c)
        ]
        if mode == "active":
            rows = [c for c in rows if c.result not in sale_flow.TERMINAL]
        elif mode == "contact":
            rows = [c for c in rows if c.data.get("requires_contact")]
        elif mode == "calls":
            rows = [c for c in rows if c.data.get("communication") == "call" and c.result not in sale_flow.TERMINAL]
        if search:
            rows = [
                c
                for c in rows
                if search in (c.id + " " + c.data.get("name", "") + " " + c.data.get("city", "")).casefold()
            ]
        items, page = paginate(rows, p.get("page", 0))
        options = [
            B(
                c.data.get("name", "Лид")
                + " · "
                + repo.entity_name(db, c.brand_id)
                + (" · требуется контакт" if c.data.get("requires_contact") else ""),
                "sales_case",
                case_id=c.id,
            )
            for c in items
        ]
        options += navigation("sales_cases", page, len(rows), filter=mode, search=p.get("search", ""))
        options += [
            B(label, "sales_cases", filter=k)
            for k, label in {
                "active": "Активные",
                "all": "Все интересы",
                "contact": "Требуется контакт",
                "calls": "Звонки",
            }.items()
            if k != mode
        ]
        options += [B("Поиск", "search", target="sales_cases", target_params={"filter": mode}), B("В меню", "home")]
        return show(
            db,
            actor,
            s,
            f"Очередь продаж · {len(rows)} интересов.\nЛид, интерес, договор и передача куратору учитываются раздельно.",
            options,
            "sales_cases",
            **p,
        )
    if action in {
        "sales_case",
        "sales_action",
        "sales_history",
        "sales_knowledge",
        "sales_kb_entry",
        "sales_expert_answers",
        "sales_expert_answer",
        "sales_expert_draft",
    }:
        case = sale_flow.get(db, p["case_id"])
        sale_flow.authorize(db, actor, case)
        if action == "sales_action":
            data = {}
            if p["name"] == "qualify":
                from ama.ui.schema import fields_for

                data = {k["key"]: case.data[k["key"]] for k in fields_for("case:qualify") if k["key"] in case.data}
            draft = business.make_draft(db, actor, "case:" + p["name"], ACTIONS[p["name"]], data, target=case)
            if p["name"] in {"take", "ready"}:
                from ama.ui.controller import dispatch as controller

                return controller(db, actor, s, "preview", {"draft_id": draft.id})
            return draft_screen(db, actor, s, {"draft_id": draft.id})
        if action == "sales_expert_answers":
            rows = list(
                db.scalars(
                    select(SalesExpert).where(SalesExpert.case_id == case.id).order_by(SalesExpert.due_at.desc())
                )
            )
            items, page = paginate(rows, p.get("page", 0))
            return show(
                db,
                actor,
                s,
                "Внутренние консультации по интересу",
                [
                    B(e.question[:70] + " · " + e.state, "sales_expert_answer", case_id=case.id, expert_id=e.id)
                    for e in items
                ]
                + navigation("sales_expert_answers", page, len(rows), case_id=case.id)
                + [B("К интересу", "sales_case", case_id=case.id)],
                "sales_expert_answers",
                **p,
            )
        if action in {"sales_expert_answer", "sales_expert_draft"}:
            expert = db.get(SalesExpert, p["expert_id"])
            if not expert or expert.case_id != case.id:
                raise AccessDenied("Ответ относится к другому интересу.")
            if action == "sales_expert_draft":
                draft = business.make_draft(
                    db,
                    actor,
                    "case:reply",
                    "Клиентский ответ после внутренней консультации",
                    {"text": "", "touch_reason": "consultant_reply"},
                    target=case,
                    before={"expert_reference": expert.id},
                )
                return draft_screen(db, actor, s, {"draft_id": draft.id})
            return show(
                db,
                actor,
                s,
                "Внутренняя консультация\nВопрос: " + expert.question + "\nОтвет: " + (expert.answer or "Ещё не готов"),
                (
                    [B("Составить клиентский ответ…", "sales_expert_draft", case_id=case.id, expert_id=expert.id)]
                    if expert.answer
                    else []
                )
                + [B("К консультациям", "sales_expert_answers", case_id=case.id)],
                "sales_expert_answer",
                **p,
            )
        if action == "sales_history":
            rows = list(
                db.scalars(
                    select(SalesEvent).where(SalesEvent.case_id == case.id).order_by(SalesEvent.created_at.desc())
                )
            )
            items, page = paginate(rows, p.get("page", 0))
            text = "История контактов:\n" + "\n\n".join(
                f"{r.created_at:%d.%m.%Y %H:%M} UTC · {r.kind}\n"
                + "\n".join(str(k) + ": " + str(v) for k, v in r.data.items())
                for r in items
            )
            return show(
                db,
                actor,
                s,
                text,
                navigation("sales_history", page, len(rows), case_id=case.id)
                + [B("К интересу", "sales_case", case_id=case.id)],
                "sales_history",
                **p,
            )
        if action == "sales_knowledge":
            rows = prospects.knowledge(db, case.brand_id, search=p.get("search", ""), public=False)
            items, page = paginate(rows, p.get("page", 0))
            opts = [B(r.data.get("title", r.key), "sales_kb_entry", case_id=case.id, record_id=r.id) for r in items]
            opts += navigation("sales_knowledge", page, len(rows), case_id=case.id, search=p.get("search", ""))
            opts += [
                B("Поиск по базе", "search", target="sales_knowledge", target_params={"case_id": case.id}),
                B("Вопрос Главному маркетологу…", "sales_action", case_id=case.id, name="expert"),
                B("К интересу", "sales_case", case_id=case.id),
            ]
            return show(
                db,
                actor,
                s,
                "Утверждённая база выбранного проекта. Внутренние записи не пересылаются клиенту автоматически.",
                opts,
                "sales_knowledge",
                **p,
            )
        if action == "sales_kb_entry":
            row = next(
                (r for r in prospects.knowledge(db, case.brand_id, public=False) if r.id == p["record_id"]), None
            )
            if not row:
                raise AccessDenied("Материал больше не действует или относится к другому бренду.")
            opts = [B("К базе", "sales_knowledge", case_id=case.id), B("К интересу", "sales_case", case_id=case.id)]
            if row.data.get("public", True):
                opts.insert(
                    0,
                    B(
                        "Подготовить клиентский ответ…",
                        "sales_kb_draft",
                        case_id=case.id,
                        record_id=row.id,
                        version=row.version,
                    ),
                )
            return show(db, actor, s, prospects.content(row), opts, "sales_kb_entry", **p)
        lead = db.get(Lead, case.lead_id)
        text = (
            sale_flow.card(db, case) + "\nОтветственный: " + repo.name(db, case.owner_id) + f"\nВерсия: {case.version}"
        )
        text += "\nПоследнее сообщение лида: " + case.data.get("last_message", "не было")
        text += "\nАвтоматических напоминаний в текущем молчании доставлено: " + str(
            lead.data.get("silence", {}).get("sent", 0)
        )
        names = (
            ["take"]
            if not case.owner_id or case.data.get("needs_accept")
            else [
                "qualify",
                "assign_brand",
                "stage",
                "financial_review",
                "reply",
                "call",
                "call_schedule",
                "contact",
                "wait_lead",
                "not_now",
                "expert",
                "ready",
                "contract",
                "refuse",
                "not_target",
                "note",
            ]
            if case.result not in sale_flow.TERMINAL
            else ["reopen"]
            if case.result != "success"
            else ["note"]
        )
        if case.stage in {"contract", "signing", "payment_wait"}:
            names = ["reply", "note", "expert"]
        opts = [
            B(ACTIONS[n] + "…", "sales_action", case_id=case.id, name=n)
            for n in names
            if not case.owner_id or case.owner_id == actor.person_id
        ]
        if sale_flow.manager(db, actor):
            opts.append(B("Переназначить консультанта…", "sales_action", case_id=case.id, name="reassign"))
        opts += [
            B("База продаж проекта", "sales_knowledge", case_id=case.id),
            B("Внутренние консультации и ответы", "sales_expert_answers", case_id=case.id),
            B("История контактов", "sales_history", case_id=case.id),
            B("← Продажи", "sales_cases"),
            B("В меню", "home"),
        ]
        return show(db, actor, s, text, opts, "sales_case", case_id=case.id)
    if action == "sales_kb_draft":
        case = sale_flow.get(db, p["case_id"])
        sale_flow.authorize(db, actor, case)
        row = next(
            (r for r in prospects.knowledge(db, case.brand_id) if r.id == p["record_id"] and r.version == p["version"]),
            None,
        )
        if not row:
            raise AccessDenied("Публичная редакция материала изменилась.")
        d = business.make_draft(
            db,
            actor,
            "case:reply",
            "Клиентский ответ из базы",
            {
                "text": prospects.content(row),
                "knowledge_id": row.id,
                "knowledge_version": row.version,
                "touch_reason": "materials",
            },
            target=case,
        )
        return draft_screen(db, actor, s, {"draft_id": d.id})
    if action == "experts":
        rows = []
        for e in db.scalars(select(SalesExpert).order_by(SalesExpert.due_at)):
            case = sale_flow.get(db, e.case_id)
            area = type("Scope", (), {"partner_id": None, "brand_id": case.brand_id, "establishment_id": None})()
            if e.owner_id == actor.person_id and access.scope(db, actor, area, {"chief_marketer"}):
                rows.append(e)
        items, page = paginate(rows, p.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Внутренние консультации Главному маркетологу",
            [B(e.question[:60] + " · " + e.state, "expert", expert_id=e.id) for e in items]
            + navigation("experts", page, len(rows))
            + [B("В меню", "home")],
            "experts",
            **p,
        )
    if action in {"expert", "expert_action"}:
        e = db.get(SalesExpert, p["expert_id"])
        if not e or e.owner_id != actor.person_id:
            raise AccessDenied("Консультация недоступна.")
        case = sale_flow.get(db, e.case_id)
        area = type("Scope", (), {"partner_id": None, "brand_id": case.brand_id, "establishment_id": None})()
        if not access.scope(db, actor, area, {"chief_marketer"}):
            raise AccessDenied("Нет назначения Главного маркетолога в этой области.")
        if action == "expert_action":
            if actor.binding_id != e.binding_id:
                raise AccessDenied(
                    "Откройте консультацию в исходной теме: "
                    + repo.label(db, db.get(__import__("ama.models", fromlist=["Binding"]).Binding, e.binding_id))
                )
            d = business.make_draft(db, actor, "expert:" + p["name"], "Внутренний ответ консультанту", {}, target=e)
            return draft_screen(db, actor, s, {"draft_id": d.id})
        opts = (
            [B("Ответить консультанту…", "expert_action", expert_id=e.id, name="answer")]
            if e.state != "answered"
            else []
        )
        return show(
            db,
            actor,
            s,
            sale_flow.card(db, case) + f"\nВопрос: {e.question}\nСрок UTC: {e.due_at}\nОтвет: {e.answer or 'пока нет'}",
            opts + [B("К консультациям", "experts"), B("В меню", "home")],
            "expert",
            **p,
        )
    raise AccessDenied("Экран продаж недоступен.")

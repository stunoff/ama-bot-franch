"""Role-aware employee dashboards and domain action forms."""
from copy import deepcopy
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from ama.auth import roles_for
from ama.errors import BusinessError, AccessDenied
from ama.models import Work, Entity, Binding, Lead, Deal, Attachment, Audit, Role, Training, EstablishmentBrand, Payment, now
from ama.service import STATUSES, versioned, queue
from ama.ui import access, business, repository as repo, schema
from ama.ui.engine import button as B, render
from ama.ui.controller import show, paginate, navigation, draft_screen

OPERATIONS = {"offer": "Предложение платной услуги", "invoice": "Выставить счёт", "debt_payment": "Подтвердить оплату долга",
              "deferral": "Согласовать отсрочку", "passport": "Цифровой паспорт", "tech_confirm": "Подтвердить подключение",
              "opening_confirm": "Подтвердить допуск к открытию"}
LEAD_ACTIONS = {"take": "Взять лида", "qualify": "Квалификация", "stage": "Изменить этап", "wait_lead": "Ожидаем лида",
                "not_now": "Не сейчас", "no_answer": "Недозвон", "refuse": "Зафиксировать отказ", "reopen": "Возобновить с согласием", "reply": "Ответить лиду"}
DEAL_ACTIONS = {"sent": "Обязательный комплект отправлен", "curator_clarify": "Запросить уточнение по передаче", "package": "Новая редакция документов", "signed": "Подтвердить подписи", "payment": "Подтвердить оплату",
                "confirm": "Проверить условия успеха", "assign_curator": "Назначить куратора", "curator_accept": "Принять ФП в сопровождение"}
FILTERS = {"active": "Открытые работы", "mine": "Моя очередь", "review": "На моей приёмке", "overdue": "Просроченные", "waiting": "Ожидания", "done": "Завершённые", "all": "Все работы", "blockers": "Блокеры", "requests": "Запросы ФП"}


def date_text(db, w, date=None):
    est = db.get(Entity, w.establishment_id) if w.establishment_id else None
    tz = (est.data.get("timezone") if est else None) or "Europe/Moscow"
    try:
        local = (date or w.due_at).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))
    except (ValueError, KeyError):
        tz, local = "UTC", (date or w.due_at)
    return f"{local:%d.%m.%Y %H:%M} ({tz})"


def work(db, actor, work_id, *, detail=False):
    w = db.get(Work, work_id)
    if not w or not access.can_work(db, actor, w, detail=detail):
        raise AccessDenied("Работа недоступна в этой области или чате.")
    return w


def work_actions(db, actor, w):
    if w.status == "done" or not access.can_work(db, actor, w, detail=True):
        return []
    roles = {r for r in roles_for(db, actor, w) & access.STAFF if access.staff_role(db, actor, r)}
    if not roles:
        return []
    actions = []
    if actor.person_id == w.owner_id:
        if w.status in {"new", "waiting_uk", "rework"}:
            actions.append("take")
        if w.status in {"in_progress", "materials", "rework"}:
            actions.append("submit")
        if w.status.startswith("waiting_"):
            actions.append("resume")
        actions += ["wait_partner", "wait_uk", "wait_external"]
        if not w.pending_owner_id:
            actions.append("handoff")
    required = {"marketing_manager"} if w.kind.startswith("marketing") else {"contract"} if w.kind in {"legal", "debt"} else {"curator"} if w.kind in {"internal_opening", "technical_opening", "official_opening"} else access.STAFF
    if actor.person_id == w.reviewer_id and w.status == "review" and roles & required:
        actions += ["accept", "reject"]
    if actor.person_id == w.pending_owner_id and actor.binding_id == w.pending_binding_id:
        actions.append("handoff_accept")
    if actor.person_id in {w.owner_id, w.reviewer_id}:
        actions.append("reschedule")
    if roles & {"curator", "support_head"}:
        actions.append("cancel")
    return actions


def operations(db, actor, w):
    roles = {r for r in roles_for(db, actor, w) & access.STAFF if access.staff_role(db, actor, r)}
    result = []
    if w.status == "done":
        return result
    if w.kind == "paid_work" and actor.person_id in {w.owner_id, w.executor_id} and roles & {"curator", "specialist", "marketing_manager"}:
        if db.get(Binding, w.binding_id).purpose in {"projecting", "software", "personnel", "menu", "design", "advertising", "printing", "systems_training", "smm"}:
            result.append("offer")
    if w.kind == "debt" and "contract" in roles:
        result += ["invoice", "debt_payment", "deferral"]
    if w.kind == "digital":
        if roles & {"curator", "specialist"}:
            result.append("passport")
        if "specialist" in roles and actor.person_id == w.executor_id:
            result.append("tech_confirm")
    if w.kind in {"internal_opening", "technical_opening", "official_opening"} and "curator" in roles and actor.person_id == w.reviewer_id:
        result.append("opening_confirm")
    return result


def begin_action(db, actor, s, kind, target, data=None):
    d = business.make_draft(db, actor, kind, schema.ACTIONS.get(kind.split(":")[-1], OPERATIONS.get(kind.split(":")[-1], LEAD_ACTIONS.get(kind.split(":")[-1], DEAL_ACTIONS.get(kind.split(":")[-1], "Передача на договор")))),
                            data or {}, target=target)
    if not schema.fields_for(kind):
        from ama.ui.controller import dispatch
        return dispatch(db, actor, s, "preview", {"draft_id": d.id})
    return draft_screen(db, actor, s, {"draft_id": d.id})


def dispatch(db, actor, s, action, p):
    access.require_staff(db, actor)
    if action == "leads" or action.startswith("sales_") or action in {"experts", "expert", "expert_action"}:
        from ama.ui.sales import dispatch as sales_ui
        return sales_ui(db, actor, s, action, p)
    if action in {"home", "workspace"}:
        rows = access.works(db, actor)
        active = [w for w in rows if w.status != "done"]
        mine = [w for w in active if w.owner_id == actor.person_id]
        review = [w for w in active if w.status == "review" and w.reviewer_id == actor.person_id]
        late = [w for w in active if w.due_at < now()]
        granted = {r.name for r in access.assignments(db, actor) if access.staff_role(db, actor, r.name)}
        text = f"Рабочее место · {repo.name(db, actor.person_id)}\n" + ", ".join(schema.ROLES.get(r, r) for r in sorted(granted))
        text += f"\nОткрыто: {len(active)} · Моя очередь: {len(mine)} · Приёмка: {len(review)} · Просрочено: {len(late)}"
        actions = [B(f"Моя очередь · {len(mine)}", "works", filter="mine"), B(f"Моя приёмка · {len(review)}", "works", filter="review"),
                   B(f"Просрочено · {len(late)}", "works", filter="overdue"), B("Все доступные работы", "works"), B("Ожидания", "works", filter="waiting")]
        if granted & {"curator", "support_head", "marketing_manager"}:
            actions += [B("Предприятия", "enterprises"), B("Франчайзи-партнёры", "partners"), B("Создать работу…", "new", kind="create_work"), *( [B("Новый проект…", "new", kind="plan:create")] if granted & {"curator", "support_head"} else []),
                        B("Мои проекты", "pp_list"), B("Блокеры", "works", filter="blockers"), B("Запросы ФП", "works", filter="requests"), B("Календарь сроков", "calendar"), B("Информационная публикация…", "new", kind="broadcast")]
        if "chief_marketer" in granted:
            actions.append(B("Внутренние консультации", "experts"))
        if "consultant" in granted:
            actions.append(B("Очередь продаж", "leads"))
        if granted & {"contract", "curator", "support_head"}:
            actions.append(B("Договоры и передача ФП", "deals"))
        actions += [B("Аналитика и выгрузки…", "new", kind="export"), B("Мои черновики", "drafts"), B("Материалы", "materials"), B("Помощь сотруднику", "staff_help")]
        if repo.is_admin(db, actor):
            actions.append(B("Настройки", "settings"))
        return show(db, actor, s, text, actions)
    if action == "staff_help":
        return show(db, actor, s, "Выберите работу в своей очереди. Карточка показывает ответственного, принимающего, срок и доступные действия.\nЗаполните форму и проверьте предпросмотр перед подтверждением. Незавершённые формы находятся в «Моих черновиках» в том же чате.\nДля файла откройте карточку и нажмите «Добавить файл». Работы другого направления доступны для обзора; изменять их нужно в указанном рабочем чате.\n/start — рабочее место; /tasks — работы; /af_settings — настройки в управляющем чате.", [B("В меню", "home")])
    if action in {"works", "calendar"}:
        mode, search = p.get("filter", "active"), p.get("search", "")
        rows = access.works(db, actor)
        if p.get("project_id"):
            rows = [w for w in rows if w.project_id == p["project_id"]]
        if p.get("partner_id"):
            rows = [w for w in rows if w.partner_id == p["partner_id"]]
        if p.get("establishment_id"):
            rows = [w for w in rows if w.establishment_id == p["establishment_id"]]
        rows = [w for w in rows if (mode == "all" or mode == "done" and w.status == "done" or mode != "done" and w.status != "done")
                and (mode != "mine" or w.owner_id == actor.person_id or w.pending_owner_id == actor.person_id)
                and (mode != "review" or w.status == "review" and w.reviewer_id == actor.person_id)
                and (mode != "blockers" or w.data.get("blockers") or w.due_at < now())
                and (mode != "requests" or w.kind in {"support", "marketing"})
                and (mode != "overdue" or w.due_at < now()) and (mode != "waiting" or w.status.startswith("waiting_"))
                and (not search or search.casefold() in (w.title+" "+w.id+" "+repo.entity_name(db, w.establishment_id)).casefold())]
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(("⚠ " if w.status != "done" and w.due_at < now() else "")+w.title, "work", work_id=w.id) for w in items]
        params = {k: v for k, v in p.items() if k not in {"page"}}
        actions += navigation(action, page, len(rows), **params)
        text = ("Календарь фактических сроков" if action == "calendar" else FILTERS.get(mode, mode)) + f"\nРабот: {len(rows)}."
        if action == "calendar":
            text += "\n" + "\n".join(f"{date_text(db, w)} · {w.title}" for w in items)
        actions += [B("Поиск", "search", target=action, target_params=params)]
        if not p.get("establishment_id"):
            actions += [B(label, "works", filter=k) for k, label in FILTERS.items() if k != mode]
        return show(db, actor, s, text, actions+[B("В меню", "home")], action, **{**params, "page": page})
    if action == "work":
        w = work(db, actor, p["work_id"])
        detail = access.can_work(db, actor, w, detail=True)
        text = f"{w.title}\n{STATUSES[w.status]}\nФП: {repo.entity_name(db, w.partner_id)}\nПредприятие: {repo.entity_name(db, w.establishment_id)}\nБренд: {repo.entity_name(db, w.brand_id)}\nОтветственный: {repo.name(db, w.owner_id)}\nПринимающий: {repo.name(db, w.reviewer_id)}\nСрок: {date_text(db, w)}"
        if w.status != "done" and w.due_at < now():
            text += "\n⚠ Срок просрочен."
        if w.original_due_at:
            text += "\nИсходный срок: " + date_text(db, w, w.original_due_at)
        text += f"\nВозвратов: {w.returns}\nID: {w.id} · версия {w.version}"
        if w.pending_owner_id:
            text += f"\nПредложена передача: {repo.name(db, w.pending_owner_id)}. До принятия отвечает текущий владелец."
        actions = []
        if detail:
            if w.data.get("waiting"):
                wait = w.data["waiting"]
                text += f"\nОжидаем: {wait.get('party')}\nНужно: {wait.get('required')}\nСледующий контроль: {wait.get('next_check')}"
            for key, label in {"expected_result": "Ожидаемый результат", "result_description": "Результат", "rejection": "Замечание"}.items():
                if w.data.get(key):
                    text += f"\n{label}: {w.data[key]}"
            if w.data.get("blockers"):
                text += "\nБлокирующие замечания: " + "; ".join(w.data["blockers"])
            actions += [B(schema.ACTIONS[a]+"…", "work_action", work_id=w.id, expected=w.version, action=a) for a in work_actions(db, actor, w)]
            actions += [B(OPERATIONS[a]+"…", "operation_action", work_id=w.id, expected=w.version, action=a) for a in operations(db, actor, w)]
            actions += [B("Добавить файл", "upload", work_id=w.id, expected=w.version), B("Материалы пункта", "work_files", work_id=w.id), B("История пункта", "work_history", work_id=w.id)]
        else:
            b = db.get(Binding, w.binding_id)
            text += "\nДля изменения откройте карточку в рабочем чате: " + repo.label(db, b)
        return show(db, actor, s, text, actions+[B("Обновить карточку", "work", work_id=w.id), B("← Работы", "works"), B("В меню", "home")], "work", work_id=w.id)
    if action in {"work_action", "operation_action"}:
        w = work(db, actor, p["work_id"], detail=True)
        versioned(w, p["expected"])
        available = work_actions(db, actor, w) if action == "work_action" else operations(db, actor, w)
        if p["action"] not in available:
            raise AccessDenied("Это действие недоступно текущему исполнителю в данном состоянии.")
        data = deepcopy(w.data.get("passport", {})) if p["action"] == "passport" else {}
        return begin_action(db, actor, s, ("work:" if action == "work_action" else "operation:")+p["action"], w, data)
    if action == "upload":
        w = work(db, actor, p["work_id"], detail=True)
        versioned(w, p["expected"])
        if w.status == "done":
            raise BusinessError("Работа завершена.")
        prompt = {"type": "attachment", "work_id": w.id, "work_version": w.version, "expires": (now()+timedelta(minutes=15)).isoformat()}
        return render(db, actor, s, f"Отправьте файл или альбом для пункта «{w.title}» в течение 15 минут.\nПункт: {w.id}\nФайл сохранится как внутренний материал. Приёмка результата — отдельное действие.", [B("← Карточка", "work", work_id=w.id)], prompt=prompt)
    if action == "work_files":
        w = work(db, actor, p["work_id"], detail=True)
        rows = list(db.scalars(select(Attachment).where(Attachment.work_id == w.id).order_by(Attachment.created_at)))
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(a.filename, "work_file", work_id=w.id, attachment_id=a.id) for a in items if a.status == "stored"]
        actions += navigation("work_files", page, len(rows), work_id=w.id)
        return show(db, actor, s, f"Материалы: {len(rows)}.\n"+"\n".join(f"{a.filename} · {a.status} · {a.visibility}" for a in items), actions+[B("← Карточка", "work", work_id=w.id)], "work_files", **p)
    if action == "work_file":
        w = work(db, actor, p["work_id"], detail=True)
        a = db.get(Attachment, p["attachment_id"])
        if not a or a.work_id != w.id or a.status != "stored":
            raise BusinessError("Сохранённый материал не найден.")
        # Explicit internal delivery only. Publishing internal files to clients is a separate workflow.
        msg = queue(db, db.get(Binding, actor.binding_id), "ui-file:"+s.id+":"+str(s.version), "Материал: "+a.filename, work=w, attachment_id=a.id)
        msg.payload = {**msg.payload, "ui_actor": actor.person_id, "ui_grants": access.grants_hash(db, actor)}
        return show(db, actor, s, "Материал поставлен в очередь доставки в текущий рабочий чат.", [B("← Материалы", "work_files", work_id=w.id)])
    if action == "work_history":
        w = work(db, actor, p["work_id"], detail=True)
        rows = list(db.scalars(select(Audit).where(Audit.object_id == w.id).order_by(Audit.created_at.desc())))
        items, page = paginate(rows, p.get("page", 0))
        text = "История пункта:\n" + "\n".join(f"{r.created_at:%d.%m %H:%M} UTC · {repo.name(db, r.actor_id)} · {schema.ACTIONS.get(r.action.lower(), r.action)} · v{r.old_version} → v{r.new_version}" for r in items)
        return show(db, actor, s, text, navigation("work_history", page, len(rows), work_id=w.id)+[B("← Карточка", "work", work_id=w.id)], "work_history", **p)
    if action == "materials":
        granted = {r.name for r in access.assignments(db, actor) if access.staff_role(db, actor, r.name)}
        from ama.models import Record
        rows = [r for r in db.scalars(select(Record).where(Record.kind == "material", Record.published.is_(True)))
                if granted.intersection(r.data.get("audience", [])) and all(not r.data.get(k) or
                    (db.get(Entity, r.data[k]) and access.can_entity(db, actor, db.get(Entity, r.data[k])))
                    for k in ("partner_id", "brand_id", "establishment_id"))]
        items, page = paginate(rows, p.get("page", 0))
        text = "Материалы для вашей роли:\n"+"\n\n".join(r.data.get("title", r.key)+"\n"+r.data.get("url", "") for r in items)
        return show(db, actor, s, text, navigation("materials", page, len(rows))+[B("В меню", "home")], "materials", page=page)
    if action == "projects":
        ids = {}
        for w in access.works(db, actor):
            if w.project_id:
                ids.setdefault(w.project_id, []).append(w)
        items, page = paginate(list(ids.items()), p.get("page", 0))
        actions = [B(repo.entity_name(db, works[0].establishment_id)+f" · {len(works)} работ", "works", project_id=key, filter="all") for key, works in items]
        return show(db, actor, s, f"Проекты в вашей области: {len(ids)}", actions+navigation("projects", page, len(ids))+[B("В меню", "home")], "projects", page=page)
    if action == "partners":
        rows = [e for e in db.scalars(select(Entity).where(Entity.kind == "partner").order_by(Entity.name)) if access.can_entity(db, actor, e)]
        needle = p.get("search", "").casefold()
        rows = [e for e in rows if not needle or needle in (e.name+e.id).casefold()]
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(e.name, "partner", partner_id=e.id) for e in items]
        return show(db, actor, s, f"Франчайзи-партнёры: {len(rows)}", actions+navigation("partners", page, len(rows), search=p.get("search", ""))+
                    [B("Поиск", "search", target="partners", target_params={}), B("В меню", "home")], "partners", **p)
    if action == "partner":
        e = db.get(Entity, p["partner_id"])
        if not e or e.kind != "partner" or not access.can_entity(db, actor, e):
            raise AccessDenied("ФП недоступен.")
        people = list(db.scalars(select(Role.person_id).where(Role.name == "partner", Role.partner_id == e.id, Role.active.is_(True),
                                                              (Role.expires_at.is_(None)) | (Role.expires_at > now()))))
        training = list(db.scalars(select(Training).where(Training.person_id.in_(people)))) if people else []
        establishments = [est for est in db.scalars(select(Entity).where(Entity.kind == "establishment", Entity.partner_id == e.id)) if access.can_entity(db, actor, est)]
        text = e.name+"\nПредставители: "+(", ".join(repo.name(db, pid) for pid in people) or "не назначены")
        text += "\nОзнакомление:\n"+("\n".join(f"{repo.name(db, t.person_id)} · {t.course_key} v{t.course_version} · {len(t.acknowledged)}/{len(t.cards)} · место {t.position+1} · "+("завершено" if t.completed_at else "пауза" if t.paused else "проходит") for t in training) or "Нет сохранённого прохождения")
        items, page = paginate(establishments, p.get("page", 0))
        actions = [B(est.name, "enterprise", establishment_id=est.id) for est in items]
        actions += navigation("partner", page, len(establishments), partner_id=e.id)+[B("Работы ФП", "works", partner_id=e.id), B("← Франчайзи-партнёры", "partners"), B("В меню", "home")]
        return show(db, actor, s, text, actions, "partner", **p)
    if action == "enterprises":
        rows = [e for e in db.scalars(select(Entity).where(Entity.kind == "establishment").order_by(Entity.name)) if not e.data.get("archived") and access.can_entity(db, actor, e)]
        needle = p.get("search", "").casefold()
        rows = [e for e in rows if not needle or needle in (e.name+e.id+repo.entity_name(db, e.partner_id)).casefold()]
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(e.name+" · "+repo.entity_name(db, e.partner_id), "enterprise", establishment_id=e.id) for e in items]
        actions += navigation("enterprises", page, len(rows), search=p.get("search", "")) + [B("Найти предприятие", "search", target="enterprises", target_params={}), B("В меню", "home")]
        return show(db, actor, s, f"Предприятия в вашей области: {len(rows)}.", actions, "enterprises", **p)
    if action == "enterprise":
        e = db.get(Entity, p["establishment_id"])
        if not e or not access.can_entity(db, actor, e):
            raise AccessDenied("Предприятие недоступно.")
        brand_ids = list(db.scalars(select(EstablishmentBrand.brand_id).where(EstablishmentBrand.establishment_id == e.id, EstablishmentBrand.active.is_(True))))
        brand_ids = [b for b in brand_ids if access.scope(db, actor, type("Scope", (), {"partner_id": e.partner_id, "establishment_id": e.id, "brand_id": b})())]
        visible = [w for w in access.works(db, actor) if w.establishment_id == e.id]
        text = f"{e.name}\nФП: {repo.entity_name(db, e.partner_id)}\nБренды: {', '.join(repo.entity_name(db, b) for b in brand_ids) or 'не подключены'}\nОткрытых работ: {sum(w.status != 'done' for w in visible)}"
        text += f"\nАдрес: {e.data.get('address', 'не указан')}\nЧасовой пояс: {e.data.get('timezone', 'Europe/Moscow')}"
        for b in db.scalars(select(Binding).where(Binding.purpose == "client", Binding.partner_id == e.partner_id, Binding.active.is_(True))):
            if b.brand_id in brand_ids:
                text += "\nКлиентский чат: "+repo.entity_name(db, b.brand_id)+" · "+b.platform+" · "+b.chat_id
        for w in visible:
            if w.data.get("blockers"):
                text += "\nБлокеры «"+w.title+"»: "+"; ".join(w.data["blockers"])
            if w.kind == "digital" and access.can_work(db, actor, w, detail=True):
                passport = w.data.get("passport", {})
                text += "\nМессенджер предприятия: "+str(passport.get("work_phone_messenger", "не указан"))
                text += "\nМессенджер управления: "+str(passport.get("management_messenger", "не указан"))
                text += "\nКЦ согласован: "+("Да" if passport.get("call_center_applicable") else "Не подтверждено")
                for key in w.data.get("required_components", []):
                    text += "\n"+{"ama_rs": "АМА РС", "care": "Служба заботы", "call_center": "КЦ"}.get(key, key)+": "+("подключение подтверждено" if w.data.get("components", {}).get(key, {}).get("confirmed") else "не принято")
        actions = [B("Работы предприятия", "works", establishment_id=e.id), B("Календарь сроков", "calendar", establishment_id=e.id), B("Новый проект…", "new", kind="plan:create", data={"partner_id": e.partner_id, "establishment_id": e.id}), B("← Предприятия", "enterprises"), B("В меню", "home")]
        return show(db, actor, s, text, actions, "enterprise", **p)
    if action == "leads":
        rows = access.leads(db, actor)
        needle = p.get("search", "").casefold()
        rows = [r for r in rows if not needle or needle in (repo.label(db, r)+r.id).casefold()]
        items, page = paginate(rows, p.get("page", 0))
        actions = [B(repo.label(db, r), "lead", lead_id=r.id) for r in items]
        actions += navigation("leads", page, len(rows), search=p.get("search", "")) + [B("Найти лида", "search", target="leads", target_params={}), B("В меню", "home")]
        return show(db, actor, s, f"Очередь продаж: {len(rows)} лидов.", actions, "leads", **p)
    if action in {"lead", "lead_action", "contract_start"}:
        lead = db.get(Lead, p["lead_id"])
        if not lead or not access.can_lead(db, actor, lead):
            raise AccessDenied("Лид недоступен или назначен другому консультанту.")
        if action != "lead":
            versioned(lead, p["expected"])
            kind = "contract:start" if action == "contract_start" else "sales:"+p["action"]
            data = {k: deepcopy(v) for k, v in lead.data.items() if k in {f["key"] for f in schema.fields_for(kind)}} if kind == "sales:qualify" else {}
            # Confirmed routing is held separately and must never be replaced by a contact description.
            if isinstance(data.get("contact"), dict):
                data["contact"] = "Подтверждённый личный чат"
            return begin_action(db, actor, s, kind, lead, data)
        data = lead.data
        text = f"{repo.label(db, lead)}\nВерсия {lead.version} · {lead.id}\nОтветственный: {repo.name(db, lead.owner_id)}\nСостояние: {lead.state}\nРезультат: {lead.result or 'открыт'}\nКонтакты: {'запрещены' if lead.no_contact else 'разрешены'}"
        for key, label in {"city": "Город", "budget": "Бюджет", "start_period": "Старт", "next_action": "Следующее действие", "next_contact": "Дата контакта"}.items():
            if data.get(key):
                text += f"\n{label}: {data[key]}"
        available = ["take"] if not lead.owner_id else ["qualify", "stage", "wait_lead", "not_now", "no_answer", "refuse", "reply"] if not lead.no_contact else ["reopen"]
        actions = [B(LEAD_ACTIONS[a]+"…", "lead_action", lead_id=lead.id, expected=lead.version, action=a) for a in available]
        if lead.owner_id == actor.person_id and not lead.no_contact:
            actions.append(B("Передать на договор…", "contract_start", lead_id=lead.id, expected=lead.version))
        return show(db, actor, s, text, actions+[B("← Продажи", "leads"), B("В меню", "home")], "lead", lead_id=lead.id)
    if action == "deals":
        rows = [d for d in db.scalars(select(Deal).order_by(Deal.id)) if access.can_deal(db, actor, d)]
        items, page = paginate(rows, p.get("page", 0))
        return show(db, actor, s, f"Доступные сделки: {len(rows)}.", [B(repo.label(db, r), "deal", deal_id=r.id) for r in items]+navigation("deals", page, len(rows))+[B("В меню", "home")], "deals", page=page)
    if action in {"deal", "deal_action"}:
        d = db.get(Deal, p["deal_id"])
        if not d or not access.can_deal(db, actor, d):
            raise AccessDenied("Сделка недоступна.")
        if action == "deal_action":
            versioned(d, p["expected"])
            return begin_action(db, actor, s, "deal:"+p["action"], d)
        paid = db.scalar(select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(Payment.deal_id == d.id))
        text = f"{repo.label(db, d)}\nID: {d.id} · версия {d.version}\nРедакция документов: {d.package_version}\nПодписана редакция: {d.signed_package_version or 'нет'}\nПодтверждено оплат: {paid/100:.2f} ₽\nУсловие расчёта: {d.required_minor/100:.2f} ₽\nКуратор: {repo.name(db, d.curator_id)}"
        lead = db.get(Lead, d.lead_id)
        keys = ["package", "sent", "signed", "payment", "confirm", "assign_curator"] if access.can_lead(db, actor, lead, for_contract=True) else ["curator_accept", "curator_clarify"] if not d.handoff_accepted_at else []
        actions = [B(DEAL_ACTIONS[a]+"…", "deal_action", deal_id=d.id, expected=d.version, action=a) for a in keys]
        return show(db, actor, s, text, actions+[B("← Сделки", "deals"), B("В меню", "home")], "deal", deal_id=d.id)
    raise BusinessError("Экран недоступен. Откройте /start.")

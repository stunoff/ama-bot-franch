"""Durable §16 controls. No external I/O; caller owns the transaction."""

from copy import deepcopy
from datetime import timedelta, timezone
from sqlalchemy import select
from ama.models import ProjectPlan, SLACase, SLAEvent, SLASignal, Work, Binding, Record, now, uid
from ama.config import settings
from ama.calendar import Calendar
from ama.errors import BusinessError, AccessDenied
from ama.auth import require, active_person_role
from ama.service import queue, stamp, versioned, parse_date, STAFF
from ama.sla_rules import defaults, CATEGORIES

LABELS = {
    "take": "Взятие в работу",
    "result": "Результат / содержательный статус",
    "plan": "Назначение плановой даты",
    "input": "Проверка комплектности",
    "review": "Приёмка результата",
    "check": "Проверка ожидания",
    "handoff": "Принятие передачи",
    "critical": "Обновление аварийного статуса",
    "partner2": "Напоминание ФП: 2 рабочих дня",
    "partner4": "Напоминание ФП: 4 рабочих дня",
    "partner5": "Нет ответа ФП: 5 рабочих дней",
    "before": "За рабочий день до срока",
    "day": "День планового срока",
}


def case_for(db, w, lock=False):
    q = select(SLACase).where(SLACase.work_id == w.id)
    return db.scalar(q.with_for_update().execution_options(populate_existing=True) if lock else q)


def due(cal, base, spec):
    if spec[0] == "minutes":
        return cal.add_minutes(base, spec[1])
    if spec[0] == "today":
        return cal.end_today(base)
    return cal.end_of_workday(base, spec[1])


def clock(case):
    return Calendar(case.data["calendar"])


def event(db, case, w, kind, at, lane=None, data=None, signal_id=None):
    lane = lane or kind
    epoch = case.data.get("epochs", {}).get(lane, 0)
    key = f"sla:{case.id}:{lane}:{epoch}:{kind}"
    row = db.scalar(select(SLAEvent).where(SLAEvent.event_key == key))
    if not row:
        row = SLAEvent(
            work_id=w.id,
            signal_id=signal_id,
            event_key=key,
            kind=kind,
            due_at=at,
            data={
                "lane": lane,
                "epoch": epoch,
                "responsible_id": w.reviewer_id if kind == "review" else w.executor_id,
                **(data or {}),
            },
        )
        db.add(row)
    return row


def invalidate(db, case, w, *lanes):
    epochs = dict(case.data.get("epochs", {}))
    for lane in lanes:
        epochs[lane] = epochs.get(lane, 0) + 1
    case.data = {**case.data, "epochs": epochs}
    for e in db.scalars(select(SLAEvent).where(SLAEvent.work_id == w.id, SLAEvent.status.in_(["pending", "blocked"]))):
        if e.data.get("lane") in lanes and not e.signal_id:
            e.status = "cancelled"


def configured(db, w):
    binding = db.get(Binding, w.binding_id)
    if not binding or binding.bot_id != settings().bot_id:
        raise AccessDenied("Маршрут SLA принадлежит другому боту или отсутствует.")
    purpose = binding.purpose
    category = w.data.get("sla_category", "ordinary")
    if w.kind == "digital":
        category = "new_connection"
    if w.kind in {"marketing.city_analytics", "marketing.marketing_plan"}:
        category = "analysis"
    if w.data.get("campaign_id"):
        category = "calendar_launch"
    rules = defaults(purpose, w.priority, category)
    calendar = db.scalar(
        select(Record).where(
            Record.kind == "calendar", Record.key == w.data.get("calendar", "support"), Record.published.is_(True)
        )
    )
    if not calendar:
        raise BusinessError("Для SLA нужен опубликованный рабочий календарь.")
    source = None
    # Custom profiles apply only when an approved published profile matches the actual route.
    matches = []
    for policy in db.scalars(select(Record).where(Record.kind == "policy", Record.published.is_(True))):
        d = policy.data
        if (
            d.get("policy_type") != "sla"
            or d.get("priority") != w.priority
            or d.get("category", "ordinary") != category
        ):
            continue
        route = db.scalar(
            select(Record).where(Record.kind == "route", Record.key == d.get("route"), Record.published.is_(True))
        )
        if route and route.data.get("binding_id") == w.binding_id:
            matches.append(policy)
    if len(matches) > 1:
        raise BusinessError("Несколько SLA-профилей для одного маршрута, приоритета и категории.")
    if matches:
        p = matches[0]
        validate_profile(p.data)
        rules.update(take=["minutes", p.data["take_minutes"]], result=["minutes", p.data["result_minutes"]])
        source = {"id": p.id, "version": p.version}
    return rules, deepcopy(calendar.data), category, source


def validate_profile(data):
    if data.get("counter") != "working":
        raise BusinessError(
            "Профиль §16 использует рабочее время; календарный счёт применяется только к явно указанным событиям."
        )
    if data.get("category", "ordinary") not in CATEGORIES or not data.get("approval"):
        raise BusinessError("Нужны категория и подтверждение владельца процесса.")
    for key in ("take_minutes", "result_minutes"):
        n = data.get(key)
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 525600:
            raise BusinessError("Срок SLA должен составлять 1–525600 рабочих минут.")
    if data.get("priority") == "critical":
        raise BusinessError(
            "Критичные аварии используют обязательный профиль §16.6 без ослабления произвольной настройкой."
        )


def initialize(db, w, *, explicit_due=True, legacy=False):
    old = case_for(db, w)
    if old:
        return old
    rules, calendar, category, source = configured(db, w)
    if w.priority == "critical" and not w.data.get("incident"):
        raise BusinessError("Критичность требует описания реальной аварии, влияния на работу и основания.")
    base = now() if legacy else w.created_at
    c = SLACase(
        work_id=w.id,
        version=1,
        data={
            "rules": rules,
            "calendar": calendar,
            "category": category,
            "policy": source,
            "base": base.isoformat(),
            "epochs": {},
            "initial_due": w.original_due_at.isoformat(),
            "breaches": [],
            "legacy": legacy,
            "taken": None,
            "plan": None,
            "critical_active": w.priority == "critical",
        },
    )
    db.add(c)
    db.flush()
    cal = clock(c)
    if w.status == "new":
        event(db, c, w, "take", due(cal, base, rules["take"]))
    if rules.get("input") and rules.get("input_anchor") == "created":
        event(db, c, w, "input", due(cal, base, rules["input"]))
    if rules.get("plan_window"):
        event(db, c, w, "plan", due(cal, base, rules["plan_window"]))
    if w.priority == "critical":
        explicit_due = False
    if explicit_due:
        planned(db, c, w, w.due_at, source="existing" if legacy else "explicit")
    elif rules.get("result"):
        at = due(cal, base, rules["result"])
        w.due_at = at
        if not legacy:
            w.original_due_at = at
        result_event(db, c, w, at)
    if w.priority == "critical":
        event(db, c, w, "critical", due(cal, base, rules["result"]) + timedelta(minutes=120))
    if w.status == "review":
        invalidate(db, c, w, "result", "take", "input", "plan")
        review(db, c, w)
    c.data = {**c.data, "initial_due": w.original_due_at.isoformat()}
    opening_check(db, c, w)
    stamp(db, None, c, "SLA_INITIALIZED", data={"work_id": w.id, "source": source, "legacy": legacy, "rules": rules})
    return c


def result_event(db, c, w, at):
    c.data = {**c.data, "result_due": at.isoformat(), "result_paused": False}
    event(db, c, w, "result", at)


def planned(db, c, w, at, source="owner"):
    invalidate(db, c, w, "result", "before", "day", "plan")
    c.data = {
        **c.data,
        "plan": {"due_at": at.isoformat(), "source": source},
        "result_due": at.isoformat(),
        "result_paused": False,
    }
    result_event(db, c, w, at)
    cal = clock(c)
    previous = cal.shift_workdays(at, -1)
    local = at.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    day = (
        local.replace(hour=cal.start.hour, minute=cal.start.minute, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    if previous > now():
        event(db, c, w, "before", previous)
    if day > now():
        event(db, c, w, "day", day)


def review(db, c, w):
    if w.reviewer_id == w.partner_person_id and active_person_role(db, w.reviewer_id, {"partner"}, w):
        request(
            db,
            c,
            w,
            "Подтвердите, что ваше обращение решено. Отсутствие ответа не закрывает его автоматически.\nРезультат: "
            + str(w.data.get("result_description") or "Откройте карточку обращения"),
            confirmation=True,
        )
    else:
        at = clock(c).end_of_workday(now(), 1)
        w.due_at = at
        event(db, c, w, "review", at, data={"responsible_id": w.reviewer_id})


def request(db, c, w, text, confirmation=False):
    invalidate(db, c, w, "request")
    token = uid()
    c.data = {**c.data, "request": {"token": token, "delivered_at": None, "confirmation": confirmation, "text": text}}
    binding = db.get(Binding, w.client_binding_id) if w.client_binding_id else None
    if (
        not binding
        or binding.bot_id != settings().bot_id
        or not binding.active
        or not binding.verified
        or binding.purpose != "client"
    ):
        signal(db, c, w, "delivery:" + token, "Ошибка маршрута запроса ФП", False)
        return
    row = queue(
        db,
        binding,
        "sla-request:" + token,
        text + "\nПункт: " + w.title + "\nID: " + w.id,
        work=w,
        buttons=[["Открыть", "af:w:" + w.id]],
    )
    row.payload = {**row.payload, "sla_request": token, "sla_case": c.id}


def delivered(db, row):
    token = row.payload.get("sla_request")
    if not token or row.status != "sent":
        return
    locked_work = db.scalar(select(Work).where(Work.id == row.work_id).with_for_update())
    if not locked_work:
        return
    c = db.scalar(
        select(SLACase)
        .where(SLACase.id == row.payload.get("sla_case"))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not c:
        return
    w = db.get(Work, c.work_id)
    r = c.data.get("request", {})
    if r.get("token") != token or r.get("delivered_at") or not w or w.status not in {"waiting_partner", "review"}:
        return
    at = now()
    c.data = {**c.data, "request": {**r, "delivered_at": at.isoformat(), "outbox_id": row.id}}
    if r.get("confirmation"):
        w.due_at = clock(c).add_minutes(clock(c).shift_workdays(at, 5), 0)
        w.version += 1
    for day in (2, 4, 5):
        event(
            db,
            c,
            w,
            "partner" + str(day),
            clock(c).add_minutes(clock(c).shift_workdays(at, day), 0),
            "request",
            {"token": token, "confirmation": r.get("confirmation")},
        )
    stamp(db, None, c, "SLA_REQUEST_DELIVERED", data={"work_id": w.id, "outbox_id": row.id, "at": at.isoformat()})


def route(db, w, purpose):
    return next(
        (
            b
            for b in db.scalars(
                select(Binding).where(
                    Binding.bot_id == settings().bot_id,
                    Binding.purpose == purpose,
                    Binding.active.is_(True),
                    Binding.verified.is_(True),
                )
            )
            if all(
                not getattr(b, k) or getattr(b, k) == getattr(w, k)
                for k in ("partner_id", "brand_id", "establishment_id")
            )
        ),
        None,
    )


def signal(db, c, w, key, reason, critical=False, *, curator_only=False, responsible_id=None):
    unique = "sla-signal:" + key
    row = db.scalar(select(SLASignal).where(SLASignal.event_key == unique))
    if row:
        return row
    cal = clock(c)
    mgmt = db.scalar(
        select(Record).where(Record.kind == "route", Record.key == "management", Record.published.is_(True))
    )
    calendar = (
        db.scalar(
            select(Record).where(
                Record.kind == "calendar", Record.key == mgmt.data.get("calendar"), Record.published.is_(True)
            )
        )
        if mgmt
        else None
    )
    if calendar:
        cal = Calendar(calendar.data)
    at = cal.add_minutes(now(), 240) if critical else cal.end_of_workday(now(), 1)
    row = SLASignal(
        bot_id=settings().bot_id,
        work_id=w.id,
        event_key=unique,
        kind=reason[:40],
        critical=critical,
        due_at=at,
        data={
            "reason": reason,
            "curator_only": curator_only,
            "responsible_id": responsible_id or w.owner_id,
            "notified": False,
        },
    )
    db.add(row)
    db.flush()
    if not notify_signal(db, row, w):
        db.add(
            SLAEvent(
                work_id=w.id,
                signal_id=row.id,
                event_key="notify:" + row.id,
                kind="signal_delivery",
                due_at=now(),
                data={},
            )
        )
    # A signal is an administrative control, never a second Work or owner transfer.
    db.add(
        SLAEvent(
            work_id=w.id,
            signal_id=row.id,
            event_key="reaction:" + row.id,
            kind="management_reaction",
            due_at=at,
            data={},
        )
    )
    stamp(db, None, row, "SLA_SIGNAL_CREATED", data={"work_id": w.id, "reason": reason, "critical": critical})
    return row


def notify_signal(db, s, w):
    purpose = "support" if s.data.get("curator_only") else "management"
    b = route(db, w, purpose)
    if not b:
        return False
    queue(
        db,
        b,
        "sla-signal:" + s.id,
        s.data["reason"]
        + "\nРабота: "
        + w.title
        + "\nID: "
        + w.id
        + "\nОтветственный за действие: "
        + str(s.data.get("responsible_id") or w.owner_id),
        buttons=[["Контроль SLA", "af:sla"]],
    )
    s.data = {**s.data, "notified": True, "binding_id": b.id}
    return True


def preserve_overdue(db, c, w):
    epoch = c.data.get("epochs", {}).get("result", 0)
    e = db.scalar(select(SLAEvent).where(SLAEvent.event_key == f"sla:{c.id}:result:{epoch}:result"))
    if e and e.status in {"pending", "blocked"} and e.due_at < now():
        breach(db, c, w, e)
        e.status = "done"


def breach(db, c, w, e):
    reason = LABELS.get(e.kind, e.kind) + " — срок нарушен"
    s = signal(
        db,
        c,
        w,
        e.event_key,
        reason,
        c.data.get("critical_active", False),
        responsible_id=e.data.get("responsible_id")
        or (w.reviewer_id if e.kind == "review" else w.pending_owner_id if e.kind == "handoff" else w.owner_id),
    )
    s.data = {
        **s.data,
        "responsible_id": e.data.get("responsible_id")
        or (w.reviewer_id if e.kind == "review" else w.pending_owner_id if e.kind == "handoff" else w.owner_id),
        "original_due_at": e.due_at.isoformat(),
    }
    history = list(c.data.get("breaches", []))
    if not any(x["event"] == e.event_key for x in history):
        history.append(
            {
                "event": e.event_key,
                "kind": e.kind,
                "due_at": e.due_at.isoformat(),
                "at": now().isoformat(),
                "signal_id": s.id,
            }
        )
        c.data = {**c.data, "breaches": history}
    w.data = {**w.data, "overdue": True}


def on_transition(db, actor, w, action, data):
    c = case_for(db, w, True)
    if not c:
        return
    if c.data.get("critical_active") and action in {"submit", "cancel", "reschedule", "sla_plan"}:
        raise BusinessError(
            "Активная авария требует обновлений статуса до явного восстановления / согласованного обхода; её контроль нельзя перенести или закрыть."
        )
    affected = {
        "take": {"take"},
        "submit": {"result", "plan", "input", "check"},
        "accept": {"review"},
        "reject": {"review", "result"},
        "resume": {"check"},
        "handoff_accept": {"handoff"},
        "wait_partner": {"result", "check"},
        "wait_external": {"check"},
        "wait_uk": {"check"},
    }
    preserve_due(db, c, w, affected.get(action, set()))
    if action in {"reschedule", "wait_partner", "reject", "sla_plan"}:
        preserve_overdue(db, c, w)
    if action == "take":
        invalidate(db, c, w, "take")
        c.data = {**c.data, "taken": now().isoformat()}
        rules = c.data["rules"]
        if rules.get("input") and rules.get("input_anchor") == "taken":
            event(db, c, w, "input", due(clock(c), now(), rules["input"]))
        if (
            rules.get("plan_required")
            and not c.data.get("plan")
            and not rules.get("input")
            and not rules.get("plan_window")
        ):
            if not data.get("due_at") or not data.get("reason"):
                raise BusinessError(
                    "Для длительной работы вместе со взятием укажите плановую дату результата и основание."
                )
            at = parse_date(data["due_at"])
            if at <= now():
                raise BusinessError("Плановая дата должна быть будущей.")
            planned(db, c, w, at)
            w.due_at = at
    elif action == "submit":
        invalidate(db, c, w, "result", "before", "day", "plan", "input", "check", "request")
        review(db, c, w)
    elif action == "accept" and w.status == "review":
        invalidate(db, c, w, "review", "result")
        review(db, c, w)
    elif action == "reject":
        invalidate(db, c, w, "review", "request")
        planned(db, c, w, w.due_at)
        if w.returns >= 2:
            signal(db, c, w, f"return:{w.id}:{w.returns}", "Повторный возврат: контроль качества")
    elif action == "wait_partner":
        invalidate(db, c, w, *(["check"] if c.data.get("critical_active") else ["result", "before", "day", "check"]))
        c.data = {**c.data, "result_paused": True, "paused_due": c.data.get("result_due")}
        event(db, c, w, "check", w.due_at)
        request(
            db,
            c,
            w,
            "Нужны данные: "
            + data["required"]
            + "\nСторона: "
            + data["party"]
            + "\nПричина: "
            + data.get("reason", ""),
        )
    elif action in {"wait_external", "wait_uk"}:
        event_reset(db, c, w, "check", w.due_at)
    elif action in {"resume", "reschedule"}:
        invalidate(db, c, w, "check", "request")
        if not c.data.get("critical_active"):
            planned(db, c, w, w.due_at)
    elif action == "handoff":
        event_reset(
            db, c, w, "handoff", due(clock(c), now(), c.data["rules"]["take"]), {"responsible_id": w.pending_owner_id}
        )
    elif action == "handoff_accept":
        invalidate(db, c, w, "handoff", "take")
        c.data = {**c.data, "taken": now().isoformat()}
        for e in db.scalars(
            select(SLAEvent).where(SLAEvent.work_id == w.id, SLAEvent.status.in_(["pending", "blocked"]))
        ):
            e.data = {**e.data, "responsible_id": w.owner_id}
        # A new queue gets its own take controls, but the existing result promise survives.
    if w.status == "done":
        invalidate(
            db,
            c,
            w,
            *list(c.data.get("epochs", {})),
            "take",
            "result",
            "plan",
            "input",
            "review",
            "check",
            "request",
            "before",
            "day",
            "critical",
            "handoff",
        )
        c.data = {**c.data, "critical_active": False, "closed_at": now().isoformat()}
    c.version += 1
    stamp(db, actor, c, "SLA_STATE_CHANGED", data={"work_id": w.id, "action": action})


def event_reset(db, c, w, kind, at, data=None):
    invalidate(db, c, w, kind)
    return event(db, c, w, kind, at, data=data)


def action(db, actor, w, name, data):
    role = require(db, actor, STAFF, w)
    if name == "sla_adopt":
        if actor.person_id not in {w.owner_id, w.reviewer_id}:
            raise AccessDenied("SLA подключает назначенный ответственный или принимающий.")
        initialize(db, w, legacy=True)
        return role
    c = case_for(db, w, True)
    if not c:
        raise BusinessError("Сначала подключите SLA к существующей карточке.")
    if actor.person_id != w.owner_id or w.status in {"review", "done"}:
        raise AccessDenied("Содержательный статус и план задаёт текущий ответственный.")
    if name in {"sla_plan", "sla_complete_data", "sla_status", "sla_recovered"}:
        preserve_due(db, c, w, {"result", "plan", "input", "critical", "check"})
    if name == "sla_plan":
        if not data.get("reason"):
            raise BusinessError("Для плановой даты нужны объём, основание и следующий результат.")
        at = parse_date(data["due_at"])
        if at <= now():
            raise BusinessError("Укажите будущую плановую дату.")
        preserve_overdue(db, c, w)
        if w.data.get("paid_started_at") or w.data.get("procurement_ordered"):
            raise BusinessError("Срок заказанной/оплаченной работы меняется через согласование проекта.")
        maximum = c.data["rules"].get("range_days")
        if maximum and at > clock(c).end_of_workday(now(), maximum[1]) and not data.get("approval"):
            raise BusinessError("Срок выше ориентира требует основания индивидуального согласования.")
        limit = opening_check(db, c, w)
        if limit and limit > now() and at > limit:
            raise BusinessError("Цифровой результат нужен не позднее чем за 5 календарных дней до открытия.")
        planned(db, c, w, at)
        w.due_at = at
        w.data = {**w.data, "sla_plan_reason": data["reason"], "sla_plan_approval": data.get("approval")}
    elif name == "sla_complete_data":
        if not data.get("evidence"):
            raise BusinessError("Нужно основание проверки комплектности.")
        if c.data["rules"].get("plan_required"):
            if not data.get("due_at") or not data.get("reason"):
                raise BusinessError("Вместе с комплектностью назначьте плановую дату результата и основание.")
            at = parse_date(data["due_at"])
            if at <= now():
                raise BusinessError("Плановая дата должна быть будущей.")
            limit = opening_check(db, c, w)
            if limit and limit > now() and at > limit:
                raise BusinessError("Цифровой результат нужен не позднее чем за 5 календарных дней до открытия.")
            planned(db, c, w, at)
            w.due_at = at
        invalidate(db, c, w, "input")
        c.data = {**c.data, "data_complete": {"at": now().isoformat(), "evidence": data["evidence"]}}

    elif name in {"sla_status", "sla_recovered"}:
        if not data.get("status_text") or not data.get("evidence"):
            raise BusinessError("Нужны содержательный статус и основание, пустая отметка не останавливает SLA.")
        c.data = {
            **c.data,
            "last_status": {"at": now().isoformat(), "text": data["status_text"], "evidence": data["evidence"]},
        }
        if c.data.get("critical_active"):
            if name == "sla_recovered":
                if data.get("resolution") not in {"restored", "agreed_workaround"}:
                    raise BusinessError("Подтвердите восстановление или согласованный обходной способ.")
                c.data = {**c.data, "critical_active": False, "resolution": data}
                invalidate(db, c, w, "critical", "result")
            else:
                invalidate(db, c, w, "result")
                event_reset(db, c, w, "critical", now() + timedelta(minutes=120))
        else:
            preserve_overdue(db, c, w)
            if not data.get("next_check"):
                raise BusinessError("Укажите дату следующего содержательного действия.")
            next_at = parse_date(data["next_check"])
            if next_at <= now():
                raise BusinessError("Контрольная дата должна быть будущей.")
            invalidate(db, c, w, "result")
            event_reset(db, c, w, "check", next_at)
            w.due_at = next_at
    else:
        raise BusinessError("Неизвестное действие SLA.")
    c.version += 1
    return role


def deliverable(db, row):
    if row.payload.get("sla_request"):
        c = db.get(SLACase, row.payload.get("sla_case"))
        w = db.get(Work, row.work_id) if row.work_id else None
        if (
            not c
            or not w
            or w.status not in {"waiting_partner", "review"}
            or c.data.get("request", {}).get("token") != row.payload["sla_request"]
        ):
            return False
    marker = row.payload.get("sla_event")
    if not marker:
        return True
    e = db.get(SLAEvent, marker)
    w = db.get(Work, row.work_id)
    c = case_for(db, w) if w else None
    if not e or not w or not c or w.status == "done":
        return False
    lane = e.data.get("lane")
    if e.data.get("epoch") != c.data.get("epochs", {}).get(lane, 0):
        return False
    if e.kind in {"partner2", "partner4"}:
        return (
            w.status in {"waiting_partner", "review"}
            and c.data.get("request", {}).get("token") == e.data.get("token")
            and bool(c.data.get("request", {}).get("delivered_at"))
        )
    return True


def tick(db):
    current = now()
    candidate = db.execute(
        select(SLAEvent.id, SLAEvent.work_id)
        .join(Work, Work.id == SLAEvent.work_id)
        .join(Binding, Binding.id == Work.binding_id)
        .where(
            Binding.bot_id == settings().bot_id,
            SLAEvent.status.in_(["pending", "blocked"]),
            SLAEvent.due_at <= current,
            SLAEvent.retry_at <= current,
        )
        .order_by(SLAEvent.due_at)
        .limit(1)
    ).first()
    if not candidate:
        return False
    # Every mutation locks Work first, then the SLA case/events/signals.
    w = db.scalar(select(Work).where(Work.id == candidate.work_id).with_for_update(skip_locked=True))
    if not w:
        return False
    c = case_for(db, w, True)
    e = db.scalar(
        select(SLAEvent).where(SLAEvent.id == candidate.id).with_for_update().execution_options(populate_existing=True)
    )
    if not e or e.status not in {"pending", "blocked"} or e.retry_at > current:
        return True
    if not w or not c:
        e.status = "cancelled"
        return True
    if e.signal_id:
        s = db.get(SLASignal, e.signal_id)
        if not s or s.status not in ({"acknowledged"} if e.kind == "management_followup" else {"open"}):
            e.status = "cancelled"
            return True
        if e.kind == "management_followup" and e.data.get("signal_version") != s.version:
            e.status = "cancelled"
            return True
        if e.kind == "signal_delivery":
            if notify_signal(db, s, w):
                e.status = "done"
            else:
                e.status = "blocked"
                e.retry_at = current + timedelta(minutes=5)
            return True
        b = route(db, w, "support" if s.data.get("curator_only") else "management")
        if not b:
            e.status = "blocked"
            e.retry_at = current + timedelta(minutes=5)
            return True
        queue(
            db,
            b,
            "sla-reaction:" + e.id,
            (
                "Наступила следующая проверка сигнала: "
                if e.kind == "management_followup"
                else "Просрочена реакция на контрольный сигнал: "
            )
            + s.data["reason"]
            + "\nРабота: "
            + w.id,
            buttons=[["Контроль SLA", "af:sla"]],
        )
        e.status = "done"
        return True
    lane = e.data.get("lane")
    if w.status == "done" or e.data.get("epoch") != c.data.get("epochs", {}).get(lane, 0):
        e.status = "cancelled"
        return True
    if e.kind in {"before", "day", "partner2", "partner4"}:
        target = (
            db.get(Binding, w.client_binding_id)
            if e.kind.startswith("partner") and w.client_binding_id
            else db.get(Binding, w.binding_id)
        )
        if e.kind.startswith("partner") and (
            w.status not in {"waiting_partner", "review"} or not c.data.get("request", {}).get("delivered_at")
        ):
            e.status = "cancelled"
            return True
        if not target or not target.active or not target.verified:
            e.status = "blocked"
            e.retry_at = current + timedelta(minutes=5)
            return True
        row = queue(
            db,
            target,
            "sla-notice:" + e.id,
            LABELS[e.kind] + "\n" + w.title + "\nID: " + w.id,
            work=w,
            buttons=[["Открыть", "af:w:" + w.id]],
        )
        row.payload = {**row.payload, "sla_event": e.id}
    elif e.kind == "partner5":
        confirmation = e.data.get("confirmation", False)
        signal(
            db, c, w, e.event_key, "ФП не ответил на доставленный запрос за 5 рабочих дней", curator_only=confirmation
        )
        if not confirmation:
            target = route(db, w, "support")
            if target:
                queue(
                    db,
                    target,
                    "sla-fp5:" + e.id,
                    "ФП не ответил на доставленный запрос. Контроль куратора.\nРабота: " + w.id,
                )
    else:
        breach(db, c, w, e)
        if e.kind == "critical" and c.data.get("critical_active"):
            # Keep controlling the incident; do not fabricate a status from a reminder.
            invalidate(db, c, w, "critical")
            event(db, c, w, "critical", current + timedelta(minutes=120))
    e.status = "done"
    return True


def signal_action(db, actor, signal_id, expected, data):
    source = db.execute(
        select(SLASignal.work_id).where(SLASignal.id == signal_id, SLASignal.bot_id == settings().bot_id)
    ).scalar_one_or_none()
    if not source:
        raise BusinessError("Сигнал не найден.")
    w = db.scalar(select(Work).where(Work.id == source).with_for_update())
    s = db.scalar(
        select(SLASignal).where(SLASignal.id == signal_id).with_for_update().execution_options(populate_existing=True)
    )
    versioned(s, expected)
    from ama.ui import access

    b = db.get(Binding, actor.binding_id)
    allowed = (
        s.data.get("curator_only")
        and b.purpose == "support"
        and access.scope(db, actor, w, {"curator", "support_head"})
    ) or (not s.data.get("curator_only") and b.purpose == "management" and access.scope(db, actor, w, {"support_head"}))
    if not allowed:
        raise AccessDenied(
            "Реакцию фиксирует назначенный руководитель сопровождения; техническая роль администратора не даёт этого права."
        )
    if (
        s.status not in {"open", "acknowledged"}
        or not data.get("reaction")
        or (not data.get("close_signal") and not data.get("next_check"))
    ):
        raise BusinessError("Нужны содержательная реакция и дата следующего контроля.")
    at = parse_date(data["next_check"]) if data.get("next_check") else now()
    if not data.get("close_signal") and at <= now():
        raise BusinessError("Следующая проверка должна быть будущей.")
    s.status = "closed" if data.get("close_signal") else "acknowledged"
    s.owner_id = actor.person_id
    s.version += 1
    s.data = {
        **s.data,
        "reaction": data["reaction"],
        "reacted_at": now().isoformat(),
        "next_check": data.get("next_check"),
    }
    if s.status != "closed":
        db.add(
            SLAEvent(
                work_id=w.id,
                signal_id=s.id,
                event_key=f"signal-followup:{s.id}:{s.version}",
                kind="management_followup",
                due_at=at,
                data={"signal_version": s.version},
            )
        )
    stamp(db, actor, s, "SLA_SIGNAL_REACTED", data={"reaction": data["reaction"], "work_id": w.id})
    return {"id": s.id, "version": s.version, "status": s.status}


def refresh_deadline(db, w, source):
    c = case_for(db, w, True)
    if c and w.status != "done" and c.data.get("result_due") != w.due_at.isoformat():
        preserve_overdue(db, c, w)
        planned(db, c, w, w.due_at, source)
        c.version += 1
        stamp(db, None, c, "SLA_PLAN_UPDATED", data={"source": source, "due_at": w.due_at.isoformat()})


def text(db, w):
    c = case_for(db, w)
    if not c:
        return "SLA: прежний контроль; отдельный профиль ещё не подключён."
    rows = list(
        db.scalars(
            select(SLAEvent)
            .where(SLAEvent.work_id == w.id, SLAEvent.status.in_(["pending", "blocked"]), SLAEvent.signal_id.is_(None))
            .order_by(SLAEvent.due_at)
        )
    )
    lines = ["SLA: " + CATEGORIES[c.data["category"]]]
    for e in rows:
        if e.data.get("epoch") == c.data.get("epochs", {}).get(e.data.get("lane"), 0):
            lines.append(
                LABELS.get(e.kind, e.kind)
                + f": {e.due_at:%d.%m.%Y %H:%M} UTC"
                + (" · нет маршрута" if e.status == "blocked" else "")
            )
    r = c.data.get("request", {})
    if r and not r.get("delivered_at"):
        lines.append("Запрос ФП ещё не доставлен: отсчёт 2/4/5 не начат.")
    if c.data.get("result_paused"):
        lines.append("Действие приостановлено в ожидании ФП; исходный срок сохранён.")
    if c.data.get("critical_active"):
        lines.append("Авария активна: содержательный статус каждые 2 часа до восстановления / согласованного обхода.")
    if c.data.get("opening_deadline"):
        lines.append("Цифровая готовность к открытию: " + c.data["opening_deadline"] + " UTC")
    if c.data.get("opening_risk"):
        lines.append(c.data["opening_risk"])
    if c.data.get("last_status"):
        lines.append(
            "Последний статус: " + c.data["last_status"]["text"][:1000] + " (" + c.data["last_status"]["at"] + " UTC)"
        )
    lines.append("Зафиксированных нарушений: " + str(len(c.data.get("breaches", []))))
    return "\n".join(lines)


def opening_check(db, c, w):
    if not c.data["rules"].get("opening_offset_days"):
        return None
    project = db.get(ProjectPlan, w.project_id) if w.project_id else None
    planned_date = project.dates.get("planned_technical") if project else None
    if not planned_date:
        c.data = {
            **c.data,
            "opening_deadline": None,
            "opening_risk": "Не определена плановая дата технического открытия",
        }
        return None
    cutoff = parse_date(planned_date) - timedelta(days=5)
    c.data = {**c.data, "opening_deadline": cutoff.isoformat()}
    promised = c.data.get("result_due")
    if cutoff <= now() or (promised and parse_date(promised + "+00:00") > cutoff):
        c.data = {**c.data, "opening_risk": "Риск цифровой готовности за 5 календарных дней до открытия"}
        signal(
            db,
            c,
            w,
            "opening-risk:" + w.id + ":" + planned_date,
            "Риск срока цифровой готовности: техническое открытие −5 дней",
            True,
        )
        b = route(db, w, "support")
        if b:
            queue(
                db,
                b,
                "opening-risk:" + w.id + ":" + planned_date,
                "Риск цифровой готовности к техническому открытию.\nРабота: " + w.id,
            )
    else:
        c.data = {**c.data, "opening_risk": None}
    return cutoff


def before_transition(db, w, name):
    c = case_for(db, w)
    if c and c.data.get("critical_active") and name in {"submit", "cancel", "reschedule", "sla_plan"}:
        raise BusinessError(
            "Аварийный контроль продолжается до подтверждённого восстановления или согласованного обхода."
        )


def partner_return(db, actor, w, expected, data, key):
    require(db, actor, {"partner"}, w)
    versioned(w, expected)
    if (
        actor.person_id != w.partner_person_id
        or w.reviewer_id != actor.person_id
        or w.status != "review"
        or not data.get("reason")
    ):
        raise AccessDenied("Назначенный ФП возвращает своё обращение с описанием нерешённой проблемы.")
    c = case_for(db, w)
    if not c:
        raise BusinessError("Контроль обращения ещё не подключён.")
    from ama.service import transition

    deadline = clock(c).end_of_workday(now(), 1)
    return transition(
        db, actor, w.id, expected, "reject", {"reason": data["reason"], "due_at": deadline.isoformat() + "+00:00"}, key
    )


def delivery_problem(db, row):
    if not row.payload.get("sla_request") or row.status not in {"failed", "uncertain", "blocked"}:
        return
    c = db.get(SLACase, row.payload.get("sla_case"))
    w = db.get(Work, row.work_id) if row.work_id else None
    if c and w and c.data.get("request", {}).get("token") == row.payload["sla_request"]:
        signal(
            db,
            c,
            w,
            "delivery-error:" + row.id,
            "Запрос ФП не доставлен / исход неизвестен: требуется проверка",
            curator_only=True,
        )


def preserve_due(db, c, w, kinds):
    """A late user action must not erase a breach just because the worker was offline."""
    if not kinds:
        return
    for e in db.scalars(
        select(SLAEvent).where(
            SLAEvent.work_id == w.id,
            SLAEvent.kind.in_(kinds),
            SLAEvent.status.in_(["pending", "blocked"]),
            SLAEvent.due_at < now(),
        )
    ):
        if e.data.get("epoch") == c.data.get("epochs", {}).get(e.data.get("lane"), 0):
            breach(db, c, w, e)
            e.status = "done"


def overdue(db, w):
    if w.status == "done":
        return False
    c = case_for(db, w)
    if not c:
        return w.due_at < now()
    if c.data.get("breaches"):
        return True
    controls = {"take", "result", "plan", "input", "review", "check", "handoff", "critical", "partner5"}
    return any(
        e.data.get("epoch") == c.data.get("epochs", {}).get(e.data.get("lane"), 0)
        for e in db.scalars(
            select(SLAEvent).where(
                SLAEvent.work_id == w.id,
                SLAEvent.kind.in_(controls),
                SLAEvent.signal_id.is_(None),
                SLAEvent.status.in_(["pending", "blocked"]),
                SLAEvent.due_at < now(),
            )
        )
    )

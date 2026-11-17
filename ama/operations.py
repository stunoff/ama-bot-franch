"""Специализированные подтверждения платных работ, долга, цифровой готовности и допуска."""
from sqlalchemy import select
from ama.auth import require, roles_for
from ama.errors import BusinessError, AccessDenied
from ama.models import Work, Training, now, Binding
from ama.service import versioned, stamp, parse_date, queue, make_timer


def operation(db, actor, work_id, expected, action, data):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update())
    if not w:
        raise BusinessError("Карточка не найдена.")
    roles_for(db, actor, w)
    versioned(w, expected)
    old, role = w.version, None
    if action == "offer":
        role = require(db, actor, {"curator", "marketing_manager"}, w)
        if db.get(Binding, w.binding_id).purpose not in {"projecting", "software", "personnel", "menu", "design", "advertising", "printing", "systems_training", "smm"}:
            raise BusinessError("В этом рабочем чате платные предложения не разрешены.")
        if w.kind != "paid_work" or not all(data.get(k) for k in ("scope", "amount_minor", "valid_until", "start_rule", "deadline")):
            raise BusinessError("Предложение требует объём, сумму, срок действия, правило старта и дату результата.")
        if not isinstance(data["amount_minor"], int) or data["amount_minor"] <= 0:
            raise BusinessError("Сумма должна быть положительной и указана в копейках.")
        parse_date(data["valid_until"])
        parse_date(data["deadline"])
        if data["start_rule"] not in {"partner_confirmation", "partner_payment_evidence"}:
            raise BusinessError("Не утверждено правило старта платной работы.")
        prior = w.data.get("offer", {})
        if w.data.get("paid_started_at"):
            raise BusinessError("Изменение уже начатой платной работы требует отдельного согласования.")
        w.data = {**w.data, "offer": {**data, "version": prior.get("version", 0) + 1}, "offer_accepted": False}
    elif action == "offer_accept":
        role = require(db, actor, {"partner"}, w)
        if actor.person_id != w.partner_person_id or w.kind != "paid_work":
            raise AccessDenied("Предложение подтверждает назначенный ФП.")
        offer = w.data.get("offer")
        if not offer or data.get("offer_version") != offer["version"]:
            raise BusinessError("Подтвердите актуальную версию предложения.")
        if parse_date(offer["valid_until"]) < now():
            raise BusinessError("Предложение истекло: требуется актуализация условий.")
        if offer["start_rule"] == "partner_payment_evidence":
            if data.get("amount_minor") != offer["amount_minor"] or not data.get("evidence"):
                raise BusinessError("Для старта нужны согласованная сумма и подтверждение ФП; частичная/иная оплата требует уточнения.")
        deadline = parse_date(offer["deadline"])
        if deadline <= now():
            raise BusinessError("Нужно согласовать будущий срок: просрочка задним числом не создаётся.")
        w.data = {**w.data, "offer_accepted": True, "paid_started_at": now().isoformat(), "partner_evidence": data.get("evidence")}
        w.status, w.due_at = "in_progress", deadline
        w.owner_id = w.executor_id
    elif action in {"invoice", "debt_payment", "deferral"}:
        role = require(db, actor, {"contract"}, w)
        if w.kind != "debt":
            raise BusinessError("Договорная задолженность ведётся в отдельной карточке debt.")
        ledger = list(w.data.get("ledger", []))
        if action == "invoice":
            if not isinstance(data.get("amount_minor"), int) or data["amount_minor"] <= 0 or not data.get("document"):
                raise BusinessError("Нужны положительная сумма в копейках и документ.")
            ledger.append({"type": "invoice", **data, "at": now().isoformat(), "actor": actor.person_id})
        elif action == "debt_payment":
            if not isinstance(data.get("amount_minor"), int) or data["amount_minor"] <= 0 or not data.get("bank_reference"):
                raise BusinessError("Нужны положительная сумма и банковское подтверждение.")
            # Банк. ссылка уникальна в истории, а не повторяемое клиентское фото.
            if any(e.get("bank_reference") == data["bank_reference"] for e in ledger):
                raise BusinessError("Операция уже учтена.")
            ledger.append({"type": "payment", **data, "at": now().isoformat(), "actor": actor.person_id})
        else:
            if not data.get("approval"):
                raise BusinessError("Нужно подтверждение согласованной отсрочки.")
            w.due_at = parse_date(data["due_at"])
            if w.due_at <= now():
                raise BusinessError("Отсрочка должна быть в будущем.")
            ledger.append({"type": "deferral", **data, "at": now().isoformat(), "actor": actor.person_id})
        amount = sum(e["amount_minor"] if e["type"] == "invoice" else -e["amount_minor"] if e["type"] == "payment" else 0 for e in ledger)
        w.data = {**w.data, "ledger": ledger, "balance_minor": amount}
    elif action == "passport":
        role = require(db, actor, {"curator", "specialist"}, w)
        if w.kind != "digital":
            raise BusinessError("Цифровой паспорт ведётся в карточке digital.")
        allowed = {"work_phone_messenger", "management_messenger", "systems", "channels", "equipment", "zones", "prices", "address", "phone", "call_center_applicable"}
        if not set(data) <= allowed:
            raise BusinessError("Неизвестное поле цифрового паспорта.")
        for key in ("work_phone_messenger", "management_messenger"):
            if key in data and data[key] not in {"telegram", "max"}:
                raise BusinessError("Рабочий телефон и чаты управления выбираются независимо: telegram/max.")
        passport = {**w.data.get("passport", {}), **data}
        components = dict(w.data.get("components", {}))
        if any(k in data for k in ("zones", "prices", "channels", "systems")):
            affected = set(data.get("systems", [])) or set(components)
            components = {k: {**v, "confirmed": False, "needs_recheck": True} if k in affected else v for k, v in components.items()}
        # Служба заботы обязательна независимо от применимости КЦ.
        required = set(passport.get("systems", [])) | {"ama_rs", "care"}
        if passport.get("call_center_applicable"):
            required.add("call_center")
        w.data = {**w.data, "passport": passport, "components": components, "required_components": sorted(required)}
    elif action == "tech_confirm":
        role = require(db, actor, {"specialist"}, w)
        if w.kind != "digital" or actor.person_id != w.executor_id:
            raise AccessDenied("Подключение подтверждает назначенный технический исполнитель.")
        if not data.get("evidence") or not data.get("component") or not data.get("control_test"):
            raise BusinessError("Нужны компонент, подтверждение подключения и результат контрольного теста.")
        if data["component"] not in w.data.get("required_components", []):
            raise BusinessError("Применимость компонента ещё не подтверждена в паспорте.")
        components = {**w.data.get("components", {}), data["component"]: {**data, "confirmed": True, "at": now().isoformat()}}
        w.data = {**w.data, "components": components}
    elif action == "opening_confirm":
        role = require(db, actor, {"curator"}, w)
        if w.kind not in {"internal_opening", "technical_opening", "official_opening"} or actor.person_id != w.reviewer_id:
            raise AccessDenied("Допуск к открытию подтверждает назначенный куратор.")
        training = db.scalar(select(Training).where(Training.person_id == w.partner_person_id, Training.course_key == "general",
                                                   Training.completed_at.is_not(None)))
        if not training or w.data.get("blockers"):
            raise BusinessError("Нет завершённого персонального ознакомления или есть красный блокер.")
        for dep in w.data.get("depends_on", []):
            item = db.get(Work, dep)
            if not item or item.outcome != "accepted":
                raise BusinessError("Есть непринятый обязательный результат.")
        if not data.get("evidence"):
            raise BusinessError("Нужен фактический результат проверки готовности.")
        w.data = {**w.data, "opening_permission": {"at": now().isoformat(), "evidence": data["evidence"]}}
    else:
        raise BusinessError("Неизвестная профильная операция.")
    w.version += 1
    make_timer(db, w, "deadline", w.due_at)
    stamp(db, actor, w, action.upper(), old, data, role)
    queue(db, db.get(Binding, w.binding_id), f"operation:{w.id}:{w.version}", f"Обновлена работа {w.id}: {action}", work=w)
    return w

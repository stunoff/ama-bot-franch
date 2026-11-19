"""Специализированные подтверждения платных работ, долга, цифровой готовности и допуска."""

from sqlalchemy import select
from ama.auth import require_business as require, business_roles as roles_for
from ama.errors import BusinessError, AccessDenied
from ama.models import Work, now, Binding
from ama.service import versioned, stamp, queue, make_timer


def operation(db, actor, work_id, expected, action, data):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True))
    if not w:
        raise BusinessError("Карточка не найдена.")
    roles_for(db, actor, w)
    versioned(w, expected)
    if action in {"offer", "offer_accept"}:
        from ama.finance import extra_operation

        return extra_operation(db, actor, w, expected, action, data)
    if action == "deferral":
        raise BusinessError(
            "Отсрочка применяется через /finance по адресному решению руководства. История и исходный срок совместимой карточки сохраняются."
        )
    old, role = w.version, None
    if w.data.get("digital_program") and action in {"passport", "tech_confirm"}:
        raise BusinessError("Используйте цифровой мастер: поля и результаты принимаются раздельно.")
    if action in {"invoice", "debt_payment"}:
        role = require(db, actor, {"contract"}, w)
        if w.kind != "debt":
            raise BusinessError("Договорная задолженность ведётся в отдельной карточке debt.")
        ledger = list(w.data.get("ledger", []))
        if action == "invoice":
            if not isinstance(data.get("amount_minor"), int) or data["amount_minor"] <= 0 or not data.get("document"):
                raise BusinessError("Нужны положительная сумма в копейках и документ.")
            ledger.append({"type": "invoice", **data, "at": now().isoformat(), "actor": actor.person_id})
        elif action == "debt_payment":
            if (
                not isinstance(data.get("amount_minor"), int)
                or data["amount_minor"] <= 0
                or not data.get("bank_reference")
            ):
                raise BusinessError("Нужны положительная сумма и банковское подтверждение.")
            # Банк. ссылка уникальна в истории, а не повторяемое клиентское фото.
            if any(e.get("bank_reference") == data["bank_reference"] for e in ledger):
                raise BusinessError("Операция уже учтена.")
            from ama.bank_references import legacy_payment

            legacy_payment(db, w, data["bank_reference"])
            ledger.append({"type": "payment", **data, "at": now().isoformat(), "actor": actor.person_id})
        amount = sum(
            e["amount_minor"] if e["type"] == "invoice" else -e["amount_minor"] if e["type"] == "payment" else 0
            for e in ledger
        )
        w.data = {**w.data, "ledger": ledger, "balance_minor": amount}
    elif action == "passport":
        role = require(db, actor, {"curator", "specialist"}, w)
        if w.kind != "digital":
            raise BusinessError("Цифровой паспорт ведётся в карточке digital.")
        allowed = {
            "work_phone_messenger",
            "management_messenger",
            "systems",
            "channels",
            "equipment",
            "zones",
            "prices",
            "address",
            "phone",
            "call_center_applicable",
        }
        if not set(data) <= allowed:
            raise BusinessError("Неизвестное поле цифрового паспорта.")
        for key in ("work_phone_messenger", "management_messenger"):
            if key in data and data[key] not in {"telegram", "max"}:
                raise BusinessError("Рабочий телефон и чаты управления выбираются независимо: telegram/max.")
        passport = {**w.data.get("passport", {}), **data}
        components = dict(w.data.get("components", {}))
        if any(k in data for k in ("zones", "prices", "channels", "systems")):
            affected = set(data.get("systems", [])) or set(components)
            components = {
                k: {**v, "confirmed": False, "needs_recheck": True} if k in affected else v
                for k, v in components.items()
            }
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
        components = {
            **w.data.get("components", {}),
            data["component"]: {**data, "confirmed": True, "at": now().isoformat()},
        }
        w.data = {**w.data, "components": components}
    elif action == "opening_confirm":
        role = require(db, actor, {"curator"}, w)
        if (
            w.kind not in {"internal_opening", "technical_opening", "official_opening"}
            or actor.person_id != w.reviewer_id
        ):
            raise AccessDenied("Допуск к открытию подтверждает назначенный куратор.")
        from ama.learning import completed

        if not completed(db, w.partner_person_id) or w.data.get("blockers"):
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
    from ama.sla import refresh_deadline

    refresh_deadline(db, w, "profile_operation:" + action)
    w.version += 1
    make_timer(db, w, "deadline", w.due_at)
    stamp(db, actor, w, action.upper(), old, data, role)
    queue(
        db, db.get(Binding, w.binding_id), f"operation:{w.id}:{w.version}", f"Обновлена работа {w.id}: {action}", work=w
    )
    return w

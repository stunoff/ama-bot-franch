"""Human-confirmed contracts, bank allocation, recurring billing and debt workflow (§12AA/G)."""

from copy import deepcopy
from datetime import timedelta, datetime, timezone
from calendar import monthrange
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from sqlalchemy import select, func
from ama.models import (
    FinanceAgreement,
    FinanceObligation,
    FinanceReceipt,
    FinanceAllocation,
    FinanceSchedule,
    FinanceBatch,
    Work,
    Record,
    Binding,
    Entity,
    Outbox,
    Payment,
    now,
)
from ama.auth import active_person_role
from ama.config import settings
from ama.errors import AccessDenied, BusinessError, Conflict
from ama.service import parse_date, versioned, queue, stamp, create_work


def money(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 2_000_000_000:
        raise BusinessError("Сумма должна быть положительным целым числом копеек.")
    return value


def binding(db, actor, purposes):
    b = db.get(Binding, actor.binding_id)
    if not b or b.bot_id != settings().bot_id or not b.active or not b.verified or b.purpose not in purposes:
        raise AccessDenied("Откройте действие в действующей профильной очереди.")
    return b


def scope_check(b, obj):
    if any(
        getattr(b, k) and getattr(b, k) != getattr(obj, k, None) for k in ("partner_id", "brand_id", "establishment_id")
    ):
        raise AccessDenied("Другая область договора / предприятия.")


def get(db, ident, lock=False):
    q = select(FinanceAgreement).where(FinanceAgreement.id == ident, FinanceAgreement.bot_id == settings().bot_id)
    a = db.scalar(q.with_for_update().execution_options(populate_existing=True) if lock else q)
    if not a:
        raise AccessDenied("Договор недоступен.")
    return a


def root(db, a):
    return db.get(Work, a.work_id)


def authorize(db, actor, a, *, mutate=False):
    b = binding(db, actor, {"contracts", "support", "sales", "legal", "leadership"})
    scope_check(b, a)
    w = root(db, a)
    if b.purpose == "contracts" and active_person_role(db, actor.person_id, {"contract"}, a):
        if mutate and actor.person_id not in {w.owner_id, w.executor_id}:
            raise AccessDenied("Действие назначенного ответственного по договорам.")
        return "contract"
    if (
        b.purpose in {"support", "sales"}
        and actor.person_id == a.data.get("communicator_id")
        and active_person_role(db, actor.person_id, {"curator", "consultant"}, a)
    ):
        return "communicator"
    if (
        b.purpose == "legal"
        and active_person_role(db, actor.person_id, {"lawyer"}, a)
        and any(
            w.id == a.work_id or w.parent_id == a.work_id
            for w in db.scalars(select(Work).where(Work.owner_id == actor.person_id, Work.binding_id == b.id))
        )
    ):
        return "lawyer"
    raise AccessDenied("Нет назначения в договорном процессе.")


def route(db, purpose, area, roles):
    records = list(
        db.scalars(select(Record).where(Record.kind == "route", Record.key == purpose, Record.published.is_(True)))
    )
    candidates = []
    for r in records:
        b = db.get(Binding, r.data.get("binding_id"))
        if not b or b.bot_id != settings().bot_id or not b.active or not b.verified or b.purpose != purpose:
            continue
        if any(
            getattr(b, k) and getattr(b, k) != getattr(area, k, None)
            for k in ("partner_id", "brand_id", "establishment_id")
        ):
            continue
        if active_person_role(db, r.data.get("owner_id"), roles, area):
            candidates.append((r, b))
    if len(candidates) != 1:
        raise BusinessError("Нужен один опубликованный маршрут с назначенным ответственным: " + purpose)
    return candidates[0]


def history(data, actor, event, **values):
    return {
        **data,
        "history": [
            *data.get("history", []),
            {
                "event": event,
                "actor": actor.person_id if actor else None,
                "at": now().isoformat() + "Z",
                **deepcopy(values),
            },
        ],
    }


def prepare_topic(db, w, target, key):
    if target.platform == "telegram":
        row = Outbox(
            dedupe_key=key,
            platform="telegram",
            chat_id=target.chat_id,
            topic_key=None,
            binding_id=target.id,
            binding_version=target.version,
            work_id=w.id,
            payload={
                "op": "create_topic",
                "topic_name": (w.title + " · " + w.id)[:128],
                "finance_topic": {"work_id": w.id, "target_id": target.id, "pending": bool(w.pending_owner_id)},
            },
        )
        db.add(row)
    else:
        queue(db, target, key, w.title + "\n/finance — договорные действия", work=w)


def on_topic(db, row, external_id):
    if row.payload.get("finance_batch_topic"):
        batch = db.get(FinanceBatch, row.payload["finance_batch_topic"])
        source = db.get(Binding, row.binding_id)
        if batch.data.get("topic_binding_id"):
            return
        if not external_id or not str(external_id).isdigit():
            raise BusinessError("Не получен ID месячной темы.")
        b = Binding(
            bot_id=source.bot_id,
            platform=source.platform,
            chat_id=source.chat_id,
            topic_key=str(external_id),
            purpose=source.purpose,
            active=True,
            verified=True,
            partner_id=source.partner_id,
            brand_id=source.brand_id,
            establishment_id=source.establishment_id,
        )
        db.add(b)
        db.flush()
        batch.data = {**batch.data, "topic_binding_id": b.id}
        queue(
            db,
            b,
            "finance-batch-card:" + batch.id,
            "Счета ФП · " + batch.period + "\n/finance — подтверждённые документы и отправки",
        )
        return

    ctx = row.payload["finance_topic"]
    w = db.get(Work, ctx["work_id"])
    source = db.get(Binding, ctx["target_id"])
    if not w or not source or not external_id or not str(external_id).isdigit():
        raise BusinessError("Не получен подтверждённый ID темы договорного запроса.")
    field = "pending_binding_id" if ctx["pending"] else "binding_id"
    if getattr(w, field) != source.id:
        return
    b = Binding(
        bot_id=source.bot_id,
        platform=source.platform,
        chat_id=source.chat_id,
        topic_key=str(external_id),
        purpose=source.purpose,
        active=True,
        verified=True,
        partner_id=source.partner_id,
        brand_id=source.brand_id,
        establishment_id=source.establishment_id,
    )
    db.add(b)
    db.flush()
    setattr(w, field, b.id)
    queue(
        db,
        b,
        "finance-topic-card:" + w.id + ":" + b.id,
        w.title + "\nЕдиный request_id: " + w.id + "\n/finance",
        work=w,
    )


def request(db, actor, data, key):
    b = binding(db, actor, {"support", "sales"})
    area = SimpleNamespace(
        partner_id=data.get("partner_id"), brand_id=data.get("brand_id"), establishment_id=data.get("establishment_id")
    )
    role = "consultant" if b.purpose == "sales" else "curator"
    scope_check(b, area)
    if not active_person_role(db, actor.person_id, {role}, area):
        raise AccessDenied("Запрос создаёт владелец клиентского общения.")
    if not data.get("terms") or not data.get("party_details") or not data.get("basis"):
        raise BusinessError("Нужны реквизиты / перечень отсутствующего, согласованные условия и основание.")
    if not db.get(Entity, area.partner_id):
        raise BusinessError(
            "Для нового лида сначала используйте договорную передачу сделки продаж; здесь нужен учтённый ФП."
        )
    old = db.scalar(select(Work).where(Work.origin_key == key, Work.kind == "finance_contract"))
    if old:
        existing = get(db, old.data["finance_agreement"])
        authorize(db, actor, existing)
        return existing
    r, target = route(db, "contracts", area, {"contract"})
    due = parse_date(data["due_at"])
    previous = db.info.get("finance_create")
    db.info["finance_create"] = True
    try:
        w = create_work(
            db,
            actor,
            {
                "kind": "finance_contract",
                "title": ("Договор / допсоглашение · " + db.get(Entity, area.partner_id).name)[:250],
                "partner_id": area.partner_id,
                "brand_id": area.brand_id,
                "establishment_id": area.establishment_id,
                "owner_id": r.data["owner_id"],
                "reviewer_id": actor.person_id,
                "binding_id": target.id,
                "calendar": r.data.get("calendar", "support"),
                "due_at": due,
                "data": {},
                "origin_key": key,
            },
            key,
        )
    finally:
        db.info["finance_create"] = previous
    w.review_binding_id = b.id
    a = FinanceAgreement(
        bot_id=settings().bot_id,
        work_id=w.id,
        partner_id=area.partner_id,
        brand_id=area.brand_id,
        establishment_id=area.establishment_id,
        data={
            "communicator_id": actor.person_id,
            "communication_binding": b.id,
            "contract_owner": r.data["owner_id"],
            "terms": data["terms"],
            "party_details": data["party_details"],
            "basis": data["basis"],
            "package": 1,
            "signatures": {},
            "invitation": {},
            "history": [],
        },
    )
    db.add(a)
    db.flush()
    w.data = {**w.data, "finance_agreement": a.id}
    prepare_topic(db, w, target, "finance-topic:" + w.id)
    return a


def agreement_action(db, actor, ident, expected, action, data):
    a = get(db, ident, True)
    versioned(a, expected)
    role = authorize(db, actor, a, mutate=True)
    w = root(db, a)
    if action == "reply_clarification":
        if role != "communicator":
            raise AccessDenied("Отвечает владелец клиентского общения.")
        if not data.get("answer"):
            raise BusinessError("Нужен ответ по уточнению.")
        a.data = history(a.data, actor, "clarification_answer", answer=data["answer"])
        a.data = {**a.data, "clarification": None}
    else:
        if role != "contract":
            raise AccessDenied("Факт фиксирует назначенный ответственный по договорам.")
        if action == "clarify":
            if not data.get("missing") or not data.get("due_at"):
                raise BusinessError("Нужны недостающие данные и дата контроля.")
            due = parse_date(data["due_at"])
            if due <= now():
                raise BusinessError("Нужен будущий контроль.")
            a.data = history(a.data, actor, "clarification", missing=data["missing"], due=data["due_at"])
            a.data = {**a.data, "clarification": data}
            queue(
                db,
                db.get(Binding, a.data["communication_binding"]),
                "finance-clarify:" + a.id + ":" + str(a.version),
                data["missing"] + "\n/finance — ответ в той же карточке",
                work=w,
            )
        elif action == "package":
            if not data.get("document_reference") or not data.get("reason"):
                raise BusinessError("Нужны актуальный комплект и причина редакции.")
            a.data = history(
                a.data,
                actor,
                "new_package",
                previous_package=a.data["package"],
                previous_signatures=a.data.get("signatures", {}),
                previous_document=a.data.get("document_reference"),
                reason=data["reason"],
            )
            a.data = {
                **a.data,
                "package": a.data["package"] + 1,
                "signatures": {},
                "document_reference": data["document_reference"],
                "edo_sent": None,
            }
        elif action in {"invitation_sent", "invitation_accepted", "edo_sent", "signed_uk", "signed_partner"}:
            if not data.get("evidence") or not data.get("at"):
                raise BusinessError("Нужны подтверждение и фактическая дата.")
            at = parse_date(data["at"])
            if at > now():
                raise BusinessError("Фактическая дата не может быть будущей.")
            if action.startswith("signed_") and data.get("package") != a.data["package"]:
                raise Conflict("Подпись относится к другой редакции комплекта.")
            a.data = history(a.data, actor, action, evidence=data["evidence"], at=data["at"], package=a.data["package"])
            if action in {"edo_sent", "signed_uk", "signed_partner"} and not a.data.get("document_reference"):
                raise BusinessError("Сначала зарегистрируйте актуальный договорный комплект.")
            if action.startswith("signed_") and not a.data.get("edo_sent"):
                raise BusinessError("Нет подтверждённой отправки актуального комплекта в ЭДО.")
            if action.startswith("signed_"):
                if data.get("package") != a.data["package"]:
                    raise Conflict("Подпись относится к другой редакции комплекта.")
                a.data = {
                    **a.data,
                    "signatures": {
                        **a.data.get("signatures", {}),
                        action: {
                            "evidence": data["evidence"],
                            "at": data["at"],
                            "actor": actor.person_id,
                            "package": a.data["package"],
                        },
                    },
                }
            else:
                a.data = {
                    **a.data,
                    action: {"evidence": data["evidence"], "at": data["at"], "package": a.data["package"]},
                }
        elif action == "activate":
            if set(a.data.get("signatures", {})) != {"signed_uk", "signed_partner"}:
                raise BusinessError("Нет подтверждённых подписей обеих сторон этой редакции.")
            if data.get("purpose") not in {"franchise", "royalty", "additional_service"}:
                raise BusinessError("Разделите франшизное обязательство и дополнительную услугу.")
            if (
                not data.get("activation_evidence")
                or data.get("first_period_rule") not in {"full", "manual"}
                or not isinstance(data.get("payment_day"), int)
                or not 1 <= data["payment_day"] <= 31
            ):
                raise BusinessError(
                    "Нужны подтверждённые договорные условия: основание, день оплаты и правило неполного периода."
                )
            ZoneInfo(data["payment_timezone"])
            recipient = payment_policy(db, data["payment_policy"], a)
            amount = money(data["amount_minor"])
            start = parse_date(data["starts_at"])
            a.active = True
            a.data = history(a.data, actor, "activated", package=a.data["package"], terms=data)
            a.data = {
                **a.data,
                "active_package": a.data["package"],
                "purpose": data["purpose"],
                "period_amount": amount,
                "starts_at": start.isoformat() + "Z",
                "recipient": recipient,
                "payment_day": data["payment_day"],
                "payment_timezone": data["payment_timezone"],
                "first_period_rule": data["first_period_rule"],
                "activation_evidence": data["activation_evidence"],
            }
        elif action == "suspend":
            if not data.get("reason"):
                raise BusinessError("Нужно основание прекращения будущего начисления.")
            a.active = False
            a.data = history(a.data, actor, "suspended", reason=data["reason"])
        else:
            raise BusinessError("Неизвестное договорное действие.")
    if role == "contract" and w.status == "new" and actor.person_id == w.owner_id:
        w.status = "in_progress"
        from ama.sla import on_transition

        on_transition(db, actor, w, "take", {})
    a.version += 1
    w.version += 1
    stamp(db, actor, a, "FINANCE_" + action.upper(), expected, data, role)
    return a


def payment_policy(db, ident, area=None):
    r = db.get(Record, ident)
    if (
        not r
        or not r.published
        or r.kind != "policy"
        or r.data.get("policy_type") not in {"payment", "finance_recipient"}
        or not r.data.get("approval")
        or not r.data.get("reference")
    ):
        raise BusinessError("Нужны утверждённые реквизиты получателя из настроек.")
    if area and any(
        r.data.get(k) and r.data[k] != getattr(area, k, None) for k in ("partner_id", "brand_id", "establishment_id")
    ):
        raise AccessDenied("Реквизиты другой области.")
    return {
        "record_id": r.id,
        "version": r.version,
        "reference": r.data["reference"],
        "recipient_key": r.data.get("recipient_key") or r.id,
        "approval": r.data["approval"],
        "owner_id": r.owner_id,
    }


def obligation(db, actor, agreement_id, expected, data):
    a = get(db, agreement_id, True)
    versioned(a, expected)
    if authorize(db, actor, a, mutate=True) != "contract":
        raise AccessDenied("Обязательство фиксирует ответственный по договорам.")
    if not a.active:
        raise BusinessError("Договорное основание ещё не активировано.")
    old = db.scalar(
        select(FinanceObligation).where(
            FinanceObligation.agreement_id == a.id, FinanceObligation.period == data["period"]
        )
    )
    if old:
        return old
    due = parse_date(data["due_at"])
    amount = money(data["amount_minor"])
    if not data.get("basis"):
        raise BusinessError("Нужно документальное основание суммы и срока.")
    o = FinanceObligation(
        agreement_id=a.id,
        period=data["period"],
        amount_minor=amount,
        due_at=due,
        data={
            "purpose": a.data["purpose"],
            "agreement_package": a.data["active_package"],
            "recipient": deepcopy(a.data["recipient"]),
            "basis": data["basis"],
            "invoice": {},
            "history": [],
        },
    )
    db.add(o)
    db.flush()
    a.version += 1
    return o


def get_obligation(db, ident, lock=False):
    q = select(FinanceObligation).where(FinanceObligation.id == ident)
    o = db.scalar(q.with_for_update().execution_options(populate_existing=True) if lock else q)
    if not o:
        raise BusinessError("Обязательство не найдено.")
    return o, get(db, o.agreement_id)


def balance(db, o, lock=False):
    q = select(FinanceAllocation.amount_minor).where(FinanceAllocation.obligation_id == o.id)
    if lock:
        q = q.with_for_update()
    return o.amount_minor - sum(db.scalars(q))


def invoice(db, actor, ident, expected, data):
    o, a = get_obligation(db, ident, True)
    versioned(o, expected)
    if authorize(db, actor, a, mutate=True) != "contract":
        raise AccessDenied("Счёт фиксирует ответственный.")
    if not all(data.get(k) for k in ("number", "date", "document", "status")):
        raise BusinessError("Нужны номер/дата и подтверждённый документ счёта.")
    parse_date(data["date"])
    if data["status"] not in {"prepared", "sent"}:
        raise BusinessError("Укажите подготовку либо подтверждённую отправку.")
    if data["status"] == "sent" and (not data.get("sent_at") or not data.get("evidence")):
        raise BusinessError("Отправка требует фактической даты и подтверждения.")
    if data.get("sent_at") and parse_date(data["sent_at"]) > now():
        raise BusinessError("Отправка не может быть будущей.")
    o.data = history(o.data, actor, "invoice", previous=o.data.get("invoice"), invoice=data)
    o.data = {**o.data, "invoice": deepcopy(data)}
    o.version += 1
    return o


def receipt(db, actor, agreement_id, data):
    a = get(db, agreement_id, True)
    if authorize(db, actor, a, mutate=False) != "contract":
        raise AccessDenied("Поступление подтверждает договорной ответственный, не клиентский скрин.")
    amount = money(data["amount_minor"])
    at = parse_date(data["received_at"])
    if at > now() or not data.get("bank_reference") or not data.get("evidence"):
        raise BusinessError("Нужны фактическое банковское подтверждение, ссылка операции и дата.")
    if db.scalar(select(Payment.id).where(func.trim(Payment.source_key) == data["bank_reference"].strip())):
        raise BusinessError("Эта банковская операция уже учтена в оплате франшизы.")
    recipient = a.data.get("recipient")
    if not recipient:
        raise BusinessError("Получатель ещё не утверждён.")
    # Lock one recipient record to serialize first sight of the same bank transaction.
    db.scalar(select(Record).where(Record.id == recipient["record_id"]).with_for_update())
    old = db.scalar(
        select(FinanceReceipt).where(
            FinanceReceipt.recipient_key == recipient["recipient_key"],
            FinanceReceipt.bank_reference == data["bank_reference"].strip(),
        )
    )
    if old:
        if old.bot_id != settings().bot_id:
            raise Conflict("Операция банка уже закреплена в другом финансовом контуре.")
        if old.amount_minor != amount or old.data.get("partner_id") != a.partner_id:
            raise Conflict("Операция банка уже зарегистрирована с другими данными.")
        return old
    r = FinanceReceipt(
        bot_id=a.bot_id,
        recipient_key=recipient["recipient_key"],
        bank_reference=data["bank_reference"].strip(),
        amount_minor=amount,
        received_at=at,
        confirmed_by=actor.person_id,
        data={"partner_id": a.partner_id, "recipient": deepcopy(recipient), "evidence": data["evidence"]},
    )
    db.add(r)
    db.flush()
    return r


def allocate(db, actor, obligation_id, expected, data, key):
    o, a = get_obligation(db, obligation_id, True)
    versioned(o, expected)
    if authorize(db, actor, a, mutate=False) != "contract":
        raise AccessDenied("Распределение подтверждает ответственный по договорам.")
    r = db.scalar(
        select(FinanceReceipt)
        .where(FinanceReceipt.id == data["receipt_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not r
        or r.bot_id != settings().bot_id
        or r.data.get("partner_id") != a.partner_id
        or r.recipient_key != o.data["recipient"]["recipient_key"]
    ):
        raise AccessDenied("Поступление другого ФП / получателя.")
    old = db.scalar(select(FinanceAllocation).where(FinanceAllocation.source_key == key))
    if old:
        return old
    amount = money(data["amount_minor"])
    used = sum(
        db.scalars(select(FinanceAllocation.amount_minor).where(FinanceAllocation.receipt_id == r.id).with_for_update())
    )
    if amount > r.amount_minor - used or amount > balance(db, o, True):
        raise BusinessError("Нельзя распределить больше поступления или остатка обязательства.")
    item = FinanceAllocation(
        receipt_id=r.id,
        obligation_id=o.id,
        amount_minor=amount,
        source_key=key,
        data={"actor": actor.person_id, "bank_reference": r.bank_reference, "purpose": o.data["purpose"]},
    )
    db.add(item)
    db.flush()
    o.version += 1
    o.data = history(o.data, actor, "bank_allocation", receipt=r.id, amount_minor=amount, balance_minor=balance(db, o))
    # Additional services never become a franchise payment, and a zero balance does not close a legal case.
    if o.debt_work_id:
        w = db.get(Work, o.debt_work_id)
        w.data = {**w.data, "financial_balance": balance(db, o)}
        w.version += 1
    return item


def open_debt(db, actor, obligation_id, expected, data):
    o, a = get_obligation(db, obligation_id, True)
    versioned(o, expected)
    if authorize(db, actor, a, mutate=True) != "contract":
        raise AccessDenied("Задолженность фиксирует договорной ответственный.")
    if o.debt_work_id:
        return db.get(Work, o.debt_work_id)
    if o.due_at >= now() or balance(db, o) <= 0 or not data.get("evidence"):
        raise BusinessError("Нужны просроченный подтверждённый остаток и документальное основание.")
    inv = o.data.get("invoice", {})
    if not inv.get("number") or not inv.get("document"):
        raise BusinessError("Для карточки долга укажите фактический счёт и подтверждающие документы.")
    r, target = route(db, "contracts", a, {"contract"})
    due = now() + timedelta(days=1)
    w = Work(
        kind="finance_debt",
        title=("Задолженность · " + db.get(Entity, a.partner_id).name + " · " + o.period)[:250],
        partner_id=a.partner_id,
        brand_id=a.brand_id,
        establishment_id=a.establishment_id,
        owner_id=actor.person_id,
        executor_id=actor.person_id,
        reviewer_id=actor.person_id,
        binding_id=target.id,
        parent_id=a.work_id,
        due_at=due,
        original_due_at=due,
        data={
            "calendar": r.data.get("calendar", "support"),
            "financial_obligation": o.id,
            "finance_agreement": a.id,
            "stage": "identified",
            "history": [],
            "financial_balance": balance(db, o),
            "original_payment_due": o.due_at.isoformat() + "Z",
            "evidence": data["evidence"],
        },
    )
    db.add(w)
    db.flush()
    o.debt_work_id = w.id
    o.version += 1
    prepare_topic(db, w, target, "finance-topic:" + w.id)
    return w


def debt_action(db, actor, work_id, expected, action, data):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True))
    if not w or w.kind != "finance_debt":
        raise BusinessError("Нет карточки задолженности.")
    versioned(w, expected)
    o, a = get_obligation(db, w.data["financial_obligation"], True)
    if o.debt_work_id != w.id:
        raise AccessDenied("Некорректная связь request_id с обязательством.")
    b = binding(db, actor, {"contracts", "support", "legal"})
    scope_check(b, a)
    stage = w.data["stage"]
    owed = balance(db, o, True)
    contract = (
        actor.person_id == w.owner_id
        and b.purpose == "contracts"
        and active_person_role(db, actor.person_id, {"contract"}, a)
    )
    lawyer = (
        actor.person_id == w.owner_id
        and b.purpose == "legal"
        and active_person_role(db, actor.person_id, {"lawyer"}, a)
    )
    if action == "curator_reply":
        if (
            actor.person_id != w.data.get("curator_id")
            or b.purpose != "support"
            or not active_person_role(db, actor.person_id, {"curator"}, a)
        ):
            raise AccessDenied("Отвечает назначенный куратор в сопровождении.")
        if not data.get("evidence"):
            raise BusinessError("Нужен результат общения с ФП.")
    elif action == "legal_accept":
        if (
            actor.person_id != w.pending_owner_id
            or actor.binding_id != w.pending_binding_id
            or not active_person_role(db, actor.person_id, {"lawyer"}, a)
        ):
            raise AccessDenied("Передачу принимает назначенный юрист в своей теме.")
        w.owner_id = w.executor_id = w.pending_owner_id
        w.binding_id = w.pending_binding_id
        w.pending_owner_id = None
        w.pending_binding_id = None
        stage = "legal"
    elif action == "close":
        if not (contract and stage != "legal" or lawyer and stage == "legal"):
            raise AccessDenied("Закрывает текущий владелец процедуры.")
        if owed != 0 or not data.get("evidence"):
            raise BusinessError("Нужны нулевой подтверждённый остаток и результат проверки.")
        stage = "closed"
        w.status = "done"
        w.outcome = "accepted"
    else:
        if owed <= 0:
            raise BusinessError("Остаток погашен. Проверьте оплату и закройте карточку, без дальнейшего взыскания.")
        if not contract or stage in {"legal", "closed"}:
            raise AccessDenied("До принятия юристом процедуру ведёт договорной ответственный.")
        if action == "curator":
            if stage not in {"identified", "disputed"}:
                raise BusinessError("Первый этап уже запускался.")
            r, target = route(db, "support", a, {"curator"})
            w.data = {**w.data, "curator_id": r.data["owner_id"]}
            w.reviewer_id = r.data["owner_id"]
            w.review_binding_id = target.id
            row = queue(
                db,
                target,
                "finance-curator:" + w.id + ":" + str(w.version),
                "Урегулирование задолженности через куратора. Остаток: "
                + str(owed)
                + " коп.\nЕдиный request_id: "
                + w.id,
                work=w,
            )
            row.payload = {**row.payload, "finance_curator_delivery": w.id}
            stage = "curator_delivery"
        elif action == "formal":
            if stage != "curator_check" or not data.get("bank_review"):
                raise BusinessError("Сначала завершите 7-дневный контроль и подтвердите проверку оплаты.")
            for field in ("email", "edo"):
                if (
                    not data.get(field + "_evidence")
                    or not data.get(field + "_sent_at")
                    or parse_date(data[field + "_sent_at"]) > now()
                ):
                    raise BusinessError("Нужны фактические отправки по почте и ЭДО.")
            due = max(parse_date(data["email_sent_at"]), parse_date(data["edo_sent_at"])) + timedelta(days=3)
            stage = "formal_wait"
            w.data = {**w.data, "control_due": due.isoformat() + "Z"}
            w.due_at = due
        elif action == "pretrial":
            if stage != "formal_check" or not data.get("bank_review") or not data.get("evidence"):
                raise BusinessError("Сначала завершите 3-дневный контроль, проверьте оплату и приложите уведомление.")
            due = parse_date(data["due_at"])
            if due <= now():
                raise BusinessError("Укажите согласованный будущий срок досудебного уведомления.")
            w.data = {**w.data, "control_due": due.isoformat() + "Z"}
            w.due_at = due
            stage = "pretrial_wait"
        elif action == "legal":
            if stage != "pretrial_check":
                raise BusinessError("Сначала истёкший досудебный срок и проверка результата.")
            if not data.get("bank_review") or not data.get("evidence"):
                raise BusinessError("Нужна проверка остатка и основание передачи.")
            r, target = route(db, "legal", a, {"lawyer"})
            w.pending_owner_id = r.data["owner_id"]
            w.pending_binding_id = target.id
            w.data = {
                **w.data,
                "legal_packet": {
                    "agreement": deepcopy(a.data),
                    "obligation": deepcopy(o.data),
                    "amount_minor": o.amount_minor,
                    "balance_minor": owed,
                    "request_id": w.id,
                    "history": deepcopy(w.data.get("history", [])),
                },
            }
            stage = "legal_pending"
            prepare_topic(db, w, target, "finance-legal:" + w.id + ":" + str(w.version))
        elif action == "dispute":
            disputed = data.get("disputed_minor")
            if (
                not isinstance(disputed, int)
                or isinstance(disputed, bool)
                or not 0 <= disputed <= owed
                or not data.get("evidence")
            ):
                raise BusinessError("Нужны оспариваемая часть в пределах остатка и основание.")
            w.data = {
                **w.data,
                "dispute": {
                    "disputed_minor": disputed,
                    "undisputed_minor": owed - disputed,
                    "evidence": data["evidence"],
                },
            }
            stage = "disputed"
        else:
            raise BusinessError("Неизвестный этап задолженности.")
    w.data = history(w.data, actor, action, previous_stage=w.data["stage"], details=data, balance_minor=owed)
    w.data = {**w.data, "stage": stage}
    w.version += 1
    o.version += 1
    stamp(
        db,
        actor,
        w,
        "FINANCE_DEBT_" + action.upper(),
        expected,
        data,
        "lawyer" if lawyer else "contract" if contract else "curator",
    )
    return w


def decision_request(db, actor, work_id, expected, data):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update())
    versioned(w, expected)
    a = get(db, w.data["finance_agreement"])
    if authorize(db, actor, a, mutate=True) != "contract":
        raise AccessDenied("Запрашивает договорной ответственный.")
    if not data.get("question") or not data.get("position") or not data.get("due_at"):
        raise BusinessError("Нужны вопрос, позиция ответственного и срок решения.")
    due = parse_date(data["due_at"])
    if due <= now():
        raise BusinessError("Нужен будущий срок решения.")
    purpose = "legal" if data.get("legal_review") else "leadership"
    role = "lawyer" if purpose == "legal" else "leadership"
    r, target = route(db, purpose, a, {role})
    existing = w.data.get("decision_work")
    if existing and db.get(Work, existing) and db.get(Work, existing).status != "done":
        return db.get(Work, existing)
    child = Work(
        kind="finance_decision",
        title=("Правовая оценка · " if purpose == "legal" else "Решение руководства · ") + w.id,
        partner_id=w.partner_id,
        brand_id=w.brand_id,
        establishment_id=w.establishment_id,
        owner_id=r.data["owner_id"],
        executor_id=r.data["owner_id"],
        reviewer_id=r.data["owner_id"],
        binding_id=target.id,
        parent_id=w.id,
        due_at=due,
        original_due_at=due,
        data={
            "calendar": r.data.get("calendar", "support"),
            "finance_agreement": a.id,
            "finance_decision": True,
            "decision_role": role,
            "question": data["question"],
            "position": data["position"],
            "source_request_id": w.id,
            "packet": deepcopy(w.data),
        },
    )
    db.add(child)
    db.flush()
    w.data = history(w.data, actor, "decision_requested", decision_id=child.id, question=data)
    w.data = {**w.data, "decision_work": child.id}
    w.version += 1
    prepare_topic(db, child, target, "finance-topic:" + child.id)
    return child


def record_decision(db, actor, work_id, expected, data):
    child = db.scalar(
        select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True)
    )
    if not child or child.kind not in {"finance_decision", "finance_extra_review"}:
        raise BusinessError("Нет карточки решения.")
    versioned(child, expected)
    a = (
        get(db, child.data["finance_agreement"])
        if child.data.get("finance_agreement")
        else db.get(Work, child.parent_id)
    )
    b = binding(db, actor, {"leadership", "legal", "contracts"})
    scope_check(b, a)
    if (
        child.owner_id != actor.person_id
        or child.binding_id != actor.binding_id
        or not active_person_role(db, actor.person_id, {child.data["decision_role"]}, a)
    ):
        raise AccessDenied("Решение фиксирует назначенный руководитель / юрист в своей теме.")
    if child.status == "done":
        raise BusinessError("Решение уже зафиксировано.")
    if not data.get("decision") or not data.get("basis"):
        raise BusinessError("Нужны решение и подтверждённое основание.")
    if data.get("defer_until") and parse_date(data["defer_until"]) <= now():
        raise BusinessError("Отсрочка должна иметь будущий срок.")
    if data.get("valid_until") and parse_date(data["valid_until"]) <= now():
        raise BusinessError("Срок действия решения уже истёк.")
    child.data = history(child.data, actor, "decision", decision=data)
    child.data = {
        **child.data,
        "decision": {**deepcopy(data), "author_id": actor.person_id, "at": now().isoformat() + "Z"},
    }
    child.status = "done"
    child.outcome = "accepted"
    child.version += 1
    parent = db.get(Work, child.parent_id)
    parent.data = history(parent.data, actor, "decision_received", decision_id=child.id)
    parent.version += 1
    queue(
        db,
        db.get(Binding, parent.binding_id),
        "finance-decision:" + child.id,
        "Принято решение по request_id " + parent.id + "\n" + data["decision"],
        work=parent,
    )
    return child


def apply_deferral(db, actor, work_id, expected, data):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update().execution_options(populate_existing=True))
    versioned(w, expected)
    o, a = get_obligation(db, w.data["financial_obligation"], True)
    if authorize(db, actor, a, mutate=True) != "contract":
        raise AccessDenied("Решение применяет ответственный по договорам.")
    if w.data.get("stage") == "legal":
        raise BusinessError(
            "После принятия передачи решение направляется текущему юристу; договорной отдел не меняет его процедуру."
        )
    child = db.get(Work, data["decision_id"])
    decision = child.data.get("decision") if child else None
    if (
        not child
        or child.parent_id != w.id
        or child.outcome != "accepted"
        or child.data.get("decision_role") != "leadership"
        or not decision
        or not decision.get("approved")
        or not decision.get("defer_until")
    ):
        raise BusinessError("Нужно адресное утверждённое решение Руководства с будущим сроком.")
    if decision.get("valid_until") and parse_date(decision["valid_until"]) < now():
        raise BusinessError("Решение руководства истекло.")
    due = parse_date(decision["defer_until"])
    if due <= now():
        raise BusinessError("Согласованный срок уже прошёл; требуется актуальное решение.")
    w.data = history(
        w.data,
        actor,
        "deferral_applied",
        decision_id=child.id,
        original_control=w.data.get("control_due"),
        due=decision["defer_until"],
    )
    w.data = {**w.data, "control_due": decision["defer_until"], "stage": "deferral_wait", "applied_decision": child.id}
    w.due_at = due
    w.pending_owner_id = w.pending_binding_id = None
    w.version += 1
    o.data = history(o.data, actor, "deferral", decision=deepcopy(decision), original_due=o.due_at.isoformat() + "Z")
    o.version += 1
    for row in db.scalars(
        select(Outbox).where(Outbox.work_id == w.id, Outbox.status.in_(["pending", "failed", "blocked"]))
    ):
        if row.payload.get("finance_control"):
            row.status = "cancelled"
    return w


def create_schedule(db, actor, data):
    b = binding(db, actor, {"contracts"})
    if not active_person_role(db, actor.person_id, {"contract"}, b):
        raise AccessDenied("Расписание подтверждает ответственный по договорам.")
    start = parse_date(data["start_at"])
    end = parse_date(data["until_at"])
    tz = ZoneInfo(data["timezone"])
    if start <= now() or end < start:
        raise BusinessError("Нужны будущая начальная дата и конечная граница расписания.")
    if not data.get("approval"):
        raise BusinessError("Требуется утверждённый месячный календарь выставления счетов.")
    s = FinanceSchedule(
        bot_id=settings().bot_id,
        binding_id=b.id,
        owner_id=actor.person_id,
        next_at=start,
        until_at=end,
        data={
            "anchor_day": start.replace(tzinfo=timezone.utc).astimezone(tz).day,
            "timezone": data["timezone"],
            "approval": data["approval"],
            "binding_version": b.version,
            "history": [],
        },
    )
    db.add(s)
    db.flush()
    return s


def month_date(year, month, day, hour, minute, tz):
    return (
        datetime(year, month, min(day, monthrange(year, month)[1]), hour, minute, tzinfo=tz)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def batch_for(db, s, period, local):
    db.scalar(select(Binding).where(Binding.id == s.binding_id).with_for_update())
    old = db.scalar(
        select(FinanceBatch)
        .where(FinanceBatch.bot_id == s.bot_id, FinanceBatch.binding_id == s.binding_id, FinanceBatch.period == period)
        .with_for_update()
    )
    if old:
        return old
    b = db.get(Binding, s.binding_id)
    items = []
    for a in db.scalars(
        select(FinanceAgreement).where(FinanceAgreement.bot_id == s.bot_id, FinanceAgreement.active.is_(True))
    ):
        if any(
            getattr(b, k) and getattr(b, k) != getattr(a, k, None)
            for k in ("partner_id", "brand_id", "establishment_id")
        ):
            continue
        if not active_person_role(db, s.owner_id, {"contract"}, a):
            continue
        starts = parse_date(a.data["starts_at"]).replace(tzinfo=timezone.utc).astimezone(local.tzinfo)
        if (starts.year, starts.month) > (local.year, local.month):
            continue
        o = db.scalar(
            select(FinanceObligation).where(FinanceObligation.agreement_id == a.id, FinanceObligation.period == period)
        )
        if (
            starts.day > 1
            and (starts.year, starts.month) == (local.year, local.month)
            and a.data.get("first_period_rule") != "full"
        ):
            items.append(
                {
                    "agreement_id": a.id,
                    "status": "manual_first_period",
                    "reason": "Нужно подтвердить сумму неполного периода по договору.",
                }
            )
            continue
        if not a.data.get("payment_day") or not a.data.get("payment_timezone"):
            items.append(
                {
                    "agreement_id": a.id,
                    "status": "missing_payment_terms",
                    "reason": "Не настроен договорный срок оплаты.",
                }
            )
            continue
        if not o:
            due = month_date(
                local.year, local.month, a.data["payment_day"], 23, 59, ZoneInfo(a.data["payment_timezone"])
            )
            o = FinanceObligation(
                agreement_id=a.id,
                period=period,
                amount_minor=money(a.data["period_amount"]),
                due_at=due,
                data={
                    "purpose": a.data["purpose"],
                    "recipient": deepcopy(a.data["recipient"]),
                    "agreement_package": a.data["active_package"],
                    "basis": a.data["basis"],
                    "invoice": {},
                    "history": [],
                },
            )
            db.add(o)
            db.flush()
        items.append({"agreement_id": a.id, "obligation_id": o.id, "status": "needs_invoice"})
    batch = FinanceBatch(
        bot_id=s.bot_id,
        binding_id=b.id,
        period=period,
        data={
            "items": items,
            "schedule_id": s.id,
            "owner_id": s.owner_id,
            "status": "new",
            "due_at": (s.next_at + timedelta(days=1)).isoformat() + "Z",
        },
    )
    db.add(batch)
    db.flush()
    if b.platform == "telegram":
        db.add(
            Outbox(
                dedupe_key="finance-batch-topic:" + batch.id,
                platform="telegram",
                chat_id=b.chat_id,
                topic_key=None,
                binding_id=b.id,
                binding_version=b.version,
                payload={"op": "create_topic", "topic_name": "Счета ФП · " + period, "finance_batch_topic": batch.id},
            )
        )
    else:
        queue(db, b, "finance-batch:" + batch.id, "Счета ФП · " + period + "\n/finance — пакет счетов")
    return batch


def schedule_tick(db):
    s = db.scalar(
        select(FinanceSchedule)
        .where(
            FinanceSchedule.bot_id == settings().bot_id,
            FinanceSchedule.enabled.is_(True),
            FinanceSchedule.next_at <= now(),
        )
        .order_by(FinanceSchedule.next_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not s:
        return False
    b = db.get(Binding, s.binding_id)
    if (
        not b
        or not b.active
        or not b.verified
        or b.version != s.data["binding_version"]
        or not active_person_role(db, s.owner_id, {"contract"}, b)
    ):
        s.enabled = False
        s.data = {**s.data, "error": "Изменились получатель / маршрут. Требуется новое подтверждение календаря."}
        return True
    if s.next_at > s.until_at:
        s.enabled = False
        return True
    tz = ZoneInfo(s.data["timezone"])
    current = now().replace(tzinfo=timezone.utc).astimezone(tz)
    planned = s.next_at.replace(tzinfo=timezone.utc).astimezone(tz)
    if (planned.year, planned.month) < (current.year, current.month):
        s.data = {
            **s.data,
            "missed_periods": {
                "from": s.next_at.isoformat() + "Z",
                "to": now().isoformat() + "Z",
                "requires_reconciliation": True,
            },
        }
        queue(
            db,
            b,
            "finance-missed:" + s.id + ":" + str(s.version),
            "Пропущены периоды календаря счетов. Проверьте обязательства по договорам; сроки оплаты не меняются из-за отсутствия счёта.",
        )
        candidate = month_date(current.year, current.month, s.data["anchor_day"], planned.hour, planned.minute, tz)
        s.next_at = candidate
        if candidate > now():
            return True
    if s.next_at <= s.until_at:
        batch_for(db, s, current.strftime("%Y-%m"), current)
    month = current.month + 1
    year = current.year + (month == 13)
    month = 1 if month == 13 else month
    s.next_at = month_date(year, month, s.data["anchor_day"], planned.hour, planned.minute, tz)
    s.version += 1
    if s.next_at > s.until_at:
        s.enabled = False
    return True


def finish_batch(db, actor, ident, expected):
    batch = db.scalar(
        select(FinanceBatch).where(FinanceBatch.id == ident).with_for_update().execution_options(populate_existing=True)
    )
    authorize_batch(db, actor, batch)
    versioned(batch, expected)
    b = binding(db, actor, {"contracts"})
    if batch.data["owner_id"] != actor.person_id or not active_person_role(db, actor.person_id, {"contract"}, b):
        raise AccessDenied("Пакет завершает назначенный ответственный.")
    for item in batch.data["items"]:
        o = db.scalar(
            select(FinanceObligation)
            .where(FinanceObligation.agreement_id == item["agreement_id"], FinanceObligation.period == batch.period)
            .with_for_update()
        )
        if not o or o.data.get("invoice", {}).get("status") != "sent":
            raise BusinessError(
                "Не все применимые счета подтверждены как отправленные; уточните неполные периоды и договорные условия."
            )
    batch.data = {
        **batch.data,
        "status": "done",
        "completed_at": now().isoformat() + "Z",
        "completed_by": actor.person_id,
    }
    batch.version += 1
    return batch


def delivered(db, row):
    ctx = row.payload.get("finance_paid_result")
    if ctx:
        w = db.scalar(
            select(Work).where(Work.id == ctx["work_id"]).with_for_update().execution_options(populate_existing=True)
        )
        if w and w.version == ctx["version"] and w.status == "review":
            w.data = {**w.data, "paid_result_delivered_at": now().isoformat() + "Z"}

    if row.payload.get("finance_curator_delivery"):
        w = db.scalar(
            select(Work)
            .where(Work.id == row.payload["finance_curator_delivery"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if w and w.data["stage"] == "curator_delivery" and not w.data.get("curator_delivered_at"):
            due = now() + timedelta(days=7)
            w.data = {
                **w.data,
                "stage": "curator_wait",
                "curator_delivered_at": now().isoformat() + "Z",
                "control_due": due.isoformat() + "Z",
            }
            w.due_at = due
            w.version += 1


def debt_tick(db):
    for candidate in db.scalars(select(Work).where(Work.kind == "finance_debt", Work.status != "done")):
        if not candidate.data.get("control_due") or parse_date(candidate.data["control_due"]) > now():
            continue
        stage = candidate.data["stage"]
        transitions = {
            "curator_wait": "curator_check",
            "formal_wait": "formal_check",
            "pretrial_wait": "pretrial_check",
            "deferral_wait": "curator_check",
        }
        if stage not in transitions:
            continue
        w = db.scalar(
            select(Work)
            .where(Work.id == candidate.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if not w or w.data["stage"] != stage:
            continue
        o, a = get_obligation(db, w.data["financial_obligation"])
        if balance(db, o) == 0:
            reason = "Проверьте подтверждённую оплату и закройте карточку явно."
        else:
            reason = {
                "curator_wait": "Вопрос через куратора не закрыт. Проверьте оплату задолженности.",
                "formal_wait": "Прошло 3 календарных дня после отправки по почте и ЭДО. Проверьте оплату.",
                "pretrial_wait": "Истёк срок досудебного уведомления. Проверьте результат перед передачей юристу.",
                "deferral_wait": "Истёк согласованный срок отсрочки. Проверьте остаток.",
            }[stage]
        w.data = history(w.data, None, "control_due", previous_stage=stage, balance_minor=balance(db, o))
        w.data = {**w.data, "stage": transitions[stage]}
        w.version += 1
        row = queue(
            db,
            db.get(Binding, w.binding_id),
            "finance-control:" + w.id + ":" + str(w.version),
            reason + "\nrequest_id: " + w.id,
            work=w,
        )
        row.payload = {**row.payload, "finance_control": {"work_id": w.id, "stage": w.data["stage"]}}
        return True
    return False


def before_transition(db, actor, w, action, data):
    if handoff_guard(db, actor, w, action, data):
        return
    if w.kind == "paid_work" and w.data.get("financial_protocol"):
        if action == "reschedule" and w.data.get("offer"):
            raise BusinessError(
                "Согласованный срок меняется отдельным согласованием; прогноз не переписывает предложение."
            )
        if action == "submit" and (not w.data.get("paid_started_at") or not data.get("result_description")):
            raise BusinessError("Нужен допустимый старт услуги и описание результата для ФП.")
        if action in {"accept", "reject"}:
            b = binding(db, actor, {"client"})
            if (
                actor.person_id != w.partner_person_id
                or b.id != w.client_binding_id
                or not active_person_role(db, actor.person_id, {"partner"}, w)
            ):
                raise AccessDenied("Результат платной услуги принимает назначенный ФП.")
            if action == "accept" and not w.data.get("paid_result_delivered_at"):
                raise BusinessError("Дождитесь подтверждённой доставки результата.")

    if (
        w.data.get("financial_obligation")
        or w.data.get("finance_agreement")
        or w.data.get("finance_batch")
        or w.data.get("finance_decision")
    ) and action in {"submit", "accept", "reject", "cancel", "reschedule", "handoff", "handoff_accept"}:
        raise BusinessError(
            "Используйте договорные действия: суммы, стадии и ответственность сохраняются в одном финансовом процессе."
        )


def deliverable(db, row):
    if row.payload.get("finance_paid_result"):
        ctx = row.payload["finance_paid_result"]
        w = db.get(Work, ctx["work_id"])
        return bool(w and w.status == "review" and w.version == ctx["version"])
    if row.payload.get("finance_paid_before"):
        ctx = row.payload["finance_paid_before"]
        w = db.get(Work, ctx["work_id"])
        return bool(
            w
            and w.status not in {"done", "review", "waiting_partner"}
            and w.data.get("paid_started_at")
            and paid_deadline(w).isoformat() + "Z" == ctx["deadline"]
        )
    if row.payload.get("finance_batch_control"):
        b = db.get(FinanceBatch, row.payload["finance_batch_control"])
        return bool(b and b.data["status"] != "done")

    if row.payload.get("finance_batch_topic"):
        batch = db.get(FinanceBatch, row.payload["finance_batch_topic"])
        b = db.get(Binding, row.binding_id)
        return bool(
            batch
            and batch.bot_id == settings().bot_id
            and b
            and b.active
            and b.verified
            and active_person_role(db, batch.data["owner_id"], {"contract"}, b)
        )

    topic = row.payload.get("finance_topic")
    if topic:
        w = db.get(Work, topic["work_id"])
        target = db.get(Binding, topic["target_id"])
        if not w or not target or not target.active or not target.verified:
            return False
        person = w.pending_owner_id if topic["pending"] else w.owner_id
        role = {"contracts": "contract", "legal": "lawyer", "leadership": "leadership"}.get(target.purpose)
        return bool(role and active_person_role(db, person, {role}, w))
    control = row.payload.get("finance_control")
    if control:
        w = db.get(Work, control["work_id"])
        return bool(w and w.status != "done" and w.data.get("stage") == control["stage"])
    return True


def replace_binding(db, old, new):
    for s in db.scalars(
        select(FinanceSchedule).where(FinanceSchedule.bot_id == settings().bot_id, FinanceSchedule.binding_id == old.id)
    ):
        s.enabled = False
        s.data = {**s.data, "error": "Маршрут заменён; переподтвердите календарь счетов."}
        s.version += 1
    for a in db.scalars(select(FinanceAgreement).where(FinanceAgreement.bot_id == settings().bot_id)):
        if a.data.get("communication_binding") == old.id:
            a.data = {**a.data, "communication_binding": new.id}
            a.version += 1


AGREEMENT_ACTIONS = {
    "package",
    "clarify",
    "reply_clarification",
    "invitation_sent",
    "invitation_accepted",
    "edo_sent",
    "signed_uk",
    "signed_partner",
    "activate",
    "suspend",
    "obligation",
    "receipt",
}


def same_bot_work(db, w):
    b = db.get(Binding, w.binding_id)
    return bool(b and b.bot_id == settings().bot_id)


def can_debt(db, actor, w):
    try:
        o, a = get_obligation(db, w.data["financial_obligation"])
        b = binding(db, actor, {"contracts", "support", "legal"})
        scope_check(b, a)
        if o.debt_work_id != w.id:
            return False
        if b.purpose == "contracts":
            return active_person_role(db, actor.person_id, {"contract"}, a)
        if b.purpose == "support":
            return actor.person_id == w.data.get("curator_id") and active_person_role(
                db, actor.person_id, {"curator"}, a
            )
        return actor.person_id in {w.owner_id, w.pending_owner_id} and active_person_role(
            db, actor.person_id, {"lawyer"}, a
        )
    except (BusinessError, KeyError):
        return False


def authorize_draft(db, actor, draft):
    a = draft.kind.split(":")[1]
    if a in {"request", "schedule"}:
        b = binding(db, actor, {"support", "sales"} if a == "request" else {"contracts"})
        role = {"support": "curator", "sales": "consultant", "contracts": "contract"}[b.purpose]
        if not active_person_role(db, actor.person_id, {role}, b):
            raise AccessDenied("Нет профильной роли.")
        return
    if a in AGREEMENT_ACTIONS:
        obj = get(db, draft.target_id)
        authorize(db, actor, obj, mutate=True)
    elif a in {"invoice", "allocate", "open_debt"}:
        obj, agreement = get_obligation(db, draft.target_id)
        if authorize(db, actor, agreement, mutate=True) != "contract":
            raise AccessDenied("Финансовый факт подтверждает договорной ответственный.")
    elif a == "batch_finish":
        obj = db.get(FinanceBatch, draft.target_id)
        authorize_batch(db, actor, obj)
        b = binding(db, actor, {"contracts"})
        if (
            not obj
            or obj.bot_id != settings().bot_id
            or obj.data["owner_id"] != actor.person_id
            or not active_person_role(db, actor.person_id, {"contract"}, b)
        ):
            raise AccessDenied("Чужой месячный пакет.")
    elif a == "schedule_stop":
        obj = db.get(FinanceSchedule, draft.target_id)
        b = binding(db, actor, {"contracts"})
        if (
            not obj
            or obj.bot_id != settings().bot_id
            or obj.owner_id != actor.person_id
            or not active_person_role(db, actor.person_id, {"contract"}, b)
        ):
            raise AccessDenied("Чужое расписание.")
    else:
        obj = db.get(Work, draft.target_id)
        if not obj or not same_bot_work(db, obj):
            raise AccessDenied("Другая карточка / бот.")
        if a.startswith("extra_"):
            b = binding(db, actor, {"client"})
            if (
                obj.kind != "paid_work"
                or obj.client_binding_id != b.id
                or obj.partner_person_id != actor.person_id
                or not active_person_role(db, actor.person_id, {"partner"}, obj)
            ):
                raise AccessDenied("Решение принимает назначенный ФП.")
        elif a == "decision_record":
            b = binding(db, actor, {"contracts", "legal", "leadership"})
            if (
                obj.owner_id != actor.person_id
                or obj.binding_id != actor.binding_id
                or not active_person_role(db, actor.person_id, {obj.data["decision_role"]}, obj)
            ):
                raise AccessDenied("Не назначен на решение.")
        elif not can_debt(db, actor, obj):
            raise AccessDenied("Нет назначения на задолженность.")
    versioned(obj, draft.target_version)


def execute_draft(db, actor, draft, data):
    a = draft.kind.split(":")[1]
    ident = draft.target_id
    expected = draft.target_version
    if a == "request":
        obj = request(db, actor, data, "ui:" + draft.id)
    elif a == "schedule":
        obj = create_schedule(db, actor, data)
    elif a in AGREEMENT_ACTIONS - {"obligation", "receipt"}:
        obj = agreement_action(db, actor, ident, expected, a, data)
    elif a == "obligation":
        obj = obligation(db, actor, ident, expected, data)
    elif a == "receipt":
        obj = receipt(db, actor, ident, data)
    elif a == "invoice":
        obj = invoice(db, actor, ident, expected, data)
    elif a == "allocate":
        obj = allocate(db, actor, ident, expected, data, "ui:" + draft.id)
    elif a == "open_debt":
        obj = open_debt(db, actor, ident, expected, data)
    elif a.startswith("debt_"):
        obj = debt_action(db, actor, ident, expected, a[5:], data)
    elif a == "decision_request":
        obj = decision_request(db, actor, ident, expected, data)
    elif a == "decision_record":
        obj = record_decision(db, actor, ident, expected, data)
    elif a == "deferral":
        obj = apply_deferral(db, actor, ident, expected, data)
    elif a == "batch_finish":
        obj = finish_batch(db, actor, ident, expected)
    elif a == "schedule_stop":
        obj = db.get(FinanceSchedule, ident)
        if not data.get("reason"):
            raise BusinessError("Нужно основание остановки.")
        obj.enabled = False
        obj.version += 1
        obj.data = history(obj.data, actor, "stopped", reason=data["reason"])
    elif a == "extra_accept":
        from ama.operations import operation

        obj = operation(db, actor, ident, expected, "offer_accept", data)
    elif a in {"extra_result_accept", "extra_result_reject"}:
        from ama.service import transition

        obj = transition(
            db, actor, ident, expected, "accept" if a.endswith("accept") else "reject", data, "ui:" + draft.id
        )
    elif a == "extra_claim":
        obj = extra_claim(db, actor, ident, expected, data)
    else:
        raise BusinessError("Неизвестная финансовая операция.")
    return {"id": obj.id, "version": getattr(obj, "version", None)}


def extra_operation(db, actor, w, expected, action, data):
    """Paid-service consent/claim is separate from verified franchise receipts."""
    versioned(w, expected)
    from ama.service import make_timer
    from ama.sla import refresh_deadline

    if action == "offer":
        b = binding(
            db,
            actor,
            {
                "projecting",
                "software",
                "personnel",
                "menu",
                "design",
                "advertising",
                "printing",
                "systems_training",
                "smm",
                "reviews",
                "support",
            },
        )
        if (
            w.kind != "paid_work"
            or actor.person_id != w.executor_id
            or not active_person_role(db, actor.person_id, {"specialist", "curator", "marketing_manager"}, w)
        ):
            raise AccessDenied("Предложение формирует назначенный исполнитель разрешённой услуги.")
        if w.data.get("paid_started_at"):
            raise BusinessError(
                "Изменение начатой оплаченной работы оформляется отдельным согласованием / договорным разбором; прежние условия сохраняются."
            )
        policy = db.get(Record, data.get("payment_policy"))
        if (
            not policy
            or policy.data.get("policy_type") != "payment"
            or b.purpose not in policy.data.get("services", [])
        ):
            raise BusinessError("Нужны утверждённые реквизиты для этой платной услуги.")
        recipient = payment_policy(db, policy.id, w)
        amount = money(data["amount_minor"])
        expires = parse_date(data["valid_until"])
        if (
            expires <= now()
            or not data.get("scope")
            or data.get("start_rule") not in {"partner_confirmation", "partner_payment_evidence"}
        ):
            raise BusinessError("Нужны актуальные объём, срок действия и согласованное правило старта.")
        mode = data.get("deadline_mode", "fixed")
        if mode == "fixed":
            if not data.get("deadline") or parse_date(data["deadline"]) <= now():
                raise BusinessError("Нужен будущий фиксированный срок результата.")
        elif mode == "duration":
            if (
                not isinstance(data.get("duration_minutes"), int)
                or isinstance(data.get("duration_minutes"), bool)
                or data["duration_minutes"] <= 0
                or data.get("duration_counter") not in {"calendar", "working"}
            ):
                raise BusinessError("Нужна согласованная длительность и вид времени.")
        else:
            raise BusinessError("Неизвестный способ срока.")
        old = w.data.get("offer", {})
        offer = {
            **deepcopy(data),
            "version": old.get("version", 0) + 1,
            "recipient": recipient,
            "deadline_mode": mode,
            "prepared_by": actor.person_id,
        }
        w.data = history(w.data, actor, "paid_offer", previous_offer=old)
        w.data = {**w.data, "financial_protocol": True, "offer": offer, "offer_accepted": False}
        w.status = "waiting_partner"
        # The execution result clock does not run before valid client consent.
        from ama.models import SLAEvent

        db.query(SLAEvent).filter(
            SLAEvent.work_id == w.id,
            SLAEvent.kind.in_(["result", "before_result", "result_day"]),
            SLAEvent.status.in_(["pending", "blocked"]),
        ).update({"status": "cancelled"}, synchronize_session=False)
        target = db.get(Binding, w.client_binding_id)
        queue(
            db,
            target,
            "paid-offer:" + w.id + ":" + str(offer["version"]),
            w.title
            + "\nОбъём: "
            + data["scope"]
            + "\nСтоимость: "
            + str(amount)
            + " коп.\nСрок / длительность: "
            + str(data.get("deadline") or data.get("duration_minutes"))
            + "\nПредложение действует до: "
            + data["valid_until"]
            + "\nРеквизиты утверждённого получателя: "
            + recipient["reference"]
            + "\n/finance — согласовать условия и передать подтверждение",
            work=w,
        )
    elif action == "offer_accept":
        b = binding(db, actor, {"client"})
        if (
            w.partner_person_id != actor.person_id
            or w.client_binding_id != b.id
            or not active_person_role(db, actor.person_id, {"partner"}, w)
        ):
            raise AccessDenied("Условия подтверждает назначенный ФП.")
        offer = w.data.get("offer")
        if not offer or data.get("offer_version") != offer["version"] or not offer.get("recipient"):
            raise Conflict("Нужно актуальное структурированное предложение с утверждёнными реквизитами.")
        if w.data.get("paid_started_at"):
            raise BusinessError("Работа уже начата; повторное согласие не создаёт второй старт.")
        claim = {
            "amount_minor": data.get("amount_minor"),
            "evidence": data.get("evidence"),
            "actor": actor.person_id,
            "at": now().isoformat() + "Z",
            "offer_version": offer["version"],
            "kind": "client_claim_not_bank_receipt",
        }
        invalid = (
            parse_date(offer["valid_until"]) < now()
            or offer["deadline_mode"] == "fixed"
            and parse_date(offer["deadline"]) <= now()
        )
        if offer["start_rule"] == "partner_payment_evidence" and (
            data.get("amount_minor") != offer["amount_minor"] or not data.get("evidence")
        ):
            invalid = True
        w.data = history(w.data, actor, "client_paid_claim", claim=claim)
        w.data = {**w.data, "last_payment_claim": claim}
        if invalid:
            w.status = "waiting_uk"
            w.owner_id = w.executor_id
            w.data = {
                **w.data,
                "payment_clarification": "Подтверждение клиента получено — требуется уточнение условий. Старт не разрешён.",
            }
            queue(
                db,
                db.get(Binding, w.binding_id),
                "paid-clarify:" + w.id + ":" + str(w.version),
                w.data["payment_clarification"],
                work=w,
            )
        else:
            if offer["deadline_mode"] == "fixed":
                due = parse_date(offer["deadline"])
            elif offer["duration_counter"] == "calendar":
                due = now() + timedelta(minutes=offer["duration_minutes"])
            else:
                from ama.calendar import Calendar

                cal = db.scalar(
                    select(Record).where(
                        Record.kind == "calendar",
                        Record.key == w.data.get("calendar", "support"),
                        Record.published.is_(True),
                    )
                )
                if not cal:
                    raise BusinessError("Рабочий календарь срока не опубликован.")
                due = Calendar(cal.data).add_minutes(now(), offer["duration_minutes"])
            w.data = {
                **w.data,
                "offer_accepted": True,
                "paid_started_at": now().isoformat() + "Z",
                "paid_original_deadline": due.isoformat() + "Z",
                "paid_pauses": [],
                "payment_clarification": None,
                "technical_reviewer_id": w.reviewer_id,
            }
            w.owner_id = w.executor_id
            w.reviewer_id = w.partner_person_id
            w.status = "in_progress"
            w.due_at = due
            refresh_deadline(db, w, "paid_start")
    w.version += 1
    make_timer(db, w, "deadline", w.due_at)
    stamp(
        db, actor, w, "PAID_" + action.upper(), expected, data, "partner" if action == "offer_accept" else "specialist"
    )
    return w


def paid_deadline(w):
    due = parse_date(w.data["paid_original_deadline"])
    for pause in w.data.get("paid_pauses", []):
        if pause.get("end"):
            due += parse_date(pause["end"]) - parse_date(pause["start"])
    return due


def extra_claim(db, actor, ident, expected, data):
    w = db.scalar(select(Work).where(Work.id == ident).with_for_update().execution_options(populate_existing=True))
    versioned(w, expected)
    b = binding(db, actor, {"client"})
    if (
        w.kind != "paid_work"
        or w.client_binding_id != b.id
        or w.partner_person_id != actor.person_id
        or not active_person_role(db, actor.person_id, {"partner"}, w)
    ):
        raise AccessDenied("Решение заявляет назначенный ФП.")
    if not data.get("reason") or data.get("kind") not in {"decline", "dispute", "cancel"}:
        raise BusinessError("Нужны явное действие, причина и фактически выполненная часть.")
    w.data = history(w.data, actor, "paid_claim", details=data, previous_status=w.status)
    if not w.data.get("paid_started_at") and data["kind"] == "decline" and not w.data.get("last_payment_claim"):
        w.status = "done"
        w.outcome = "not_agreed"
    else:
        previous = w.data.get("financial_review_work")
        review = db.get(Work, previous) if previous else None
        if not review or review.status == "done":
            r, target = route(db, "contracts", w, {"contract"})
            due = now() + timedelta(days=1)
            review = Work(
                kind="finance_extra_review",
                title=("Разбор платной услуги · " + w.title)[:250],
                partner_id=w.partner_id,
                brand_id=w.brand_id,
                establishment_id=w.establishment_id,
                binding_id=target.id,
                parent_id=w.id,
                owner_id=r.data["owner_id"],
                executor_id=r.data["owner_id"],
                reviewer_id=r.data["owner_id"],
                due_at=due,
                original_due_at=due,
                data={
                    "calendar": r.data.get("calendar", "support"),
                    "offer": deepcopy(w.data.get("offer")),
                    "payment_claim": deepcopy(w.data.get("last_payment_claim")),
                    "claim": deepcopy(data),
                    "no_automatic_refund": True,
                    "finance_decision": True,
                    "decision_role": "contract",
                },
            )
            db.add(review)
            db.flush()
            prepare_topic(db, review, target, "finance-topic:" + review.id)
            w.data = {**w.data, "financial_review_work": review.id}
        if w.status != "done":
            w.status = "waiting_uk"
    w.version += 1
    return w


def after_transition(db, actor, w, action, data):
    if w.kind != "paid_work" or not w.data.get("financial_protocol") or not w.data.get("paid_started_at"):
        return
    if action == "wait_partner":
        if not data.get("reason"):
            raise BusinessError("Пауза оплаченной работы требует объективной причины невозможности продолжения.")
        pauses = [
            *w.data.get("paid_pauses", []),
            {
                "start": now().isoformat() + "Z",
                "reason": data["reason"],
                "required": data.get("required"),
                "actor": actor.person_id,
            },
        ]
        w.data = {**w.data, "paid_pauses": pauses}
    elif action == "resume" and w.data.get("paid_pauses"):
        pauses = deepcopy(w.data["paid_pauses"])
        if not pauses[-1].get("end"):
            pauses[-1]["end"] = now().isoformat() + "Z"
        w.data = {**w.data, "paid_pauses": pauses}
        w.due_at = paid_deadline(w)
        from ama.sla import refresh_deadline

        refresh_deadline(db, w, "paid_resume_after_objective_pause")
    elif action == "submit":
        w.data = {k: v for k, v in w.data.items() if k != "paid_result_delivered_at"}
        row = queue(
            db,
            db.get(Binding, w.client_binding_id),
            "paid-result:" + w.id + ":" + str(w.version),
            "Результат платной услуги готов. "
            + w.title
            + "\n"
            + data["result_description"][:2500]
            + "\n/finance — полный результат и подтверждение / замечания.",
            work=w,
        )
        row.payload = {**row.payload, "finance_paid_result": {"work_id": w.id, "version": w.version + 1}}


def extra_tick(db):
    for w in db.scalars(select(Work).where(Work.kind == "paid_work", Work.status != "done")):
        if (
            not w.data.get("financial_protocol")
            or not w.data.get("paid_started_at")
            or w.status in {"review", "waiting_partner"}
        ):
            continue
        due = paid_deadline(w)
        if now() < due - timedelta(hours=2):
            continue
        marker = due.isoformat() + "Z"
        if w.data.get("paid_before_due") != marker:
            row = queue(
                db,
                db.get(Binding, w.binding_id),
                "paid-before:" + w.id + ":" + marker,
                "Контроль срока платной услуги: "
                + w.title
                + "\nСогласованный срок с учётом объективных пауз UTC: "
                + marker,
                work=w,
            )
            row.payload = {**row.payload, "finance_paid_before": {"work_id": w.id, "deadline": marker}}
            w.data = {**w.data, "paid_before_due": marker}
            return True
        if now() > due:
            from ama.sla import case_for, signal

            c = case_for(db, w)
            if c and not w.data.get("paid_breach_signal"):
                s = signal(
                    db,
                    c,
                    w,
                    "paid-work:" + w.id + ":" + marker,
                    "Просрочен результат согласованной платной услуги; заказ не пересоздаётся.",
                )
                w.data = {**w.data, "paid_breach_signal": s.id}
                return True
    for batch in db.scalars(select(FinanceBatch).where(FinanceBatch.bot_id == settings().bot_id)):
        if (
            batch.data["status"] == "done"
            or batch.data.get("control_notified")
            or parse_date(batch.data["due_at"]) > now()
        ):
            continue
        b = db.get(Binding, batch.data.get("topic_binding_id") or batch.binding_id)
        row = queue(
            db,
            b,
            "finance-batch-control:" + batch.id,
            "Проверьте подготовку и отправку счетов за "
            + batch.period
            + ". Договорные сроки оплаты сохраняются независимо от счёта.",
        )
        row.payload = {**row.payload, "finance_batch_control": batch.id}
        batch.data = {**batch.data, "control_notified": True}
        return True
    return False


def authorize_batch(db, actor, batch):
    b = binding(db, actor, {"contracts"})
    source = db.get(Binding, batch.binding_id) if batch else None
    if (
        not batch
        or batch.bot_id != settings().bot_id
        or batch.data["owner_id"] != actor.person_id
        or not source
        or any(getattr(source, k) != getattr(b, k) for k in ("partner_id", "brand_id", "establishment_id"))
        or not active_person_role(db, actor.person_id, {"contract"}, b)
    ):
        raise AccessDenied("Месячный пакет относится к другой области / ответственному.")
    return batch


def batch_visible(db, actor, batch):
    try:
        authorize_batch(db, actor, batch)
        return True
    except AccessDenied:
        return False


def handoff_guard(db, actor, w, action, data):
    if action not in {"handoff", "handoff_accept"} or w.kind not in {
        "finance_contract",
        "finance_debt",
        "finance_decision",
        "finance_extra_review",
    }:
        return False
    if w.kind == "finance_debt":
        role = "lawyer" if w.data.get("stage") == "legal" else "contract"
    else:
        role = w.data.get("decision_role", "contract")
    purpose = {"contract": "contracts", "lawyer": "legal", "leadership": "leadership"}[role]
    person = data.get("owner_id") if action == "handoff" else w.pending_owner_id
    target = db.get(Binding, data.get("binding_id")) if action == "handoff" else db.get(Binding, w.pending_binding_id)
    if (
        not target
        or target.bot_id != settings().bot_id
        or target.purpose != purpose
        or not target.active
        or not target.verified
        or not active_person_role(db, person, {role}, w)
    ):
        raise BusinessError(
            "Ответственность передаётся только назначенному сотруднику того же направления; юридическая передача использует отдельную цепочку."
        )
    scope_check(target, w)
    return True

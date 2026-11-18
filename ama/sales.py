from sqlalchemy import select, func
from ama.auth import require
from ama.errors import BusinessError, AccessDenied
from ama.models import Lead, Interest, Deal, Payment, Binding, Entity, now
from ama.service import stamp, versioned, record, parse_date

STAGES = ["new", "qualification", "presentation", "financial_review", "contract"]


def sales_role(db, actor, names):
    binding = db.get(Binding, actor.binding_id)
    allowed = {"consultant": {"sales"}, "contract": {"contracts"}, "curator": {"support"},
               "support_head": {"support", "management"}, "chief_marketer": {"chief_marketer"}}
    role = require(db, actor, names)
    if binding.purpose not in allowed.get(role, set()):
        raise AccessDenied("Действие доступно только в профильном рабочем чате.")
    return role


def authorize_scope(db, actor, obj, names):
    from types import SimpleNamespace
    if isinstance(obj, Lead):
        brand, partner = obj.data.get("brand_id"), None
    else:
        brand, partner = obj.data.get("brand_id"), obj.data.get("partner_id")
    area = SimpleNamespace(binding_id=actor.binding_id, client_binding_id=None,
                           partner_id=partner, brand_id=brand, establishment_id=None)
    result = require(db, actor, names, area)
    if isinstance(obj, Deal) and obj.data.get("case_ids"):
        from ama.models import SalesCase
        for case_id in obj.data["case_ids"]:
            case = db.get(SalesCase,case_id)
            if not case:
                raise BusinessError("Интерес договорного комплекта отсутствует.")
            require(db,actor,names,SimpleNamespace(binding_id=actor.binding_id,client_binding_id=None,
                partner_id=partner,brand_id=case.brand_id,establishment_id=None))
    return result


def lead_entry(db, person_id, source, consent_version, brand_id=None):
    # Вызывается только после явного согласия, текст и версия которого опубликованы владельцем.
    record(db, "consent", consent_version)
    lead = db.scalar(select(Lead).where(Lead.person_id == person_id).with_for_update())
    if not lead:
        lead = Lead(person_id=person_id, source=source, consent_at=now(), consent_version=consent_version,
                    last_reaction_at=now(), data={})
        db.add(lead)
        db.flush()
        stamp(db, None, lead, "LEAD_CREATED")
    else:
        lead.last_reaction_at = now()
        lead.version += 1
    if brand_id:
        brand = db.get(Entity, brand_id)
        if not brand or brand.kind != "brand" or not brand.data.get("sales_available"):
            raise BusinessError("Продажи бренда не активированы.")
        if not db.scalar(select(Interest).where(Interest.lead_id == lead.id, Interest.brand_id == brand_id)):
            db.add(Interest(lead_id=lead.id, brand_id=brand_id))
    # no_contact не снимается при повторном входе.
    return lead


def update_lead(db, actor, lead_id, expected, action, data):
    from ama.models import SalesCase
    existing = db.get(SalesCase,lead_id)
    if existing:
        from ama.sale_flow import action as case_action
        case_action(db,actor,lead_id,expected,action,data,"legacy:"+__import__('ama.models',fromlist=['uid']).uid())
        return existing
    role = sales_role(db, actor, {"consultant"})
    lead = db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if not lead:
        raise BusinessError("Лид не найден.")
    authorize_scope(db, actor, lead, {"consultant"})
    versioned(lead, expected)
    if lead.owner_id and lead.owner_id != actor.person_id:
        raise AccessDenied("Лид назначен другому консультанту.")
    old = lead.version
    if action == "take":
        lead.owner_id = actor.person_id
    elif not lead.owner_id:
        raise BusinessError("Сначала возьмите лида в работу.")
    elif action == "qualify":
        allowed = {"name", "contact", "city", "brand_id", "budget", "start_period", "experience", "premises",
                   "commercial_key", "commercial_version", "intent_confirmed", "requisites", "request_requisites", "exceptions"}
        changes = {k: v for k, v in data.items() if k in allowed}
        if isinstance(lead.data.get("contact"), dict) and "contact" in changes:
            changes["contact_description"] = changes.pop("contact")
        lead.data = {**lead.data, **changes}
        authorize_scope(db, actor, lead, {"consultant"})
        lead.stage = "qualification"
    elif action == "stage":
        if data.get("stage") not in STAGES:
            raise BusinessError("Неизвестный этап продажи.")
        if data["stage"] == "contract":
            raise BusinessError("Этап договора устанавливается подтверждённой передачей.")
        lead.stage = data["stage"]
    elif action in {"wait_lead", "not_now", "no_answer"}:
        if not data.get("next_contact"):
            raise BusinessError("Необходима дата следующего контакта.")
        next_contact = parse_date(data["next_contact"])
        if next_contact <= now():
            raise BusinessError("Контакт должен быть назначен в будущем.")
        lead.state = action
        lead.data = {**lead.data, "next_contact": data["next_contact"], "next_action": data.get("next_action", "contact")}
    elif action == "refuse":
        if not data.get("reason"):
            raise BusinessError("Укажите причину отказа.")
        lead.result, lead.no_contact = "refused", True
        lead.data = {**lead.data, "refusal_reason": data["reason"]}
    elif action == "reopen":
        if not data.get("consent_evidence"):
            raise BusinessError("Необходимо подтверждение согласия на возобновление контакта.")
        lead.no_contact, lead.result, lead.state = False, None, "active"
    else:
        raise BusinessError("Неизвестное действие продаж.")
    lead.version += 1
    stamp(db, actor, lead, f"SALES_{action.upper()}", old, data, role)
    return lead


def start_contract(db, actor, lead_id, expected, data):
    from ama.models import SalesCase
    if db.get(SalesCase,lead_id):
        if not data.get("confirm"):
            raise BusinessError("Требуется подтверждение передачи после предпросмотра.")
        from ama.sale_flow import action as case_action
        case_action(db,actor,lead_id,expected,"contract",data,"legacy-contract:"+__import__('ama.models',fromlist=['uid']).uid())
        return db.scalar(select(Deal).where(Deal.case_id==lead_id))
    role = sales_role(db, actor, {"consultant"})
    lead = db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if not lead:
        raise BusinessError("Лид не найден.")
    authorize_scope(db, actor, lead, {"consultant"})
    versioned(lead, expected)
    if lead.owner_id != actor.person_id:
        raise AccessDenied("Передачу выполняет консультант лида.")
    d = lead.data
    missing = [k for k in ("name", "contact", "city", "brand_id", "commercial_key", "intent_confirmed") if not d.get(k)]
    if missing or not (d.get("requisites") or d.get("request_requisites")):
        raise BusinessError("Недостаточно данных для договора: " + ", ".join(missing + (["реквизиты или запрос реквизитов"] if not (d.get("requisites") or d.get("request_requisites")) else [])))
    commercial = record(db, "commercial", d["commercial_key"])
    if d.get("commercial_version") != commercial.version or commercial.data.get("brand_id") != d["brand_id"]:
        raise BusinessError("Нужно подтвердить ознакомление с действующей коммерческой редакцией этого бренда.")
    if not data.get("confirm"):
        raise BusinessError("Требуется явное подтверждение передачи после предпросмотра.")
    existing = db.scalar(select(Deal).where(Deal.lead_id == lead.id))
    if existing:
        return existing
    amount = commercial.data.get("required_minor")
    if not isinstance(amount, int) or amount < 0:
        raise BusinessError("Не утверждена сумма/условие расчёта по сделке.")
    deal = Deal(lead_id=lead.id, required_minor=amount, data={"commercial_id": commercial.id,
                "commercial_version": commercial.version, "brand_id": d["brand_id"]})
    db.add(deal)
    db.flush()
    lead.stage, lead.version = "contract", lead.version + 1
    stamp(db, actor, deal, "CONTRACT_STARTED", data={"lead_id": lead.id}, role=role)
    return deal


def contract_action(db, actor, deal_id, expected, action, data, event_key):
    role = sales_role(db, actor, {"contract"} if action not in {"curator_accept", "curator_clarify"} else {"curator", "support_head"})
    deal = db.scalar(select(Deal).where(Deal.id == deal_id).with_for_update())
    if not deal:
        raise BusinessError("Сделка не найдена.")
    if deal.data.get("binding_id"):
        destination=db.get(Binding,deal.data["binding_id"])
        current=db.get(Binding,actor.binding_id)
        if not destination or destination.chat_id!=current.chat_id or destination.platform!=current.platform:
            raise AccessDenied("Откройте договор в его рабочем чате.")
    authorize_scope(db, actor, deal, {"contract"} if action not in {"curator_accept", "curator_clarify"} else {"curator", "support_head"})
    versioned(deal, expected)
    old = deal.version
    if action == "package":
        if deal.successful_at:
            raise BusinessError("Новая редакция после успеха оформляется отдельным изменением.")
        if not data.get("document_reference"):
            raise BusinessError("Укажите ссылку на редакцию комплекта документов.")
        deal.package_version += 1
        deal.data = {**deal.data, "document_reference": data["document_reference"]}
    elif action == "sent":
        if not deal.data.get("document_reference") or not data.get("evidence"):
            raise BusinessError("Нужны текущий обязательный комплект и подтверждение его отправки.")
        deal.data = {**deal.data, "sent_package_version": deal.package_version, "sent_evidence": data["evidence"]}
        if deal.case_id:
            from ama.sale_flow import get, event
            case = get(db, deal.case_id)
            case.stage = "signing"
            case.version += 1
            event(db, case, "PACKAGE_SENT", {"package_version": deal.package_version}, actor)
    elif action == "signed":
        if not data.get("evidence"):
            raise BusinessError("Нужно подтверждение подписей актуального комплекта.")
        if deal.case_id and not deal.data.get("document_reference"):
            raise BusinessError("Не указан актуальный обязательный комплект документов.")
        deal.signed_package_version = deal.package_version
        if deal.case_id:
            from ama.sale_flow import get, event
            case = get(db, deal.case_id)
            case.stage = "payment_wait"
            case.version += 1
            event(db, case, "PACKAGE_SIGNED", {"package_version": deal.package_version}, actor)
        deal.data = {**deal.data, "signature_evidence": data["evidence"]}
    elif action == "payment":
        if not isinstance(data.get("amount_minor"), int) or data["amount_minor"] <= 0 or not data.get("bank_reference"):
            raise BusinessError("Нужны сумма в копейках и банковское подтверждение.")
        source = data["bank_reference"]
        if db.scalar(select(Payment).where(Payment.source_key == source)):
            raise BusinessError("Банковская операция уже учтена.")
        db.add(Payment(deal_id=deal.id, source_key=source, amount_minor=data["amount_minor"], confirmed_by=actor.person_id))
        db.flush()
    elif action == "confirm":
        paid = db.scalar(select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(Payment.deal_id == deal.id))
        if deal.signed_package_version != deal.package_version or paid < deal.required_minor:
            raise BusinessError("Не подтверждены подписи текущей редакции или условия расчётов.")
        deal.successful_at = deal.successful_at or now()
        lead = db.get(Lead, deal.lead_id)
        if deal.case_id:
            from ama.models import SalesCase
            from ama.sale_flow import sync, event
            case = db.get(SalesCase, deal.case_id)
            first_success = not case.result == "success"
            for case_id in deal.data.get("case_ids", [deal.case_id]):
                item = db.get(SalesCase, case_id)
                item.result, item.state, item.stage = "success", "closed", "won"
                item.version += 1
                sync(db, item)
            if first_success:
                event(db, case, "DEAL_SUCCESS", {"deal_id": deal.id}, actor)
        else:
            lead.result = "success"
    elif action == "assign_curator":
        from ama.auth import active_person_role
        from types import SimpleNamespace
        scope = SimpleNamespace(partner_id=data.get("partner_id"), brand_id=deal.data.get("brand_id"), establishment_id=None)
        if not deal.successful_at or not active_person_role(db, data.get("curator_id"), {"curator"}, scope):
            raise BusinessError("Нужны успешная сделка и действующий куратор в выбранной области.")
        partner = db.get(Entity, data.get("partner_id"))
        if not partner or partner.kind != "partner":
            raise BusinessError("Укажите действующего ФП для передачи.")
        if deal.handoff_accepted_at:
            raise BusinessError("Передача уже принята. Смена куратора выполняется отдельным назначением сопровождения.")
        deal.curator_id = data["curator_id"]
        deal.data = {**deal.data, "partner_id": data.get("partner_id"), "handoff_state": "pending"}
        if deal.case_id:
            from ama.sale_flow import route
            from ama.service import queue
            _, destination = route(db, "support")
            queue(db, destination, f"sales-handoff:{deal.id}:{old}", "Успешная сделка ожидает принятия куратором.\nСделка: "+deal.id+"\nКуратор: "+deal.curator_id,
                  buttons=[["Открыть сделки", "af:deal"]])
    elif action == "curator_clarify":
        if not deal.successful_at or deal.handoff_accepted_at or actor.person_id != deal.curator_id or not data.get("question"):
            raise AccessDenied("Уточнение запрашивает назначенный куратор по успешной сделке.")
        deal.data = {**deal.data, "handoff_state": "clarification", "handoff_question": data["question"]}
        if deal.case_id:
            from ama.sale_flow import get
            from ama.service import queue
            case = get(db, deal.case_id)
            if case.binding_id:
                queue(db, db.get(Binding, case.binding_id), f"handoff-question:{deal.id}:{old}", "Куратор запрашивает уточнение: "+data["question"])
    elif action == "curator_accept":
        if not deal.successful_at or actor.person_id != deal.curator_id:
            raise AccessDenied("Передачу принимает назначенный куратор.")
        first_acceptance = deal.handoff_accepted_at is None
        deal.handoff_accepted_at = deal.handoff_accepted_at or now()
        deal.data = {**deal.data, "handoff_state": "accepted"}
        if deal.case_id and first_acceptance:
            from ama.sale_flow import get, event
            event(db, get(db, deal.case_id), "CURATOR_ACCEPTED", {"deal_id": deal.id}, actor)
    else:
        raise BusinessError("Неизвестное договорное действие.")
    deal.version += 1
    stamp(db, actor, deal, f"DEAL_{action.upper()}", old, {"event_key": event_key}, role)
    return deal


def cohort_report(db, start, end):
    cohort = list(db.scalars(select(Lead).where(Lead.created_at >= start, Lead.created_at < end)))
    ids = [lead.id for lead in cohort]
    won_cohort = db.scalar(select(func.count(func.distinct(Deal.lead_id))).select_from(Deal).where(Deal.lead_id.in_(ids), Deal.successful_at.is_not(None)))
    period_wins = db.scalar(select(func.count()).select_from(Deal).where(Deal.successful_at >= start, Deal.successful_at < end))
    return {"cohort_leads": len(ids), "cohort_won": won_cohort, "period_wins": period_wins,
            "cohort_conversion": won_cohort / len(ids) if ids else 0}

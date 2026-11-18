"""Public, progressively disclosed brand consultation. No unapproved generated claims."""

from copy import deepcopy
from datetime import timedelta
import re
from sqlalchemy import select
from ama.config import settings
from ama.models import (
    Identity,
    Account,
    ProspectSession,
    LandingTicket,
    Lead,
    SalesCase,
    SalesEvent,
    Record,
    Entity,
    Binding,
    Deal,
    Outbox,
    now,
)
from ama.errors import BusinessError, AccessDenied, Conflict
from ama.service import versioned, queue, parse_date
from ama import sales, sale_flow, reminders

CATEGORIES = {
    "about": "О проекте",
    "advantages": "Почему гости выбирают проект",
    "formats": "Форматы предприятия",
    "finance": "Инвестиции и финансовая модель",
    "benefits": "Что получает партнёр",
    "launch": "Как проходит запуск",
    "menu": "Меню и продукт",
    "marketing": "Маркетинг и продвижение",
    "systems": "Автоматизация и системы",
    "support": "Поддержка после открытия",
    "premises": "Требования к помещению",
    "media": "Фото и видео",
    "faq": "Частые вопросы",
}


def approved(record):
    from datetime import date

    d = record.data
    try:
        today = (now() + timedelta(hours=3)).date()
        approved_on = date.fromisoformat(d["approved_on"])
        expires = date.fromisoformat(d["valid_until"]) if d.get("valid_until") else None
    except (ValueError, KeyError, TypeError):
        return False
    return bool(
        record.published
        and record.owner_id
        and d.get("approval")
        and approved_on <= today
        and (not expires or expires >= today)
    )


def brand(db, brand_id):
    e = db.get(Entity, brand_id) if brand_id else None
    return e if e and e.kind == "brand" and e.data.get("sales_available") and not e.data.get("archived") else None


def knowledge(db, brand_id, category=None, search="", public=True):
    if not brand(db, brand_id):
        return []
    rows = []
    for r in db.scalars(select(Record).where(Record.kind == "policy").order_by(Record.key)):
        d = r.data
        if d.get("policy_type") not in {"sales_content", "faq"} or d.get("brand_id") != brand_id or not approved(r):
            continue
        if public and not d.get("public", True):
            continue
        if category and ("faq" if d.get("policy_type") == "faq" else d.get("category")) != category:
            continue
        if (
            search
            and search.casefold()
            not in " ".join(str(d.get(k, "")) for k in ("title", "question", "answer", "text")).casefold()
        ):
            continue
        rows.append(r)
    return rows


def content(r):
    d = r.data
    text = d.get("title", "") + "\n" + (d.get("answer") if d.get("policy_type") == "faq" else d.get("text", ""))
    text += f"\nРедакция {r.version} · утверждено {d['approved_on']}"
    for material in d.get("links", [])[:6]:
        text += "\n" + material["title"] + "\n" + material["url"]
    return text


def source(db, key):
    candidates = [
        r
        for r in db.scalars(select(Record).where(Record.kind == "policy", Record.published.is_(True)))
        if r.data.get("policy_type") == "source" and key in {r.key, r.data.get("source_id")}
    ]
    return candidates[0] if len(candidates) == 1 else None


def landing_ticket(db, source_key, platform, tags):
    if platform not in {"telegram", "max"}:
        raise BusinessError("Неизвестный мессенджер.")
    r = source(db, source_key)
    known = bool(r and r.data.get("policy_type") == "source")
    permitted = set(r.data.get("tags", [])) if known else set()
    clean = {k: str(v)[:150] for k, v in tags.items() if k in permitted}
    count = db.scalar(
        select(__import__("sqlalchemy").func.count())
        .select_from(LandingTicket)
        .where(LandingTicket.created_at > now() - timedelta(minutes=1))
    )
    if count >= settings().landing_requests_per_minute:
        raise BusinessError("Слишком много переходов. Повторите позже.")
    ticket = LandingTicket(
        source_key=source_key[:100] if known else "unknown",
        platform=platform,
        data={"tags": clean, "brand_id": r.data.get("brand_id") if known else None},
        expires_at=now() + timedelta(hours=24),
    )
    db.add(ticket)
    db.flush()
    handle = (settings().telegram_bot_username if platform == "telegram" else settings().max_bot_username) or (
        r.data.get(platform + "_bot_username", "") if known else ""
    )
    if not handle:
        configured = {
            row.data.get(platform + "_bot_username")
            for row in db.scalars(select(Record).where(Record.kind == "policy", Record.published.is_(True)))
            if row.data.get("policy_type") == "source" and row.data.get(platform + "_bot_username")
        }
        if len(configured) == 1:
            handle = next(iter(configured))
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", handle or ""):
        raise BusinessError("Не задано публичное имя бота для ссылки лендинга.")
    token = "l_" + ticket.id
    return (
        ("https://t.me/" + handle + "?start=" + token)
        if platform == "telegram"
        else ("https://max.ru/" + handle + "?start=" + token)
    )


def session(db, event):
    account = db.scalar(
        select(Account)
        .where(Account.platform == event.platform, Account.external_id == event.user_id)
        .with_for_update()
    )
    if account and not account.active:
        raise AccessDenied("Аккаунт отключён. Обратитесь к куратору.")
    if not account:
        p = Identity(name="Посетитель")
        db.add(p)
        db.flush()
        account = Account(person_id=p.id, platform=event.platform, external_id=event.user_id)
        db.add(account)
        db.flush()
    s = db.scalar(
        select(ProspectSession)
        .where(
            ProspectSession.bot_id == settings().bot_id,
            ProspectSession.person_id == account.person_id,
            ProspectSession.platform == event.platform,
            ProspectSession.chat_id == event.chat_id,
        )
        .with_for_update()
    )
    if not s:
        lead = db.scalar(select(Lead).where(Lead.person_id == account.person_id))
        existing = None
        if lead:
            existing = (
                db.get(SalesCase, lead.data.get("reminder_case_id")) if lead.data.get("reminder_case_id") else None
            )
            existing = existing or sale_flow.primary(db, lead)
        initial = {"first_seen_at": lead.data.get("first_seen_at", now().isoformat()) if lead else now().isoformat()}
        if existing:
            keys = {
                "name",
                "city",
                "phone",
                "communication",
                "preferred_time",
                "business",
                "premises",
                "start_period",
                "experience",
            }
            initial.update(
                {
                    "profile": {k: v for k, v in existing.data.items() if k in keys},
                    "brand_id": existing.brand_id,
                    "source": lead.source,
                    "seen": existing.data.get("seen", []),
                }
            )
        s = ProspectSession(
            bot_id=settings().bot_id,
            person_id=account.person_id,
            platform=event.platform,
            chat_id=event.chat_id,
            data=initial,
            case_id=existing.id if existing else None,
        )
        db.add(s)
        db.flush()
    return s


def choice(label, action, **params):
    return {"label": label, "action": action, "params": params}


def screen(s, text, options, prompt=None):
    s.version += 1
    s.updated_at = now()
    s.data = {**s.data, "choices": options, "prompt": prompt}
    return text, [[o["label"], f"af:p:{s.id}:{s.version}:{i}"] for i, o in enumerate(options)]


def common():
    return [
        choice("В меню проекта", "home"),
        choice("Задать вопрос", "question"),
        choice("Перезвоните мне", "request", mode="call"),
    ]


def home(db, s):
    b = brand(db, s.data.get("brand_id"))
    if not b:
        return screen(
            s,
            "Добро пожаловать в АМА Франчайзинг. Выберите проект или обсудите выбор с консультантом.",
            [
                choice("Посмотреть проекты", "brands"),
                choice("Помогите выбрать проект", "request", mode="chat"),
                choice("Задать вопрос консультанту", "question"),
                choice("Перезвоните мне", "request", mode="call"),
            ],
        )
    return screen(
        s,
        f"{b.name}\nПомогу познакомиться с проектом, посмотреть утверждённые материалы или связаться с консультантом.",
        [
            choice("Познакомиться с проектом", "categories"),
            choice("Посмотреть цифры", "category", category="finance"),
            choice("Фото и видео", "category", category="media"),
            choice("Задать вопрос", "question"),
            choice("Консультант в чате", "request", mode="chat"),
            choice("Перезвоните мне", "request", mode="call"),
            choice("Другие проекты", "brands"),
        ],
    )


def activity(db, s, kind, extra=None):
    lead = db.scalar(select(Lead).where(Lead.person_id == s.person_id).with_for_update())
    if lead:
        reminders.react(db, lead, {"platform": s.platform, "chat_id": s.chat_id})
        db.add(
            SalesEvent(
                event_key=__import__("ama.models", fromlist=["uid"]).uid(),
                lead_id=lead.id,
                case_id=s.case_id,
                kind=kind,
                data=deepcopy(extra or {}),
            )
        )
    return lead


def handoff_step(db, s):
    profile = s.data.get("profile", {})
    missing = [k for k in ("name", "city") if not profile.get(k)]
    if profile.get("communication") == "call":
        missing += [k for k in ("phone", "preferred_time") if not profile.get(k)]
    if missing:
        key = missing[0]
        labels = {
            "name": "Как к Вам обращаться?",
            "city": "В каком городе или регионе планируете проект?",
            "phone": "Укажите номер телефона для звонка, включая код страны.",
            "preferred_time": "Когда удобно созвониться? Укажите дату/время или удобный интервал.",
        }
        if key == "preferred_time":
            lead = db.scalar(select(Lead).where(Lead.person_id == s.person_id))
            if lead:
                s.data = {
                    **s.data,
                    "_delivery": {
                        "lead_id": lead.id,
                        "case_id": s.case_id,
                        "touch_reason": "call_time",
                        "reply_to_inbound": True,
                        "silence_epoch": lead.data.get("silence", {}).get("epoch"),
                    },
                }
        return screen(s, labels[key], common(), {"field": key, "expires": (now() + timedelta(minutes=30)).isoformat()})
    consent = db.scalar(select(Record).where(Record.kind == "consent", Record.published.is_(True)).order_by(Record.key))
    if not consent or not consent.owner_id or not consent.data.get("text") or not consent.data.get("privacy_url"):
        return screen(
            s,
            "Передача консультанту пока недоступна: форма согласия не опубликована. Можно продолжить знакомство.",
            common(),
        )
    b = brand(db, s.data.get("brand_id"))
    text = f"Проверим перед передачей:\nИмя: {profile['name']}\nГород: {profile['city']}\nПроект: {b.name if b else 'Помочь выбрать'}\nСвязь: {'звонок' if profile.get('communication') == 'call' else 'чат'}"
    if profile.get("phone"):
        text += f"\nТелефон: {profile['phone']}\nУдобное время: {profile.get('preferred_time', '')}"
    return screen(
        s,
        text + "\n\n" + consent.data["text"] + "\n" + consent.data["privacy_url"],
        [
            choice("Согласен, передать консультанту", "consent", key=consent.key, version=consent.version),
            choice("Изменить данные", "profile_edit"),
            choice("Пока продолжу знакомство", "home"),
        ],
    )


def dispatch(db, s, action, p):
    if action == "home":
        return home(db, s)
    if action == "brands":
        rows = [
            e for e in db.scalars(select(Entity).where(Entity.kind == "brand").order_by(Entity.name)) if brand(db, e.id)
        ]
        return screen(
            s,
            "Активные проекты:" if rows else "Публичные материалы проектов готовятся. Консультант поможет с выбором.",
            [choice(e.name, "brand", brand_id=e.id) for e in rows]
            + [choice("Помогите выбрать", "request", mode="chat"), *common()],
        )
    if action == "brand":
        if not brand(db, p["brand_id"]):
            raise BusinessError("Продажи проекта сейчас не активны.")
        old = sale_flow.get(db, s.case_id) if s.case_id else None
        if old and (old.brand_id not in {None, p["brand_id"]} or old.result == "success"):
            return screen(
                s,
                "Вы хотите сравнить проекты или открыть отдельное обращение о новой покупке?",
                [
                    choice("Только посмотреть материалы", "browse_brand", brand_id=p["brand_id"]),
                    choice("Подтверждаю отдельный интерес к покупке", "parallel", brand_id=p["brand_id"]),
                    *common(),
                ],
            )
        s.data = {**s.data, "brand_id": p["brand_id"]}
        return home(db, s)
    if action in {"browse_brand", "parallel"}:
        if not brand(db, p["brand_id"]):
            raise BusinessError("Проект не активен.")
        s.data = {
            **s.data,
            "brand_id": p["brand_id"],
            "parallel": action == "parallel",
            "parallel_evidence": "Явная кнопка подтверждения" if action == "parallel" else None,
        }
        return handoff_step(db, s) if action == "parallel" else home(db, s)
    if action == "categories":
        rows = knowledge(db, s.data.get("brand_id"))
        categories = [
            k
            for k in CATEGORIES
            if any(("faq" if r.data.get("policy_type") == "faq" else r.data.get("category")) == k for r in rows)
        ]
        return screen(
            s,
            "Выберите интересующий раздел:",
            [choice(CATEGORIES[k], "category", category=k) for k in categories] + common(),
        )
    if action in {"category", "content"}:
        rows = knowledge(db, s.data.get("brand_id"), p["category"])
        page = max(0, min(int(p.get("page", 0)), max(0, len(rows) - 1)))
        if not rows:
            return screen(
                s,
                "По этому разделу пока нет действующего утверждённого материала. Консультант поможет уточнить информацию.",
                common() + [choice("Консультант в чате", "request", mode="chat")],
            )
        r = rows[page]
        seen = list(s.data.get("seen", []))
        if p["category"] not in seen:
            seen.append(p["category"])
        s.data = {**s.data, "seen": seen}
        options = []
        if page + 1 < len(rows):
            options.append(choice("Смотреть ещё", "category", category=p["category"], page=page + 1))
        options += [choice("Следующий раздел", "categories")] + common()
        if len(seen) >= 2 or p["category"] == "finance":
            options.insert(0, choice("Продолжить с консультантом", "request", mode="chat"))
        lead = activity(
            db,
            s,
            "CONTENT_REQUESTED",
            {"record_id": r.id, "version": r.version, "category": p["category"], "read_confirmed": False},
        )
        issued = s.data.get("issued", [])[-99:] + [{"id": r.id, "version": r.version}]
        s.data = {**s.data, "issued": issued}
        if lead:
            s.data = {
                **s.data,
                "_delivery": {
                    "lead_id": lead.id,
                    "case_id": s.case_id,
                    "public_record": r.id,
                    "public_record_version": r.version,
                    "touch_reason": "materials"
                    if r.data.get("links") or p["category"] in {"finance", "media"}
                    else "onboarding",
                    "reply_to_inbound": True,
                    "silence_epoch": lead.data.get("silence", {}).get("epoch"),
                },
            }
        return screen(s, content(r), options)
    if action == "question":
        return screen(
            s,
            "Напишите свой вопрос. Отвечу по утверждённой базе или передам его консультанту.",
            common(),
            {"field": "question", "expires": (now() + timedelta(minutes=30)).isoformat()},
        )
    if action == "profile_edit":
        s.data = {**s.data, "profile": {"communication": s.data.get("profile", {}).get("communication", "chat")}}
        return handoff_step(db, s)
    if action == "request_existing":
        current = sale_flow.get(db, s.case_id)
        s.data = {**s.data, "brand_id": current.brand_id, "parallel": False}
        return handoff_step(db, s)
    if action == "request":
        s.data = {**s.data, "profile": {**s.data.get("profile", {}), "communication": p["mode"]}}
        if s.case_id:
            current = sale_flow.get(db, s.case_id)
            if current.brand_id and current.brand_id != s.data.get("brand_id") and not s.data.get("parallel"):
                return screen(
                    s,
                    "Сохранить текущий диалог или подтвердить отдельный интерес к выбранному проекту?",
                    [
                        choice("Продолжить текущий диалог", "request_existing"),
                        choice("Подтверждаю отдельный интерес", "parallel", brand_id=s.data["brand_id"]),
                        *common(),
                    ],
                )
        return handoff_step(db, s)
    if action == "understood":
        activity(db, s, "QUESTION_UNDERSTOOD")
        return home(db, s)
    if action == "consent":
        consent = db.scalar(
            select(Record).where(Record.kind == "consent", Record.key == p["key"], Record.published.is_(True))
        )
        if not consent or consent.version != p["version"]:
            raise Conflict("Редакция согласия изменилась. Проверьте форму заново.")
        try:
            sale_flow.route(db, "sales")
            sale_flow.sales_calendar(db)
        except BusinessError:
            return screen(
                s,
                "Передача консультанту временно недоступна. Можно продолжить знакомство и повторить запрос позже.",
                common(),
            )
        lead = sales.lead_entry(db, s.person_id, s.data.get("source", "direct"), consent.key)
        # This button is explicit renewed consent; a bare landing visit never clears opt-out.
        lead.no_contact = False
        reminders.react(db, lead, {"platform": s.platform, "chat_id": s.chat_id})
        profile = {
            **s.data.get("profile", {}),
            "seen": s.data.get("seen", []),
            "source": s.data.get("source", "direct"),
            "tags": s.data.get("tags", {}),
            "first_seen_at": s.data.get("first_seen_at"),
            "issued": s.data.get("issued", []),
            "questions": s.data.get("questions", []),
            "last_message": s.data.get("last_message", ""),
            "consent_record_id": consent.id,
            "consent_version": consent.version,
        }
        case = sale_flow.submit(
            db,
            lead,
            profile,
            s.data.get("brand_id"),
            bool(s.data.get("parallel")),
            s.data.get("parallel_evidence"),
            case_id=s.case_id,
        )
        sale_flow.event(
            db,
            case,
            "CONSENT_ACCEPTED",
            {"record_id": consent.id, "version": consent.version, "scope": "contacts_and_communication"},
        )
        s.case_id = case.id
        s.data = {**s.data, "parallel": False}
        nearest = sale_flow.sales_calendar(db).add_minutes(now(), 0)
        text = "Запрос передан консультанту. Можно продолжить знакомство."
        if nearest > now():
            from datetime import timezone
            from zoneinfo import ZoneInfo

            text += "\nСейчас нерабочее время. Ближайшее рабочее окно: " + nearest.replace(
                tzinfo=timezone.utc
            ).astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК")
        return screen(s, text, [choice("Дополнить сведения о проекте", "extra", index=0)] + common())
    if action == "extra":
        fields = [
            ("business", "Есть ли действующий бизнес?", ["Да", "Нет"]),
            ("premises", "Есть ли помещение?", ["Есть", "Нет", "Рассматриваю варианты"]),
            ("project_type", "Какой проект рассматриваете?", ["Новое предприятие", "Дополнительный бренд"]),
            ("start_period", "Когда планируете запуск?", ["До 3 месяцев", "3–6 месяцев", "Позже", "Пока изучаю"]),
            ("experience", "Есть ли опыт в общественном питании?", ["Есть", "Нет"]),
        ]
        index = int(p.get("index", 0))
        if "value" in p:
            key = fields[index][0]
            profile = {**s.data.get("profile", {}), key: p["value"]}
            s.data = {**s.data, "profile": profile}
            if s.case_id:
                case = sale_flow.get(db, s.case_id, True)
                case.data = {**case.data, key: p["value"]}
                case.version += 1
                sale_flow.event(db, case, "CLIENT_QUALIFICATION", {key: p["value"]})
            index += 1
        if index >= len(fields):
            return screen(s, "Сведения сохранены. Консультант увидит их в карточке.", common())
        _, question, options = fields[index]
        return screen(
            s,
            question,
            [choice(v, "extra", index=index, value=v) for v in options]
            + [choice("Не указывать", "extra", index=index, value="Не указано"), *common()],
        )
    raise BusinessError("Откройте /start для продолжения.")


def handle(db, event):
    from ama.bot import send

    if not event.private:
        raise AccessDenied("Чат не подключён. Обратитесь к куратору.")
    s = session(db, event)
    text = (event.text or "").strip()
    if text.startswith("/stop"):
        lead = activity(db, s, "OPT_OUT")
        if lead:
            lead.no_contact = True
            for case in db.scalars(select(SalesCase).where(SalesCase.lead_id == lead.id)):
                if case.result != "success":
                    case.result, case.state = "refused", "closed"
        response = screen(s, "Автоматические сообщения отключены. Чтобы обратиться снова, напишите свой вопрос.", [])
    elif event.callback:
        parts = event.callback.split(":")
        if len(parts) != 5 or parts[:2] != ["af", "p"] or parts[2] != s.id:
            raise AccessDenied("Это меню другого пользователя. Откройте /start.")
        try:
            expected, index = int(parts[3]), int(parts[4])
        except ValueError as error:
            raise BusinessError("Откройте меню заново.") from error
        versioned(s, expected)
        options = s.data.get("choices", [])
        if not 0 <= index < len(options):
            raise BusinessError("Кнопка недоступна.")
        db.add(
            Outbox(
                dedupe_key="prospect-callback:" + event.key,
                platform=event.platform,
                chat_id=event.chat_id,
                topic_key=event.topic,
                payload={"callback_id": event.callback_id},
            )
        )
        item = deepcopy(options[index])
        if item["action"] not in {"home", "brands", "browse_brand"}:
            activity(db, s, "PROSPECT_ACTION", {"action": item["action"]})
        response = dispatch(db, s, item["action"], item["params"])
    elif text.startswith(("/start", "/menu")):
        payload = text.partition(" ")[2]
        if payload.startswith("l_"):
            ticket = db.scalar(select(LandingTicket).where(LandingTicket.id == payload[2:]).with_for_update())
            if (
                ticket
                and ticket.platform == event.platform
                and ticket.expires_at > now()
                and ticket.person_id in {None, s.person_id}
            ):
                ticket.person_id = s.person_id
                # Preserve attribution and record the visit, without silently changing a confirmed interest.
                history = s.data.get("attribution", [])[-49:] + [
                    {"source": ticket.source_key, "tags": ticket.data.get("tags", {}), "at": now().isoformat()}
                ]
                s.data = {
                    **s.data,
                    "source": s.data.get("source", ticket.source_key),
                    "tags": s.data.get("tags", ticket.data.get("tags", {})),
                    "attribution": history,
                }
                new_brand = ticket.data.get("brand_id")
                if new_brand and not s.case_id and brand(db, new_brand):
                    s.data = {**s.data, "brand_id": new_brand}
                elif new_brand and s.case_id and new_brand != s.data.get("brand_id"):
                    response = dispatch(db, s, "brand", {"brand_id": new_brand})
                    send(db, event, *response)
                    return
        response = home(db, s)
    elif event.files:
        response = screen(
            s,
            "Для консультации напишите вопрос текстом или запросите звонок. Скриншот оплаты не подтверждает поступление денег и не создаёт сделку.",
            common(),
        )
    else:
        prompt = s.data.get("prompt")
        if prompt and prompt.get("expires") and parse_date(prompt["expires"] + "+00:00") <= now():
            prompt = None
        activity(db, s, "CLIENT_MESSAGE", {"text": text[:3500]})
        if prompt and prompt["field"] in {"name", "city", "phone", "preferred_time"}:
            if not text or len(text) > 250:
                raise BusinessError("Введите текст длиной до 250 символов.")
            if prompt["field"] == "phone" and not re.fullmatch(r"\+?[0-9 ()-]{7,25}", text):
                raise BusinessError("Проверьте номер телефона с кодом страны.")
            profile = {**s.data.get("profile", {}), prompt["field"]: text}
            s.data = {**s.data, "profile": profile}
            response = handoff_step(db, s)
        else:
            s.data = {
                **s.data,
                "last_message": text[:3500],
                "questions": s.data.get("questions", [])[-49:] + [text[:3500]],
            }
            active = sale_flow.get(db, s.case_id, True) if s.case_id else None
            if active and active.result != "success":
                active.version += 1
                active.data = {
                    **active.data,
                    "last_message": text[:3500],
                    "reply_to_inbound": True,
                    "requires_contact": True,
                }
                if active.result not in {"refused", "not_target"} and active.stage not in {
                    "contract",
                    "signing",
                    "payment_wait",
                }:
                    active.state, active.result = "active", None
                if active.binding_id:
                    queue(
                        db,
                        db.get(Binding, active.binding_id),
                        "inbound:" + event.key,
                        "Новое сообщение лида:\n" + text[:3000] + "\ninterest_id: " + active.id,
                    )
                sale_flow.event(db, active, "INBOUND_MESSAGE", {"text": text[:3500]}, key="public:" + event.key)
            matches = knowledge(db, s.data.get("brand_id"), search=text)
            if active and active.result == "success":
                matches = []
            if matches:
                r = matches[0]
                lead = db.scalar(select(Lead).where(Lead.person_id == s.person_id))
                if lead:
                    s.data = {
                        **s.data,
                        "_delivery": {
                            "lead_id": lead.id,
                            "case_id": s.case_id,
                            "public_record": r.id,
                            "public_record_version": r.version,
                            "touch_reason": "auto_answer",
                            "reply_to_inbound": True,
                            "silence_epoch": lead.data.get("silence", {}).get("epoch"),
                        },
                    }
                response = screen(
                    s,
                    content(r),
                    [
                        choice("Да, всё понятно", "understood"),
                        *common(),
                        choice("Консультант в чате", "request", mode="chat"),
                    ],
                )
            elif s.case_id:
                case = sale_flow.get(db, s.case_id, True)
                if case.result == "success":
                    deal = db.scalar(select(Deal).where(Deal.case_id == case.id))
                    if deal and deal.curator_id:
                        _, b = sale_flow.route(db, "support")
                        queue(
                            db,
                            b,
                            "post-sale:" + event.key,
                            "Обращение действующего ФП для куратора:\n" + text[:3000] + "\nСделка: " + deal.id,
                        )
                    response = screen(
                        s,
                        "Сделка уже состоялась. Вопрос сопровождения адресуйте куратору в клиентском чате бренда. Для новой покупки можно выбрать проект и подтвердить отдельный интерес.",
                        common() + [choice("Новая покупка", "brands")],
                    )
                else:
                    response = screen(s, "Сообщение сохранено для консультанта.", common())
            else:
                response = screen(
                    s,
                    "Для точного ответа подключим консультанта. Уточним только данные для связи.",
                    [choice("Продолжить в чате", "request", mode="chat"), *common()],
                )
    send(db, event, *response)
    delivery = s.data.get("_delivery")
    if delivery:
        rows = [
            row
            for row in list(db.new)
            if isinstance(row, __import__("ama.models", fromlist=["Outbox"]).Outbox)
            and row.dedupe_key.startswith("reply:" + event.key + ":")
        ]
        if rows:
            row = max(rows, key=lambda row: int(row.dedupe_key.rsplit(":", 1)[1]))
            row.payload = {**row.payload, **delivery}
        s.data = {k: v for k, v in s.data.items() if k != "_delivery"}

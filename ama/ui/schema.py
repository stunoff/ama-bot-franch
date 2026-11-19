"""Declarative typed fields. Users enter text and choose buttons, never JSON."""
from copy import deepcopy
from ama.sla_rules import CATEGORIES

ROLES = {"lawyer":"Юрист", "leadership":"Руководство АМА УК", "admin": "Системный администратор", "partner": "Франчайзи-партнёр", "observer": "Наблюдатель",
         "curator": "Куратор", "support_head": "Руководитель сопровождения", "marketing_manager": "Менеджер маркетинга",
         "specialist": "Специалист", "contract": "Договорный ответственный", "consultant": "Консультант продаж",
         "chief_marketer": "Главный маркетолог"}
PURPOSES = {"management": "Управление сопровождения УК", "support": "Сопровождение ФП УК", "client": "Клиентский брендовый чат",
            "marketing": "Маркетинг и реклама УК", "sales": "Продажи и консультации лидов ФП УК", "contracts": "Договоры, счета и ЭДО с ФП УК",
            "chief_marketer": "Главный маркетолог УК", "projecting": "Проектирование предприятий УК", "software": "Программное оснащение УК",
            "technical_support": "Техническая поддержка УК", "product": "Продукт и закупки УК", "personnel": "Персонал и рекрутинг УК",
            "registrations": "Регистрации в системах УК", "menu": "Меню и производство УК", "design": "Дизайн и макеты УК",
            "advertising": "Таргет и Директ УК", "printing": "Печать полиграфии УК", "accounting": "Консультации по бухгалтерии УК",
            "engineering": "Инженерные сети и эксплуатация УК", "equipment": "Производственное оборудование УК",
            "reviews": "Отзывы и карточки предприятий УК", "systems_training": "Обучение работе с ПО и системами УК",
            "smm": "SMM УК", "instructors": "Инструкторы открытий УК", "legal": "Юридический отдел УК",
            "leadership": "Руководство АМА УК", "systems_equipment": "Системное оснащение предприятия УК"}
KINDS = {"support": "Обращение ФП", "marketing": "Маркетинговая работа", "digital": "Цифровое подключение",
         "photo_check": "Пункт фото-приёмки", "paid_work": "Дополнительная платная работа", "debt": "Договорная задолженность",
         "legal": "Юридическая работа", "internal_opening": "Внутреннее открытие", "technical_opening": "Техническое открытие",
         "official_opening": "Официальное открытие", **{k: v for k, v in PURPOSES.items() if k not in {"management", "client", "sales"}}}
PRIORITIES = {"normal": "Обычный", "important": "Важный", "urgent": "Срочный", "critical": "Критичный"}
ACTIONS = {"sla_adopt":"Подключить SLA", "sla_plan":"Плановая дата результата", "sla_complete_data":"Данные комплектны", "sla_status":"Содержательный статус", "sla_recovered":"Восстановление / обходной способ", "take": "Взять в работу", "submit": "Результат готов", "accept": "Принять результат", "reject": "Вернуть на доработку",
           "wait_partner": "Запросить данные ФП", "wait_uk": "Запросить действие УК", "wait_external": "Ожидать внешнюю сторону",
           "resume": "Возобновить работу", "handoff": "Передать в другое направление", "handoff_accept": "Принять передачу",
           "cancel": "Отменить работу", "reschedule": "Изменить срок"}
STAGES = {"new": "Новый лид", "qualification": "Квалификация", "presentation": "Знакомство с проектом",
          "financial_review": "Финансовый разбор", "contract": "Оформление договора"}
SCENARIOS = {"new": "Новое предприятие", "additional_brand": "Дополнительный бренд", "existing": "Действующее предприятие",
             "technical": "Предприятие в техническом режиме", "reconstruction": "Реконструкция"}
MODES = {"perform": "Выполнить", "verify": "Проверить / обновить", "inherit": "Использовать готовое", "not_applicable": "Не применяется"}


def f(key, label, kind="text", required=False, **kw):
    return {"key": key, "label": label, "type": kind, "required": required, **kw}


def ref(key, label, source, required=False, **kw):
    return f(key, label, "ref", required, source=source, **kw)


def choice(key, label, options, required=False, **kw):
    return f(key, label, "choice", required, choices=options, **kw)


SCOPE = [ref("partner_id", "ФП", "partner"), ref("brand_id", "Бренд", "brand"), ref("establishment_id", "Предприятие", "establishment")]
REASON = f("reason", "Основание изменения", required=True)
OWNER = ref("owner_id", "Владелец содержимого", "person", True)
CALENDAR = [f("timezone", "Часовой пояс", "timezone", True), f("start", "Начало рабочего окна", "time", True),
            f("end", "Конец рабочего окна", "time", True), f("weekdays", "Рабочие дни", "multi", True,
                choices={str(i): name for i, name in enumerate(["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"])}, integer=True),
            f("holidays", "Нерабочие даты", "dates"), f("extra_workdays", "Дополнительные рабочие даты", "dates")]
CARD = [f("module", "Раздел (0–11)", "int", True, minimum=0, maximum=11), f("lesson", "Номер карточки", required=True),
        f("title", "Название", required=True, maximum=250), f("text", "Сообщение партнёру", required=True, maximum=12000),
        f("required_materials", "Обязательные материалы", "multiref", source="record:material")]
BLOCK = [f("key", "Код пункта", "key", True), f("title", "Название работы", required=True),
         choice("mode", "Применимость", MODES, True), f("shared", "Общая работа физической площадки", "bool"),
         ref("owner_id", "Исполнитель", "person", True), ref("reviewer_id", "Принимающий", "person", True),
         ref("binding_id", "Рабочая очередь", "binding", True), ref("client_binding_id", "Клиентский чат", "client_binding"),
         ref("partner_person_id", "Назначенный ФП", "partner_person"), ref("calendar", "Календарь", "record:calendar", True),
         f("due_at", "Дата результата", "datetime", True),
         f("data", "Зависимости и требования", "object", fields=[f("depends_on", "Принятые результаты", "multiref", source="work"),
            f("expected_result", "Ожидаемый результат", required=True), f("blockers", "Блокирующие замечания", "lines")])]
POLICY_BLOCK = [f("key", "Код пункта", "key", True), f("title", "Название", required=True),
                f("expected_result", "Ожидаемый результат", required=True), choice("executor_role", "Роль исполнителя", ROLES, True),
                choice("reviewer_role", "Роль принимающего", ROLES, True), ref("route", "Маршрут", "record:route", True),
                choice("mode", "Применимость", MODES, True), f("depends_on", "Коды зависимостей", "lines"),
                f("start_event", "Событие старта", "key", True), f("date_anchor", "Опорная дата", "key"),
                f("materials", "Материалы", "multiref", source="record:material"),
                f("completion_rule", "Правило завершения", required=True), f("photo_requirements", "Требования к фото", "lines")]
RECORD_VALUES = {
    "calendar": CALENDAR,
    "material": [f("title", "Пользовательское название", required=True), choice("type", "Тип", {"link": "Ссылка", "video": "Видео", "document": "Документ", "image": "Изображение"}),
                 f("url", "HTTPS-ссылка", "url", True), f("audience", "Кому доступен", "multi", True, choices=ROLES),
                 *SCOPE, f("purpose", "Назначение"), f("module", "Раздел ознакомления", "int", minimum=0, maximum=11), f("verified_on", "Дата проверки ссылки", "date"), f("content_approval","Подтверждение владельца содержания"),f("link_checks","Проверки ссылки","multi",choices={"opens":"Открывается","audience":"Права нужной аудитории","exists":"Файл существует","title":"Название совпадает"}),choice("access_stage","Доступ",{"current":"Текущие материалы","general_completed":"После общего ознакомления"}),f("call_center_only","Только по выбранному КЦ","bool"),choice("availability","Доступность",{"active":"Активен","updating":"На обновлении","blocked":"Техническое ожидание"}),f("review_days","Период проверки ссылки, дни","int",minimum=1),f("substantial_update","Существенное обновление","bool"),f("required_update_reason","Причина обязательного обновления"),f("update_cards","Карточки изменений","list",fields=CARD),f("update_brand_ids","Только затронутые бренды (пусто — все)","multiref",source="brand")],
    "course": [f("title", "Название курса"), f("cards", "Карточки ознакомления", "list", True, fields=CARD),
               f("substantial_update", "Существенное обновление", "bool"), f("required_update_reason", "Причина обязательного обновления"),f("content_approval","Подтверждение владельца содержания"),f("update_cards","Только карточки изменений","list",fields=CARD),f("update_brand_ids","Только затронутые бренды (пусто — все)","multiref",source="brand")],
    "route": [ref("binding_id", "Рабочий чат", "binding", True), ref("owner_id", "Следующий ответственный", "person", True),
              ref("reviewer_id", "Принимающий", "person", True), ref("calendar", "Календарь", "record:calendar", True), ref("queue_manager_id", "Ответственный за очередь", "person"), *SCOPE],
    "commercial": [ref("brand_id", "Бренд", "brand", True), f("required_minor", "Условие расчёта, ₽", "money", True, minimum=0),
                   f("terms", "Утверждённые коммерческие условия", required=True), f("approved_on", "Дата утверждения", "date", True),
                   f("valid_until", "Действует до", "date"), f("approval", "Подтверждение владельца / ссылка на решение", required=True)],
    "consent": [f("text", "Утверждённый текст согласия", required=True), f("privacy_url", "Политика обработки данных", "url", True)],
    "template": [f("title", "Название"), f("text", "Текст сообщения", required=True), f("audience", "Аудитория", "multi", choices=ROLES),
                 f("variables", "Разрешённые переменные", "lines"), f("conditions", "Условия отправки")],
    "policy": [],
}
POLICIES = {
    "finance_recipient": [f("title","Получатель",required=True),f("recipient_key","Устойчивый идентификатор банковского получателя", "key",True),f("reference","Утверждённые реквизиты","url",True),f("approval","Основание финансового утверждения",required=True),*deepcopy(SCOPE)],
    "broadcast_permission": [ref("person_id","Автор с правом рассылки","person",True),f("brand_ids","Разрешённые бренды (пусто — все в области роли)","multiref",source="brand"),f("allow_general","Разрешена общая информация УК","bool"),*deepcopy(SCOPE),f("approval","Основание назначения права",required=True)],
    "scenario": [f("title", "Название сценария", required=True), choice("scenario", "Тип проекта", SCENARIOS, True),
                 f("blocks", "Пункты и зависимости", "list", True, fields=POLICY_BLOCK), f("approval", "Подтверждение владельца процесса", required=True)],
    "sla": [f("title", "Название профиля", required=True), ref("route", "Контур", "record:route", True),
            choice("priority", "Приоритет", PRIORITIES, True), f("take_minutes", "Срок принятия, минуты", "int", True, minimum=1),
            f("result_minutes", "Срок результата, минуты", "int", True, minimum=1),
            choice("category","Категория",CATEGORIES),
            choice("counter", "Счёт времени", {"working": "Рабочее", "calendar": "Календарное"}, True),
            f("reminders", "Интервалы напоминаний, минуты", "integers"), f("approval", "Подтверждение владельца процесса", required=True)],
    "source": [f("title", "Название источника", required=True), f("source_id", "Идентификатор источника", "key", True),
               ref("brand_id", "Бренд", "brand"), f("telegram_bot_username", "Публичное имя Telegram-бота"), f("max_bot_username", "Публичное имя MAX-бота"), f("tags", "Разрешённые метки", "lines"), f("welcome", "Приветствие")],
    "faq": [f("title", "Тема", required=True), f("question", "Вопрос", required=True), f("answer", "Утверждённый ответ", required=True),
            ref("brand_id", "Бренд", "brand"), f("approved_on", "Дата утверждения", "date", True), f("approval", "Подтверждение владельца", required=True)],
    "payment": [f("recipient_key","Устойчивый идентификатор получателя","key"),f("title", "Утверждённый получатель", required=True), f("reference", "Ссылка на защищённые реквизиты", "url", True),
                f("services", "Применимость к услугам", "multi", True, choices={k: PURPOSES[k] for k in ["projecting", "software", "personnel", "menu", "design", "advertising", "printing", "systems_training", "smm", "reviews", "support"]}),
                f("approval", "Основание финансового владельца", required=True), f("valid_until", "Действует до", "date")],
    "schedule": [f("title", "Название расписания", required=True), f("event", "Существующее событие", "key", True),
                 ref("calendar", "Календарь", "record:calendar", True), ref("binding_id", "Получатель", "binding", True),
                 f("start_on", "Начало периода", "date", True), f("frequency", "Периодичность", required=True),
                 f("approval", "Подтверждение владельца процесса", required=True)],
    "sales_content": [f("title", "Название карточки", required=True), ref("brand_id", "Бренд", "brand", True),
        choice("category", "Раздел", {"about":"О проекте", "advantages":"Преимущества", "formats":"Форматы", "finance":"Экономика", "benefits":"Что получает партнёр", "launch":"Запуск", "menu":"Меню", "marketing":"Маркетинг", "systems":"Системы", "support":"Поддержка", "premises":"Помещение", "media":"Медиа"}, True),
        f("text", "Утверждённый текст", required=True, maximum=3500), f("public", "Разрешено показывать лиду", "bool"),
        f("links", "Материалы", "list", fields=[f("title","Название",required=True),f("url","HTTPS-ссылка","url",True)]),
        choice("finance_basis", "Тип финансовых сведений", {"terms":"Условия франшизы", "model":"Модель конкретного формата"}),
        f("format", "Формат финансовой модели"), f("assumptions", "Допущения расчёта"), f("approved_on","Дата утверждения","date",True),
        f("approval","Основание владельца содержимого",required=True), f("valid_until","Действует до","date")],
    "generic": [f("title", "Название", required=True), f("description", "Описание"), f("settings", "Параметры", "object", fields=[])],
}


def fields_for(kind):
    if kind.startswith("finance:"):
        from ama.ui.finance import fields
        return fields(kind)
    if kind.startswith("learning:"):
        from ama.ui.learning import fields
        return fields(kind)
    if kind.startswith("digital:"):
        from ama.ui.digital import fields
        return fields(kind)
    if kind.startswith(("packet:","obligation:")):
        from ama.ui.packet_schema import fields
        return fields(kind.split(":")[1])
    if kind == "work:take":
        return [f("due_at","Плановый результат для длительной работы (для обычной не требуется)","datetime"),f("reason","Объём / этап поиска и основание даты")]
    if kind == "client_sla:return":
        return [f("reason","Что осталось нерешённым",required=True)]
    if kind == "signal:react":
        return [f("reaction","Принятое решение / действие",required=True), f("next_check","Следующий контроль","datetime"), f("close_signal","Контрольный сигнал урегулирован (исходная работа не закрывается)","bool")]
    if kind.startswith("work:sla_"):
        action=kind.split(":")[1]
        return {"sla_adopt": [], "sla_plan": [f("due_at","Плановая дата результата","datetime",True),REASON,f("approval","Основание индивидуального срока")],
                "sla_complete_data": [f("evidence","Проверенные данные и основание комплектности",required=True),f("due_at","Плановый результат для длительной работы","datetime"),f("reason","Объём и основание плановой даты")],
                "sla_status": [f("status_text","Содержательный статус",required=True),f("evidence","Основание статуса",required=True),f("next_check","Следующее действие (для неаварийных работ)","datetime")],
                "sla_recovered": [f("status_text","Восстановление / обходной способ",required=True),f("evidence","Подтверждение восстановления или согласования обхода",required=True),choice("resolution","Итог",{"restored":"Работа восстановлена","agreed_workaround":"Обходной способ согласован"},True)]}.get(action,[])

    if kind.startswith("plan:"):
        from ama.ui.project_schema import fields
        return fields(kind.split(":")[1])
    if kind == "config:reconcile_delivery":
        return [choice("outcome", "Проверенный исход", {"delivered": "Сообщение уже доставлено", "not_delivered": "Доставка точно не состоялась"}, True),
                f("external_id", "ID доставленного сообщения"), f("evidence", "Как проверен фактический исход", required=True),
                f("worker_stopped", "Все воркеры остановлены перед сверкой зависшей sending", "bool"), REASON]
    if kind.startswith("expert:"):
        return [f("answer", "Внутренний ответ консультанту", required=True, maximum=12000), f("approved_exception", "Явно согласовать индивидуальные условия по вопросу категории «Условия»", "bool")] if kind.endswith("answer") else []
    if kind.startswith("case:"):
        action = kind.split(":")[1]
        if action == "contract":
            return [f("additional_case_ids", "Дополнительные подтверждённые интересы того же человека", "multiref", source="sales_case")]
        if action == "qualify":
            return [f("name", "Имя", required=True), f("city", "Город", required=True), f("phone", "Телефон"),
                f("location", "Локация / адрес / карта"), f("premises", "Помещение"), f("business", "Действующий бизнес"),
                f("start_period", "Срок запуска"), f("experience", "Опыт"), ref("commercial_key", "Текущая коммерческая редакция", "record:commercial"),
                f("economics_acknowledged", "Лид ознакомлен с экономикой", "bool"), f("intent_confirmed", "Лид подтвердил намерение заключить договор", "bool"),
                f("requisites", "Реквизиты / защищённая ссылка"), f("request_requisites", "Запросить реквизиты при оформлении", "bool"),
                f("exceptions", "Индивидуальные договорённости"), ref("exception_reference", "Подтверждение Главного маркетолога", "sales_expert")]
        if action in {"contact", "call_schedule", "wait_lead", "not_now"}:
            return [f("next_contact", "Следующая дата контакта", "datetime", True), f("next_action", "Следующий шаг", required=True), f("reason", "Причина / договорённость", required=True)]
        if action == "call":
            return [choice("result", "Фактический результат звонка", {"connected":"Связались", "no_answer":"Не дозвонились", "call_later":"Перезвонить позже", "next_stage":"Готов к следующему этапу"}, True),
                f("comment", "Результат и следующий шаг", required=True), f("next_contact", "Следующий контакт (по договорённости)", "datetime"), f("next_action", "Следующее действие")]
        if action == "expert":
            return [choice("category", "Категория вопроса", {"question":"Вопрос", "location":"Аналитика города / локации", "economics":"Экономика", "terms":"Индивидуальные условия", "menu":"Меню", "premises":"Помещение"}, True),
                f("question", "Вопрос Главному маркетологу", required=True), f("due_at", "Контрольная дата", "datetime", True)]
        if action == "assign_brand":
            return [ref("brand_id", "Подтверждённый проект", "brand", True), f("evidence", "Как лид подтвердил выбор", required=True)]
        if action == "financial_review":
            return [f("evidence", "Дата / содержание состоявшегося финансового разбора", required=True)]
        if action == "reassign":
            return [ref("person_id", "Новый консультант", "person", True), REASON]
        if action == "note":
            return [f("text", "Внутренняя заметка", required=True)]
        if action == "reply":
            return [f("text", "Клиентский ответ", required=True, maximum=3500), choice("touch_reason", "Ожидание после ответа", {"consultant_reply":"Ответ консультанта", "clarification":"Уточняющий вопрос", "materials":"Запрошенные материалы"}, True)]
        if action == "not_target":
            return [f("reason", "Причина нецелевого интереса", required=True)]
        if action == "stage":
            return [choice("stage", "Этап", {"selection":"S0 · Подбор проекта", "presentation":"S1 · Знакомство", "financial_review":"S2 · Начато обсуждение экономики", "terms":"S3 · Согласование условий"}, True), REASON]
        return fields_for("sales:"+action) if action in {"refuse", "reopen"} else []
    if kind.startswith("config:"):
        return ([ref("new_id", "Новый проверенный чат", "binding", True)] if kind == "config:replace_binding" else []) + [REASON]
    if kind.startswith("record:"):
        parts = kind.split(":")
        content = POLICIES.get(parts[2], []) if len(parts) > 2 else RECORD_VALUES[parts[1]]
        return [f("key", "Постоянный ключ", "key", True), OWNER,
                f("value", "Содержимое", "object", True, fields=deepcopy(content)), REASON]
    if kind.startswith("entity:"):
        entity_kind = kind.split(":")[1]
        fields = [f("name", "Название", required=True, maximum=200)]
        if entity_kind == "establishment":
            fields += [ref("partner_id", "ФП", "partner", True)]
        details = [f("archived", "Архивная запись", "bool")]
        if entity_kind == "brand":
            details += [f("sales_available", "Продажи доступны", "bool"), f("knowledge_available", "База знаний доступна", "bool"),
                        f("content_version", "Редакция продажного содержания"), f("approved_on", "Дата утверждения", "date")]
        else:
            details += [f("city", "Город"), f("address", "Адрес"), f("timezone", "Часовой пояс", "timezone"),
                        f("call_center_applicable", "Услуга КЦ согласована", "bool"),
                        f("notification_window", "Согласованное окно уведомлений", "object", fields=deepcopy(CALENDAR))]
        if entity_kind=="partner":
            details += [ref("partner_person_id","Уполномоченный ФП (при нескольких назначениях)","person"),ref("curator_id","Закреплённый куратор","person")]
        if entity_kind=="establishment":
            details += [ref("curator_id","Куратор физического предприятия","person"),f("format","Формат предприятия"),choice("lifecycle","Состояние",{"operating":"Работающее","starting":"Запускающееся","unknown":"Не определено"})]
        return fields + [f("data", "Свойства", "object", fields=details), REASON]
    if kind == "person":
        return [f("name", "Имя сотрудника", required=True, maximum=200), REASON]
    if kind in {"role", "employee"}:
        person = [f("name", "Имя", required=True), choice("platform", "Мессенджер", {"telegram": "Telegram", "max": "MAX"}, True),
                  f("external_id", "Подтверждённый ID пользователя", "external_id", True)] if kind == "employee" else [ref("person_id", "Человек", "person", True)]
        return person + [choice("role" if kind == "employee" else "name", "Роль", ROLES, True), *deepcopy(SCOPE),
                         f("expires_at", "Действует до (необязательно)", "datetime"), REASON]
    if kind == "account":
        return [ref("person_id", "Подтверждённый человек", "person", True), choice("platform", "Мессенджер", {"telegram": "Telegram", "max": "MAX"}, True),
                f("external_id", "ID аккаунта", "external_id", True), f("active", "Аккаунт активен", "bool"), REASON]
    if kind == "binding":
        return [choice("purpose", "Функциональное назначение", PURPOSES, True), choice("platform", "Мессенджер", {"telegram": "Telegram", "max": "MAX"}, True),
                f("chat_id", "Фактический ID чата", "external_id", True), f("topic_key", "ID темы (если используется)", "topic"), *deepcopy(SCOPE), REASON]
    if kind == "link":
        return [ref("establishment_id", "Физическое предприятие", "establishment", True), ref("brand_id", "Бренд", "brand", True), REASON]
    if kind == "create_work":
        return [f("title", "Название / ожидаемый результат", required=True, maximum=250), choice("kind", "Вид работы", KINDS, True),
                ref("partner_id", "ФП", "partner", True), ref("establishment_id", "Предприятие", "establishment"), ref("brand_id", "Бренд", "brand"),
                *deepcopy(BLOCK[4:11]), choice("priority", "Приоритет", PRIORITIES, True),
                f("data", "Требования и зависимости", "object", fields=deepcopy(BLOCK[-1]["fields"])+[choice("sla_category","Категория SLA",CATEGORIES),f("incident","Реальная авария: влияние, основание и подтверждённые симптомы")])]
    if kind == "project":
        return [ref("partner_id", "ФП", "partner", True), ref("establishment_id", "Предприятие", "establishment", True), ref("brand_id", "Бренд", "brand"),
                choice("scenario", "Ситуация", SCENARIOS, True), f("blocks", "Состав работ", "list", fields=deepcopy(BLOCK))]
    if kind == "broadcast":
        return [f("text", "Текст публикации", required=True, maximum=3500), ref("brand_id", "Бренд (пусто — общая информация)", "brand")]
    if kind == "export":
        return [choice("report", "Вид отчёта", {"packets":"Отчёт конкретного пакета (из карточки)", "works": "Работы и сроки", "summary": "Сводка сопровождения", "training": "Ознакомление ФП", "sales": "Совместимый отчёт продаж 0.2", "sales_events": "Продажи: события периода", "sales_funnel": "Продажи: текущая воронка", "sales_cohort": "Продажи: когорта первой передачи", "audit": "Журнал настроек"}, True),
                f("start", "Начало периода", "date", True), f("end", "Конец периода (включительно)", "date", True),
                ref("partner_id", "ФП", "partner"), ref("establishment_id", "Предприятие", "establishment"), ref("brand_id", "Бренд", "brand")]
    if kind == "substitution":
        return [ref("from_person", "Кого замещаем", "person", True), ref("to_person", "Новый ответственный", "person", True),
                f("work_ids", "Передаваемые работы", "multiref", True, source="work"), f("expires_at", "Конец временного назначения", "datetime"), REASON]
    if kind.startswith("work:"):
        action = kind.split(":")[1]
        fields = {"reject": [f("reason", "Замечание", required=True), f("due_at", "Новый срок", "datetime", True)],
                  "cancel": [f("reason", "Причина отмены", required=True)],
                  "reschedule": [f("reason", "Причина переноса", required=True), f("due_at", "Новый срок", "datetime", True)],
                  "resume": [f("due_at", "Срок следующего действия", "datetime", True)],
                  "handoff": [ref("person_id", "Новый исполнитель", "person", True), ref("binding_id", "Новая рабочая очередь", "binding", True)]}.get(action, [])
        if action.startswith("wait_"):
            fields = [f("party", "Кого ждём", required=True), f("required", "Какое действие / материал нужен", required=True),
                      f("next_check", "Следующая контрольная дата", "datetime", True)]
        if action == "wait_partner":
            fields += [f("reason","Почему действие невозможно без этих данных (обязательно для SLA)")]
        if action == "wait_external":
            fields += [f("sent_information","Какие сведения уже отправлены внешней стороне (обязательно для SLA)")]
        if action == "submit":
            fields = [f("result_description", "Что сделано", required=True)]
        return fields
    if kind.startswith("sales:"):
        action = kind.split(":")[1]
        if action == "qualify":
            return [f("name", "Имя лида", required=True), f("contact", "Подтверждённый контакт", required=True), f("city", "Город", required=True),
                    ref("brand_id", "Выбранная франшиза", "brand", True), f("budget", "Бюджет / диапазон"), f("start_period", "Планируемый старт"),
                    f("experience", "Опыт"), f("premises", "Ситуация с помещением"), ref("commercial_key", "Утверждённые условия", "record:commercial"),
                    f("intent_confirmed", "Лид подтвердил намерение оформить договор", "bool"), f("requisites", "Реквизиты / защищённая ссылка"),
                    f("request_requisites", "Реквизиты нужно дозапросить", "bool"), f("exceptions", "Подтверждённые индивидуальные договорённости")]
        if action in {"wait_lead", "not_now", "no_answer"}:
            return [f("next_contact", "Дата следующего контакта", "datetime", True), f("next_action", "Следующий шаг", required=True)]
        return {"stage": [choice("stage", "Содержательный этап", {k: v for k, v in STAGES.items() if k != "contract"}, True)],
                "refuse": [f("reason", "Причина отказа", required=True)], "reopen": [f("consent_evidence", "Подтверждение согласия на контакт", required=True)],
                "reply": [f("text", "Текст ответа клиенту", required=True, maximum=3500)]}.get(action, [])
    if kind.startswith("deal:"):
        action = kind.split(":")[1]
        return {"signed_uk":[f("package","Версия комплекта","int",True,minimum=1),f("evidence","Подтверждение подписи УК",required=True)],"signed_partner":[f("package","Версия комплекта","int",True,minimum=1),f("evidence","Подтверждение подписи клиента",required=True)],"invitation_sent":[f("evidence","Подтверждение отправки приглашения",required=True)],"invitation_accepted":[f("evidence","Подтверждение принятия приглашения",required=True)],"sent": [f("evidence", "Подтверждение отправки текущего обязательного комплекта", required=True)],
                "curator_clarify": [f("question", "Что нужно уточнить консультанту", required=True)],
                "package": [f("document_reference", "Ссылка на редакцию комплекта", required=True)],
                "signed": [f("evidence", "Подтверждение подписей текущей редакции", required=True)],
                "payment": [f("amount_minor", "Подтверждённая сумма, ₽", "money", True, minimum=1), f("bank_reference", "Уникальная банковская операция", required=True)],
                "assign_curator": [ref("curator_id", "Куратор", "person", True), ref("partner_id", "ФП", "partner", True)]}.get(action, [])
    if kind.startswith("operation:"):
        action = kind.split(":")[1]
        return {
            "offer": [ref("payment_policy","Утверждённые реквизиты услуги","record:policy",True),choice("deadline_mode","Срок",{"fixed":"Фиксированная дата","duration":"Длительность от допустимого старта"}),f("duration_minutes","Длительность, минуты","int",minimum=1),choice("duration_counter","Счёт длительности",{"calendar":"Календарное время","working":"Рабочее время"}),f("scope", "Объём услуги", required=True), f("amount_minor", "Стоимость, ₽", "money", True, minimum=1), f("valid_until", "Предложение действует до", "datetime", True),
                      choice("start_rule", "Согласованное правило старта", {"partner_confirmation": "Подтверждение ФП", "partner_payment_evidence": "Подтверждение оплаты ФП"}, True), f("deadline", "Срок результата для фиксированной даты", "datetime")],
            "invoice": [f("amount_minor", "Сумма счёта, ₽", "money", True, minimum=1), f("document", "Документ / ссылка", required=True)],
            "debt_payment": [f("amount_minor", "Подтверждённая сумма, ₽", "money", True, minimum=1), f("bank_reference", "Банковская операция", required=True)],
            "deferral": [f("due_at", "Согласованный срок", "datetime", True), f("approval", "Основание отсрочки", required=True)],
            "passport": [choice("work_phone_messenger", "Мессенджер рабочего телефона", {"telegram": "Telegram", "max": "MAX"}),
                         choice("management_messenger", "Мессенджер чатов управления", {"telegram": "Telegram", "max": "MAX"}),
                         f("systems", "Применимые системы", "lines"), f("channels", "Каналы заказов", "lines"), f("equipment", "Оборудование", "lines"),
                         f("zones", "Зоны", "lines"), f("prices", "Цены / версия прайс-листа"), f("address", "Адрес"), f("phone", "Рабочий телефон"), f("call_center_applicable", "КЦ согласован", "bool")],
            "tech_confirm": [f("component", "Компонент", "key", True), f("evidence", "Подтверждение подключения", required=True), f("control_test", "Результат контрольного теста", required=True)],
            "opening_confirm": [f("evidence", "Фактическое подтверждение готовности", required=True)],
        }.get(action, [])
    return []


def infer(key, value):
    kind = "bool" if isinstance(value, bool) else "int" if isinstance(value, int) else "object" if isinstance(value, dict) else "list" if isinstance(value, list) and value and isinstance(value[0], dict) else "lines" if isinstance(value, list) else "text"
    return f(str(key), str(key), kind, fields=[] if kind in {"object", "list"} else None)


def value_at(data, path, default=None):
    for key in path:
        try:
            data = data[int(key)] if isinstance(data, list) else data[key]
        except (KeyError, IndexError, TypeError, ValueError):
            return default
    return data


def set_at(data, path, value):
    if not path:
        raise ValueError("Empty field path")
    root = deepcopy(data)
    current = root
    for index, key in enumerate(path[:-1]):
        if isinstance(current, list):
            current = current[int(key)]
        else:
            if key not in current or current[key] is None:
                current[key] = [] if str(path[index + 1]).isdigit() else {}
            current = current[key]
    key = path[-1]
    if isinstance(current, list):
        current[int(key)] = value
    else:
        current[key] = value
    return root


def descriptor(kind, data, path):
    fields = fields_for(kind)
    spec = {"type": "object", "fields": fields, "label": "Поля"}
    current = data
    for key in path:
        if spec["type"] == "list":
            spec = {"type": "object", "fields": spec.get("fields", []), "label": f"Пункт {int(key)+1}"}
            current = current[int(key)]
        else:
            found = next((x for x in spec.get("fields", []) if x["key"] == key), None)
            spec = deepcopy(found or infer(key, current.get(key) if isinstance(current, dict) else None))
            current = current.get(key) if isinstance(current, dict) else None
    return spec

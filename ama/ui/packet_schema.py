from ama.ui.schema import f, ref, choice, REASON


def payload_fields():
    return [
        f("title", "Заголовок", required=True, maximum=250),
        f("text", "Что меняется и какие действия нужны", required=True, maximum=2500),
        choice("audience", "Аудитория", {"brands": "По выбранным брендам", "general": "Общая информация УК"}, True),
        f("brand_ids", "Бренды", "multiref", source="brand"),
        f(
            "establishment_ids",
            "Только выбранные предприятия (пусто — все подходящие)",
            "multiref",
            source="establishment",
        ),
        choice(
            "population",
            "Предприятия",
            {"all": "Все подходящие", "operating": "Работающие", "starting": "Запускающиеся"},
            True,
        ),
        f("cities", "Города (точные значения из паспорта)", "lines"),
        f("formats", "Форматы предприятий", "lines"),
        choice("call_center", "КЦ", {"all": "Любая применимость", "yes": "Применяется", "no": "Не применяется"}, True),
        f("apply_at", "Дата применения", "datetime", True),
        choice(
            "mode",
            "Режим",
            {
                "information": "Информация без ответа",
                "acknowledgement": "Обязательное ознакомление",
                "task": "Задание с результатом",
            },
            True,
        ),
        choice(
            "scope",
            "Область обязательства",
            {
                "partner": "ФП без бренда",
                "partner_brand": "ФП + бренд",
                "facility": "Физическое предприятие",
                "facility_brand": "Предприятие + бренд",
            },
            True,
        ),
        f("due_at", "Срок ознакомления / задания", "datetime"),
        f("checklist", "Применимые пункты задания", "lines"),
        choice(
            "evidence_mode",
            "Доказательство задания",
            {"text": "Описание результата", "file": "Файл и описание", "photo": "Фото и описание"},
        ),
        f("evidence_requirements", "Какой результат / доказательства нужны"),
        f(
            "links",
            "Ссылки и видео",
            "list",
            fields=[f("title", "Название", required=True), f("url", "HTTPS-ссылка", "url", True)],
        ),
        f("material_keys", "Опубликованные материалы для ФП", "multiref", source="record:material"),
        f("asset_ids", "Подготовленные файлы пакета", "multiref", source="packet_asset"),
        ref("calendar", "Календарь обязательств", "record:calendar"),
        f(
            "remind_days",
            "Повтор напоминания после срока, рабочих дней (0 — без повторов)",
            "int",
            minimum=0,
            maximum=365,
        ),
        f(
            "escalate_days",
            "Контроль управления через рабочие дни после срока (0 — выключен)",
            "int",
            minimum=0,
            maximum=365,
        ),
    ]


def fields(action):
    if action == "create":
        return payload_fields()
    if action == "revise":
        return [
            f("payload", "Новая редакция", "object", True, fields=payload_fields()),
            REASON,
            f("repeat_required", "Нужно повторное ознакомление / новый результат", "bool", True),
            f("cancel_pending", "Явно отменить старые незакрытые обязательства с указанной причиной", "bool", True),
        ]
    if action == "typo":
        return [f("text", "Исправленный текст (без смены условий)", required=True, maximum=2500), REASON]
    if action == "add":
        return [f("establishment_ids", "Новые предприятия", "multiref", True, source="establishment"), REASON]
    if action == "retry":
        return [REASON]
    if action == "ack":
        return []
    if action == "submit":
        return [
            f("completed_items", "Выполненные пункты", "multiref", True, source="packet_checklist"),
            f("result_description", "Результат по каждому пункту", required=True),
        ]
    if action == "answer":
        return [
            f("text", "Ответ ФП (наружу отправляется только этот текст)", required=True, maximum=2500),
            f("next_check", "Следующий контроль", "datetime", True),
        ]
    if action == "question":
        return [
            ref("establishment_id", "Предприятие (если вопрос относится к объекту)", "packet_establishment"),
            f("question", "Ваш вопрос закреплённому куратору", required=True),
        ]
    if action == "schedule":
        return [
            f(
                "payload",
                "Шаблон пакета",
                "object",
                True,
                fields=[x for x in payload_fields() if x["key"] not in {"apply_at", "due_at"}],
            ),
            f("start_at", "Первый запуск", "datetime", True),
            f("until_at", "Конец периода", "datetime", True),
            choice("repeat", "Повторение", {"weekly": "Еженедельно", "monthly": "Ежемесячно"}, True),
            f("due_workdays", "Срок каждого обязательства, рабочих дней", "int", minimum=1, maximum=365),
        ]
    if action == "reschedule":
        return [f("due_at", "Будущий согласованный срок", "datetime", True), REASON]
    if action == "stop":
        return [REASON]
    return []

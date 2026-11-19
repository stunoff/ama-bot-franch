from ama.ui.schema import f, ref, choice, SCENARIOS, MODES, REASON


def fields(action):
    if action == "create":
        return [
            f("title", "Название проекта", required=True, maximum=250),
            ref("partner_id", "ФП", "partner", True),
            ref("establishment_id", "Предприятие", "establishment", True),
            ref("brand_id", "Бренд", "brand", True),
            choice("scenario", "Ситуация", SCENARIOS, True),
            ref("scenario_template","Опубликованный шаблон (если несколько)","record:policy"),
            ref("marketing_manager_id", "Назначенный менеджер маркетинга", "person"),
            ref("partner_person_id", "Уполномоченный ФП", "partner_person"),
            ref("client_binding_id", "Клиентский чат", "client_binding"),
        ]
    if action == "filter":
        return [
            choice("mode", "Применимость", MODES, True),
            f("route_key", "Опубликованный маршрут (пусто — профильный)"),
            f("control_days", "Контрольный срок, рабочих дней", "int", minimum=1, maximum=365),
            ref("source_work_id", "Принятый результат для наследования", "work"),
            f("evidence", "Внешний проверенный источник наследования"),
            REASON,
        ]
    if action == "profile":
        names = {
            "own_delivery": "Собственная доставка",
            "stamp": "Нужна печать",
            "mercury_help": "Нужна помощь с Меркурием",
            "insurance": "Страхование",
            "psn": "ПСН",
            "instructor": "Выезд инструктора",
            "event": "Мероприятие открытия",
            "online_acquiring": "Интернет-эквайринг",
            "startup_cycle": "Стартовый цикл D+7–180",
            "marketing_followup": "Брендовый план D+90/180",
            "campaign": "Объектная кампания",
        }
        return (
            [
                ref("marketing_manager_id", "Назначенный менеджер маркетинга", "person"),
                ref("partner_person_id", "Уполномоченный ФП", "partner_person"),
            ]
            + [choice(k, v, {"yes": "Да", "no": "Нет", "unknown": "Неизвестно"}) for k, v in names.items()]
            + [REASON]
        )
    if action == "date":
        names = {
            "planned_internal": "План внутреннего тестирования",
            "planned_technical": "План технического открытия",
            "planned_official": "План официального открытия",
            "planned_event": "План мероприятия",
            "actual_internal": "Факт внутреннего тестирования",
            "actual_technical": "Факт первых продаж (D0)",
            "actual_official": "Факт официального открытия",
            "actual_event": "Факт мероприятия",
        }
        return [
            choice("date_kind", "Событие", names, True),
            f("date", "Дата и время", "datetime", True),
            f("evidence", "Основание фактического события"),
            REASON,
        ]
    if action == "remark":
        return [
            choice(
                "severity",
                "Критичность",
                {
                    "red": "Красное — блокирует открытие",
                    "yellow": "Жёлтое — устранить позднее",
                    "green": "Зелёное — рекомендация",
                },
                True,
            ),
            f("text", "Замечание", required=True),
            ref("owner_id", "Ответственный", "person", True),
            f("due_at", "Контрольная дата", "datetime", True),
        ]
    if action == "close_remark":
        return [f("evidence", "Основание устранения", required=True)]
    if action == "change":
        return [
            choice(
                "decision",
                "Решение",
                {
                    "continue": "Продолжить действующую работу",
                    "cancel": "Отменить по согласованию",
                    "reschedule": "Согласовать новый срок",
                },
                True,
            ),
            f("due_at", "Новый срок при переносе", "datetime"),
            REASON,
            f("settlement", "Условия по оплатам, заказам и внешним обязательствам", required=True),
        ]
    if action == "approval":
        return [f("approve", "Согласовать предложенное решение (нет — отклонить)", "bool", True)]
    if action == "campaign":
        return [
            f("title", "Название", required=True, maximum=250),
            f("start_at", "Первый запуск", "datetime", True),
            f("until_at", "Конец периода", "datetime", True),
            choice("interval", "Повторение", {"once": "Один раз", "monthly": "Ежемесячно"}, True),
            f("plan_reference", "Согласованный план / материалы", required=True),
            f("route_key", "Маршрут (пусто — marketing)"),
            f("control_days", "Срок исполнения в рабочих днях", "int", True, minimum=1, maximum=365),
        ]
    if action == "evidence":
        return [
            choice(
                "step",
                "Шаг",
                {
                    "specification": "Подготовить спецификацию",
                    "approve_specification": "Подтверждение спецификации ФП",
                    "ordered": "Заказ подтверждён ФП",
                    "receipt": "Получена часть поставки",
                    "confirm_receipt": "ФП подтвердил получение всего комплекта",
                    "smm_period": "Согласованный период SMM и отчёт",
                    "community_ready": "Принять готовое сообщество VK",
                    "bank_form":"Форма ИП/ООО для банка","bank_sent":"Форма передана специалистом в банк","bank_result":"Результат открытия счёта подтверждён","tax_regime":"Подтверждённый налоговый режим",
                },
                True,
            ),
            f(
                "items",
                "Позиции спецификации",
                "list",
                fields=[
                    f("key", "Код позиции", "key", True),
                    f("model", "Модель / наименование", required=True),
                    f("supplier", "Поставщик", required=True),
                    f("quantity", "Количество", "int", True, minimum=1),
                    f("price_minor", "Цена за единицу в копейках", "int", True, minimum=0),
                    f("delivery_at", "Ожидаемая поставка", "datetime", True),
                ],
            ),
            f("item_key", "Код полученной позиции"),
            f("received_quantity", "Всего получено по позиции", "int", minimum=0),
            f("evidence", "Основание / подтверждение ФП"),
            f("start_at", "Начало SMM", "datetime"),
            f("until_at", "Конец SMM", "datetime"),
            f("volume", "Объём SMM и порядок получения материалов"),
            f("plan_reference", "Контент-план"),
            f("community_url", "Ссылка на готовое сообщество VK"),
            f("admin_access", "Проверенные права администрирования"),
            f("guest_care_owner", "Ответственный за сообщения гостей"),
            f("report", "Отчёт завершённого периода"),
            choice("company_type","Организационная форма",{"ip":"ИП","llc":"ООО"}),f("company_name","Наименование ИП/ООО"),f("inn","ИНН"),f("ogrn","ОГРНИП / ОГРН"),f("contact_name","Контактное лицо"),f("phone","Телефон +код"),f("consent_evidence","Подтверждение согласия ФП на передачу контакта"),f("form_version","Версия формы","int",minimum=1),f("next_check","Следующая проверка","datetime"),f("at","Фактическая дата отправки","datetime"),choice("regime","Налоговый режим",{"usn":"УСН","psn":"ПСН","other":"Иной подтверждённый","consultation":"Нужна консультация"}),
        ]
    return []

"""Passport vocabulary and result dependencies (§17A/B)."""

from ama.ui.schema import f

MODES = {"connect": "Подключить", "verify": "Уже работает — проверить", "na": "Не применяется"}
PAYMENTS = {
    "cash": "Наличные",
    "card": "Карта на кассе",
    "portable": "Переносной терминал",
    "online": "Онлайн",
    "platform": "Оплата площадки",
    "sbp": "QR/СБП",
}
METHODS = {
    "hall": "Зал",
    "takeaway": "С собой",
    "pickup": "Самовывоз",
    "own_delivery": "Своя доставка",
    "aggregator": "Доставка площадки",
    "table_qr": "QR со стола",
    "general_qr": "Общий QR",
    "preorder": "Предзаказ",
    "drive": "Drive",
}
MESSENGERS = {"telegram": "Telegram", "max": "MAX"}
FIELDS = []


def add(key, label, kind="text", brand=False, when=None, **kw):
    FIELDS.append(dict(key=key, label=label, type=kind, brand=brand, when=when, **kw))


add("phone", "Рабочий номер предприятия")
add("phone_owner", "Ответственный за устройство", "ref", source="person")
add("phone_ready", "Рабочее устройство готово", "bool")
add("same_phone", "Публичный номер совпадает с рабочим", "bool")
add("public_phone", "Публичный номер заказов", when=("same_phone", False))
add("internet", "Интернет", "choice", choices={"ready": "Подключён", "waiting": "Ожидаем", "problem": "Есть проблема"})
add(
    "equipment",
    "Фактически полученное цифровое оборудование",
    "list",
    fields=[
        f("name", "Позиция", required=True),
        f("quantity", "Количество", "int", True, minimum=1),
        f("state", "ready / partial / waiting / mismatch", required=True),
        f("specification", "Утверждённая спецификация", "url", True),
        f("details", "Недостающие позиции / расхождения"),
    ],
)
add("equipment_approved", "Комплект проверен специалистом", "bool", staff=True)
add("stations", "Утверждённые Зоны и Станции", "url", True)
add("menu_mode", "Меню", "choice", True, choices={"base": "Базовое", "individual": "Согласованные изменения"})
add("menu_reference", "Утверждённый продуктовый документ", "url", True, ("menu_mode", "individual"))
add("menu_excluded", "Исключённые категории", "lines", True)
add("menu_added", "Дополнительные категории / позиции", "lines", True)
add("menu_apply", "Дата применения меню", "datetime", True)
add("price_mode", "Цены", "choice", True, choices={"base": "Базовые", "individual": "Индивидуальные"})
add("price_reference", "Утверждённый прайс: ссылка на документ", "url", True, ("price_mode", "individual"))
add("methods", "Способы получения", "multi", True, choices=METHODS)
add("pickup_terms", "Условия и время самовывоза: базовая конфигурация или локальные значения", brand=True)
add("own_delivery", "Есть собственная доставка", "bool", True)
add("delivery_terms", "Коммерческие условия собственной доставки", brand=True, when=("own_delivery", True))
add(
    "zones",
    "Зоны: каждая отдельным контуром Яндекс Карт",
    "list",
    True,
    ("own_delivery", True),
    fields=[
        f("name", "Название", required=True),
        f("map_url", "Отдельный контур Яндекс Карт", "url", True),
        f("minimum_minor", "Минимальный заказ, копейки", "int", True, minimum=0),
        f("delivery_minor", "Стоимость, копейки", "int", True, minimum=0),
        f("free_minor", "Бесплатно от, копейки", "int", True, minimum=0),
        f("minutes", "Время, минуты", "int", True, minimum=1),
        f("hours", "Режим", required=True),
        f("restrictions", "Ограничения"),
    ],
)
add("payments", "Способы оплаты", "multi", choices=PAYMENTS)
add("bank", "Банк-эквайер / утверждённые вводные")
add("call_center", "Обслуживание предприятия КЦ", "bool")
add("call_scheme", "Утверждённая схема звонков / переадресаций")
add("seating", "Есть гостевая посадка", "bool")
add("tables", "Количество и нумерация столов", when=("seating", True))
add("table_qr", "Нужны QR на столах", "bool", when=("seating", True))
add("table_service", "Подача к столу или выдача", when=("seating", True))
add("general_qr", "Нужен общий QR", "bool")
add(
    "screens",
    "Фактически установленные экраны",
    "list",
    fields=[
        f("kind", "menu / cash / queue / advert", required=True),
        f("quantity", "Количество", "int", True, minimum=1),
        f("material", "Утверждённый материал", "url", True),
    ],
)
for key, label, kind in [
    ("sellkit_local", "Локальные отличия от мастер-настроек / базовые", "text"),
    ("order_links", "Сайт и приложение / ссылки заказа", "lines"),
    ("photos", "Готовые фотографии / материалы", "url"),
]:
    add(key, label, kind, True)
add(
    "geocards",
    "Карточки по площадкам",
    "list",
    True,
    fields=[
        f("platform", "Площадка", required=True),
        f("mode", "existing / new", required=True),
        f("url", "Существующая ссылка", "url"),
    ],
)
add("work_phone_messenger", "Мессенджер предприятия", "choice", choices=MESSENGERS)
add("account_ready", "Рабочая учётная запись подготовлена", "bool")
add("initial_contact", "Контакт для первоначального добавления")
add("operational_chat", "Рабочий чат: реквизиты либо требуется создать")
add("management_messenger", "Мессенджер управления", "choice", choices=MESSENGERS)
add("management_chat", "Чат управления: реквизиты либо требуется создать")
add(
    "participants",
    "Первоначальные участники управления",
    "list",
    fields=[
        f("name", "Имя", required=True),
        f("roles", "Назначенные роли", "lines", True),
        f("contact", "Контакт в выбранном мессенджере", required=True),
    ],
)
add(
    "guest_links",
    "Гостевые входы бренда",
    "list",
    True,
    fields=[f("name", "Источник", required=True), f("url", "Ссылка", "url", True)],
)
add("care_sources", "Применимые источники обращений", "lines", True)
add("care_calendar", "Календарь Службы заботы", "ref", True, source="record:calendar")
add("care_escalation", "Конечный маршрут эскалации / не предусмотрен", brand=True)
for platform in ("yandex", "kuper", "chibis"):
    add(
        platform + "_terms",
        "Условия площадки " + platform,
        "object",
        True,
        fields=[
            f("markup", "Локальная наценка", required=True),
            f("minimum_minor", "Минимальный заказ, копейки", "int", True, minimum=0),
            f("minutes", "Приготовление, минуты", "int", True, minimum=1),
            f("zone", "Применимая зона / ссылка", required=True),
            f("launch_at", "Дата запуска", "datetime", True),
        ],
    )
    if platform != "chibis":
        add(
            platform + "_delivery",
            "Кто доставляет: " + platform,
            "choice",
            True,
            choices={"enterprise": "Предприятие", "platform": "Площадка"},
        )
LOOKUP = {x["key"]: x for x in FIELDS}
EXTERNAL = {
    "internet",
    "phone",
    "phone_owner",
    "phone_ready",
    "work_phone_messenger",
    "management_messenger",
    "account_ready",
    "initial_contact",
    "operational_chat",
    "management_chat",
    "participants",
    "call_center",
}
MENU = {
    "stations",
    "menu_mode",
    "menu_reference",
    "menu_excluded",
    "menu_added",
    "menu_apply",
    "price_mode",
    "price_reference",
}
ORDER = {"methods", "pickup_terms", "own_delivery", "delivery_terms", "zones"}
HW = {"internet", "equipment", "equipment_approved"}
COMPONENTS = {
    "equipment": ("Оборудование и связь", "software", HW, (), ("hardware", "network"), False),
    "iiko": (
        "iiko",
        "software",
        MENU | {"methods", "equipment"},
        ("equipment",),
        ("cash_register", "production", "test_order"),
        True,
    ),
    "zones": ("Условия и зоны", "support", ORDER, (), ("completeness",), True),
    "sellkit": (
        "SellKit, сайт и приложение",
        "software",
        MENU | ORDER | {"payments", "sellkit_local"},
        ("iiko",),
        ("enterprise", "menu_prices", "order_payment", "iiko_order_no_duplicates"),
        True,
    ),
    "telephony": (
        "Телефония / КЦ",
        "software",
        {"phone", "same_phone", "public_phone", "call_center", "call_scheme", "internet"},
        (),
        ("control_call", "routing"),
        False,
    ),
    "geo": (
        "Геокарточки",
        "reviews",
        {"phone", "same_phone", "public_phone", "geocards", "photos", "order_links"},
        (),
        ("links", "address_hours", "publication_date"),
        True,
    ),
    "pointer": ("Pointer", "reviews", {"methods", "order_links"}, (), ("enterprise", "readiness_test"), True),
    "table_qr": (
        "Столовые QR",
        "software",
        {"seating", "tables", "table_qr", "table_service"},
        ("sellkit",),
        ("destination_table", "fulfilment"),
        True,
    ),
    "general_qr": ("Общий QR", "software", {"general_qr"}, ("sellkit",), ("destination", "fulfilment"), True),
    "screens": (
        "Клиентские экраны",
        "software",
        {"screens"},
        ("equipment",),
        ("installed_device", "content_mode"),
        False,
    ),
    "ama_rs": ("АМА РС", "support", EXTERNAL, (), ("enterprise", "management", "cross_platform"), False),
    "care": (
        "Служба заботы АМА",
        "support",
        EXTERNAL | {"guest_links", "care_sources", "care_calendar", "care_escalation"},
        (),
        ("guest_scope", "enterprise_response", "management", "escalation", "sources", "cross_platform"),
        True,
    ),
}
for p in ("yandex", "kuper", "chibis"):
    COMPONENTS["agg_" + p] = (
        p,
        "software",
        MENU | {"methods", "payments", "own_delivery", p + "_terms"} | ({p + "_delivery"} if p != "chibis" else set()),
        ("iiko",),
        ("application", "account", "menu_terms", "control_order"),
        True,
    )
for p in PAYMENTS:
    COMPONENTS["payment_" + p] = (
        PAYMENTS[p],
        "software",
        {"payments", "bank", "equipment"},
        ("equipment",),
        ("registration", "mapping", "test_settlement"),
        False,
    )


def typed(key):
    x = LOOKUP[key]
    return f(
        "value",
        x["label"],
        x["type"],
        x["type"] not in {"list", "lines", "multi"},
        **{k: v for k, v in x.items() if k in {"choices", "fields", "source"}},
    )


# Registration is a separate internal responsibility; software receives only its accepted result.
for pay in ("card", "portable", "online", "sbp"):
    key = "register_" + pay
    COMPONENTS[key] = (
        "Регистрация: " + PAYMENTS[pay],
        "registrations",
        {"payments", "bank"},
        (),
        ("documents", "bank_ready"),
        False,
    )
    old = COMPONENTS["payment_" + pay]
    COMPONENTS["payment_" + pay] = (*old[:3], ("equipment", key), *old[4:])

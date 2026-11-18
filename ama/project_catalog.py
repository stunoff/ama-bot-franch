"""Versioned technical matrix for specification §§2–12 (no commercial content)."""

VERSION = 1
GROUPS = {
    "legal": "Компания и документы",
    "premises": "Помещение и аналитика",
    "design": "Проектирование",
    "registrations": "Регистрации и документы",
    "suppliers": "Поставщики и стандарты",
    "equipment": "Оснащение",
    "repair": "Ремонт",
    "personnel": "Персонал и обучение",
    "stock": "Сырьё и первая поставка",
    "digital": "Цифровая готовность",
    "readiness": "Подготовка и открытия",
    "followup": "Контроль после запуска",
    "analytics": "Маркетинговая аналитика",
    "marketing_plan": "Маркетинговый план",
    "community": "Сообщество",
    "community_design": "Дизайн сообщества",
    "start_smm": "Стартовое SMM",
    "printing": "Дизайн и печать",
    "promotion": "Продвижение",
    "campaign": "Акции и новинки",
    "event": "Мероприятие открытия",
}
MARKETING = {
    "analytics",
    "marketing_plan",
    "community",
    "community_design",
    "start_smm",
    "printing",
    "promotion",
    "campaign",
    "event",
}
# code | title | group | route | prerequisites | physical shared | conditional flag | section
_ROWS = """company|Регистрация компании|legal|registrations||1||2
 tax|Налоговый режим и предупреждение ОСНО|legal|support|company|1||2
 signature|Электронная подпись|legal|registrations|company|1||2
 bank|Расчётный счёт и анкета банка|legal|registrations|company|1||2
 edo|ЭДО|legal|registrations|signature|1||2
 franchise|Подтверждённый договор франшизы|legal|contracts||0||2
 stamp|Печать при необходимости|legal|registrations|company|1|stamp|2
 fp_training|Личное ознакомление ФП|premises|support||0||3
 premises_options|Варианты помещений: адрес, аренда, фото, трафик, коммуникации|premises|support||1||3
 city_analytics|Анализ города и локации после продажи|analytics|chief_marketer|premises_options|0||3
 premises_choice|Выбор помещения самим ФП|premises|support|premises_options|1||3
 lease|Рекомендации УК и подписанная аренда|premises|support|premises_choice|1||3
 dimensions|Обмеры и исходные данные пяти групп|design|projecting|lease|1||4
 project_bundle|Единый комплект проекта предприятия|design|projecting|dimensions|1||4
 extract|Выписка предприятия|registrations|registrations|company,lease|1||4
 mercury|ВетИС Меркурий|registrations|registrations|extract|1||4
 mercury_help|Помощь с Меркурием|registrations|registrations|extract|1|mercury_help|4
 marking|Честный знак|registrations|registrations|edo,extract|1||4
 insurance|Страхование|registrations|registrations|lease|1|insurance|4
 ses_contract|Договор санитарной обработки|registrations|support|lease|1||4
 medical_provider|Договор с медицинской организацией|registrations|support|lease|1||4
 journal_templates|Шаблоны журналов|registrations|support||1||4
 accounting|Бухгалтерское обслуживание организовано|registrations|support|tax|1||4
 goods_suppliers|Поставщики товаров|suppliers|support||1||4
 service_suppliers|Поставщики услуг|suppliers|support||1||4
 packaging_spec|Упаковка по спецификации УК|suppliers|support||0||4
 uniforms|Форма по спецификации УК|suppliers|support||0||4
 thermal_bags|Термосумки собственной доставки|suppliers|support||1|own_delivery|4
 printed_standards|Актуальный перечень печатных стандартов|suppliers|support||0||4
 kitchen_equipment|Комплект кухонного и физического оснащения|equipment|equipment|project_bundle|1||5
 systems_equipment|Комплект цифрового оборудования|equipment|systems_equipment|project_bundle|1||5
 repair_preparation|Ремонт: подготовка|repair|support|project_bundle|1||5
 repair_engineering|Ремонт: инженерные сети|repair|support|repair_preparation|1||5
 repair_finishing|Ремонт: отделка|repair|support|repair_engineering|1||5
 repair_installation|Ремонт: монтаж оснащения|repair|support|repair_finishing,kitchen_equipment|1||5
 repair_branding|Ремонт: оформление|repair|support|repair_installation|0||5
 repair_finish|Ремонт: завершение|repair|support|repair_branding|1||5
 marketing_brief|Маркетинговый бриф|marketing_plan|marketing||0||6
 purchase_prices|Файл закупочных цен|marketing_plan|menu|goods_suppliers|0||6
 menu|Меню|marketing_plan|menu|purchase_prices|0||6
 pricing|Цены и согласование ФП|marketing_plan|menu|menu|0||6
 marketing_plan|План маркетинга и каналы|marketing_plan|marketing|marketing_brief,city_analytics,pricing|0||6
 community|Создание или обновление VK: ссылка, права и наполнение|community|smm|marketing_plan|0||6
 community_design|Дизайн сообщества|community_design|design|marketing_brief|0||6
 start_smm|Стартовое SMM: подготовка и согласованный период|start_smm|smm|community,community_design|0||6
 digital|Цифровой паспорт и применимые компоненты|digital|software|systems_equipment|0||6
 guest_care|Служба заботы|digital|software|digital|0||6
 instructor|Выезд инструктора: программа, даты, расходы ФП|readiness|instructors|lease|1|instructor|7
 information_stand|Информационный стенд|readiness|support|printed_standards|1||7
 advertising_design|Дизайн рекламы|printing|design|marketing_plan|0||7
 advertising_order|Заказ печати ФП|printing|printing|advertising_design|0||7
 advertising_received|Получение рекламы|printing|printing|advertising_order|0||7
 advertising_placement|Размещение, фото и приёмка|printing|advertising|advertising_received|0||7
 menu_media|Печатное меню и экраны|readiness|support|pricing|0||7
 psn|ПСН при применимости|registrations|support|lease,tax|1|psn|8
 personnel_plan|План смен и штат|personnel|personnel||1||8
 personnel_search|Поиск персонала|personnel|personnel|personnel_plan|1||8
 personnel_hired|Найм персонала|personnel|personnel|personnel_search|1||8
 personnel_theory|Теория персонала|personnel|personnel|personnel_hired|1||8
 personnel_practice|Практика персонала|personnel|systems_training|personnel_theory|1||8
 medical_documents|Медицинские документы: сводный статус|personnel|support|medical_provider,personnel_hired|1||8
 training_stock|Сырьё для обучения|stock|support|goods_suppliers|1||8
 initial_stock|Первоначальная закупка сырья|stock|support|goods_suppliers|1||8
 packaging_stock|Первоначальная закупка упаковки|stock|support|packaging_spec|0||8
 household_stock|Хозяйственные материалы|stock|support|service_suppliers|1||8
 delivery_schedule|Файл графика поставок|stock|support|goods_suppliers|1||8
 first_iiko_delivery|Первая поставка в iiko: единая проверка|stock|software|initial_stock,digital|1||8
 initial_inventory|Первоначальная инвентаризация|stock|support|first_iiko_delivery|1||8
 sanitation|Фактическая санитарная обработка|readiness|support|repair_finish,ses_contract|1||8
 cleanup|Уборка|readiness|support|sanitation|1||8
 arrangement|Расстановка и подготовка зон|readiness|support|cleanup|1||8
 zone_photos|Фото применимых зон|readiness|support|arrangement|1||9
 filled_journals|Заполненные журналы|readiness|support|journal_templates|1||9
 delivery_channels|Каналы доставки и геосервисы|promotion|advertising|digital,marketing_plan|0||9
 readiness|Сводная проверка обязательных условий|readiness|support|fp_training,zone_photos,filled_journals,accounting,medical_documents,personnel_practice,initial_inventory,information_stand,menu_media,guest_care,franchise|0||9
 internal_opening|Внутреннее тестирование|readiness|support|readiness|0||10
 technical_opening|Техническое открытие: первые реальные продажи|readiness|support|internal_opening|0||10
 official_opening|Официальное открытие и усиленное сопровождение|readiness|support|technical_opening|0||10
 event|Отдельное мероприятие открытия|event|marketing|marketing_plan|0|event|10
 online_acquiring|Интернет-эквайринг при применимости|digital|software|digital|0|online_acquiring|10
 support|Постоянное сопровождение предприятия|followup|support||0||12
 campaign|Применимые акции и новинки|campaign|marketing||0|campaign|12"""
CATALOG = {}
for row in _ROWS.strip().splitlines():
    code, title, group, route, deps, shared, flag, section = row.strip().split("|")
    CATALOG[code] = dict(
        code=code,
        title=title,
        group=group,
        route=route,
        depends_on=deps.split(",") if deps else [],
        shared=shared == "1",
        flag=flag or None,
        section=section,
        lane="marketing" if group in MARKETING else "curator",
    )
for day in (7, 14, 30, 60, 90, 180):
    code = f"d{day}"
    CATALOG[code] = dict(
        code=code,
        title=f"Контроль D+{day}",
        group="followup",
        route="support",
        depends_on=[],
        shared=False,
        flag="startup_cycle",
        section="11",
        lane="curator",
        anchor="actual_technical",
        offset=day,
    )
for day in (90, 180):
    code = f"marketing_d{day}"
    CATALOG[code] = dict(
        code=code,
        title=f"Маркетинговый контроль D+{day}",
        group="promotion",
        route="marketing",
        depends_on=[],
        shared=False,
        flag="marketing_followup",
        section="11",
        lane="marketing",
        anchor="actual_technical",
        offset=day,
    )
CATALOG["event"].update(anchor="planned_event", offset=0)
CATALOG["readiness"]["depends_on"] += ["community", "community_design", "start_smm", "delivery_channels"]


def default_mode(item, scenario):
    if scenario == "existing":
        return "perform" if item["code"] == "support" else "not_applicable"
    if scenario == "additional_brand" and item["shared"]:
        return (
            "inherit"
            if item["code"]
            in {
                "company",
                "tax",
                "signature",
                "bank",
                "edo",
                "lease",
                "premises_options",
                "premises_choice",
                "ses_contract",
                "medical_provider",
                "journal_templates",
                "accounting",
                "goods_suppliers",
                "service_suppliers",
            }
            else "verify"
        )
    if scenario == "reconstruction":
        return (
            "verify"
            if item["group"] in {"design", "repair", "equipment", "digital", "readiness"}
            and item["code"] not in {"internal_opening", "technical_opening", "official_opening"}
            else "not_applicable"
        )
    if scenario == "technical" and item["code"] in {"internal_opening", "technical_opening"}:
        return "inherit"
    return "perform"

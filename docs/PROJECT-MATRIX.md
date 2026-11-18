# Матрица проектных блоков

Техническая редакция 1; 90 блоков. Подробные условия — PROJECTS-0.4.0.md.

| Код | Результат | Раздел | Контур | Общая площадка | Маршрут | Зависимости | Условие |
|---|---|---|---|---|---|---|---|
| company | Регистрация компании | 2 | curator | да | registrations | — | — |
| tax | Налоговый режим и предупреждение ОСНО | 2 | curator | да | support | company | — |
| signature | Электронная подпись | 2 | curator | да | registrations | company | — |
| bank | Расчётный счёт и анкета банка | 2 | curator | да | registrations | company | — |
| edo | ЭДО | 2 | curator | да | registrations | signature | — |
| franchise | Подтверждённый договор франшизы | 2 | curator | нет | contracts | — | — |
| stamp | Печать при необходимости | 2 | curator | да | registrations | company | stamp |
| fp_training | Личное ознакомление ФП | 3 | curator | нет | support | — | — |
| premises_options | Варианты помещений: адрес, аренда, фото, трафик, коммуникации | 3 | curator | да | support | — | — |
| city_analytics | Анализ города и локации после продажи | 3 | marketing | нет | chief_marketer | premises_options | — |
| premises_choice | Выбор помещения самим ФП | 3 | curator | да | support | premises_options | — |
| lease | Рекомендации УК и подписанная аренда | 3 | curator | да | support | premises_choice | — |
| dimensions | Обмеры и исходные данные пяти групп | 4 | curator | да | projecting | lease | — |
| project_bundle | Единый комплект проекта предприятия | 4 | curator | да | projecting | dimensions | — |
| extract | Выписка предприятия | 4 | curator | да | registrations | company, lease | — |
| mercury | ВетИС Меркурий | 4 | curator | да | registrations | extract | — |
| mercury_help | Помощь с Меркурием | 4 | curator | да | registrations | extract | mercury_help |
| marking | Честный знак | 4 | curator | да | registrations | edo, extract | — |
| insurance | Страхование | 4 | curator | да | registrations | lease | insurance |
| ses_contract | Договор санитарной обработки | 4 | curator | да | support | lease | — |
| medical_provider | Договор с медицинской организацией | 4 | curator | да | support | lease | — |
| journal_templates | Шаблоны журналов | 4 | curator | да | support | — | — |
| accounting | Бухгалтерское обслуживание организовано | 4 | curator | да | support | tax | — |
| goods_suppliers | Поставщики товаров | 4 | curator | да | support | — | — |
| service_suppliers | Поставщики услуг | 4 | curator | да | support | — | — |
| packaging_spec | Упаковка по спецификации УК | 4 | curator | нет | support | — | — |
| uniforms | Форма по спецификации УК | 4 | curator | нет | support | — | — |
| thermal_bags | Термосумки собственной доставки | 4 | curator | да | support | — | own_delivery |
| printed_standards | Актуальный перечень печатных стандартов | 4 | curator | нет | support | — | — |
| kitchen_equipment | Комплект кухонного и физического оснащения | 5 | curator | да | equipment | project_bundle | — |
| systems_equipment | Комплект цифрового оборудования | 5 | curator | да | systems_equipment | project_bundle | — |
| repair_preparation | Ремонт: подготовка | 5 | curator | да | support | project_bundle | — |
| repair_engineering | Ремонт: инженерные сети | 5 | curator | да | support | repair_preparation | — |
| repair_finishing | Ремонт: отделка | 5 | curator | да | support | repair_engineering | — |
| repair_installation | Ремонт: монтаж оснащения | 5 | curator | да | support | repair_finishing, kitchen_equipment | — |
| repair_branding | Ремонт: оформление | 5 | curator | нет | support | repair_installation | — |
| repair_finish | Ремонт: завершение | 5 | curator | да | support | repair_branding | — |
| marketing_brief | Маркетинговый бриф | 6 | marketing | нет | marketing | — | — |
| purchase_prices | Файл закупочных цен | 6 | marketing | нет | menu | goods_suppliers | — |
| menu | Меню | 6 | marketing | нет | menu | purchase_prices | — |
| pricing | Цены и согласование ФП | 6 | marketing | нет | menu | menu | — |
| marketing_plan | План маркетинга и каналы | 6 | marketing | нет | marketing | marketing_brief, city_analytics, pricing | — |
| community | Создание или обновление VK: ссылка, права и наполнение | 6 | marketing | нет | smm | marketing_plan | — |
| community_design | Дизайн сообщества | 6 | marketing | нет | design | marketing_brief | — |
| start_smm | Стартовое SMM: подготовка и согласованный период | 6 | marketing | нет | smm | community, community_design | — |
| digital | Цифровой паспорт и применимые компоненты | 6 | curator | нет | software | systems_equipment | — |
| guest_care | Служба заботы | 6 | curator | нет | software | digital | — |
| instructor | Выезд инструктора: программа, даты, расходы ФП | 7 | curator | да | instructors | lease | instructor |
| information_stand | Информационный стенд | 7 | curator | да | support | printed_standards | — |
| advertising_design | Дизайн рекламы | 7 | marketing | нет | design | marketing_plan | — |
| advertising_order | Заказ печати ФП | 7 | marketing | нет | printing | advertising_design | — |
| advertising_received | Получение рекламы | 7 | marketing | нет | printing | advertising_order | — |
| advertising_placement | Размещение, фото и приёмка | 7 | marketing | нет | advertising | advertising_received | — |
| menu_media | Печатное меню и экраны | 7 | curator | нет | support | pricing | — |
| psn | ПСН при применимости | 8 | curator | да | support | lease, tax | psn |
| personnel_plan | План смен и штат | 8 | curator | да | personnel | — | — |
| personnel_search | Поиск персонала | 8 | curator | да | personnel | personnel_plan | — |
| personnel_hired | Найм персонала | 8 | curator | да | personnel | personnel_search | — |
| personnel_theory | Теория персонала | 8 | curator | да | personnel | personnel_hired | — |
| personnel_practice | Практика персонала | 8 | curator | да | systems_training | personnel_theory | — |
| medical_documents | Медицинские документы: сводный статус | 8 | curator | да | support | medical_provider, personnel_hired | — |
| training_stock | Сырьё для обучения | 8 | curator | да | support | goods_suppliers | — |
| initial_stock | Первоначальная закупка сырья | 8 | curator | да | support | goods_suppliers | — |
| packaging_stock | Первоначальная закупка упаковки | 8 | curator | нет | support | packaging_spec | — |
| household_stock | Хозяйственные материалы | 8 | curator | да | support | service_suppliers | — |
| delivery_schedule | Файл графика поставок | 8 | curator | да | support | goods_suppliers | — |
| first_iiko_delivery | Первая поставка в iiko: единая проверка | 8 | curator | да | software | initial_stock, digital | — |
| initial_inventory | Первоначальная инвентаризация | 8 | curator | да | support | first_iiko_delivery | — |
| sanitation | Фактическая санитарная обработка | 8 | curator | да | support | repair_finish, ses_contract | — |
| cleanup | Уборка | 8 | curator | да | support | sanitation | — |
| arrangement | Расстановка и подготовка зон | 8 | curator | да | support | cleanup | — |
| zone_photos | Фото применимых зон | 9 | curator | да | support | arrangement | — |
| filled_journals | Заполненные журналы | 9 | curator | да | support | journal_templates | — |
| delivery_channels | Каналы доставки и геосервисы | 9 | marketing | нет | advertising | digital, marketing_plan | — |
| readiness | Сводная проверка обязательных условий | 9 | curator | нет | support | fp_training, zone_photos, filled_journals, accounting, medical_documents, personnel_practice, initial_inventory, information_stand, menu_media, guest_care, franchise, community, community_design, start_smm, delivery_channels | — |
| internal_opening | Внутреннее тестирование | 10 | curator | нет | support | readiness | — |
| technical_opening | Техническое открытие: первые реальные продажи | 10 | curator | нет | support | internal_opening | — |
| official_opening | Официальное открытие и усиленное сопровождение | 10 | curator | нет | support | technical_opening | — |
| event | Отдельное мероприятие открытия | 10 | marketing | нет | marketing | marketing_plan | event |
| online_acquiring | Интернет-эквайринг при применимости | 10 | curator | нет | software | digital | online_acquiring |
| support | Постоянное сопровождение предприятия | 12 | curator | нет | support | — | — |
| campaign | Применимые акции и новинки | 12 | marketing | нет | marketing | — | campaign |
| d7 | Контроль D+7 | 11 | curator | нет | support | — | startup_cycle |
| d14 | Контроль D+14 | 11 | curator | нет | support | — | startup_cycle |
| d30 | Контроль D+30 | 11 | curator | нет | support | — | startup_cycle |
| d60 | Контроль D+60 | 11 | curator | нет | support | — | startup_cycle |
| d90 | Контроль D+90 | 11 | curator | нет | support | — | startup_cycle |
| d180 | Контроль D+180 | 11 | curator | нет | support | — | startup_cycle |
| marketing_d90 | Маркетинговый контроль D+90 | 11 | marketing | нет | marketing | — | marketing_followup |
| marketing_d180 | Маркетинговый контроль D+180 | 11 | marketing | нет | marketing | — | marketing_followup |

## Режим по умолчанию

| Код | Новое | Новый бренд | Действующее | Технический режим | Реконструкция |
|---|---|---|---|---|---|
| company | perform | inherit | not_applicable | perform | not_applicable |
| tax | perform | inherit | not_applicable | perform | not_applicable |
| signature | perform | inherit | not_applicable | perform | not_applicable |
| bank | perform | inherit | not_applicable | perform | not_applicable |
| edo | perform | inherit | not_applicable | perform | not_applicable |
| franchise | perform | perform | not_applicable | perform | not_applicable |
| stamp | perform | verify | not_applicable | perform | not_applicable |
| fp_training | perform | perform | not_applicable | perform | not_applicable |
| premises_options | perform | inherit | not_applicable | perform | not_applicable |
| city_analytics | perform | perform | not_applicable | perform | not_applicable |
| premises_choice | perform | inherit | not_applicable | perform | not_applicable |
| lease | perform | inherit | not_applicable | perform | not_applicable |
| dimensions | perform | verify | not_applicable | perform | verify |
| project_bundle | perform | verify | not_applicable | perform | verify |
| extract | perform | verify | not_applicable | perform | not_applicable |
| mercury | perform | verify | not_applicable | perform | not_applicable |
| mercury_help | perform | verify | not_applicable | perform | not_applicable |
| marking | perform | verify | not_applicable | perform | not_applicable |
| insurance | perform | verify | not_applicable | perform | not_applicable |
| ses_contract | perform | inherit | not_applicable | perform | not_applicable |
| medical_provider | perform | inherit | not_applicable | perform | not_applicable |
| journal_templates | perform | inherit | not_applicable | perform | not_applicable |
| accounting | perform | inherit | not_applicable | perform | not_applicable |
| goods_suppliers | perform | inherit | not_applicable | perform | not_applicable |
| service_suppliers | perform | inherit | not_applicable | perform | not_applicable |
| packaging_spec | perform | perform | not_applicable | perform | not_applicable |
| uniforms | perform | perform | not_applicable | perform | not_applicable |
| thermal_bags | perform | verify | not_applicable | perform | not_applicable |
| printed_standards | perform | perform | not_applicable | perform | not_applicable |
| kitchen_equipment | perform | verify | not_applicable | perform | verify |
| systems_equipment | perform | verify | not_applicable | perform | verify |
| repair_preparation | perform | verify | not_applicable | perform | verify |
| repair_engineering | perform | verify | not_applicable | perform | verify |
| repair_finishing | perform | verify | not_applicable | perform | verify |
| repair_installation | perform | verify | not_applicable | perform | verify |
| repair_branding | perform | perform | not_applicable | perform | verify |
| repair_finish | perform | verify | not_applicable | perform | verify |
| marketing_brief | perform | perform | not_applicable | perform | not_applicable |
| purchase_prices | perform | perform | not_applicable | perform | not_applicable |
| menu | perform | perform | not_applicable | perform | not_applicable |
| pricing | perform | perform | not_applicable | perform | not_applicable |
| marketing_plan | perform | perform | not_applicable | perform | not_applicable |
| community | perform | perform | not_applicable | perform | not_applicable |
| community_design | perform | perform | not_applicable | perform | not_applicable |
| start_smm | perform | perform | not_applicable | perform | not_applicable |
| digital | perform | perform | not_applicable | perform | verify |
| guest_care | perform | perform | not_applicable | perform | verify |
| instructor | perform | verify | not_applicable | perform | verify |
| information_stand | perform | verify | not_applicable | perform | verify |
| advertising_design | perform | perform | not_applicable | perform | not_applicable |
| advertising_order | perform | perform | not_applicable | perform | not_applicable |
| advertising_received | perform | perform | not_applicable | perform | not_applicable |
| advertising_placement | perform | perform | not_applicable | perform | not_applicable |
| menu_media | perform | perform | not_applicable | perform | verify |
| psn | perform | verify | not_applicable | perform | not_applicable |
| personnel_plan | perform | verify | not_applicable | perform | not_applicable |
| personnel_search | perform | verify | not_applicable | perform | not_applicable |
| personnel_hired | perform | verify | not_applicable | perform | not_applicable |
| personnel_theory | perform | verify | not_applicable | perform | not_applicable |
| personnel_practice | perform | verify | not_applicable | perform | not_applicable |
| medical_documents | perform | verify | not_applicable | perform | not_applicable |
| training_stock | perform | verify | not_applicable | perform | not_applicable |
| initial_stock | perform | verify | not_applicable | perform | not_applicable |
| packaging_stock | perform | perform | not_applicable | perform | not_applicable |
| household_stock | perform | verify | not_applicable | perform | not_applicable |
| delivery_schedule | perform | verify | not_applicable | perform | not_applicable |
| first_iiko_delivery | perform | verify | not_applicable | perform | not_applicable |
| initial_inventory | perform | verify | not_applicable | perform | not_applicable |
| sanitation | perform | verify | not_applicable | perform | verify |
| cleanup | perform | verify | not_applicable | perform | verify |
| arrangement | perform | verify | not_applicable | perform | verify |
| zone_photos | perform | verify | not_applicable | perform | verify |
| filled_journals | perform | verify | not_applicable | perform | verify |
| delivery_channels | perform | perform | not_applicable | perform | not_applicable |
| readiness | perform | perform | not_applicable | perform | verify |
| internal_opening | perform | perform | not_applicable | inherit | not_applicable |
| technical_opening | perform | perform | not_applicable | inherit | not_applicable |
| official_opening | perform | perform | not_applicable | perform | not_applicable |
| event | perform | perform | not_applicable | perform | not_applicable |
| online_acquiring | perform | perform | not_applicable | perform | verify |
| support | perform | perform | perform | perform | not_applicable |
| campaign | perform | perform | not_applicable | perform | not_applicable |
| d7 | perform | perform | not_applicable | perform | not_applicable |
| d14 | perform | perform | not_applicable | perform | not_applicable |
| d30 | perform | perform | not_applicable | perform | not_applicable |
| d60 | perform | perform | not_applicable | perform | not_applicable |
| d90 | perform | perform | not_applicable | perform | not_applicable |
| d180 | perform | perform | not_applicable | perform | not_applicable |
| marketing_d90 | perform | perform | not_applicable | perform | not_applicable |
| marketing_d180 | perform | perform | not_applicable | perform | not_applicable |

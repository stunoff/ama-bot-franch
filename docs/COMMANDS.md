# Команды и настройка

SLA 0.5.0: `/sla` → сигналы контроля в управляющем чате для `support_head` и отдельные сигналы куратора в сопровождении. В карточке: «Подключить SLA» для прежней работы, «Плановая дата результата», «Данные комплектны», «Содержательный статус», «Восстановление / обходной способ». [SLA-0.5.0.md](SLA-0.5.0.md).

Проекты 0.4.0: `/projects` → «Новый проект…» в чате сопровождения. [PROJECTS-0.4.0.md](PROJECTS-0.4.0.md). Старый `/project` с JSON сохраняет прежний импортный формат; он не создаёт автоматически новый план из 90 блоков.

Продажи 0.3.0: [SALES-0.3.0.md](SALES-0.3.0.md). Новые интересы и договорные проверки используют кнопки; старые JSON-примеры не описывают все новые обязательные поля.

Основной интерфейс с версии 0.2.0 — кнопки и формы: [CHAT_UI.md](CHAT_UI.md). Ниже сохранён совместимый формат команд для импорта и технического обслуживания.

Все действия используют аккаунт отправителя webhook; передать произвольный actor/person_id для бизнес-действия нельзя. Даты — ISO 8601 с часовым поясом. Суммы — целые копейки.

## Партнёр

`/start`, `/tasks`, `/work ID`, `/enterprises`, `/new`, `/training`, `/knowledge`, `/files ID`.

«Задать вопрос» открывает мастер с кнопками и предпросмотром. Файл: подпись `/file REQUEST_ID`. Для Telegram-альбома достаточно одной подписи. Фото, пришедшие раньше подписи, ожидают контекст до 30 секунд. Без достоверной связи материалы не прикрепляются к последней задаче.

Дополнительный формат:

```text
/request {"establishment_id":"EST_ID","kind":"support","title":"Нужна помощь с поставкой"}
```

Для общего вопроса `establishment_id` отсутствует. Партнёр/бренд берутся из текущего чата, ответственные — из утверждённого маршрута.

## Администратор в управляющем чате

`/af_settings` показывает реестры. Изменения:

```text
/af_settings {"type":"person","data":{"name":"Имя сотрудника"}}
/af_settings {"type":"account","data":{"person_id":"PERSON_ID","platform":"telegram","external_id":"REAL_USER_ID"}}
/af_settings {"type":"role","data":{"person_id":"PERSON_ID","name":"curator","partner_id":"PARTNER_ID"}}
/af_settings {"type":"entity","data":{"kind":"partner","name":"Название ФП"}}
/af_settings {"type":"entity","data":{"kind":"establishment","name":"Предприятие","partner_id":"PARTNER_ID"}}
/af_settings {"type":"link_brand","data":{"establishment_id":"EST_ID","brand_id":"burger"}}
```

Ответ команды содержит внутренний ID новой записи. Роли: `admin`, `partner`, `observer`, `curator`, `support_head`, `marketing_manager`, `specialist`, `contract`, `consultant`, `chief_marketer`. Область — `partner_id`, `brand_id`, `establishment_id`; клиентская роль обязательно ограничена партнёром. Отзыв: `type: revoke_role`, `data.id` назначения. Последнего администратора и назначение с открытыми работами удалить нельзя.

Бренды после seed: `burger`, `sushi`, `grill`, `pizza`, `cheburek`, `shawarma`. Два бренда одной площадки — две связи с одним establishment.

```text
/af_settings {"type":"binding","data":{"platform":"telegram","chat_id":"REAL_CHAT_ID","purpose":"support"}}
/af_settings {"type":"probe_binding","data":{"id":"BINDING_ID","nonce":"UNIQUE_NONCE"}}
/af_settings {"type":"activate_binding","data":{"id":"BINDING_ID","version":1}}
```

Активация — после успешной доставки probe. Для клиентского чата: `purpose: client`, `partner_id`, `brand_id`; ФП должен быть назначен. Telegram-тема задаётся `topic_key`. В MAX логическая тема обозначается ID работы внутри общего чата. Несколько функций одного Telegram-чата можно разместить по темам; в MAX у физического чата одна основная функциональная привязка.

Основные purpose: `management`, `support`, `marketing`, `sales`, `contracts`, `chief_marketer`. Профильные: `projecting`, `software`, `technical_support`, `personnel`, `menu`, `design`, `advertising`, `printing`, `systems_training`, `smm`.

Все справочники меняются командой `type: record`:

```text
/af_settings {"type":"record","data":{"kind":"calendar","key":"support","owner_id":"OWNER_ID","published":true,"value":{"timezone":"Europe/Moscow","weekdays":[0,1,2,3,4],"start":"11:00","end":"20:00","holidays":[],"extra_workdays":[]}}}
/af_settings {"type":"record","data":{"kind":"route","key":"support","owner_id":"OWNER_ID","published":true,"value":{"binding_id":"SUPPORT_BINDING","owner_id":"CURATOR_ID","reviewer_id":"CURATOR_ID","calendar":"support"}}}
```

Дни примера не утверждены за заказчика. Продажи используют календарь с ключом `sales`, стартовые часы ТЗ 11–20 МСК. Для маркетинга создайте route `marketing` с профильным менеджером. Незаполненный/непроверенный маршрут не публикуется.

```text
/af_settings {"type":"record","data":{"kind":"consent","key":"consent-v1","owner_id":"OWNER_ID","published":true,"value":{"text":"УТВЕРЖДЁННЫЙ ЮРИСТОМ ТЕКСТ","privacy_url":"https://YOUR_DOMAIN/privacy"}}}
/af_settings {"type":"record","data":{"kind":"material","key":"MATERIAL_KEY","owner_id":"OWNER_ID","published":true,"value":{"title":"Название","url":"https://REAL_URL","audience":["partner"],"brand_id":"burger"}}}
```

У общего материала нет brand_id. Внутренние материалы получают соответствующие внутренние роли в `audience`. Для изменения существующей записи добавляйте текущую `version`. Архивация действующего материала/курса/маршрута блокируется; используйте согласованную замену.

Курс `course/general` после seed — черновик. `value.cards` содержит массив из `data/course-general.json`; `required_materials` — ключи обязательных опубликованных материалов. Владелец должен сверить карточки, привязать реальные ссылки и опубликовать версию. Курс КЦ публикуется отдельно как `course/call_center`; применимость устанавливается в данных партнёра `call_center_applicable:true`. Техническую готовность это не подтверждает.

Большие записи и пачки настроек удобнее загружать с сервера. `configuration.json` — JSON-массив объектов, обычно следующих после `/af_settings`:

```bash
docker compose exec -T api python -m ama.cli configure /dev/stdin < configuration.json
```

Пачка атомарна. CLI привилегирован: доступ к Docker должен быть только у владельца инфраструктуры.

Смена чата: `replace_binding`, данные `old_id`, `new_id`, текущая `version` старого. Новая привязка должна пройти probe и иметь ту же область/назначение. Работы и недоставленные сообщения переносятся, исходные даты не меняются, старые действия отключаются.

Обновление сущности: `entity_update`, `id`, `version`, `value` с изменяемыми полями `Entity.data`. Так задаются, например, `sales_available`, `knowledge_available`, окно уведомлений и применимость КЦ.

## Карточки сотрудников

```text
/create {"kind":"support","title":"Проверить исходные данные","partner_id":"PARTNER_ID","establishment_id":"EST_ID","brand_id":"burger","owner_id":"EXECUTOR_ID","reviewer_id":"REVIEWER_ID","partner_person_id":"FP_PERSON_ID","binding_id":"WORK_BINDING_ID","client_binding_id":"CLIENT_BINDING_ID","calendar":"support","due_at":"2030-01-10T15:00:00+03:00"}
/act {"id":"REQUEST_ID","version":1,"action":"take"}
/act {"id":"REQUEST_ID","version":2,"action":"submit"}
/act {"id":"REQUEST_ID","version":3,"action":"accept"}
/act {"id":"REQUEST_ID","version":3,"action":"reject","data":{"reason":"Исправить пункт","due_at":"2030-01-15T15:00:00+03:00"}}
```

Даты здесь только иллюстрируют формат. Самостоятельный пункт — работа с `parent_id`. `data.depends_on` — ID обязательных зависимостей. Ожидание: `wait_partner/wait_external/wait_uk` с `party`, `required`, `next_check`. Возобновление: `resume` с `due_at`. Отмена: `cancel` с `reason` (куратор/руководитель).

Передача: `handoff` с `person_id`, `binding_id`; новый исполнитель подтверждает `handoff_accept` в новом рабочем чате. До подтверждения владелец не меняется.

`/project`: поля `partner_id`, `establishment_id`, при необходимости `brand_id`, стабильный `project_id`, `scenario: new/additional_brand/existing/technical/reconstruction`, `confirmed`, `blocks`. Блок: `key`, `mode: perform/verify/inherit/not_applicable`, остальные поля как у `/create`. Для общего объектного блока `shared:true`. `confirmed:false` даёт предпросмотр без рассылки; `true` создаёт только отсутствующие применимые работы. Действующий объект без изменений получает пустой blocks.

## Профильные факты

`/op {"id":"REQUEST_ID","version":N,"action":"ACTION","data":{...}}`.

| action | Назначение и обязательные поля |
|---|---|
| offer | paid_work; `scope`, `amount_minor`, `valid_until`, `start_rule: partner_confirmation/partner_payment_evidence`, `deadline`; только разрешённые платные чаты |
| offer_accept | назначенный ФП; `offer_version`, при платёжном правиле `amount_minor`, `evidence`; другая/частичная/поздняя сумма не запускает работу |
| invoice | debt, договорный ответственный; `amount_minor`, `document` |
| debt_payment | debt; `amount_minor`, уникальная `bank_reference` |
| deferral | debt; `due_at`, `approval` |
| passport | digital; независимые `work_phone_messenger`, `management_messenger`; `systems`, `channels`, `equipment`, `zones`, `prices`, `address`, `phone`, `call_center_applicable` |
| tech_confirm | назначенный специалист; `component`, `evidence`, `control_test` |
| opening_confirm | назначенный куратор, карточка допуска; `evidence`; нужны ознакомление и принятые зависимости |

Внешние системы подтверждаются вручную по фактической проверке. Машинного обмена с АМА РС/Службой заботы нет. Допуск не заменяет последующую сдачу/приёмку результата. Автоматических претензий и денежных возвратов нет.

## Продажи

Консультант — чат `sales`, договорный сотрудник — `contracts`.

`/sales`: `id`, `version`, `action`, `data`. Действия: `take`, `qualify`, `stage`, `wait_lead`, `not_now`, `no_answer`, `refuse`, `reopen`. Ожидание требует `next_contact`; отказ — `reason`; реактивация — `consent_evidence`.

Квалификация: `name`, `contact`, `city`, `brand_id`, `budget`, `start_period`, `experience`, `premises`, `commercial_key`, `commercial_version`, `intent_confirmed`, `requisites` либо `request_requisites`, `exceptions`. Этапы: `new`, `qualification`, `presentation`, `financial_review`, `contract`; последний устанавливается подтверждённой передачей.

Коммерческая запись — опубликованный `record/commercial`, в value — `brand_id`, `required_minor` и утверждённые условия. Бот не выдумывает экономику.

```text
/lead_reply {"id":"LEAD_ID","text":"Явно подтверждённый ответ клиенту","touch_reason":"consultant_reply"}
/contract {"id":"LEAD_ID","version":N,"data":{"confirm":true}}
/deal {"id":"DEAL_ID","version":N,"action":"signed","data":{"evidence":"Подтверждение подписей"}}
/deal {"id":"DEAL_ID","version":N,"action":"payment","data":{"amount_minor":10000,"bank_reference":"УНИКАЛЬНАЯ_ОПЕРАЦИЯ"}}
/deal {"id":"DEAL_ID","version":N,"action":"confirm"}
```

Редакция комплекта: `package`, `document_reference`; старые подписи не подтверждают новую. `assign_curator`: `curator_id`, `partner_id`; затем назначенный куратор выполняет `curator_accept`. Успех остаётся отдельным фактом.

Автокасания после доставленного `/lead_reply` требуют календарь `sales` и опубликованные `template` с ключами `lead.REASON.1/2`, значением `{"text":"Утверждённый текст"}`. Причины: `onboarding`, `auto_answer`, `consultant_reply`, `clarification`, `call_time`, `materials`. Шаблоны не создаются с вымышленным содержанием. Лимит двух сообщений не сбрасывается сообщением бота; ответ лида отменяет очередь, `/stop` запрещает контакты. Ручная дата и договор подавляют автоматику. Автопрезентация, недозвоны и внутренние продажи SLA имеют неполное покрытие — см. COVERAGE.

## Рассылка

У партнёра заполните `notification_window` в формате календаря, с согласованными часовым поясом/днями/окном.

```text
/broadcast {"text":"Утверждённая публикация","brand_id":"burger"}
/broadcast {"action":"publish","id":"BROADCAST_ID","version":1}
```

Предпросмотр фиксирует аудиторию. Изменённая привязка требует нового предпросмотра. Повтор публикации не создаёт второй пакет. Сейчас это информационная рассылка; задания по объектам и общие подтверждения ФП пока не реализованы.

"""Typed financial facts and scoped work queues; no external signature/bank calls."""

from sqlalchemy import select
from ama import finance as f
from ama.models import FinanceAgreement, FinanceObligation, FinanceBatch, FinanceSchedule, Work, Binding, Entity
from ama.errors import AccessDenied, BusinessError
from ama.ui import business, access
from ama.ui.engine import button as B
from ama.ui.controller import show, paginate, navigation, draft_screen
from ama.ui.schema import f as F, ref, choice

ACTIONS = {
    "extra_result_accept": "Принять результат услуги",
    "extra_result_reject": "Вернуть результат с замечанием",
    "package": "Актуальный комплект",
    "clarify": "Запросить уточнение",
    "reply_clarification": "Ответить на уточнение",
    "invitation_sent": "Приглашение ЭДО отправлено",
    "invitation_accepted": "Приглашение ЭДО принято",
    "edo_sent": "Комплект отправлен в ЭДО",
    "signed_uk": "Подпись УК подтверждена",
    "signed_partner": "Подпись ФП подтверждена",
    "activate": "Подтвердить договорные обязательства",
    "suspend": "Остановить будущие начисления",
    "obligation": "Добавить обязательство",
    "receipt": "Подтвердить поступление банка",
    "invoice": "Счёт подготовлен / отправлен",
    "allocate": "Распределить поступление",
    "open_debt": "Зафиксировать задолженность",
    "debt_curator": "Передать куратору на 7 дней",
    "debt_curator_reply": "Результат общения куратора",
    "debt_formal": "Уведомление по почте и ЭДО",
    "debt_pretrial": "Досудебное уведомление",
    "debt_legal": "Передать юристу",
    "debt_legal_accept": "Принять юридическую передачу",
    "debt_close": "Оплата проверена / закрыть",
    "debt_dispute": "Зафиксировать спор",
    "decision_request": "Запрос решения / правовой оценки",
    "decision_record": "Зафиксировать решение",
    "deferral": "Применить адресную отсрочку",
    "batch_finish": "Подтвердить отправку всех счетов",
    "schedule_stop": "Остановить календарь",
    "extra_accept": "Согласование / подтверждение оплаты услуги",
    "extra_claim": "Отказ / спор / отмена",
}


def fields(kind):
    a = kind.split(":")[1]
    evidence = [
        F("evidence", "Подтверждение фактического события", required=True),
        F("at", "Фактическая дата", "datetime", True),
    ]
    if a == "request":
        return [
            ref("partner_id", "ФП", "partner", True),
            ref("brand_id", "Бренд", "brand"),
            ref("establishment_id", "Предприятие", "establishment"),
            F("party_details", "Реквизиты или точный список отсутствующего", required=True),
            F("terms", "Согласованные условия", required=True),
            F("basis", "Основание условий", required=True),
            F("due_at", "Срок результата", "datetime", True),
        ]
    if a == "package":
        return [
            F("document_reference", "Подготовленная актуальная редакция", required=True),
            F("reason", "Причина редакции", required=True),
        ]
    if a == "clarify":
        return [
            F("missing", "Какие сведения отсутствуют", required=True),
            F("due_at", "Контроль уточнения", "datetime", True),
        ]
    if a == "reply_clarification":
        return [F("answer", "Подтверждённые сведения / ответ", required=True)]
    if a in {"invitation_sent", "invitation_accepted", "edo_sent"}:
        return evidence
    if a in {"signed_uk", "signed_partner"}:
        return [F("package", "Версия подписанного комплекта", "int", True, minimum=1), *evidence]
    if a == "activate":
        return [
            choice(
                "purpose",
                "Основание платежа",
                {
                    "franchise": "Франшизное обязательство",
                    "royalty": "Регулярный платёж / роялти",
                    "additional_service": "Дополнительная услуга",
                },
                True,
            ),
            ref("payment_policy", "Утверждённые реквизиты", "record:policy", True),
            F("amount_minor", "Сумма за период, ₽", "money", True, minimum=1),
            F("starts_at", "Начало обязательств по документу", "datetime", True),
            F("payment_day", "День оплаты по договору", "int", True, minimum=1, maximum=31),
            F("payment_timezone", "Часовой пояс договорных дат", "timezone", True),
            choice(
                "first_period_rule",
                "Первый неполный месяц",
                {
                    "full": "Полная сумма — прямо предусмотрена документом",
                    "manual": "Требуется отдельный расчёт ответственного",
                },
                True,
            ),
            F("activation_evidence", "Документальное подтверждение суммы и сроков", required=True),
        ]
    if a in {"suspend", "schedule_stop"}:
        return [F("reason", "Основание", required=True)]
    if a == "obligation":
        return [
            F("period", "Период / устойчивый ключ обязательства", required=True),
            F("amount_minor", "Сумма, ₽", "money", True, minimum=1),
            F("due_at", "Изначальный договорный срок оплаты", "datetime", True),
            F("basis", "Документальное основание", required=True),
        ]
    if a == "invoice":
        return [
            F("number", "Номер фактического счёта", required=True),
            F("date", "Дата счёта", "datetime", True),
            F("document", "Фактический документ / ссылка", required=True),
            choice("status", "Факт", {"prepared": "Подготовлен", "sent": "Отправлен"}, True),
            F("sent_at", "Фактическая отправка", "datetime"),
            F("evidence", "Подтверждение отправки"),
        ]
    if a == "receipt":
        return [
            F("amount_minor", "Подтверждённое поступление, ₽", "money", True, minimum=1),
            F("bank_reference", "Уникальный идентификатор операции банка", required=True),
            F("received_at", "Фактическая дата зачисления", "datetime", True),
            F("evidence", "Подтверждение банка, не клиентский скрин", required=True),
        ]
    if a == "allocate":
        return [
            ref("receipt_id", "Подтверждённое нераспределённое поступление", "finance_receipt", True),
            F("amount_minor", "Зачесть в это обязательство, ₽", "money", True, minimum=1),
        ]
    if a in {"open_debt", "debt_curator_reply", "debt_close", "debt_legal"}:
        return [
            F("evidence", "Фактический результат / подтверждающие документы", required=True),
            F("bank_review", "Подтверждение проверки оплаты"),
        ]
    if a == "debt_formal":
        return [
            F("bank_review", "Проверка оплаты после первого этапа", required=True),
            F("email_evidence", "Подтверждение отправки по email", required=True),
            F("email_sent_at", "Фактическая отправка email", "datetime", True),
            F("edo_evidence", "Подтверждение отправки по ЭДО", required=True),
            F("edo_sent_at", "Фактическая отправка ЭДО", "datetime", True),
        ]
    if a == "debt_pretrial":
        return [
            F("bank_review", "Проверка остатка после второго этапа", required=True),
            F("evidence", "Досудебное уведомление и подтверждение отправки", required=True),
            F("due_at", "Срок исполнения из уведомления", "datetime", True),
        ]
    if a == "debt_dispute":
        return [
            F("disputed_minor", "Оспариваемая часть, ₽", "money", True, minimum=0),
            F("evidence", "Основание спора", required=True),
        ]
    if a == "decision_request":
        return [
            F("question", "Исходный запрос клиента / вопрос", required=True),
            F("position", "Позиция ответственного и необходимые документы", required=True),
            F("legal_review", "Правовая оценка вместо управленческого решения", "bool"),
            F("due_at", "Срок решения", "datetime", True),
        ]
    if a == "decision_record":
        return [
            F("decision", "Решение по конкретному запросу", required=True),
            F("basis", "Подтверждённое основание / документ", required=True),
            F("approved", "Одобрено", "bool", True),
            F("defer_until", "Утверждённая отсрочка до", "datetime"),
            F("valid_until", "Решение действует до", "datetime"),
        ]
    if a == "deferral":
        return [ref("decision_id", "Адресное решение руководства", "finance_decision", True)]
    if a == "schedule":
        return [
            F("start_at", "Первый месячный запуск", "datetime", True),
            F("until_at", "Последняя граница календаря", "datetime", True),
            F("timezone", "Часовой пояс месячного календаря", "timezone", True),
            F("approval", "Утверждённый порядок и получатель", required=True),
        ]
    if a == "extra_accept":
        return [
            F("offer_version", "Версия предложения", "int", True, minimum=1),
            F("amount_minor", "Заявленная клиентом оплата, ₽", "money", minimum=0),
            F("evidence", "Клиентское подтверждение / файл"),
        ]
    if a == "extra_result_reject":
        return [F("reason", "Замечание к результату", required=True), F("due_at", "Срок исправления", "datetime", True)]
    if a == "extra_claim":
        return [
            choice(
                "kind", "Действие", {"decline": "Не согласовано", "dispute": "Спор", "cancel": "Запрос отмены"}, True
            ),
            F("reason", "Причина и фактически выполненная часть", required=True),
            F("evidence", "Документы / подтверждение"),
        ]
    return []


def begin(db, actor, s, action, target=None, data=None):
    d = business.make_draft(
        db, actor, "finance:" + action, ACTIONS.get(action, "Договорной процесс"), data or {}, target
    )
    if not fields(d.kind):
        from ama.ui.controller import dispatch

        return dispatch(db, actor, s, "preview", {"draft_id": d.id})
    return draft_screen(db, actor, s, {"draft_id": d.id})


def dispatch(db, actor, s, action, p):
    b = db.get(Binding, actor.binding_id)
    if action == "fi_home":
        if b.purpose == "client":
            rows = [
                w
                for w in db.scalars(select(Work).where(Work.kind == "paid_work", Work.client_binding_id == b.id))
                if w.partner_person_id == actor.person_id
            ]
            return show(
                db,
                actor,
                s,
                "Дополнительные услуги: отдельные предложения и результаты. Это не оплата франшизы.",
                [B(w.title, "fi_extra", id=w.id) for w in rows],
                action,
            )
        access.require_staff(db, actor)
        agreements = []
        for a in db.scalars(select(FinanceAgreement).where(FinanceAgreement.bot_id == f.settings().bot_id)):
            try:
                f.authorize(db, actor, a)
                agreements.append(a)
            except AccessDenied:
                pass
        items, page = paginate(agreements, p.get("page", 0))
        buttons = [
            B(
                db.get(Entity, a.partner_id).name + " · " + ("Действует" if a.active else "Подготовка"),
                "fi_agreement",
                id=a.id,
            )
            for a in items
        ]
        buttons += navigation(action, page, len(agreements)) + [
            B("Задолженность / юридические передачи", "fi_debts"),
            B("Решения руководства / юриста", "fi_decisions"),
        ]
        if b.purpose in {"support", "sales"}:
            buttons.append(B("Запросить договор / допсоглашение", "fi_new", op="request"))
        if b.purpose == "contracts":
            buttons += [
                B("Месячные пакеты счетов", "fi_batches"),
                B("Календари счетов", "fi_schedules"),
                B("Настроить месячный календарь", "fi_new", op="schedule"),
            ]
        return show(
            db,
            actor,
            s,
            "Договоры и финансовые обязательства. Факты подписи / оплаты фиксируются отдельно.",
            buttons,
            action,
            page=page,
        )
    if action == "fi_new":
        return begin(db, actor, s, p["op"])
    if action == "fi_agreement":
        a = f.get(db, p["id"])
        role = f.authorize(db, actor, a)
        w = f.root(db, a)
        lines = [
            db.get(Entity, a.partner_id).name,
            "Единый запрос договора: " + w.id,
            "Комплект: " + str(a.data["package"]),
            "Подписи: " + ", ".join(a.data.get("signatures", {})),
            "Обязательства: " + ("Активны" if a.active else "Ещё не подтверждены"),
            "Владелец клиентского общения: " + a.data["communicator_id"],
        ]
        if a.data.get("clarification"):
            lines.append("Уточнение: " + str(a.data["clarification"]))
        buttons = (
            [B("Ответить на уточнение", "fi_begin", id=a.id, op="reply_clarification")]
            if role == "communicator"
            else [
                B(ACTIONS[x], "fi_begin", id=a.id, op=x)
                for x in [
                    "package",
                    "invitation_sent",
                    "invitation_accepted",
                    "edo_sent",
                    "signed_uk",
                    "signed_partner",
                    "activate",
                    "clarify",
                    "obligation",
                    "receipt",
                    "suspend",
                ]
            ]
            if role == "contract"
            else []
        )
        obligations = list(db.scalars(select(FinanceObligation).where(FinanceObligation.agreement_id == a.id)))
        buttons += [
            B(o.period + " · остаток " + str(f.balance(db, o)) + " коп.", "fi_obligation", id=o.id) for o in obligations
        ] + [B("В очередь", "fi_home")]
        return show(db, actor, s, "\n".join(lines), buttons, action, id=a.id)
    if action == "fi_obligation":
        o, a = f.get_obligation(db, p["id"])
        f.authorize(db, actor, a)
        text = f"{o.period}\nСумма: {o.amount_minor} коп.\nПодтверждённый остаток: {f.balance(db, o)} коп.\nИсходный договорный срок UTC: {o.due_at.isoformat()}\nОснование: {o.data['purpose']}\nСчёт: {o.data.get('invoice', {})}"
        buttons = [B(ACTIONS[x], "fi_begin", id=o.id, op=x) for x in ["invoice", "allocate", "open_debt"]]
        if o.debt_work_id:
            buttons.append(B("Продолжить задолженность", "fi_debt", id=o.debt_work_id))
        return show(db, actor, s, text, buttons + [B("Договор", "fi_agreement", id=a.id)], action, id=o.id)
    if action == "fi_debts":
        rows = [w for w in db.scalars(select(Work).where(Work.kind == "finance_debt")) if f.can_debt(db, actor, w)]
        items, page = paginate(rows, p.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Задолженность: одна история request_id до и после передачи юристу.",
            [B(w.title + " · " + w.data["stage"], "fi_debt", id=w.id) for w in items]
            + navigation(action, page, len(rows))
            + [B("В очередь", "fi_home")],
            action,
            page=page,
        )
    if action == "fi_debt":
        w = db.get(Work, p["id"])
        if not w or not f.can_debt(db, actor, w):
            raise AccessDenied("Нет назначения в этом процессе.")
        o, a = f.get_obligation(db, w.data["financial_obligation"])
        lines = [
            w.title,
            "request_id: " + w.id,
            "Стадия: " + w.data["stage"],
            "Подтверждённый остаток: " + str(f.balance(db, o)) + " коп.",
            "Следующий контроль UTC: " + str(w.data.get("control_due")),
            "Исходный срок оплаты UTC: " + o.due_at.isoformat(),
        ]
        keys = (
            ["debt_curator_reply"]
            if b.purpose == "support"
            else ["debt_legal_accept", "debt_close"]
            if b.purpose == "legal"
            else [
                "debt_curator",
                "debt_formal",
                "debt_pretrial",
                "debt_legal",
                "debt_close",
                "debt_dispute",
                "decision_request",
                "deferral",
            ]
        )
        if b.purpose == "contracts":
            lines.append("История: " + str(w.data.get("history", [])))
        return show(
            db,
            actor,
            s,
            "\n".join(lines),
            [B(ACTIONS[x], "fi_begin", id=w.id, op=x) for x in keys] + [B("Назад", "fi_debts")],
            action,
            id=w.id,
        )
    if action in {"fi_decisions", "fi_batches", "fi_schedules"}:
        if action == "fi_decisions":
            rows = [
                w
                for w in db.scalars(
                    select(Work).where(
                        Work.kind.in_(["finance_decision", "finance_extra_review"]), Work.owner_id == actor.person_id
                    )
                )
                if f.same_bot_work(db, w)
            ]
            target = "fi_decision"

            def labels(x):
                return x.title
        elif action == "fi_batches":
            rows = [
                x
                for x in db.scalars(select(FinanceBatch).where(FinanceBatch.bot_id == f.settings().bot_id))
                if f.batch_visible(db, actor, x)
            ]
            target = "fi_batch"

            def labels(x):
                return x.period + " · " + x.data["status"]
        else:
            rows = list(
                db.scalars(
                    select(FinanceSchedule).where(
                        FinanceSchedule.bot_id == f.settings().bot_id, FinanceSchedule.owner_id == actor.person_id
                    )
                )
            )
            target = "fi_schedule"

            def labels(x):
                return ("Активен" if x.enabled else "Остановлен") + " · " + x.next_at.isoformat()

        items, page = paginate(rows, p.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Финансовая очередь",
            [B(labels(x), target, id=x.id) for x in items]
            + navigation(action, page, len(rows))
            + [B("В очередь", "fi_home")],
            action,
            page=page,
        )
    if action == "fi_batch":
        batch = db.get(FinanceBatch, p["id"])
        f.authorize_batch(db, actor, batch)
        if (
            not batch
            or batch.bot_id != f.settings().bot_id
            or batch.data["owner_id"] != actor.person_id
            or b.purpose != "contracts"
        ):
            raise AccessDenied("Пакет другого ответственного.")
        buttons = []
        for item in batch.data["items"]:
            if item.get("obligation_id"):
                buttons.append(B("Обязательство: " + item["agreement_id"], "fi_obligation", id=item["obligation_id"]))
        buttons += [B("Все счета отправлены", "fi_begin", id=batch.id, op="batch_finish"), B("Пакеты", "fi_batches")]
        return show(
            db, actor, s, "Счета ФП · " + batch.period + "\n" + str(batch.data["items"]), buttons, action, id=batch.id
        )
    if action == "fi_schedule":
        item = db.get(FinanceSchedule, p["id"])
        if not item or item.owner_id != actor.person_id or item.bot_id != f.settings().bot_id:
            raise AccessDenied("Чужой календарь.")
        return show(
            db,
            actor,
            s,
            "Следующий запуск UTC: " + item.next_at.isoformat() + "\n" + str(item.data),
            [B("Остановить", "fi_begin", id=item.id, op="schedule_stop"), B("Календари", "fi_schedules")],
            action,
            id=item.id,
        )
    if action == "fi_decision":
        w = db.get(Work, p["id"])
        if not w or w.owner_id != actor.person_id or not f.same_bot_work(db, w):
            raise AccessDenied("Чужое решение.")
        f.scope_check(b, w)
        if b.purpose not in {"contracts", "legal", "leadership"} or not f.active_person_role(
            db, actor.person_id, {w.data["decision_role"]}, w
        ):
            raise AccessDenied("Другая область решения.")
        return show(
            db,
            actor,
            s,
            w.title + "\n" + str(w.data),
            [B("Зафиксировать решение", "fi_begin", id=w.id, op="decision_record"), B("Решения", "fi_decisions")],
            action,
            id=w.id,
        )
    if action == "fi_extra":
        w = db.get(Work, p["id"])
        if not w or w.kind != "paid_work" or w.partner_person_id != actor.person_id or w.client_binding_id != b.id:
            raise AccessDenied("Предложение другого ФП.")
        return show(
            db,
            actor,
            s,
            w.title
            + "\nПредложение: "
            + str(w.data.get("offer"))
            + "\nСостояние: "
            + w.status
            + "\nРезультат: "
            + str(w.data.get("result_description", "")),
            [
                *(
                    [
                        B("Принять результат", "fi_begin", id=w.id, op="extra_result_accept"),
                        B("Вернуть с замечанием", "fi_begin", id=w.id, op="extra_result_reject"),
                    ]
                    if w.status == "review" and w.data.get("paid_result_delivered_at")
                    else []
                ),
                *(
                    [B("Согласование / подтверждение оплаты", "fi_begin", id=w.id, op="extra_accept")]
                    if not w.data.get("paid_started_at") and w.status != "done"
                    else []
                ),
                B("Отказ / отмена / спор", "fi_begin", id=w.id, op="extra_claim"),
                B("В меню", "fi_home"),
            ],
            action,
            id=w.id,
        )
    if action == "fi_begin":
        op = p["op"]
        model = (
            FinanceAgreement
            if op in f.AGREEMENT_ACTIONS
            else FinanceObligation
            if op in {"invoice", "allocate", "open_debt"}
            else FinanceBatch
            if op == "batch_finish"
            else FinanceSchedule
            if op == "schedule_stop"
            else Work
        )
        target = db.get(model, p["id"])
        if not target:
            raise BusinessError("Исходная карточка не найдена.")
        data = (
            {"package": target.data.get("package")}
            if op in {"signed_uk", "signed_partner"}
            else {"offer_version": target.data.get("offer", {}).get("version")}
            if op == "extra_accept"
            else {}
        )
        return begin(db, actor, s, op, target, data)
    raise BusinessError("Неизвестный финансовый экран.")

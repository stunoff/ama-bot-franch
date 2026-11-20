import logging
import hashlib
import time
from datetime import timedelta, datetime, timezone
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from ama.config import settings
from ama.db import Session
from ama.models import Inbox, Outbox, Timer, Binding, Attachment, Work, Entity, Audit, now
from ama.errors import BusinessError, RetryLater
from ama.adapters import Transport, DeliveryUncertain, normalize
from ama.service import queue
from ama import bot, reminders
from ama.files import download

log = logging.getLogger("ama.worker")


def inbox_step():
    with Session.begin() as db:
        item = db.scalar(
            select(Inbox)
            .where(Inbox.status == "pending", Inbox.retry_at <= now())
            .order_by(Inbox.retry_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not item:
            return False
        try:
            with db.begin_nested():
                bot.handle(db, item.platform, item.payload)
                db.flush()
            item.status = "done"
        except RetryLater as error:
            item.attempts += 1
            item.retry_at = now() + timedelta(seconds=5)
            if item.attempts >= 6:
                item.status = "rejected"
                event = normalize(item.platform, item.payload)
                if event:
                    bot.send(db, event, str(error), suffix="context-error")
        except (BusinessError, KeyError, TypeError, ValueError) as error:
            # SAVEPOINT откатывает все частичные действия до отправки ошибки.
            event = normalize(item.platform, item.payload)
            if event:
                message = (
                    str(error)
                    if isinstance(error, BusinessError)
                    else "Некорректные параметры команды. Проверьте формат в /help."
                )
                bot.send(db, event, message, suffix="error")
            item.status, item.error = "rejected", type(error).__name__
        except Exception as error:
            item.attempts += 1
            item.error = type(error).__name__  # Не записывать токены/URL/содержимое сообщения в лог.
            item.retry_at = now() + timedelta(seconds=min(3600, 2 ** min(item.attempts, 10)))
            if item.attempts >= 8:
                item.status = "failed"
            log.error("inbox processing failed: %s", type(error).__name__)
        return True


def attachment_step(transport):
    with Session.begin() as db:
        a = db.scalar(
            select(Attachment)
            .where(Attachment.status.in_(["pending", "pending_local"]))
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not a:
            return False
        try:
            from ama.storage_policy import check_attachment, current as storage_policy

            check_attachment(db, a)
            storage = storage_policy(db)
            if a.status != "pending_local":
                a.storage_path, a.size, a.sha256 = download(
                    a, transport.client, **({"max_bytes": storage.data["max_file_bytes"]} if storage else {})
                )
            a.status = "stored"
            w = db.get(Work, a.work_id)
            target = db.get(Binding, (w.review_binding_id or w.binding_id) if w.status == "review" else w.binding_id)
            message = queue(
                db,
                target,
                f"attachment:{a.id}",
                f"Новый материал: {w.title}\nПункт: {w.id}\nАвтор: {a.author_id}",
                work=w,
                attachment_id=a.id,
            )
            if a.album_id:
                message.payload = {**message.payload, "silent": True}
                album_key = hashlib.sha256(f"{w.id}:{a.author_id}:{a.album_id}".encode()).hexdigest()
                queue(db, target, f"album:{album_key}", f"Новые материалы альбома доступны в пункте {w.id}", work=w)
            db.add(Audit(object_id=a.id, action="CHECKLIST_ATTACHMENT_ADDED", data={"work_id": w.id}))
        except Exception as error:
            a.status = "failed"
            db.add(Audit(object_id=a.id, action="ATTACHMENT_SAVE_FAILED", data={"error": type(error).__name__}))
            w = db.get(Work, a.work_id)
            queue(
                db,
                db.get(Binding, w.binding_id),
                f"attachment-error:{a.id}",
                f"Не удалось сохранить вложение пункта {w.id}. Оно не принято; требуется повтор сохранения.",
                work=w,
            )
        return True


def next_partner_window(db, binding, current):
    partner = db.get(Entity, binding.partner_id)
    if not partner:
        raise BusinessError("У маршрута не указан партнёр.")
    data = partner.data.get("notification_window")
    if not data:
        raise BusinessError("Не согласовано окно уведомлений партнёра.")
    from ama.calendar import Calendar

    cal = Calendar(data)
    local = current.replace(tzinfo=timezone.utc).astimezone(cal.tz)
    start = datetime.combine(local.date(), cal.start, cal.tz)
    end = datetime.combine(local.date(), cal.end, cal.tz)
    if cal.working(local.date()) and start <= local < end:
        return current
    return cal.add_minutes(current, 0)


def outbox_step(transport):
    with Session.begin() as db:
        row = db.scalar(
            select(Outbox)
            .where(Outbox.status == "pending", Outbox.retry_at <= now())
            .order_by(Outbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not row:
            return False
        b = db.get(Binding, row.binding_id) if row.binding_id else None
        if row.payload.get("probe"):
            from ama.delivery_checks import current_probe
            if not current_probe(b, row):
                row.status, row.error = "cancelled", "Stale route probe; create a new delivery test"
                return True
        if row.binding_id and not row.payload.get("probe"):
            if not b or not b.active or not b.verified or row.binding_version != b.version:
                row.status, row.error = "blocked", "Route changed; verify/migrate recipient"
                from ama.sla import delivery_problem

                delivery_problem(db, row)
                return True
            if row.work_id:
                w = db.get(Work, row.work_id)
                if not w or b.id not in {w.binding_id, w.pending_binding_id, w.client_binding_id, w.review_binding_id}:
                    row.status, row.error = "blocked", "Recipient no longer linked to work"
                    return True
        try:
            from ama.closures import deliverable as closure_deliverable

            if not closure_deliverable(db, row):
                row.status, row.error = "cancelled", "Unconfirmed closure context changed"
                return True
            from ama.legal_setup import deliverable as legal_deliverable

            if not legal_deliverable(db, row):
                row.status, row.error = "cancelled", "Legal setup recipient changed"
                return True
            from ama.brand_notices import deliverable as brand_deliverable

            if not brand_deliverable(db, row):
                row.status, row.error = "cancelled", "Brand access context changed"
                return True
            from ama.report_schedules import deliverable as report_deliverable

            if not report_deliverable(db, row):
                row.status, row.error = "cancelled", "Report plan changed"
                return True
            from ama.publications import deliverable as publication_deliverable, refresh as publication_refresh

            allowed = publication_deliverable(db, row)
            if allowed is None:
                row.retry_at = now() + timedelta(seconds=5)
                return True
            if not allowed:
                row.status, row.error = "blocked", "Publication rights, recipient or selected files changed"
                publication_refresh(db, row)
                return True
            from ama.finance import deliverable as finance_deliverable

            if not finance_deliverable(db, row):
                row.status, row.error = "cancelled", "Financial context changed before delivery"
                return True
            from ama.learning import deliverable as learning_deliverable

            if not learning_deliverable(db, row):
                row.status, row.error = "cancelled", "Learning context changed"
                return True
            from ama.packets import deliverable as packet_deliverable

            if not packet_deliverable(db, row):
                row.status, row.error = "cancelled", "Packet context changed before delivery"
                return True
            from ama.sla import deliverable

            if not deliverable(db, row):
                row.status, row.error = "cancelled", "SLA context changed before delivery"
                return True
            if row.payload.get("digital_group") or row.payload.get("digital_file"):
                from ama.digital import deliverable as digital_deliverable

                if not digital_deliverable(db, row):
                    row.status, row.error = "blocked", "Цифровой маршрут / назначение изменились."
                    return True
            if row.payload.get("public_record"):
                from ama.models import Record
                from ama.prospects import approved, brand

                material = db.get(Record, row.payload["public_record"])
                if (
                    not material
                    or not approved(material)
                    or not material.data.get("public", True)
                    or not brand(db, material.data.get("brand_id"))
                    or material.version != row.payload.get("public_record_version")
                ):
                    row.status, row.error = "cancelled", "Public material changed before delivery"
                    return True
            if row.payload.get("template_id"):
                from ama.models import Record

                template = db.get(Record, row.payload["template_id"])
                if not template or not template.published or template.version != row.payload.get("template_version"):
                    row.status, row.error = "cancelled", "Reminder template changed before delivery"
                    return True
            if row.payload.get("ui_actor"):
                from ama.auth import Actor
                from ama.ui.access import grants_hash

                if not b or grants_hash(db, Actor(row.payload["ui_actor"], b.id)) != row.payload.get("ui_grants"):
                    raise BusinessError("Права пользователя изменились. Откройте меню или сформируйте отчёт заново.")
            if row.payload.get("sales_actor"):
                from ama.auth import Actor
                from ama.ui.access import grants_hash
                from ama.models import SalesCase

                case = db.get(SalesCase, row.payload["case_id"])
                if (
                    not case
                    or case.owner_id != row.payload["sales_actor"]
                    or grants_hash(db, Actor(row.payload["sales_actor"], row.payload["sales_actor_binding"]))
                    != row.payload["sales_grants"]
                ):
                    raise BusinessError("Ответственный или его права изменились. Подготовьте ответ заново.")
            if row.payload.get("lead_id"):
                from ama.models import Lead

                lead = db.scalar(select(Lead).where(Lead.id == row.payload["lead_id"]).with_for_update())
                if (
                    row.payload.get("sales_reply")
                    and lead
                    and lead.data.get("contact") != row.payload.get("recipient_contact")
                ):
                    raise BusinessError("Подтверждённый канал лида изменился. Проверьте адресата заново.")
                if (
                    not lead
                    or (
                        lead.no_contact
                        and not (
                            row.payload.get("reply_to_inbound")
                            and row.payload.get("silence_epoch") == lead.data.get("silence", {}).get("epoch")
                        )
                    )
                    or (row.payload.get("lead_touch_id") and not reminders.deliverable(db, lead, row.payload))
                ):
                    row.status, row.error = "cancelled", "Lead opted out"
                    return True
            if (
                row.payload.get("sales_reply")
                and lead
                and row.payload.get("silence_epoch") != lead.data.get("silence", {}).get("epoch")
            ):
                row.status, row.error = "blocked", "Client reacted after reply preview; review context"
                return True
            if row.payload.get("lead_touch_id"):
                if reminders.message_count(db, lead, ["sent", "sending", "uncertain"]) >= 2:
                    row.status, row.error = "cancelled", "Two reminder slots already delivered or reserved"
                    return True
                cal = reminders.configured_calendar(db)
                if not cal:
                    raise BusinessError("Нет действующего календаря продаж.")
                allowed_at = cal.add_minutes(now(), 0)
                if allowed_at > now():
                    row.retry_at = allowed_at
                    return True
            if row.payload.get("partner_window"):
                next_time = next_partner_window(db, b, now())
                if next_time > now():
                    row.retry_at = next_time
                    return True
            file_path = None
            if row.payload.get("packet_asset"):
                from ama.packets import asset_path

                file_path = asset_path(db, row)
            if row.payload.get("export_id"):
                from ama.ui.reports import render as render_export

                file_path = render_export(db, row.payload["export_id"])
            if row.payload.get("attachment_id"):
                a = db.get(Attachment, row.payload["attachment_id"])
                if not a or a.status != "stored" or not a.storage_path:
                    raise BusinessError("Вложение не сохранено.")
                file_path = Path(a.storage_path)
            # Фиксируем намерение до сетевого вызова отдельной транзакцией. После crash исход не повторяется вслепую.
            row.status = "sending"
            row.attempts += 1
            db.flush()
            message_id = row.id
            platform, chat_id, topic, payload = row.platform, row.chat_id, row.topic_key, row.payload
        except BusinessError as error:
            row.status, row.error = "blocked", str(error)
            from ama.sla import delivery_problem

            delivery_problem(db, row)
            from ama.publications import refresh

            refresh(db, row)
            return True
    # Лок на строке отпущен, но sending не подбирается другими воркерами.
    try:
        if payload.get("op") == "create_topic":
            external_id = transport.create_topic(platform, chat_id, payload["topic_name"])
        elif payload.get("callback_id"):
            transport.answer_callback(platform, payload["callback_id"])
            external_id = None
        else:
            external_id = transport.send(platform, chat_id, topic, payload, file_path)
        status, error = "sent", None
    except DeliveryUncertain:
        external_id, status, error = None, "uncertain", "Delivery outcome unknown; reconcile before retry"
    except BusinessError as e:
        external_id, status, error = None, "failed", str(e)
        if "Лимит" in str(e):
            status = "pending"
    except Exception as e:
        # ConnectError означает, что соединение не установлено; прочие исходы проверяет оператор.
        import httpx

        external_id, error = None, type(e).__name__
        status = (
            "pending" if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)) else "uncertain"
        )
    with Session.begin() as db:
        row = db.scalar(select(Outbox).where(Outbox.id == message_id).with_for_update())
        row.status, row.external_id, row.error = status, external_id, error
        row.retry_at = now() + timedelta(seconds=min(3600, 5 * 2 ** min(row.attempts, 9)))
        if row.status == "pending" and row.attempts >= 12:
            row.status = "failed"
        if status == "sent" and payload.get("op") == "create_topic":
            from ama.sale_flow import on_topic

            try:
                if payload.get("finance_topic") or payload.get("finance_batch_topic"):
                    from ama.finance import on_topic as finance_topic

                    finance_topic(db, row, external_id)
                elif payload.get("digital_group"):
                    from ama.digital import on_topic as digital_topic

                    digital_topic(db, row, external_id)
                elif payload.get("packet_target"):
                    from ama.packets import on_topic as packet_topic

                    packet_topic(db, row, external_id)
                else:
                    on_topic(db, row, external_id)
            except BusinessError as topic_error:
                row.status, row.error = "uncertain", str(topic_error)
        from ama.sla import delivery_problem

        delivery_problem(db, row)
        if status == "sent":
            reminders.on_delivered(db, row)
            from ama.sla import delivered

            delivered(db, row)
            from ama.packets import delivered as packet_delivered

            packet_delivered(db, row)
            from ama.learning import delivered as learning_delivered

            learning_delivered(db, row)
            from ama.finance import delivered as finance_delivered

            finance_delivered(db, row)
        from ama.publications import refresh as publication_refresh

        publication_refresh(db, row)
        from ama.delivery_checks import confirm_probe
        confirm_probe(db, row)
        db.add(Audit(object_id=row.id, action="DELIVERY_" + row.status.upper(), data={"external_id": external_id}))
    return True


def timer_step():
    with Session.begin() as db:
        t = db.scalar(
            select(Timer)
            .where(Timer.status == "pending", Timer.due_at <= now())
            .order_by(Timer.due_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not t:
            return False
        w = db.get(Work, t.work_id)
        from ama.sla import case_for

        if w and case_for(db, w):
            t.status = "cancelled"
            return True
        if not w or w.status == "done" or w.version != t.data.get("version"):
            t.status = "cancelled"
            return True
        target = db.scalar(
            select(Binding).where(Binding.purpose == "management", Binding.active.is_(True), Binding.verified.is_(True))
        )
        if not target:
            # Не теряем сигнал. Пока не настроен маршрут, таймер остаётся видимым blocked.
            t.status = "blocked"
            db.add(Audit(object_id=t.id, action="TIMER_ROUTE_MISSING", data={"work_id": w.id}))
            return True
        queue(
            db,
            target,
            f"timer:{t.id}",
            f"Контроль срока: {w.title}\nID: {w.id}\nОтветственный: {w.owner_id}\nСостояние: {w.status}",
            work=None,
        )
        w.data = {**w.data, "overdue": True}
        t.status = "done"
        db.add(Audit(object_id=t.id, action="TIMER_DUE", data={"work_id": w.id}))
        return True


def reminder_step():
    with Session.begin() as db:
        return reminders.step(db)


def substitution_step():
    from ama.ui.configuration import substitution_step as process

    with Session.begin() as db:
        return process(db)


def sales_step():
    from ama.sale_flow import tick

    with Session.begin() as db:
        return tick(db)


def project_step():
    from ama.project_flow import tick

    with Session.begin() as db:
        return tick(db)


def brand_step():
    from ama.brand_notices import tick, wake

    with Session.begin() as db:
        wake(db)
        return tick(db)


def report_step():
    from ama.report_schedules import tick

    with Session.begin() as db:
        return tick(db)


def intake_step(transport):
    from ama.intake import worker_step

    return worker_step(transport, Session)


def finance_step():
    from ama.finance import debt_tick, schedule_tick, extra_tick

    with Session.begin() as db:
        return debt_tick(db) or schedule_tick(db) or extra_tick(db)


def learning_step():
    from ama.learning import tick

    with Session.begin() as db:
        return tick(db)


def digital_step():
    from ama.digital import tick

    with Session.begin() as db:
        return tick(db)


def sla_step():
    from ama.sla import tick

    with Session.begin() as db:
        return tick(db)


def packet_step(transport):
    from ama import packets

    with Session.begin() as db:
        asset = packets.asset_step(db, transport)
    with Session.begin() as db:
        control = packets.control_tick(db)
    with Session.begin() as db:
        schedule = packets.schedule_tick(db)
    return asset or control or schedule


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    transport = Transport()
    try:
        while True:
            try:
                processed = [
                    intake_step(transport),
                    brand_step(),
                    report_step(),
                    inbox_step(),
                    attachment_step(transport),
                    timer_step(),
                    reminder_step(),
                    substitution_step(),
                    sales_step(),
                    project_step(),
                    digital_step(),
                    finance_step(),
                    learning_step(),
                    sla_step(),
                    packet_step(transport),
                    outbox_step(transport),
                ]
                if not any(processed):
                    time.sleep(settings().worker_interval)
            except SQLAlchemyError as e:
                log.error("database failure: %s", type(e).__name__)
                time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()


if __name__ == "__main__":
    main()

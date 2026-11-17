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
        item = db.scalar(select(Inbox).where(Inbox.status == "pending", Inbox.retry_at <= now()).order_by(Inbox.retry_at).with_for_update(skip_locked=True).limit(1))
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
                message = str(error) if isinstance(error, BusinessError) else "Некорректные параметры команды. Проверьте формат в /help."
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
        a = db.scalar(select(Attachment).where(Attachment.status == "pending").with_for_update(skip_locked=True).limit(1))
        if not a:
            return False
        try:
            a.storage_path, a.size, a.sha256 = download(a, transport.client)
            a.status = "stored"
            w = db.get(Work, a.work_id)
            target = db.get(Binding, w.binding_id)
            message = queue(db, target, f"attachment:{a.id}", f"Новый материал: {w.title}\nПункт: {w.id}\nАвтор: {a.author_id}",
                  work=w, attachment_id=a.id)
            if a.album_id:
                message.payload = {**message.payload, "silent": True}
                album_key = hashlib.sha256(f"{w.id}:{a.author_id}:{a.album_id}".encode()).hexdigest()
                queue(db, target, f"album:{album_key}", f"Новые материалы альбома доступны в пункте {w.id}", work=w)
            db.add(Audit(object_id=a.id, action="CHECKLIST_ATTACHMENT_ADDED", data={"work_id": w.id}))
        except Exception as error:
            a.status = "failed"
            db.add(Audit(object_id=a.id, action="ATTACHMENT_SAVE_FAILED", data={"error": type(error).__name__}))
            w = db.get(Work, a.work_id)
            queue(db, db.get(Binding, w.binding_id), f"attachment-error:{a.id}",
                  f"Не удалось сохранить вложение пункта {w.id}. Оно не принято; требуется повтор сохранения.", work=w)
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
        row = db.scalar(select(Outbox).where(Outbox.status == "pending", Outbox.retry_at <= now())
                        .order_by(Outbox.created_at).with_for_update(skip_locked=True).limit(1))
        if not row:
            return False
        b = db.get(Binding, row.binding_id) if row.binding_id else None
        if row.binding_id and not row.payload.get("probe"):
            if not b or not b.active or not b.verified or row.binding_version != b.version:
                row.status, row.error = "blocked", "Route changed; verify/migrate recipient"
                return True
            if row.work_id:
                w = db.get(Work, row.work_id)
                if not w or b.id not in {w.binding_id, w.pending_binding_id, w.client_binding_id}:
                    row.status, row.error = "blocked", "Recipient no longer linked to work"
                    return True
        try:
            if row.payload.get("lead_id"):
                from ama.models import Lead
                lead = db.get(Lead, row.payload["lead_id"])
                if (not lead or lead.no_contact or (row.payload.get("lead_touch_id") and
                    (row.payload.get("silence_epoch") != lead.data.get("silence", {}).get("epoch") or
                     lead.stage == "contract" or lead.result == "success" or lead.data.get("next_contact")))):
                    row.status, row.error = "cancelled", "Lead opted out"
                    return True
            if row.payload.get("partner_window"):
                next_time = next_partner_window(db, b, now())
                if next_time > now():
                    row.retry_at = next_time
                    return True
            file_path = None
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
            return True
    # Лок на строке отпущен, но sending не подбирается другими воркерами.
    try:
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
        status = "pending" if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)) else "uncertain"
    with Session.begin() as db:
        row = db.scalar(select(Outbox).where(Outbox.id == message_id).with_for_update())
        row.status, row.external_id, row.error = status, external_id, error
        row.retry_at = now() + timedelta(seconds=min(3600, 5 * 2 ** min(row.attempts, 9)))
        if row.status == "pending" and row.attempts >= 12:
            row.status = "failed"
        if status == "sent":
            reminders.on_delivered(db, row)
        if status == "sent" and payload.get("probe"):
            b = db.get(Binding, row.binding_id)
            if b and b.version == row.binding_version:
                b.verified = True
        db.add(Audit(object_id=row.id, action="DELIVERY_" + row.status.upper(), data={"external_id": external_id}))
    return True


def timer_step():
    with Session.begin() as db:
        t = db.scalar(select(Timer).where(Timer.status == "pending", Timer.due_at <= now()).order_by(Timer.due_at)
                      .with_for_update(skip_locked=True).limit(1))
        if not t:
            return False
        w = db.get(Work, t.work_id)
        if not w or w.status == "done" or w.version != t.data.get("version"):
            t.status = "cancelled"
            return True
        target = db.scalar(select(Binding).where(Binding.purpose == "management", Binding.active.is_(True), Binding.verified.is_(True)))
        if not target:
            # Не теряем сигнал. Пока не настроен маршрут, таймер остаётся видимым blocked.
            t.status = "blocked"
            db.add(Audit(object_id=t.id, action="TIMER_ROUTE_MISSING", data={"work_id": w.id}))
            return True
        queue(db, target, f"timer:{t.id}", f"Контроль срока: {w.title}\nID: {w.id}\nОтветственный: {w.owner_id}\nСостояние: {w.status}", work=None)
        w.data = {**w.data, "overdue": True}
        t.status = "done"
        db.add(Audit(object_id=t.id, action="TIMER_DUE", data={"work_id": w.id}))
        return True


def reminder_step():
    with Session.begin() as db:
        return reminders.step(db)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    transport = Transport()
    try:
        while True:
            try:
                processed = [inbox_step(), attachment_step(transport), timer_step(), reminder_step(), outbox_step(transport)]
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

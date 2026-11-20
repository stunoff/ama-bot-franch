import argparse
import httpx
from urllib.parse import urlsplit
import json
from pathlib import Path
from sqlalchemy import select, func
from ama.db import Session
from ama.models import Entity, Record, Outbox, Attachment, Inbox, Timer, Audit, now
from ama import admin
from ama.adapters import Transport, DeliveryUncertain
from ama.config import settings
from ama.errors import BusinessError
from ama.logging_security import response_error, redact


def main():
    parser = argparse.ArgumentParser(description="Защищённая настройка АМА Франчайзинг на сервере")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed")
    initial = commands.add_parser("init-admin")
    initial.add_argument("--platform", choices=["telegram", "max"], required=True)
    initial.add_argument("--user-id", required=True)
    initial.add_argument("--chat-id", required=True)
    initial.add_argument("--topic", default="")
    initial.add_argument(
        "--recover", action="store_true", help="Восстановить управляющий маршрут того же администратора"
    )
    bootstrap = commands.add_parser("configure")
    bootstrap.add_argument("file", help="JSON-массив команд конфигурации")
    commands.add_parser("diagnostics")
    retry = commands.add_parser("retry")
    retry.add_argument("table", choices=["outbox", "inbox", "attachments", "timers"])
    retry.add_argument("id")
    retry.add_argument("--confirmed-not-delivered", action="store_true")
    delivered = commands.add_parser("mark-delivered")
    delivered.add_argument("id")
    delivered.add_argument("external_id")
    commands.add_parser("register-webhooks")
    args = parser.parse_args()
    if args.command == "register-webhooks":
        cfg = settings()
        base = cfg.public_url.strip().rstrip("/")
        try:
            url = urlsplit(base)
        except ValueError:
            parser.error("PUBLIC_URL содержит некорректный адрес")
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or "\\" in base
            or any(c.isspace() for c in base)
        ):
            parser.error("PUBLIC_URL должен быть публичным HTTPS URL без логина, query и fragment")
        if not cfg.telegram_token and not cfg.max_token:
            parser.error("Не заданы TELEGRAM_TOKEN и MAX_TOKEN: нечего регистрировать")
        if cfg.telegram_token:
            import re

            if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", cfg.telegram_webhook_secret):
                parser.error("TELEGRAM_WEBHOOK_SECRET: 16–256 символов A–Z, a–z, 0–9, _ или -")
        if cfg.max_token and len(cfg.max_webhook_secret) < 16:
            parser.error("Задайте MAX_WEBHOOK_SECRET длиной >= 16 символов")
        transport = Transport()
        try:
            if cfg.telegram_token:
                if len(cfg.telegram_webhook_secret) < 16:
                    parser.error("Задайте TELEGRAM_WEBHOOK_SECRET длиной >= 16 символов")
                response = transport.client.post(
                    f"https://api.telegram.org/bot{cfg.telegram_token}/setWebhook",
                    json={
                        "url": base + "/webhooks/telegram",
                        "secret_token": cfg.telegram_webhook_secret,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if response.status_code != 200 or not isinstance(data, dict) or not data.get("ok"):
                    parser.error(
                        "Telegram отклонил регистрацию webhook: "
                        + response_error(response, [cfg.telegram_token, cfg.telegram_webhook_secret])
                    )
                print("Telegram webhook зарегистрирован")
            if cfg.max_token:
                if len(cfg.max_webhook_secret) < 16:
                    parser.error("Задайте MAX_WEBHOOK_SECRET длиной >= 16 символов")
                response = transport.client.post(
                    f"{cfg.max_api_url}/subscriptions",
                    headers={"Authorization": cfg.max_token},
                    json={
                        "url": base + "/webhooks/max",
                        "secret": cfg.max_webhook_secret,
                        "update_types": ["message_created", "message_callback", "bot_started"],
                    },
                )
                if response.status_code >= 400:
                    parser.error(
                        "MAX отклонил регистрацию webhook: "
                        + response_error(response, [cfg.max_token, cfg.max_webhook_secret])
                    )
                print("MAX webhook зарегистрирован")
        except (httpx.RequestError, ValueError) as error:
            parser.error("Ошибка регистрации webhook: " + type(error).__name__)
        finally:
            transport.close()
        return
    with Session.begin() as db:
        if args.command == "init-admin":
            from ama.bootstrap import initialize

            transport = Transport()
            try:
                result = initialize(
                    db, args.platform, args.user_id, args.chat_id, args.topic, recover=args.recover, transport=transport
                )
            except BusinessError as error:
                parser.error(redact(error))
            except DeliveryUncertain:
                parser.error(
                    "Исход тестовой отправки неизвестен. Сверьте сообщение в чате перед повтором; настройки не сохранены."
                )
            except httpx.RequestError as error:
                parser.error("Не удалось проверить управляющий чат: " + type(error).__name__)
            finally:
                transport.close()
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "seed":
            for key, name in [
                ("burger", "Бургер"),
                ("sushi", "Суши"),
                ("grill", "Гриль"),
                ("pizza", "Пицца"),
                ("cheburek", "Чебурек"),
                ("shawarma", "Шаурма"),
            ]:
                if not db.get(Entity, key):
                    db.add(
                        Entity(
                            id=key,
                            kind="brand",
                            name="АМА " + name,
                            data={"sales_available": False, "knowledge_available": False},
                        )
                    )
            if not db.scalar(select(Record).where(Record.kind == "course", Record.key == "general")):
                cards = json.loads((Path(__file__).resolve().parent.parent / "data/course-general.json").read_text())
                db.add(Record(kind="course", key="general", data={"cards": cards}, published=False))
            print("Созданы 6 брендов (продажи выключены) и черновик общего ознакомления.")
        elif args.command == "configure":
            commands_data = json.loads(Path(args.file).read_text())
            for command in commands_data:
                print(json.dumps(admin.apply(db, None, command, bootstrap=True), ensure_ascii=False))
        elif args.command == "diagnostics":
            for model in (Inbox, Outbox, Attachment, Timer):
                rows = db.execute(select(model.status, func.count()).group_by(model.status)).all()
                print(model.__tablename__, dict(rows))
            for m in db.scalars(select(Outbox).where(Outbox.status.in_(["failed", "blocked", "uncertain", "sending"]))):
                print("delivery", m.id, m.status, m.attempts, m.error)
        elif args.command == "retry":
            model = {"outbox": Outbox, "inbox": Inbox, "attachments": Attachment, "timers": Timer}[args.table]
            obj = db.get(model, args.id)
            if not obj:
                parser.error("Запись не найдена")
            if (
                isinstance(obj, Outbox)
                and obj.status in {"sent", "uncertain", "sending"}
                and not args.confirmed_not_delivered
            ):
                parser.error("Сначала сверьте фактическую доставку. Для повтора требуется --confirmed-not-delivered")
            obj.status = "pending"
            if hasattr(obj, "retry_at"):
                obj.retry_at = now()
            db.add(Audit(object_id=args.id, action="OPERATOR_RETRY", data={"table": args.table}))
        elif args.command == "mark-delivered":
            obj = db.get(Outbox, args.id)
            if not obj or obj.status not in {"sending", "uncertain"}:
                parser.error("Допустимы только неопределённые отправки")
            obj.status, obj.external_id = "sent", args.external_id
            from ama import reminders

            reminders.on_delivered(db, obj)
            from ama.sla import delivered

            delivered(db, obj)
            from ama.packets import delivered as packet_delivered

            packet_delivered(db, obj)
            from ama.learning import delivered as learning_delivered

            learning_delivered(db, obj)
            from ama.finance import delivered as finance_delivered

            finance_delivered(db, obj)
            from ama.publications import refresh

            refresh(db, obj)
            if obj.payload.get("op") == "create_topic":
                if obj.payload.get("finance_topic") or obj.payload.get("finance_batch_topic"):
                    from ama.finance import on_topic
                elif obj.payload.get("digital_group"):
                    from ama.digital import on_topic
                elif obj.payload.get("packet_target"):
                    from ama.packets import on_topic
                else:
                    from ama.sale_flow import on_topic
                on_topic(db, obj, args.external_id)
            from ama.delivery_checks import confirm_probe

            confirm_probe(db, obj)
            db.add(
                Audit(object_id=obj.id, action="OPERATOR_DELIVERY_CONFIRMED", data={"external_id": args.external_id})
            )


if __name__ == "__main__":
    main()

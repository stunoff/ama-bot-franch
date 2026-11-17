import json
from datetime import timedelta
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from ama.models import Inbox, Outbox, Attachment, Work, Binding, now
from ama.adapters import normalize, Transport, DeliveryUncertain
from ama import api, worker
from ama.service import make_timer
from ama.files import validate_url
from ama.errors import BusinessError


def tg(update=1, text="/start", user="fp", chat="client"):
    return {"update_id": update, "message": {"message_id": update, "from": {"id": user}, "chat": {"id": chat, "type": "group"}, "text": text}}


def max_event():
    return {"update_type": "message_created", "timestamp": 12, "message": {"sender": {"user_id": 7},
            "recipient": {"chat_id": 9, "chat_type": "dialog"}, "body": {"mid": "abc", "text": "/start"}}}


def test_normalize_both_platforms():
    t = normalize("telegram", tg())
    m = normalize("max", max_event())
    assert t.user_id == "fp" and t.key == "tg:1"
    assert m.user_id == "7" and m.chat_id == "9" and m.key == "max:message_created:abc"


def test_max_callback_key_not_timestamp():
    raw = max_event()
    raw["update_type"] = "message_callback"
    raw["callback"] = {"callback_id": "a", "user": {"user_id": 7}, "payload": "af:tasks"}
    first = normalize("max", raw)
    raw["callback"]["callback_id"] = "b"
    assert first.key != normalize("max", raw).key


def test_webhook_secret_and_dedup(ctx, monkeypatch):
    monkeypatch.setattr(api, "Session", ctx.factory)
    monkeypatch.setattr(api, "settings", lambda: type("Cfg", (), {"telegram_webhook_secret": "correct", "max_webhook_secret": "max-secret"})())
    client = TestClient(api.app)
    assert client.post("/webhooks/telegram", json=tg()).status_code == 403
    for _ in range(2):
        assert client.post("/webhooks/telegram", json=tg(), headers={"X-Telegram-Bot-Api-Secret-Token": "correct"}).status_code == 200
    assert ctx.db.scalar(select(func.count()).select_from(Inbox)) == 1
    assert client.get("/health").json() == {"status": "ok"}


def test_worker_transaction_rolls_back_partial_action(ctx, monkeypatch):
    from ama import bot
    def bad_handle(db, platform, raw):
        w = db.get(Work, "work")
        w.title = "SHOULD ROLLBACK"
        db.flush()
        raise BusinessError("Ошибка второй части операции")
    monkeypatch.setattr(bot, "handle", bad_handle)
    monkeypatch.setattr(worker, "Session", ctx.factory)
    ctx.db.add(Inbox(key="tg:1", platform="telegram", payload=tg()))
    ctx.db.commit()
    assert worker.inbox_step()
    ctx.db.expire_all()
    assert ctx.w.title == "Проверка"
    assert ctx.db.get(Inbox, "tg:1").status == "rejected"


def test_worker_restart_does_not_repeat_delivery(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    ctx.db.add(Inbox(key="tg:1", platform="telegram", payload=tg()))
    ctx.db.commit()
    assert worker.inbox_step()
    assert not worker.inbox_step()
    class Fake:
        count = 0
        def send(self, *args):
            self.count += 1
            return "42"
    fake = Fake()
    assert worker.outbox_step(fake)
    assert not worker.outbox_step(fake)
    assert fake.count == 1


def test_ambiguous_delivery_is_not_retried(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    ctx.db.add(Outbox(dedupe_key="send", platform="telegram", chat_id="support", payload={"text": "hello"}))
    ctx.db.commit()
    class Fake:
        def send(self, *args):
            raise DeliveryUncertain()
    assert worker.outbox_step(Fake())
    assert not worker.outbox_step(Fake())
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key == "send"))
    assert row.status == "uncertain"


def test_disabled_route_blocks_pending_send(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    b = ctx.db.get(Binding, "support")
    ctx.db.add(Outbox(dedupe_key="send", platform="telegram", chat_id="support", binding_id=b.id, binding_version=b.version, payload={"text": "hello"}))
    b.active = False
    ctx.db.commit()
    class Fake:
        def send(self, *args):
            pytest.fail("Disabled route must never receive data")
    worker.outbox_step(Fake())
    row = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key == "send"))
    assert row.status == "blocked"


def test_timer_is_one_signal_after_restart(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    make_timer(ctx.db, ctx.w, "deadline", now() - timedelta(seconds=1))
    ctx.db.commit()
    assert worker.timer_step()
    assert not worker.timer_step()
    assert ctx.db.scalar(select(func.count()).select_from(Outbox).where(Outbox.dedupe_key.like("timer:%"))) == 1


def test_attachment_saved_then_immediately_queued(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    path = tmp_path / "a.jpg"
    path.write_bytes(b"fake-image")
    monkeypatch.setattr(worker, "download", lambda a, c: (str(path), 10, "digest"))
    ctx.db.add(Attachment(id="a", work_id="work", author_id="observer", source_key="file", platform="telegram", file_id="id", filename="a.jpg"))
    ctx.db.commit()
    class Fake:
        client = None
    worker.attachment_step(Fake())
    a = ctx.db.get(Attachment, "a")
    assert a.status == "stored" and a.sha256 == "digest"
    event = ctx.db.scalar(select(Outbox).where(Outbox.dedupe_key == "attachment:a"))
    assert event.binding_id == "support" and event.payload["attachment_id"] == "a"
    assert ctx.w.status == "new"


def test_file_without_context_is_not_routed(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    raw = tg()
    raw["message"]["text"] = ""
    raw["message"]["photo"] = [{"file_id": "file"}]
    ctx.db.add(Inbox(key="tg:1", platform="telegram", payload=raw))
    ctx.db.commit()
    worker.inbox_step()
    assert ctx.db.scalar(select(func.count()).select_from(Attachment)) == 0


@pytest.mark.parametrize("url", ["http://api.telegram.org/file", "https://127.0.0.1/a", "https://evil.example/a", "https://max.ru.evil.org/a", "https://user:pass@max.ru/a"])
def test_ssrf_rejected(url):
    with pytest.raises(BusinessError):
        validate_url(url)


def test_telegram_send_payload_has_thread(ctx):
    transport = Transport()
    transport.cfg.telegram_token = "test-token"
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 44}})
    transport.client.close()
    transport.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert transport.send("telegram", "-100", "10", {"text": "hello", "buttons": [["Да", "af:tasks"]]}) == "44"
    assert captured[0]["message_thread_id"] == "10" and captured[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "af:tasks"
    transport.close()


def test_max_send_uses_native_keyboard(ctx):
    transport = Transport()
    transport.cfg.max_token = "test-max"
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"body": {"mid": "max-1"}}})
    transport.client.close()
    transport.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert transport.send("max", "9", "", {"text": "hello", "buttons": [["Да", "af:tasks"]]}) == "max-1"
    assert captured[0]["attachments"][0]["type"] == "inline_keyboard"
    transport.close()


def test_album_without_repeated_caption_keeps_original_work(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    first, second = tg(1, "/file work"), tg(2, "")
    for raw in [first, second]:
        raw["message"]["media_group_id"] = "album"
        raw["message"]["photo"] = [{"file_id": str(raw["update_id"])}]
    ctx.db.add_all([Inbox(key="tg:1", platform="telegram", payload=first), Inbox(key="tg:2", platform="telegram", payload=second)])
    ctx.db.commit()
    worker.inbox_step()
    worker.inbox_step()
    rows = list(ctx.db.scalars(select(Attachment)))
    assert len(rows) == 2 and {a.work_id for a in rows} == {"work"}


def test_album_arrives_before_caption_is_retained(ctx, monkeypatch):
    monkeypatch.setattr(worker, "Session", ctx.factory)
    raw = tg(2, "")
    raw["message"]["media_group_id"] = "album"
    raw["message"]["photo"] = [{"file_id": "second"}]
    ctx.db.add(Inbox(key="tg:2", platform="telegram", payload=raw))
    ctx.db.commit()
    worker.inbox_step()
    item = ctx.db.get(Inbox, "tg:2")
    assert item.status == "pending" and item.attempts == 1
    assert ctx.db.scalar(select(func.count()).select_from(Attachment)) == 0

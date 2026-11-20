"""Regression checks for setup, role assignment, route replacement and safe diagnostics."""

import logging
import sys
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, delete
from ama import admin, cli, worker, bot
from ama.auth import identify
from ama.errors import BusinessError, AccessDenied
from ama.models import Account, Binding, Outbox, Record, Role, now
from ama.adapters import Transport
from ama.ui import access


class FakeTransport:
    calls = []

    def __init__(self):
        pass

    def send(self, *args, **kwargs):
        self.calls.append(args)
        return "audit-message"

    def close(self):
        pass


def command(monkeypatch, factory, *args):
    monkeypatch.setattr(cli, "Session", factory)
    monkeypatch.setattr(cli, "Transport", FakeTransport)
    monkeypatch.setattr(sys, "argv", ["ama.cli", *args])
    cli.main()


def initial(monkeypatch, factory, user="70001", chat="70001"):
    command(monkeypatch, factory, "init-admin", "--platform", "telegram", "--user-id", user, "--chat-id", chat)


def test_clean_chain_admin_group_topic_delivery_activation_and_curator(factory, monkeypatch):
    FakeTransport.calls = []
    initial(monkeypatch, factory)
    with factory.begin() as db:
        management = db.scalar(select(Binding).where(Binding.purpose == "management"))
        assert management.active and management.verified
        actor = identify(db, "telegram", "70001", "70001")
        admin.check_admin(db, actor)
        result = admin.apply(
            db,
            actor,
            {
                "type": "binding",
                "data": {"purpose": "support", "platform": "telegram", "chat_id": "-10070002", "topic_key": "177"},
            },
        )
        group = db.get(Binding, result["id"])
        assert not group.active and not group.verified
        admin.apply(db, actor, {"type": "probe_binding", "data": {"id": group.id, "nonce": "audit-clean"}})
        group_id = group.id
    monkeypatch.setattr(worker, "Session", factory)
    assert worker.outbox_step(FakeTransport())
    with factory.begin() as db:
        group = db.get(Binding, group_id)
        assert group.verified and not group.active
        actor = identify(db, "telegram", "70001", "70001")
        admin.apply(db, actor, {"type": "activate_binding", "data": {"id": group.id, "version": group.version}})
        person = admin.apply(db, actor, {"type": "person", "data": {"name": "Куратор"}})
        admin.apply(
            db,
            actor,
            {"type": "account", "data": {"person_id": person["id"], "platform": "telegram", "external_id": "70003"}},
        )
        admin.apply(db, actor, {"type": "role", "data": {"person_id": person["id"], "name": "curator"}})
        curator = identify(db, "telegram", "70003", "-10070002", "177")
        assert access.staff_role(db, curator, "curator")
        assert access.staff(db, curator)
        with pytest.raises(AccessDenied):
            admin.check_admin(db, curator)
        with pytest.raises(AccessDenied):
            identify(db, "telegram", "70003", "-10070002", "178")
        bot.handle(
            db,
            "telegram",
            {
                "update_id": 70010,
                "message": {
                    "message_id": 1,
                    "from": {"id": 70001},
                    "chat": {"id": 70001, "type": "private"},
                    "text": "/af_settings",
                },
            },
        )
        db.flush()
        assert any("Настройки АМА" in x.payload.get("text", "") for x in db.scalars(select(Outbox)))


def test_init_existing_account_reuses_person(ctx, monkeypatch):
    ctx.db.execute(delete(Role).where(Role.name == "admin"))
    ctx.db.commit()
    initial(monkeypatch, ctx.factory, "admin", "70011")
    with ctx.factory() as db:
        r = db.scalar(select(Role).where(Role.name == "admin"))
        assert r.person_id == "admin"


def test_init_repeat_is_explicitly_blocked_without_duplicates(ctx, monkeypatch):
    ctx.db.execute(delete(Role).where(Role.name == "admin"))
    ctx.db.commit()
    initial(monkeypatch, ctx.factory)
    with pytest.raises(SystemExit):
        initial(monkeypatch, ctx.factory)
    with ctx.factory() as db:
        assert len(list(db.scalars(select(Account).where(Account.external_id == "70001")))) == 1


def test_other_bot_admin_does_not_block_initialization(ctx, monkeypatch):
    role = ctx.db.scalar(select(Role).where(Role.name == "admin"))
    role.bot_id = "other-bot"
    ctx.db.commit()
    initial(monkeypatch, ctx.factory)


def test_expired_admin_does_not_block_initialization(ctx, monkeypatch):
    role = ctx.db.scalar(select(Role).where(Role.name == "admin"))
    role.expires_at = now() - timedelta(days=1)
    ctx.db.commit()
    initial(monkeypatch, ctx.factory)


def test_stale_probe_is_cancelled_before_external_send(ctx, monkeypatch):
    FakeTransport.calls = []
    b = Binding(platform="telegram", chat_id="-10070021", purpose="support", active=False, verified=False)
    ctx.db.add(b)
    ctx.db.flush()
    admin.apply(ctx.db, ctx.admin, {"type": "probe_binding", "data": {"id": b.id, "nonce": "stale"}})
    admin.apply(
        ctx.db,
        ctx.admin,
        {
            "type": "binding_update",
            "data": {"id": b.id, "version": b.version, "chat_id": "-10070022", "reason": "Исправление ID"},
        },
    )
    ctx.db.commit()
    monkeypatch.setattr(worker, "Session", ctx.factory)
    worker.outbox_step(FakeTransport())
    assert not FakeTransport.calls, "Stale route probe was sent to the OLD chat"


def test_manual_probe_delivery_confirmation_verifies_binding(ctx, monkeypatch):
    b = Binding(platform="telegram", chat_id="-10070023", purpose="support", active=False, verified=False)
    ctx.db.add(b)
    ctx.db.flush()
    row = Outbox(
        dedupe_key="manual-probe",
        platform="telegram",
        chat_id=b.chat_id,
        binding_id=b.id,
        binding_version=b.version,
        payload={"probe": True, "text": "test"},
        status="uncertain",
    )
    ctx.db.add(row)
    ctx.db.commit()
    bid = b.id
    oid = row.id
    command(monkeypatch, ctx.factory, "mark-delivered", oid, "actual-message-id")
    ctx.db.expire_all()
    assert ctx.db.get(Binding, bid).verified, "CLI marks delivery sent but leaves probe unverified"


def test_replace_binding_updates_published_route_for_future_work(ctx):
    admin.apply(
        ctx.db, ctx.admin, {"type": "replace_binding", "data": {"old_id": "support", "new_id": "new", "version": 1}}
    )
    r = ctx.db.scalar(select(Record).where(Record.kind == "route", Record.key == "support"))
    assert r.data["binding_id"] == "new", "New requests still resolve the disabled old support route"


def test_client_activation_rejects_expired_partner(ctx):
    for r in ctx.db.scalars(select(Role).where(Role.person_id == "fp")):
        r.expires_at = now() - timedelta(days=1)
    b = ctx.db.get(Binding, "client")
    b.active = False
    b.active_client_key = None
    ctx.db.flush()
    with pytest.raises(BusinessError):
        admin.apply(ctx.db, ctx.admin, {"type": "activate_binding", "data": {"id": "client", "version": b.version}})


def test_http_logging_does_not_disclose_bot_token(monkeypatch, caplog):
    t = Transport()
    fake = "000000:FAKE_AUDIT_SECRET"
    t.cfg = SimpleNamespace(telegram_token=fake)
    t.client.close()
    t.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
    )
    try:
        with caplog.at_level(logging.INFO):
            t.send("telegram", "-10070030", "", {"text": "audit"})
        assert fake not in caplog.text, "INFO HTTP logger prints the full bot token in the URL"
    finally:
        t.close()


def test_published_internal_route_rejects_client_chat(ctx):
    with pytest.raises(BusinessError):
        admin.apply(
            ctx.db,
            ctx.admin,
            {
                "type": "record",
                "data": {
                    "kind": "route",
                    "key": "bad-internal",
                    "owner_id": "curator",
                    "published": True,
                    "value": {
                        "binding_id": "client",
                        "owner_id": "curator",
                        "reviewer_id": "curator",
                        "calendar": "support",
                    },
                },
            },
        )


def test_webhook_registration_reports_telegram_reason(monkeypatch, capsys):
    cfg = SimpleNamespace(
        public_url="https://example.org/franchising-bot",
        telegram_token="FAKE_TOKEN",
        telegram_webhook_secret="x" * 32,
        max_token="",
    )

    class WebTransport:
        def __init__(self):
            self.client = httpx.Client(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        400, json={"ok": False, "description": "Bad Request: failed to resolve host"}
                    )
                )
            )

        def close(self):
            self.client.close()

    monkeypatch.setattr(cli, "settings", lambda: cfg)
    monkeypatch.setattr(cli, "Transport", WebTransport)
    monkeypatch.setattr(sys, "argv", ["ama.cli", "register-webhooks"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "failed to resolve host" in capsys.readouterr().err


def test_webhook_registration_without_any_token_is_rejected(monkeypatch):
    cfg = SimpleNamespace(public_url="https://example.org", telegram_token="", max_token="")
    monkeypatch.setattr(cli, "settings", lambda: cfg)
    monkeypatch.setattr(cli, "Transport", FakeTransport)
    monkeypatch.setattr(sys, "argv", ["ama.cli", "register-webhooks"])
    with pytest.raises(SystemExit):
        cli.main()


def test_init_existing_management_binding_is_reused(ctx, monkeypatch):
    ctx.db.execute(delete(Role).where(Role.name == "admin"))
    ctx.db.commit()
    initial(monkeypatch, ctx.factory, "70001", "management")
    with ctx.factory() as db:
        assert len(list(db.scalars(select(Binding).where(Binding.chat_id == "management")))) == 1


def test_support_route_rejects_owner_without_support_permissions(ctx):
    with pytest.raises(BusinessError):
        admin.apply(
            ctx.db,
            ctx.admin,
            {
                "type": "record",
                "data": {
                    "kind": "route",
                    "key": "support",
                    "version": 1,
                    "owner_id": "curator",
                    "published": True,
                    "value": {
                        "binding_id": "support",
                        "owner_id": "marketing",
                        "reviewer_id": "curator",
                        "calendar": "support",
                    },
                },
            },
        )


def test_recover_same_admin_keeps_person_role_and_binding(ctx, monkeypatch):
    binding = ctx.db.get(Binding, "management")
    binding.active = False
    ctx.db.commit()
    original_version = binding.version
    role = ctx.db.scalar(select(Role).where(Role.person_id == "admin", Role.name == "admin"))
    role_id = role.id
    ctx.db.rollback()  # End MySQL REPEATABLE READ snapshot before the CLI transaction.
    command(
        monkeypatch,
        ctx.factory,
        "init-admin",
        "--platform",
        "telegram",
        "--user-id",
        "admin",
        "--chat-id",
        "management",
        "--recover",
    )
    ctx.db.expire_all()
    assert binding.active and binding.verified and binding.version == original_version + 1
    assert ctx.db.get(Role, role_id).active
    assert len(list(ctx.db.scalars(select(Role).where(Role.person_id == "admin", Role.name == "admin")))) == 1


def test_recover_does_not_grant_other_account_admin(ctx, monkeypatch):
    with pytest.raises(SystemExit):
        command(
            monkeypatch,
            ctx.factory,
            "init-admin",
            "--platform",
            "telegram",
            "--user-id",
            "curator",
            "--chat-id",
            "70001",
            "--recover",
        )
    assert not ctx.db.scalar(select(Role).where(Role.person_id == "curator", Role.name == "admin"))


def test_bootstrap_failed_delivery_rolls_back_new_admin(factory, monkeypatch):
    class Rejected(FakeTransport):
        def send(self, *args, **kwargs):
            raise BusinessError("HTTP 400: chat not found")

    monkeypatch.setattr(cli, "Session", factory)
    monkeypatch.setattr(cli, "Transport", Rejected)
    monkeypatch.setattr(
        sys, "argv", ["ama.cli", "init-admin", "--platform", "telegram", "--user-id", "70001", "--chat-id", "70001"]
    )
    with pytest.raises(SystemExit):
        cli.main()
    with factory() as db:
        assert not db.scalar(select(Role))
        assert not db.scalar(select(Account))
        assert not db.scalar(select(Binding))


def test_probe_confirmation_of_old_version_does_not_verify_new_chat(ctx, monkeypatch):
    b = Binding(platform="telegram", chat_id="-10070023", purpose="support", active=False, verified=False, version=2)
    ctx.db.add(b)
    ctx.db.flush()
    row = Outbox(
        dedupe_key="old-manual-probe",
        platform="telegram",
        chat_id="-10070024",
        binding_id=b.id,
        binding_version=1,
        payload={"probe": True, "text": "test"},
        status="uncertain",
    )
    ctx.db.add(row)
    ctx.db.commit()
    oid = row.id
    bid = b.id
    command(monkeypatch, ctx.factory, "mark-delivered", oid, "old-actual-message")
    ctx.db.expire_all()
    assert not ctx.db.get(Binding, bid).verified


def test_replace_route_versions_and_new_partner_request(ctx):
    from ama.service import create_work
    from ama.models import ConfigurationRevision

    original = ctx.db.scalar(select(Record).where(Record.kind == "route", Record.key == "support"))
    assert original.version == 1
    admin.apply(
        ctx.db,
        ctx.admin,
        {"type": "replace_binding", "data": {"old_id": "support", "new_id": "new", "version": 1, "reason": "Переезд"}},
    )
    assert original.version == 2
    assert (
        ctx.db.scalar(select(ConfigurationRevision).where(ConfigurationRevision.object_id == original.id)).after[
            "data"
        ]["binding_id"]
        == "new"
    )
    w = create_work(ctx.db, ctx.fp, {"title": "Новое обращение после переезда"}, "setup-new-after-replace")
    assert w.binding_id == "new" and w.owner_id == "curator"


def test_client_activation_rejects_missing_account_and_foreign_bot_role(ctx):
    b = ctx.db.get(Binding, "client")
    b.active = False
    b.active_client_key = None
    role = ctx.db.scalar(select(Role).where(Role.person_id == "fp", Role.name == "partner"))
    role.bot_id = "other-bot"
    ctx.db.flush()
    with pytest.raises(BusinessError):
        admin.apply(ctx.db, ctx.admin, {"type": "activate_binding", "data": {"id": b.id, "version": b.version}})
    role.bot_id = "ama-franchising"
    ctx.db.scalar(select(Account).where(Account.person_id == "fp")).active = False
    ctx.db.flush()
    with pytest.raises(BusinessError):
        admin.apply(ctx.db, ctx.admin, {"type": "activate_binding", "data": {"id": b.id, "version": b.version}})


def test_transport_error_includes_reason_without_echoed_credential(monkeypatch):
    from ama.logging_security import response_error

    fake = "FAKE_TEST_TOKEN"
    response = httpx.Response(400, json={"description": "Bad Request: chat not found " + fake})
    text = response_error(response, [fake])
    assert "chat not found" in text and fake not in text


def test_support_route_rejects_inaccessible_review_profile(ctx):
    ctx.db.add(Role(person_id="curator", name="support_head"))
    ctx.db.flush()
    with pytest.raises(BusinessError):
        admin.apply(
            ctx.db,
            ctx.admin,
            {
                "type": "record",
                "data": {
                    "kind": "route",
                    "key": "support",
                    "version": 1,
                    "owner_id": "curator",
                    "published": True,
                    "value": {
                        "binding_id": "management",
                        "owner_id": "curator",
                        "reviewer_id": "other",
                        "calendar": "support",
                    },
                },
            },
        )

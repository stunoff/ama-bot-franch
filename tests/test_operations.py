from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import pytest
import httpx
from sqlalchemy import select, func
from ama import operations_service as o, preflight
from ama.ops_cli import import_acceptance
from ama.ops_snapshot import snapshot, extract, file_inventory
from ama.models import OperationsJob, Role, now
from ama.config import settings
from ama.errors import AccessDenied, BusinessError
from ama.ui import business, engine, controller
from ama.probe import run as probe


def request(ctx, kind="backup", backup_id=None, key="job-one"):
    return o.request(ctx.db, ctx.admin, kind, "Проверочная процедура", key, backup_id)


def test_admin_request_is_queued_and_does_not_run_docker(ctx):
    j = request(ctx)
    assert j.status == "queued" and j.started_at is None
    assert request(ctx).id == j.id
    with pytest.raises(AccessDenied):
        o.request(ctx.db, ctx.curator, "backup", "reason", "forged")


def test_restore_request_requires_successful_backup_from_this_bot(ctx):
    with pytest.raises(BusinessError):
        request(ctx, "verify_restore", "unknown")
    j = request(ctx)
    j.status = "succeeded"
    assert request(ctx, "verify_restore", j.id, "verify").data["backup_id"] == j.id
    j.bot_id = "other-bot"
    with pytest.raises(BusinessError):
        request(ctx, "verify_restore", j.id, "another")


def test_one_running_host_procedure_and_claim_token(ctx):
    first = request(ctx)
    second = request(ctx, key="second")
    assert o.claim(ctx.db).id == first.id and o.claim(ctx.db) is None
    with pytest.raises(BusinessError):
        o.finish(ctx.db, first.id, "not-the-runner", True, {})
    token = first.data["claim_token"]
    o.finish(ctx.db, first.id, token, True, {"backup_id": first.id, "manifest_sha256": "a" * 64})
    assert first.finished_at and "claim_token" not in first.data
    assert o.claim(ctx.db).id == second.id


def test_revoked_admin_request_is_not_executed(ctx):
    j = request(ctx)
    ctx.db.scalar(select(Role).where(Role.person_id == "admin")).active = False
    assert o.claim(ctx.db) is None and j.status == "cancelled"


def test_queued_job_cancel_and_running_job_not_blindly_restarted(ctx):
    j = request(ctx)
    o.cancel(ctx.db, ctx.admin, j.id, j.version, "Операция больше не требуется")
    assert j.status == "cancelled"
    other = request(ctx, key="next")
    o.claim(ctx.db)
    with pytest.raises(BusinessError):
        o.cancel(ctx.db, ctx.admin, other.id, other.version, "cancel running")
    assert other.status == "running" and o.claim(ctx.db) is None


def test_preview_of_maintenance_is_transactional(ctx):
    d = business.make_draft(ctx.db, ctx.admin, "ops:backup", "Копия", {"reason": "Проверка"})
    result = business.preview(ctx.db, ctx.admin, d)
    assert result["status"] == "queued" and ctx.db.scalar(select(func.count()).select_from(OperationsJob)) == 0
    screen = engine.session(ctx.db, ctx.admin)
    controller.dispatch(ctx.db, ctx.admin, screen, "preview", {"draft_id": d.id})
    business.confirm(ctx.db, ctx.admin, d, d.version)
    assert ctx.db.scalar(select(func.count()).select_from(OperationsJob)) == 1


def test_snapshot_is_deterministic_and_detects_changed_business_fact(ctx, tmp_path):
    (tmp_path / "file.txt").write_text("private proof")
    before = snapshot(ctx.db.bind, tmp_path)
    assert before == snapshot(ctx.db.bind, tmp_path)
    ctx.w.data = {**ctx.w.data, "actual_payment_proof": {"amount_minor": 1000}}
    ctx.db.commit()
    after = snapshot(ctx.db.bind, tmp_path)
    assert before["tables"]["works"]["sha256"] != after["tables"]["works"]["sha256"]
    assert before["files"] == after["files"]


def archive(members):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as t:
        for name, content, kind in members:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            if kind == "link":
                item.type = tarfile.SYMTYPE
                item.linkname = "../../secret"
            t.addfile(item, io.BytesIO(content) if kind != "link" else None)
    data.seek(0)
    return data


@pytest.mark.parametrize(
    "name,kind", [("../escape", "file"), ("/absolute", "file"), ("files/../../escape", "file"), ("files/link", "link")]
)
def test_restore_rejects_path_traversal_and_links(tmp_path, name, kind):
    with pytest.raises(BusinessError):
        extract(archive([(name, b"private", kind)]), tmp_path, 100, 10)


def test_restore_is_empty_destination_only_and_checks_declared_inventory(tmp_path):
    data = archive([("files/sub/document.txt", b"proof", "file")])
    extract(data, tmp_path, 5, 1)
    assert (tmp_path / "sub/document.txt").read_bytes() == b"proof"
    with pytest.raises(BusinessError):
        extract(archive([]), tmp_path, 0, 0)


def test_restore_rejects_oversize_and_duplicate_members(tmp_path):
    with pytest.raises(BusinessError):
        extract(archive([("files/file", b"toolong", "file")]), tmp_path, 1, 1)
    other = tmp_path / "empty"
    other.mkdir()
    with pytest.raises(BusinessError):
        extract(archive([("files/file", b"one", "file"), ("files/file", b"two", "file")]), other, 100, 2)


def test_file_inventory_never_follows_symlinks(tmp_path):
    (tmp_path / "file").write_text("content")
    (tmp_path / "link").symlink_to(tmp_path / "file")
    with pytest.raises(BusinessError):
        file_inventory(tmp_path)


def acceptance(ctx, result="passed", **changes):
    return {
        "version": preflight.VERSION,
        "configuration_digest": preflight.configuration_digest(ctx.db),
        "records": [
            {
                "case": "П01",
                "platform": "telegram",
                "result": result,
                "operator": "Named tester",
                "performed_at": (now() - timedelta(minutes=1)).isoformat() + "Z",
                "evidence_reference": "private-test-protocol-1",
                **changes,
            }
        ],
    }


def test_live_acceptance_requires_actual_operator_evidence_and_configuration(ctx):
    payload = acceptance(ctx, evidence_reference="")
    with pytest.raises(BusinessError):
        import_acceptance(ctx.db, payload)
    payload = acceptance(ctx)
    payload["configuration_digest"] = "old"
    with pytest.raises(BusinessError):
        import_acceptance(ctx.db, payload)
    proof = acceptance(ctx)
    assert import_acceptance(ctx.db, proof)["recorded"] == 1
    assert import_acceptance(ctx.db, proof)["recorded"] == 0


def test_inapplicability_needs_reason_and_future_test_is_rejected(ctx):
    with pytest.raises(BusinessError):
        import_acceptance(ctx.db, acceptance(ctx, "not_applicable"))
    with pytest.raises(BusinessError):
        import_acceptance(ctx.db, acceptance(ctx, performed_at=(now() + timedelta(days=1)).isoformat() + "Z"))


def test_preflight_does_not_claim_live_readiness_and_hides_secrets(ctx, monkeypatch):
    secret = "never-print-this-token"
    monkeypatch.setattr(settings(), "telegram_token", secret)
    result = preflight.report(ctx.db)
    assert not result["ready"] and result["acceptance"]["covered"] == 0
    assert secret not in json.dumps(result)
    assert any(x["code"] == "live_acceptance_incomplete" for x in result["issues"])


def test_new_failed_evidence_supersedes_old_pass_for_same_configuration(ctx, monkeypatch):
    monkeypatch.setattr(settings(), "telegram_token", "test-token")
    import_acceptance(ctx.db, acceptance(ctx))
    ctx.db.flush()
    assert preflight.report(ctx.db)["acceptance"]["covered"] == 1
    import_acceptance(ctx.db, acceptance(ctx, "failed"))
    ctx.db.flush()
    assert preflight.report(ctx.db)["acceptance"]["covered"] == 0


def test_readonly_live_probe_redacts_tokens_and_does_not_send_messages(ctx, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "telegram_token", "test-secret-token")
    monkeypatch.setattr(cfg, "public_url", "https://approved.example")
    seen = []

    def handler(request):
        seen.append(request.method)
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 123}})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"url": "https://approved.example/webhooks/telegram", "pending_update_count": 0},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe("telegram", client)
    assert result["ready"] and result["messages_sent"] == 0 and seen == ["GET", "GET"]
    assert cfg.telegram_token not in json.dumps(result)


def runner_module():
    spec = importlib.util.spec_from_file_location(
        "maintenance_runner", Path(__file__).resolve().parents[1] / "scripts/operations-runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backup_manifest_checks_every_artifact_and_rejects_tampering(tmp_path):
    runner = runner_module()
    ident = "a" * 32
    folder = tmp_path / ident
    folder.mkdir()
    for name in ("mysql.sql.gz", "files.tar.gz"):
        (folder / name).write_bytes(b"test-private-backup")
    payload = {
        "format": 1,
        "backup_id": ident,
        "artifacts": {name: runner.digest(folder / name) for name in ("mysql.sql.gz", "files.tar.gz")},
    }
    (folder / "manifest.json").write_text(json.dumps(payload))
    assert runner.validate_backup(tmp_path, ident)[1] == payload
    (folder / "mysql.sql.gz").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum"):
        runner.validate_backup(tmp_path, ident)
    with pytest.raises(RuntimeError):
        runner.safe_directory(tmp_path, "../../anything")


def test_backup_failure_restarts_only_previously_running_services(tmp_path, monkeypatch):
    runner = runner_module()
    calls = []

    def compose(*args, **kwargs):
        calls.append(args)
        if args == ("ps", "--status", "running", "--services"):
            return b"api\nmysql\n"
        if args[0] in {"stop", "start"}:
            return b""
        raise RuntimeError("controlled export failure")

    monkeypatch.setattr(runner, "compose", compose)
    with pytest.raises(RuntimeError):
        runner.backup(tmp_path, "b" * 32)
    assert ("stop", "api") in calls and ("start", "api") in calls
    assert not (tmp_path / ("b" * 32)).exists()


def test_concurrent_host_runners_claim_only_one_maintenance_job(ctx):
    if ctx.db.bind.dialect.name != "mysql":
        pytest.skip("Requires MySQL row locks")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    request(ctx, key="first")
    request(ctx, key="second")
    ctx.db.commit()
    barrier = Barrier(2)

    def claim(_):
        with ctx.factory.begin() as db:
            barrier.wait()
            job = o.claim(db)
            return job.id if job else None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(item is not None for item in results) == 1


def test_real_database_and_file_restore_preserve_full_snapshot(ctx, tmp_path):
    if ctx.db.bind.dialect.name != "sqlite":
        pytest.skip("SQLite backup API; native MySQL restore checked separately")
    import sqlite3
    import shutil
    from sqlalchemy import create_engine

    source = tmp_path / "source"
    source.mkdir()
    (source / "proof.txt").write_text("actual proof")
    ctx.w.data = {**ctx.w.data, "signed": {"version": 4}, "partial_payment": {"amount_minor": 1234}}
    ctx.db.commit()
    before = snapshot(ctx.db.bind, source)
    target = tmp_path / "restored.db"
    with ctx.db.bind.connect() as conn, sqlite3.connect(target) as dest:
        conn.connection.driver_connection.backup(dest)
    restored_files = tmp_path / "restored-files"
    shutil.copytree(source, restored_files)
    db_engine = create_engine("sqlite:///" + str(target))
    try:
        assert snapshot(db_engine, restored_files) == before
    finally:
        db_engine.dispose()


def test_storage_policy_requires_real_owner_approval_and_adapter_limits(ctx):
    from ama.storage_policy import validate

    with pytest.raises(BusinessError):
        validate({"policy_type": "storage"})
    data = {
        "approval": "Approved",
        "location": "Owner-specified location",
        "access_owner": "Responsible person",
        "error_recipient": "Assigned route",
        "retention_basis": "Approved policy",
        "retention_days": 90,
        "max_file_bytes": 1024,
        "backup_max_age_hours": 24,
        "restore_max_age_days": 30,
        "extensions": [".pdf"],
    }
    validate(data)
    with pytest.raises(BusinessError):
        validate({**data, "extensions": [".exe"]})
    with pytest.raises(BusinessError):
        validate({**data, "max_file_bytes": settings().max_file_bytes + 1})


def test_readonly_max_probe_checks_subscription_without_messages(ctx, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "max_token", "private-max-token")
    monkeypatch.setattr(cfg, "public_url", "https://approved.example")

    def handler(req):
        assert req.method == "GET"
        if req.url.path.endswith("subscriptions"):
            return httpx.Response(200, json={"subscriptions": [{"url": "https://approved.example/webhooks/max"}]})
        return httpx.Response(200, json={"user_id": 123})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = probe("max", client)
    assert result["ready"] and result["messages_sent"] == 0 and not result["live_acceptance_completed"]
    assert cfg.max_token not in json.dumps(result)

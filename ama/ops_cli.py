"""Operator-only CLI. Output is machine-readable and never includes credentials."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select
from ama.db import Session
from ama import operations_service as ops
from ama.models import OperationsJob, Audit, now
from ama.preflight import report, cases, configuration_digest, VERSION
from ama.config import settings
from ama.errors import BusinessError


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("preflight")
    req = sub.add_parser("request")
    req.add_argument("kind", choices=sorted(ops.KINDS))
    req.add_argument("--reason", required=True)
    req.add_argument("--backup-id")
    sub.add_parser("claim")
    finish = sub.add_parser("finish")
    finish.add_argument("id")
    finish.add_argument("--success", action="store_true")
    sub.add_parser("acceptance-template")
    record = sub.add_parser("acceptance-import")
    record.add_argument("path")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("id")
    reconcile.add_argument("--reason", required=True)
    reconcile.add_argument("--failed", action="store_true", required=True)
    args = p.parse_args()
    with Session.begin() as db:
        if args.action == "preflight":
            result = report(db)
        elif args.action == "request":
            j = ops.request(db, None, args.kind, args.reason, "operator:" + uuid4().hex, args.backup_id)
            result = {"id": j.id, "status": j.status}
        elif args.action == "claim":
            j = ops.claim(db)
            result = (
                {"id": j.id, "kind": j.kind, "backup_id": j.data.get("backup_id"), "claim_token": j.data["claim_token"]}
                if j
                else None
            )
        elif args.action == "finish":
            data = json.load(__import__("sys").stdin)
            j = ops.finish(db, args.id, data["claim_token"], args.success, data.get("details", {}))
            result = {"id": j.id, "status": j.status}
        elif args.action == "reconcile":
            j = db.scalar(select(OperationsJob).where(OperationsJob.id == args.id).with_for_update())
            if not j or j.bot_id != settings().bot_id or j.status != "running":
                raise BusinessError("Only an inspected running job may be marked failed")
            j.status = "failed"
            j.finished_at = now()
            j.version += 1
            j.data = {**{k: v for k, v in j.data.items() if k != "claim_token"}, "operator_reconciliation": args.reason}
            db.add(Audit(object_id=j.id, action="OPS_RECONCILED", data={"reason": args.reason}))
            result = {"id": j.id, "status": j.status}
        elif args.action == "acceptance-template":
            result = {
                "version": VERSION,
                "configuration_digest": configuration_digest(db),
                "records": [
                    {
                        "case": c["ID"],
                        "platform": platform,
                        "result": "pending",
                        "operator": "",
                        "performed_at": "",
                        "evidence_reference": "",
                        "reason": "",
                    }
                    for c in cases()
                    for platform in ("telegram", "max")
                ],
            }
        else:
            payload = json.loads(Path(args.path).read_text())
            result = import_acceptance(db, payload)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def import_acceptance(db, payload):
    fingerprint = configuration_digest(db)
    if payload.get("version") != VERSION or payload.get("configuration_digest") != fingerprint:
        raise BusinessError("Version or configuration changed; regenerate acceptance template")
    valid = {c["ID"] for c in cases()}
    seen = set()
    count = 0
    for item in payload.get("records", []):
        if item.get("result") == "pending":
            continue
        key = (item.get("case"), item.get("platform"))
        if key in seen or key[0] not in valid or key[1] not in {"telegram", "max"}:
            raise BusinessError("Invalid or duplicate acceptance scenario")
        seen.add(key)
        if item.get("result") not in {"passed", "failed", "not_applicable"} or not all(
            item.get(k) for k in ("operator", "performed_at", "evidence_reference")
        ):
            raise BusinessError("Human evidence, operator and actual time required")
        at = datetime.fromisoformat(item["performed_at"].replace("Z", "+00:00"))
        if at.tzinfo is None or at.astimezone(timezone.utc).replace(tzinfo=None) > now():
            raise BusinessError("Actual timezone-aware past time required")
        if item["result"] == "not_applicable" and not item.get("reason"):
            raise BusinessError("Inapplicability requires an explicit reason")
        digest = (
            __import__("hashlib")
            .sha256(json.dumps({**item, "configuration_digest": fingerprint}, sort_keys=True).encode())
            .hexdigest()
        )
        if db.scalar(select(OperationsJob.id).where(OperationsJob.origin_key == "acceptance:" + digest)):
            continue
        db.add(
            OperationsJob(
                bot_id=settings().bot_id,
                origin_key="acceptance:" + digest,
                kind="acceptance",
                status="succeeded",
                finished_at=now(),
                data={**item, "version": VERSION, "configuration_digest": fingerprint},
            )
        )
        count += 1
    db.add(
        Audit(
            object_id="acceptance",
            action="ACCEPTANCE_EVIDENCE_IMPORTED",
            data={"count": count, "configuration_digest": fingerprint},
        )
    )
    return {"recorded": count, "live_acceptance_is_human_attestation": True}


if __name__ == "__main__":
    main()

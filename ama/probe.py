"""Explicit read-only live transport probe; no test messages, route activation or webhook changes."""

import argparse
import json
from ama.adapters import Transport
from ama.config import settings
from ama.db import Session
from ama.models import Binding
from sqlalchemy import select


def run(platform, client, bindings=()):
    cfg = settings()
    checks = []

    def get(code, url, **kwargs):
        try:
            r = client.get(url, timeout=15, **kwargs)
            data = r.json()
            okay = r.status_code == 200 and (data.get("ok", True) if isinstance(data, dict) else True)
            checks.append({"check": code, "ok": bool(okay), "http_status": r.status_code})
            return data if okay else None
        except Exception as error:
            checks.append({"check": code, "ok": False, "error_type": type(error).__name__})
            return None

    if platform == "telegram":
        if not cfg.telegram_token:
            return {"platform": platform, "ready": False, "checks": [{"check": "token_configured", "ok": False}]}
        base = "https://api.telegram.org/bot" + cfg.telegram_token
        me = get("getMe", base + "/getMe")
        hook = get("getWebhookInfo", base + "/getWebhookInfo")
        if hook:
            info = hook.get("result", {})
            checks.append(
                {"check": "webhook_matches", "ok": info.get("url") == cfg.public_url.rstrip("/") + "/webhooks/telegram"}
            )
            checks.append(
                {
                    "check": "webhook_delivery",
                    "ok": info.get("pending_update_count", 0) == 0,
                    "historical_error_recorded": bool(info.get("last_error_message")),
                    "pending": info.get("pending_update_count", 0),
                }
            )
        if me:
            for b in bindings:
                chat = get("chat:" + b.id, base + "/getChat", params={"chat_id": b.chat_id})
                member = get(
                    "bot_rights:" + b.id,
                    base + "/getChatMember",
                    params={"chat_id": b.chat_id, "user_id": me["result"]["id"]},
                )
                if member:
                    status = member.get("result", {}).get("status")
                    checks.append(
                        {"check": "member_access:" + b.id, "ok": status not in {"left", "kicked", "restricted"}}
                    )
                    if chat and chat.get("result", {}).get("is_forum") and b.purpose != "client":
                        checks.append(
                            {
                                "check": "forum_topics:" + b.id,
                                "ok": bool(member.get("result", {}).get("can_manage_topics") or status == "creator"),
                            }
                        )
    else:
        if not cfg.max_token:
            return {"platform": platform, "ready": False, "checks": [{"check": "token_configured", "ok": False}]}
        headers = {"Authorization": cfg.max_token}
        get("me", cfg.max_api_url + "/me", headers=headers)
        subs = get("subscriptions", cfg.max_api_url + "/subscriptions", headers=headers)
        if subs:
            checks.append(
                {
                    "check": "subscription_matches",
                    "ok": any(
                        s.get("url") == cfg.public_url.rstrip("/") + "/webhooks/max"
                        for s in subs.get("subscriptions", [])
                    ),
                }
            )
        for b in bindings:
            get("chat:" + b.id, cfg.max_api_url + "/chats/" + b.chat_id, headers=headers)
    return {
        "platform": platform,
        "ready": bool(checks) and all(x["ok"] for x in checks),
        "checks": checks,
        "messages_sent": 0,
        "live_acceptance_completed": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--platform", choices=["telegram", "max"], required=True)
    p.add_argument("--check-chats", action="store_true")
    a = p.parse_args()
    with Session() as db:
        bindings = (
            list(
                db.scalars(
                    select(Binding).where(
                        Binding.bot_id == settings().bot_id, Binding.platform == a.platform, Binding.active.is_(True)
                    )
                )
            )
            if a.check_chats
            else []
        )
        transport = Transport()
        try:
            result = run(a.platform, transport.client, bindings)
        finally:
            transport.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

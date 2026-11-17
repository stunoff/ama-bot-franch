import hmac
import json
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from ama.config import settings
from ama.db import Session
from ama.models import Inbox
from ama.adapters import normalize

app = FastAPI(title="АМА Франчайзинг", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    try:
        with Session() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


async def ingest(platform, request, expected, header):
    if not expected or not hmac.compare_digest(request.headers.get(header, ""), expected):
        raise HTTPException(403, "Invalid webhook secret")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "Update too large")
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError
        event = normalize(platform, data)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Malformed update")
    if not event:
        return {"ok": True}
    try:
        with Session.begin() as db:
            db.add(Inbox(key=event.key, platform=platform, payload=data))
    except IntegrityError:
        pass  # webhook redelivery: исходное событие уже сохранено.
    return {"ok": True}


@app.post("/webhooks/telegram")
async def telegram(request: Request):
    return await ingest("telegram", request, settings().telegram_webhook_secret, "x-telegram-bot-api-secret-token")


@app.post("/webhooks/max")
async def max_hook(request: Request):
    return await ingest("max", request, settings().max_webhook_secret, "x-max-bot-api-secret")

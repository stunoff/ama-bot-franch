import hmac
import json
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from ama.config import settings
from ama.db import Session
from ama.models import Inbox
from ama.adapters import normalize

app = FastAPI(title="АМА Франчайзинг", version="0.11.1", docs_url=None, redoc_url=None, openapi_url=None)


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


@app.get("/landing/{source_key}")
def landing(source_key: str, request: Request, platform: str = "telegram"):
    from fastapi.responses import RedirectResponse
    from ama.prospects import landing_ticket
    from ama.errors import BusinessError
    if len(source_key) > 100 or len(request.url.query) > 2048:
        raise HTTPException(400, "Parameters too long")
    try:
        with Session.begin() as db:
            url = landing_ticket(db, source_key, platform, dict(request.query_params))
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    except BusinessError as error:
        raise HTTPException(429 if "Слишком много" in str(error) else 503, str(error))

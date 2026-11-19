"""Только протокол мессенджера: бизнес-прав здесь нет."""

from dataclasses import dataclass, field
import hashlib
import json
import httpx
from ama.config import settings
from ama.errors import BusinessError


@dataclass
class Incoming:
    key: str
    platform: str
    user_id: str
    chat_id: str
    topic: str
    text: str
    callback: str | None = None
    callback_id: str | None = None
    private: bool = False
    files: list = field(default_factory=list)
    reply_message_id: str | None = None


def normalize(platform, raw):
    if platform == "telegram":
        cb = raw.get("callback_query")
        msg = (cb or {}).get("message") or raw.get("message")
        if not msg:
            return None
        user = (cb or {}).get("from") or msg.get("from", {})
        if user.get("is_bot"):
            return None
        files = []
        if msg.get("photo") and not cb:
            photo = msg["photo"][-1]
            files.append({"file_id": photo["file_id"], "filename": "photo.jpg", "album_id": msg.get("media_group_id")})
        if msg.get("document") and not cb:
            doc = msg["document"]
            files.append(
                {
                    "file_id": doc["file_id"],
                    "filename": doc.get("file_name", "document"),
                    "album_id": msg.get("media_group_id"),
                }
            )
        return Incoming(
            key=f"tg:{raw['update_id']}",
            platform=platform,
            user_id=str(user["id"]),
            chat_id=str(msg["chat"]["id"]),
            topic=str(msg.get("message_thread_id", "")),
            text=msg.get("text") or msg.get("caption") or "",
            callback=(cb or {}).get("data"),
            callback_id=(cb or {}).get("id"),
            private=msg["chat"].get("type") == "private",
            files=files,
            reply_message_id=str(msg["reply_to_message"]["message_id"]) if msg.get("reply_to_message") else None,
        )
    if platform == "max":
        cb = raw.get("callback")
        msg = raw.get("message", {})
        user = (cb or {}).get("user") or msg.get("sender") or raw.get("user", {})
        recipient = msg.get("recipient", {})
        chat_id = recipient.get("chat_id") or raw.get("chat_id")
        if not chat_id or not user.get("user_id") or user.get("is_bot"):
            return None
        body = msg.get("body", {})
        # Callback ID уникален; timestamp сам по себе ключом события не является.
        event_id = (cb or {}).get("callback_id") or f"{raw.get('update_type')}:{body.get('mid', '')}"
        if not event_id or event_id.endswith(":"):
            event_id = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        files = []
        for a in body.get("attachments", []) if not cb else []:
            if a.get("type") in {"image", "file"}:
                payload = a.get("payload", {})
                files.append(
                    {
                        "file_id": json.dumps(
                            {"url": payload.get("url"), "token": payload.get("token"), "mid": body.get("mid")}
                        ),
                        "filename": a.get("filename")
                        or payload.get("filename")
                        or ("photo.jpg" if a["type"] == "image" else "document"),
                        "album_id": None,
                    }
                )
        return Incoming(
            key=f"max:{event_id}",
            platform=platform,
            user_id=str(user["user_id"]),
            chat_id=str(chat_id),
            topic="",
            text=("/start " + str(raw.get("payload") or ""))
            if raw.get("update_type") == "bot_started"
            else body.get("text", ""),
            callback=(cb or {}).get("payload"),
            callback_id=(cb or {}).get("callback_id"),
            private=recipient.get("chat_type") == "dialog" or raw.get("update_type") == "bot_started",
            files=files,
            reply_message_id=(
                (msg.get("link") or {}).get("message", {}).get("mid") or (msg.get("link") or {}).get("mid")
            )
            if (msg.get("link") or {}).get("type") == "reply"
            else None,
        )
    raise BusinessError("Неизвестный мессенджер.")


class DeliveryUncertain(Exception):
    """Запрос мог быть принят. Автоповтор запрещён до сверки оператором."""


class Transport:
    def __init__(self):
        self.cfg = settings()
        self.client = httpx.Client(timeout=httpx.Timeout(25, connect=10), follow_redirects=False)

    def close(self):
        self.client.close()

    def send(self, platform, chat_id, topic, payload, file_path=None):
        text = payload.get("text", "")[:3900]
        buttons = payload.get("buttons", [])
        try:
            if platform == "telegram":
                if not self.cfg.telegram_token:
                    raise BusinessError("Не задан TELEGRAM_TOKEN.")
                data = {"chat_id": chat_id, "caption" if file_path else "text": text}
                if payload.get("silent"):
                    data["disable_notification"] = True
                if topic:
                    data["message_thread_id"] = topic
                if buttons:
                    data["reply_markup"] = {
                        "inline_keyboard": [[{"text": label, "callback_data": value}] for label, value in buttons]
                    }
                url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/" + (
                    "sendDocument" if file_path else "sendMessage"
                )
                if file_path:
                    with open(file_path, "rb") as f:
                        data = {k: json.dumps(v) if isinstance(v, dict) else v for k, v in data.items()}
                        response = self.client.post(
                            url,
                            data=data,
                            files={"document": (payload.get("filename", "material" + file_path.suffix), f)},
                        )
                else:
                    response = self.client.post(url, json=data)
                self._check(response)
                result = response.json()
                if not result.get("ok"):
                    raise BusinessError("Telegram отклонил сообщение.")
                return str(result["result"]["message_id"])
            if platform == "max":
                if not self.cfg.max_token:
                    raise BusinessError("Не задан MAX_TOKEN.")
                headers = {"Authorization": self.cfg.max_token}
                attachments = []
                if file_path:
                    upload = self.client.post(
                        f"{self.cfg.max_api_url}/uploads", headers=headers, params={"type": "file"}
                    )
                    self._check(upload)
                    from ama.files import validate_url

                    upload_url = upload.json()["url"]
                    validate_url(upload_url)
                    with open(file_path, "rb") as f:
                        uploaded = self.client.post(
                            upload_url, files={"data": (payload.get("filename", "material" + file_path.suffix), f)}
                        )
                    self._check(uploaded)
                    attachments.append({"type": "file", "payload": uploaded.json()})
                if buttons:
                    attachments.append(
                        {
                            "type": "inline_keyboard",
                            "payload": {
                                "buttons": [
                                    [{"type": "callback", "text": label, "payload": value}] for label, value in buttons
                                ]
                            },
                        }
                    )
                response = self.client.post(
                    f"{self.cfg.max_api_url}/messages",
                    params={"chat_id": chat_id},
                    headers=headers,
                    json={"text": text, "attachments": attachments, "notify": not payload.get("silent", False)},
                )
                self._check(response)
                return str(response.json()["message"]["body"]["mid"])
            raise BusinessError("Неизвестный мессенджер.")
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ) as e:
            raise DeliveryUncertain("Исход отправки неизвестен; требуется сверка по чату.") from e

    @staticmethod
    def _check(response):
        if response.status_code >= 500:
            raise DeliveryUncertain("Сервер мессенджера вернул 5xx; проверьте фактическую доставку.")
        if response.status_code == 429:
            raise BusinessError("Лимит мессенджера; сообщение будет повторено с задержкой.")
        if response.status_code >= 400:
            raise BusinessError(f"Мессенджер отклонил запрос, HTTP {response.status_code}.")

    def create_topic(self, platform, chat_id, name):
        if platform != "telegram":
            raise BusinessError("Темы форума доступны только в Telegram.")
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/createForumTopic",
                json={"chat_id": chat_id, "name": name[:120]},
            )
            self._check(response)
            data = response.json()
            if not data.get("ok"):
                raise BusinessError("Telegram отклонил создание темы.")
            return str(data["result"]["message_thread_id"])
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ) as error:
            raise DeliveryUncertain(
                "Исход создания темы неизвестен; выполните сверку без автоматического повтора."
            ) from error

    def answer_callback(self, platform, callback_id):
        if not callback_id:
            return
        try:
            if platform == "telegram" and self.cfg.telegram_token:
                self.client.post(
                    f"https://api.telegram.org/bot{self.cfg.telegram_token}/answerCallbackQuery",
                    json={"callback_query_id": callback_id},
                )
            elif platform == "max" and self.cfg.max_token:
                self.client.post(
                    f"{self.cfg.max_api_url}/answers",
                    params={"callback_id": callback_id},
                    headers={"Authorization": self.cfg.max_token},
                    json={"notification": "Обработано"},
                )
        except httpx.HTTPError:
            pass

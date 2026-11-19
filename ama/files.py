import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
from ama.config import settings
from ama.errors import BusinessError

# Не разрешаем произвольные URL из пользовательского вложения (SSRF).
ALLOWED_SUFFIXES = (".max.ru", ".oneme.ru", ".okcdn.ru", ".mycdn.me")


def validate_url(url):
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    if p.scheme != "https" or p.username or p.password or (p.port and p.port != 443):
        raise BusinessError("Небезопасный адрес файла.")
    if host not in {"api.telegram.org", "platform-api.max.ru", "max.ru"} and not any(host.endswith(s) for s in ALLOWED_SUFFIXES):
        raise BusinessError("Хранилище файла не входит в разрешённые домены адаптера.")


def download(attachment, client, *, max_bytes=None):
    cfg = settings()
    if attachment.platform == "telegram":
        response = client.get(f"https://api.telegram.org/bot{cfg.telegram_token}/getFile", params={"file_id": attachment.file_id})
        if response.status_code != 200 or not response.json().get("ok"):
            raise BusinessError("Telegram не предоставил файл.")
        remote = response.json()["result"]["file_path"]
        if ".." in remote or remote.startswith("/"):
            raise BusinessError("Некорректный путь файла.")
        url = f"https://api.telegram.org/file/bot{cfg.telegram_token}/{remote}"
    else:
        data = json.loads(attachment.file_id)
        url = data.get("url")
        if not url:
            response = client.get(f"{cfg.max_api_url}/messages/{data['mid']}", headers={"Authorization": cfg.max_token})
            if response.status_code != 200:
                raise BusinessError("MAX не предоставил метаданные файла.")
            items = response.json().get("body", {}).get("attachments", [])
            url = next((a.get("payload", {}).get("url") for a in items if a.get("payload", {}).get("token") == data.get("token")), None)
    validate_url(url)
    root = Path(cfg.file_root)
    root.mkdir(parents=True, exist_ok=True)
    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".txt", ".mp4"}:
        raise BusinessError("Тип файла не разрешён политикой хранилища.")
    target = root / f"{attachment.id}{suffix}"
    temp = target.with_suffix(target.suffix + ".part")
    size = 0
    digest = hashlib.sha256()
    try:
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise BusinessError("Файл недоступен или требует неподдерживаемого перенаправления.")
            with temp.open("wb") as f:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > min(cfg.max_file_bytes, max_bytes if max_bytes is not None else cfg.max_file_bytes):
                        raise BusinessError("Превышен разрешённый размер файла.")
                    digest.update(chunk)
                    f.write(chunk)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return str(target), size, digest.hexdigest()

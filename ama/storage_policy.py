"""Owner-approved storage limits. Retention dates never erase active evidence automatically."""

from sqlalchemy import select
from ama.models import Record
from ama.config import settings
from ama.errors import BusinessError

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".txt", ".mp4"}


def validate(data):
    if not all(data.get(k) for k in ("approval", "location", "access_owner", "error_recipient", "retention_basis")):
        raise BusinessError(
            "Утвердите место хранения, владельца доступа, получателя ошибок и основание сроков хранения."
        )
    for k in ("max_file_bytes", "retention_days", "backup_max_age_hours", "restore_max_age_days"):
        if not isinstance(data.get(k), int) or isinstance(data[k], bool) or data[k] <= 0:
            raise BusinessError("Нужен утверждённый положительный параметр: " + k)
    types = data.get("extensions", [])
    if not isinstance(types, list) or not types or not set(types) <= EXTENSIONS:
        raise BusinessError("Выберите поддерживаемые типы вложений.")
    if data["max_file_bytes"] > settings().max_file_bytes:
        raise BusinessError("Утверждённый размер превышает лимит адаптера MAX_FILE_BYTES.")


def current(db):
    rows = list(db.scalars(select(Record).where(Record.kind == "policy", Record.published.is_(True))))
    rows = [r for r in rows if r.data.get("policy_type") == "storage"]
    if len(rows) != 1:
        return None
    validate(rows[0].data)
    return rows[0]


def check_attachment(db, attachment):
    from pathlib import Path

    r = current(db)
    if not r:
        return  # Compatibility for the existing pilot; preflight blocks mass launch until approval.
    if Path(attachment.filename).suffix.lower() not in r.data["extensions"]:
        raise BusinessError("Тип вложения не разрешён утверждённой политикой.")
    if attachment.size is not None and attachment.size > r.data["max_file_bytes"]:
        raise BusinessError("Размер вложения превышает утверждённый лимит.")

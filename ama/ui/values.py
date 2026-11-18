"""Typed input, human labels and structural validation for draft forms."""
import re
from datetime import datetime, time, date
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ama.errors import BusinessError
from ama.service import parse_date


def parse(text, field, tz="Europe/Moscow"):
    text = text.strip()
    kind = field["type"]
    if not text:
        raise BusinessError("Введите значение или нажмите «Очистить».")
    if len(text) > field.get("maximum", 12000) and kind in {"text", "key", "url"}:
        raise BusinessError("Текст слишком длинный.")
    try:
        if kind in {"text", "url"}:
            value = text
        elif kind in {"key", "topic", "external_id"}:
            pattern = r"[a-zA-Z0-9_.-]{1,100}" if kind == "key" else r"-?\d{1,30}" if kind == "external_id" else r"\d{1,30}"
            if not re.fullmatch(pattern, text):
                raise ValueError
            value = text
        elif kind in {"int", "money"}:
            if kind == "money":
                amount = Decimal(text.replace(" ", "").replace(",", ".")) * 100
                if amount != amount.to_integral_value() or not amount.is_finite():
                    raise ValueError
                value = int(amount)
            else:
                value = int(text)
            if value < field.get("minimum", -2147483648) or value > field.get("maximum", 2147483647):
                raise ValueError
        elif kind == "timezone":
            ZoneInfo(text)
            value = text
        elif kind == "time":
            value = time.fromisoformat(text).strftime("%H:%M")
        elif kind == "date":
            try:
                value = date.fromisoformat(text).isoformat()
            except ValueError:
                value = datetime.strptime(text, "%d.%m.%Y").date().isoformat()
        elif kind == "datetime":
            try:
                parsed = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=ZoneInfo(tz))
                value = parsed.isoformat()
            except ValueError:
                parse_date(text)
                value = text
        elif kind in {"lines", "dates", "integers"}:
            entries = [part.strip() for part in re.split(r"[\n,;]+", text) if part.strip()]
            value = list(dict.fromkeys(parse(part, {"type": "date"}) for part in entries)) if kind == "dates" else list(dict.fromkeys(int(part) for part in entries)) if kind == "integers" else list(dict.fromkeys(entries))
        else:
            raise BusinessError("Это поле заполняется кнопками.")
    except (ValueError, InvalidOperation, OverflowError, ZoneInfoNotFoundError) as error:
        hints = {"datetime": "Например: 25.12.2026 15:00 или ISO 8601 с часовым поясом.", "date": "Например: 25.12.2026.",
                 "money": "Сумма в рублях, до двух знаков после запятой.", "timezone": "Например: Europe/Moscow.",
                 "time": "Формат: 11:00.", "external_id": "Введите фактический числовой ID из мессенджера.",
                 "topic": "Введите числовой ID темы, без знака минус.", "key": "Латинские буквы, цифры, точка, дефис или подчёркивание."}
        raise BusinessError("Некорректное значение. " + hints.get(kind, "Проверьте тип и допустимый диапазон.")) from error
    if kind == "url":
        from urllib.parse import urlsplit
        p = urlsplit(value)
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            raise BusinessError("Нужна HTTPS-ссылка без логина и пароля.")
    return value


def required_errors(fields, data, prefix=""):
    errors = []
    for field in fields:
        key = field["key"]
        value = data.get(key) if isinstance(data, dict) else None
        label = prefix + field["label"]
        if field["required"] and (value is None or value == "" or value == [] or value == {}):
            errors.append(label)
        if field["type"] == "object" and isinstance(value, dict):
            errors.extend(required_errors(field.get("fields", []), value, label + " → "))
        if field["type"] == "list" and isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    errors.extend(required_errors(field.get("fields", []), item, f"{label} → {i+1} → "))
    return errors


def display(value, field=None):
    if value is None or value == "":
        return "не задано"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if field and field["type"] == "money":
        return f"{Decimal(value) / 100:.2f} ₽"
    if field and field["type"] == "choice":
        return field.get("choices", {}).get(str(value), str(value))
    if isinstance(value, list):
        return f"{len(value)} пунктов" if value and isinstance(value[0], dict) else ", ".join(str(v) for v in value) or "не выбрано"
    if isinstance(value, dict):
        return f"{len(value)} полей"
    return str(value)


def structural_diff(before, after, path=""):
    lines = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                lines.extend(structural_diff(before.get(key), after.get(key), f"{path}.{key}".strip(".")))
    elif before != after:
        lines.append((path, before, after))
    return lines

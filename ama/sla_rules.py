"""Timing defaults from §16. Units: working minutes or end of Nth working day."""

from copy import deepcopy
from ama.errors import BusinessError

CATEGORIES = {
    "ordinary": "Обычное обращение",
    "complex": "Длительная работа с плановой датой",
    "analysis": "Аналитика / маркетинговый план",
    "large_analysis": "Крупное исследование",
    "recruitment": "Подбор персонала",
    "calendar_launch": "Кампания по дате запуска",
    "new_connection": "Подключение нового предприятия",
}


def defaults(purpose, priority, category="ordinary"):
    if priority not in {"normal", "important", "urgent", "critical"} or category not in CATEGORIES:
        raise BusinessError("Неизвестные приоритет или категория SLA.")
    r = {
        "take": ["days", 1],
        "result": ["days", 3],
        "result_anchor": "created",
        "plan_required": False,
        "plan_window": None,
        "input": None,
        "input_anchor": "created",
        "range_days": None,
    }
    if priority == "important":
        r.update(take=["minutes", 240], result=["days", 1])
    if priority == "urgent":
        r.update(take=["minutes", 60], result=["minutes", 240])
    if purpose == "support":
        if priority == "normal":
            r["result"] = ["days", 2]
        elif priority == "important":
            r["result"] = ["today", 0]
        elif priority == "urgent":
            r["result"] = ["minutes", 60]
    if purpose == "technical_support":
        if priority == "normal":
            r.update(take=["minutes", 240], result=["days", 1])
        if priority == "critical":
            r.update(take=["minutes", 30], result=["minutes", 60], critical_interval_minutes=120)
    if priority == "critical" and purpose != "technical_support":
        raise BusinessError(
            "Критичная авария оформляется в технической поддержке. Для риска даты открытия используйте критичный управленческий сигнал."
        )
    if priority == "normal":
        if purpose == "marketing" and category in {"analysis", "large_analysis", "complex"}:
            r.update(
                result=None,
                plan_required=True,
                plan_window=["days", 2],
                range_days=[7, 15 if category == "large_analysis" else 10],
            )
        if purpose == "projecting":
            r.update(result=None, input=["days", 2], plan_required=True, range_days=[10, 15])
        if purpose == "software" and category == "new_connection":
            r.update(result=None, input=["days", 1], input_anchor="taken", plan_required=True, opening_offset_days=5)
        if purpose in {"product", "equipment", "systems_equipment"} and category == "complex":
            r.update(result=None, plan_required=True, range_days=[5, 10])
        if purpose == "personnel" and category in {"analysis", "complex", "recruitment"}:
            r.update(result=None, plan_required=True, range_days=None if category == "recruitment" else [5, 7])
        if purpose == "registrations":
            r["input"] = ["days", 2]
    if category == "calendar_launch":
        r.update(result=None, plan_required=True)
    return deepcopy(r)

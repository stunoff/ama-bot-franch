from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from ama.errors import BusinessError


class Calendar:
    def __init__(self, data):
        try:
            self.tz = ZoneInfo(data["timezone"])
            self.start = time.fromisoformat(data["start"])
            self.end = time.fromisoformat(data["end"])
            self.days = set(data["weekdays"])
            self.holidays = set(data.get("holidays", []))
            self.extra = set(data.get("extra_workdays", []))
        except (KeyError, ValueError, TypeError) as e:
            raise BusinessError("Не заполнен рабочий календарь.") from e
        if not self.days or not self.days <= set(range(7)) or self.end <= self.start:
            raise BusinessError("Некорректное рабочее окно календаря.")

    def working(self, day):
        return day.isoformat() in self.extra or (day.weekday() in self.days and day.isoformat() not in self.holidays)

    def add_minutes(self, start, minutes):
        if minutes < 0:
            raise BusinessError("Отрицательный рабочий срок.")
        local = start.replace(tzinfo=timezone.utc).astimezone(self.tz)
        remaining = minutes * 60
        for _ in range(4000):
            day = local.date()
            begin = datetime.combine(day, self.start, self.tz)
            end = datetime.combine(day, self.end, self.tz)
            if self.working(day) and local < end:
                local = max(local, begin)
                available = (end - local).total_seconds()
                if remaining <= available:
                    return (local + timedelta(seconds=remaining)).astimezone(timezone.utc).replace(tzinfo=None)
                remaining -= available
            local = datetime.combine(day + timedelta(days=1), self.start, self.tz)
        raise BusinessError("Календарь не позволяет определить срок.")

    def end_of_workday(self, start, days=1):
        local = start.replace(tzinfo=timezone.utc).astimezone(self.tz)
        day = local.date()
        for _ in range(4000):
            day += timedelta(days=1)
            if self.working(day):
                days -= 1
                if days <= 0:
                    return datetime.combine(day, self.end, self.tz).astimezone(timezone.utc).replace(tzinfo=None)
        raise BusinessError("Нет рабочего дня.")

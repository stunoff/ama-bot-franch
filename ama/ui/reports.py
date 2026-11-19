"""Scoped report previews and CSV delivery with a live authorization check."""
import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from sqlalchemy import select
from ama.auth import Actor
from ama.config import settings
from ama.errors import BusinessError, AccessDenied
from ama.models import UIExport, Binding, Entity, Role, Training, Deal, Audit, now
from ama.service import queue
from ama.ui import access, repository
from ama.test_data import is_test


def period(data):
    try:
        tz = ZoneInfo(data.get("timezone") or "Europe/Moscow")
        start = datetime.strptime(data["start"], "%Y-%m-%d").replace(tzinfo=tz)
        end = (datetime.strptime(data["end"], "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz)
    except (ValueError, KeyError) as error:
        raise BusinessError("Укажите начало и конец отчётного периода.") from error
    if end <= start or (end-start).days > 366:
        raise BusinessError("Период должен составлять от 1 до 366 дней.")
    return start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)


def in_scope(data, obj):
    return all(not data.get(k) or data[k] == getattr(obj, k, None) for k in ("partner_id", "brand_id", "establishment_id"))


def rows(db, actor, data):
    start, end = period(data)
    report = data["report"]
    if report == "audit":
        from ama.admin import check_admin
        check_admin(db, actor)
        columns = ["Дата UTC", "Сотрудник", "Действие", "Запись", "Прежняя версия", "Новая версия"]
        items = [[a.created_at.isoformat(), repository.name(db, a.actor_id), a.action, a.object_id, a.old_version, a.new_version]
                 for a in db.scalars(select(Audit).where(Audit.created_at >= start, Audit.created_at < end).order_by(Audit.created_at))
                 if a.action.startswith(("CONFIG_", "UI_", "BOOTSTRAP_"))]
    else:
        access.require_staff(db, actor)
        if report in {"sales_events", "sales_funnel", "sales_cohort"}:
            from ama.sales_reports import build
            columns, items = build(db, actor, data, start, end)
        elif report=="packets":
            from ama.packets import report as packet_report
            columns,items=packet_report(db,actor,data["packet_id"])
        elif report in {"works", "summary"}:
            work = [w for w in access.works(db, actor) if not is_test(db,w) and in_scope(data, w) and start <= w.created_at < end]
            if report == "works":
                from ama.service import STATUSES
                columns = ["ID работы", "ФП", "Предприятие", "Бренд", "Название", "Состояние", "Ответственный", "Принимающий", "Срок UTC", "Просрочена", "Исход"]
                items = [[w.id, repository.entity_name(db, w.partner_id), repository.entity_name(db, w.establishment_id),
                          repository.entity_name(db, w.brand_id), w.title, STATUSES[w.status], repository.name(db, w.owner_id),
                          repository.name(db, w.reviewer_id), w.due_at.isoformat(), "Да" if w.status != "done" and w.due_at < now() else "Нет", w.outcome or ""] for w in work]
            else:
                from collections import Counter
                from ama.service import STATUSES
                counts = Counter(w.status for w in work)
                columns = ["Показатель", "Количество"]
                items = [["Работ в когорте", len(work)], ["Открытых", sum(w.status != "done" for w in work)],
                         ["Просрочено", sum(w.status != "done" and w.due_at < now() for w in work)]] + [[STATUSES[k], counts[k]] for k in STATUSES]
        elif report == "training":
            partners = [e for e in db.scalars(select(Entity).where(Entity.kind == "partner")) if not is_test(db,e) and access.can_entity(db, actor, e)
                        and (not data.get("partner_id") or e.id == data["partner_id"])]
            if data.get("establishment_id"):
                est = db.get(Entity, data["establishment_id"])
                partners = [p for p in partners if est and p.id == est.partner_id and access.can_entity(db, actor, est)]
            if data.get("brand_id"):
                from ama.models import EstablishmentBrand
                allowed = set(db.scalars(select(Entity.partner_id).join(EstablishmentBrand, EstablishmentBrand.establishment_id == Entity.id)
                                        .where(EstablishmentBrand.brand_id == data["brand_id"], EstablishmentBrand.active.is_(True))))
                partners = [p for p in partners if p.id in allowed]
            partner_map = {p.id: p.name for p in partners}
            people = {}
            for role in db.scalars(select(Role).where(Role.name == "partner", Role.bot_id == settings().bot_id, Role.active.is_(True))):
                if role.partner_id in partner_map and (not role.expires_at or role.expires_at > now()):
                    people.setdefault(role.person_id, set()).add(partner_map[role.partner_id])
            columns = ["ФП", "Человек", "Курс", "Версия", "Подтверждено карточек", "Всего", "Сохранённое место", "Пауза", "Завершено UTC"]
            items = [[", ".join(sorted(people[t.person_id])), repository.name(db, t.person_id), t.course_key, t.course_version,
                      len(t.acknowledged), len(t.cards), t.position + 1, "Да" if t.paused else "Нет", t.completed_at.isoformat() if t.completed_at else ""]
                     for t in db.scalars(select(Training).order_by(Training.person_id)) if t.person_id in people
                     and (not t.completed_at or start <= t.completed_at < end)]
        elif report == "sales":
            # Exactly the visible consultant cohort; an old success never inflates it.
            visible = [lead for lead in access.leads(db,actor) if not is_test(db,lead)]
            cohort = [lead for lead in visible if start <= lead.created_at < end and (not data.get("brand_id") or lead.data.get("brand_id") == data["brand_id"])]
            deals = list(db.scalars(select(Deal)))
            visible_ids = {lead.id for lead in visible if not data.get("brand_id") or lead.data.get("brand_id") == data["brand_id"]}
            cohort_ids = {lead.id for lead in cohort}
            won = len({d.lead_id for d in deals if d.successful_at and d.lead_id in cohort_ids})
            period_wins = sum(bool(d.successful_at) and start <= d.successful_at < end and d.lead_id in visible_ids for d in deals)
            columns = ["Показатель", "Значение"]
            items = [["Лидов когорты", len(cohort)], ["Успехов когорты", won], ["Конверсия когорты", won/len(cohort) if cohort else 0], ["Успехов за период, все когорты", period_wins]]
        else:
            raise BusinessError("Неизвестный отчёт.")
    if len(items) > 5000:
        raise BusinessError("Больше 5000 строк. Уточните период или область выгрузки.")
    return {"columns": columns, "rows": items, "report": report, "period": {"start": data["start"], "end": data["end"]}}


def create_export(db, actor, data, command_key):
    result = rows(db, actor, data)
    binding = db.get(Binding, actor.binding_id)
    if binding.purpose == "client":
        raise AccessDenied("Внутренние отчёты не публикуются в клиентский чат.")
    export = UIExport(bot_id=settings().bot_id, person_id=actor.person_id, binding_id=binding.id,
                      binding_version=binding.version, grants_hash=access.grants_hash(db, actor), data=result,
                      expires_at=now() + timedelta(hours=24))
    db.add(export)
    db.flush()
    message = queue(db, binding, "export:" + command_key, f"Отчёт за {data['start']} — {data['end']}. Строк: {len(result['rows'])}.")
    message.payload = {**message.payload, "export_id": export.id, "ui_actor": actor.person_id,
                       "ui_grants": export.grants_hash, "filename": f"ama-{data['report']}-{data['end']}.csv"}
    db.add(Audit(actor_id=actor.person_id, object_id=export.id, action="UI_EXPORT_CREATED", data={"report": data["report"], "rows": len(result["rows"])}))
    return {"id": export.id, "rows": len(result["rows"]), "columns": result["columns"], "recipient": repository.label(db, binding)}


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def render(db, export_id):
    export = db.get(UIExport, export_id)
    if not export or export.bot_id != settings().bot_id or export.expires_at <= now():
        raise BusinessError("Выгрузка отсутствует или срок её доставки истёк. Сформируйте отчёт заново.")
    actor = Actor(export.person_id, export.binding_id)
    b = db.get(Binding, export.binding_id)
    if not b or b.version != export.binding_version or b.purpose == "client" or access.grants_hash(db, actor) != export.grants_hash:
        raise AccessDenied("Права или маршрут изменились после предпросмотра отчёта. Сформируйте его заново.")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(export.data["columns"])
    writer.writerows([[csv_cell(v) for v in row] for row in export.data["rows"]])
    root = Path(settings().file_root) / "exports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (export.id + ".csv")
    temp = root / (export.id + ".part")
    temp.write_text(stream.getvalue(), encoding="utf-8-sig")
    temp.replace(path)
    export.storage_path, export.status = str(path), "stored"
    return path

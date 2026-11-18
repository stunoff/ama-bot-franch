"""Versioned, scoped sales measures: events, current interests and first-queued cohorts."""

from collections import Counter
from types import SimpleNamespace
from sqlalchemy import select
from ama.models import SalesCase, SalesEvent, Lead, Deal, now
from ama.ui import access
from ama.sale_flow import STAGE_LABELS

RULE_VERSION = "sales-1.0X-v1"


def visible(db, actor, data):
    return [
        c
        for c in db.scalars(select(SalesCase))
        if (not data.get("brand_id") or c.brand_id == data["brand_id"])
        and access.scope(
            db,
            actor,
            SimpleNamespace(partner_id=None, brand_id=c.brand_id, establishment_id=None),
            {"consultant", "support_head"},
        )
        and (
            access.scope(
                db,
                actor,
                SimpleNamespace(partner_id=None, brand_id=c.brand_id, establishment_id=None),
                {"support_head"},
            )
            or c.owner_id in {None, actor.person_id}
            or __import__("ama.sale_flow", fromlist=["manager"]).manager(db, actor)
        )
    ]


def build(db, actor, data, start, end):
    cases = visible(db, actor, data)
    case_ids = {c.id for c in cases}
    all_events = [e for e in db.scalars(select(SalesEvent).order_by(SalesEvent.created_at)) if e.case_id in case_ids]
    events = [e for e in all_events if start <= e.created_at < end]
    leads = {c.lead_id for c in cases}
    deals = [d for d in db.scalars(select(Deal)) if set(d.data.get("case_ids") or [d.case_id]) & case_ids]
    common = [
        ["Версия правил", RULE_VERSION],
        ["Дата среза UTC", now().isoformat()],
        ["Брендовые срезы пересекаются; их нельзя суммировать как людей", "Да"],
    ]
    if data["report"] == "sales_events":
        columns = ["Показатель", "Событий", "Уникальных лидов"]
        labels = {
            "CONSULTANT_REQUESTED": "Переданы в очередь",
            "TAKE": "Принятия",
            "REASSIGN": "Переназначения",
            "CALL": "Попытки звонков",
            "FINANCIAL_REVIEW": "Финансовый разбор проведён",
            "CONTRACT_STARTED": "Переданы в договоры",
            "DEAL_SUCCESS": "Сделки состоялись",
            "CURATOR_ACCEPTED": "Приняты кураторами",
        }
        rows = [
            [label, sum(e.kind == key for e in events), len({e.lead_id for e in events if e.kind == key})]
            for key, label in labels.items()
        ]
        new = [lead for lead in db.scalars(select(Lead)) if lead.id in leads and start <= lead.created_at < end]
        period_deals = [d for d in deals if d.successful_at and start <= d.successful_at < end]
        rows += [
            ["Новые уникальные лиды", len(new), len(new)],
            [
                "Успешные уникальные сделки за период",
                len({d.id for d in period_deals}),
                len({d.lead_id for d in period_deals}),
            ],
        ]
        rows += [[a, b, ""] for a, b in common]
    elif data["report"] == "sales_funnel":
        columns = [
            "Лид",
            "Интерес",
            "Бренд",
            "Этап",
            "Состояние",
            "Исход",
            "Последний запрос UTC",
            "Следующий контакт",
            "Требуется действие",
        ]
        rows = [
            [
                c.lead_id,
                c.id,
                c.brand_id or "Проект не выбран",
                STAGE_LABELS.get(c.stage, c.stage),
                c.state,
                c.result or "активен",
                c.data.get("submitted_at", "Не зафиксировано"),
                c.data.get("next_contact", "Не зафиксировано"),
                "Да" if c.data.get("requires_contact") or not c.owner_id else "Нет",
            ]
            for c in cases
        ]
    else:
        columns = ["Показатель", "Значение"]
        # First queue admission is fixed per person. A later parallel interest cannot create a new cohort entry.
        first = {}
        cohort_events = (
            list(db.scalars(select(SalesEvent).where(SalesEvent.lead_id.in_(leads)).order_by(SalesEvent.created_at)))
            if leads
            else []
        )
        for e in cohort_events:
            if e.kind == "CONSULTANT_REQUESTED":
                first.setdefault(e.lead_id, e.created_at)
        ids = {lead_id for lead_id, created in first.items() if start <= created < end}
        groups = Counter()
        won = {d.lead_id for d in deals if d.successful_at and d.lead_id in ids}
        for lead_id in ids:
            own = [c for c in cases if c.lead_id == lead_id]
            if lead_id in won:
                groups["Есть состоявшаяся сделка"] += 1
            else:
                active = [c for c in own if c.result not in {"refused", "not_target", "paused", "success"}]
                rank = {
                    k: i
                    for i, k in enumerate(
                        [
                            "new",
                            "selection",
                            "qualification",
                            "presentation",
                            "financial_review",
                            "terms",
                            "contract",
                            "signing",
                            "payment_wait",
                            "won",
                        ]
                    )
                }
                if active:
                    best = max(active, key=lambda c: (rank.get(c.stage, 0), c.created_at))
                    groups[STAGE_LABELS.get(best.stage, best.stage)] += 1
                elif any(c.result == "paused" for c in own):
                    groups["Не сейчас"] += 1
                else:
                    groups["Отказ / нецелевой"] += 1
        rows = (
            common
            + [
                ["Основание когорты", "Первое подтверждённое событие передачи человека в очередь"],
                ["Лидов без зафиксированной первой передачи", len(leads - set(first))],
                ["Лидов когорты", len(ids)],
                ["Лидов когорты хотя бы с одной сделкой", len(won)],
                ["Конверсия когорты", len(won) / len(ids) if ids else "Нет данных для расчёта"],
            ]
            + [[k, v] for k, v in sorted(groups.items())]
        )
    return columns, rows

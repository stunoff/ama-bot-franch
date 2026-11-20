"""Freeze approved template changes per new plan; old plans keep their accepted graph."""

from copy import deepcopy
from sqlalchemy import select
from ama.models import Record
from ama.project_catalog import CATALOG, GROUPS, default_mode
from ama.errors import BusinessError


def compiled(db, data):
    candidates = [
        r
        for r in db.scalars(select(Record).where(Record.kind == "policy", Record.published.is_(True)))
        if r.data.get("policy_type") == "scenario" and r.data.get("scenario") == data["scenario"]
    ]
    requested = data.get("scenario_template")
    if requested:
        candidates = [r for r in candidates if r.key == requested]
    if requested and not candidates:
        raise BusinessError("Выбранный шаблон не опубликован для этой ситуации.")
    if len(candidates) > 1:
        raise BusinessError("Выберите конкретный опубликованный шаблон сценария.")
    catalog = deepcopy(CATALOG)
    source = {}
    if candidates:
        rec = candidates[0]
        from ama.ui.configuration import validate_record

        validate_record(db, "policy", rec.data, published=True)
        source = {"id": rec.id, "key": rec.key, "version": rec.version}
        for b in rec.data["blocks"]:
            code = b["key"]
            old = catalog.get(
                code,
                {
                    "code": code,
                    "title": b.get("title") or code,
                    "group": "repair",
                    "lane": "curator",
                    "shared": False,
                    "section": "configured",
                    "depends_on": [],
                },
            )
            if b["reviewer_role"] not in {"curator", "marketing_manager"}:
                raise BusinessError("Проектный результат принимает куратор либо менеджер маркетинга.")
            if code in CATALOG and (
                (CATALOG[code]["lane"] == "marketing") != (b["reviewer_role"] == "marketing_manager")
            ):
                raise BusinessError("Нельзя изменить обязательный контур приёмки стандартного результата.")
            lane = "marketing" if b["reviewer_role"] == "marketing_manager" else "curator"
            group = b.get("group") or old["group"]
            if group not in GROUPS:
                raise BusinessError("Неизвестная группа настраиваемого пункта.")
            event = b["start_event"].lower()
            if event not in {
                "project_configured",
                "dependencies_accepted",
                "result_accepted",
                "manual",
                "actual_technical",
                "actual_official",
            }:
                raise BusinessError("Неподдерживаемое событие запуска пункта.")
            if event in {"dependencies_accepted", "result_accepted"} and not b.get("depends_on"):
                raise BusinessError("Для события приёмки нужны зависимости.")
            result_type = b.get("result_type", "manual")
            if result_type not in {"manual", "photo", "document"}:
                raise BusinessError("Неподдерживаемый тип результата шаблона.")
            catalog[code] = {
                **old,
                **{k: v for k, v in b.items() if k not in {"key", "mode"}},
                "code": code,
                "title": b.get("title") or old["title"],
                "lane": lane,
                "group": group,
                "template_mode": b.get("mode", default_mode(old, data["scenario"])),
                "result_type": result_type,
                "configured": True,
                "independent_review": True,
                **({"anchor": event} if event.startswith("actual_") else {}),
            }
    graph = {k: item["depends_on"] for k, item in catalog.items()}
    done = set()
    path = set()

    def visit(code):
        if code not in graph:
            raise BusinessError("Отсутствует зависимость шаблона: " + code)
        if code in path:
            raise BusinessError("Цикл зависимостей проекта.")
        if code in done:
            return
        path.add(code)
        for dependency in graph[code]:
            visit(dependency)
        path.remove(code)
        done.add(code)

    for code in graph:
        visit(code)
    return catalog, source


def template(db, block):
    item = block.data.get("template") if getattr(block, "data", None) else None
    return item or CATALOG[block.code]

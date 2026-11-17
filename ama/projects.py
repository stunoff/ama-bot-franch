from sqlalchemy import select
from ama.auth import require
from ama.models import Work, Entity
from ama.service import create_work, validate_scope
from ama.errors import BusinessError

SCENARIOS = {"new", "additional_brand", "existing", "technical", "reconstruction"}
MODES = {"perform", "verify", "inherit", "not_applicable"}


def configure(db, actor, data, command_key):
    """Куратор подтверждает применимость; не генерируем прошлый запуск действующего объекта."""
    require(db, actor, {"curator", "support_head"})
    validate_scope(db, data["partner_id"], data.get("brand_id"), data["establishment_id"])
    if data.get("scenario") not in SCENARIOS:
        raise BusinessError("Выберите сценарий проекта.")
    if not data.get("confirmed"):
        return {"preview": data.get("blocks", []), "message": "Подтвердите состав работ; черновик никому не отправлен."}
    est = db.get(Entity, data["establishment_id"])
    created = []
    for index, block in enumerate(data.get("blocks", [])):
        if block.get("mode") not in MODES:
            raise BusinessError("Для каждого блока выберите выполнить/проверить/наследовать/не применяется.")
        if block["mode"] in {"inherit", "not_applicable"}:
            continue
        if data["scenario"] == "existing" and block.get("key") in {"internal_opening", "technical_opening", "official_opening", "d90", "d180"}:
            raise BusinessError("Действующее предприятие не получает фиктивную историю открытия.")
        scope = "shared" if block.get("shared") else data.get("brand_id", "shared")
        origin = f"project:{data['project_id']}:{est.id}:{scope}:{block['key']}"
        old = db.scalar(select(Work).where(Work.origin_key == origin))
        if old:
            created.append(old.id)
            continue
        payload = {**block, "kind": block["key"], "partner_id": data["partner_id"],
                   "establishment_id": est.id, "brand_id": None if block.get("shared") else data.get("brand_id"),
                   "project_id": data["project_id"], "origin_key": origin,
                   "data": {**block.get("data", {}), "mode": block["mode"], "calendar": block.get("calendar", "support")}}
        w = create_work(db, actor, payload, f"{command_key}:{index}")
        created.append(w.id)
    return {"works": created, "scenario": data["scenario"]}

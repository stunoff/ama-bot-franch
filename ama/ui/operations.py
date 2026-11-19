from sqlalchemy import select
from ama.admin import check_admin
from ama.models import OperationsJob
from ama.config import settings
from ama.errors import BusinessError
from ama.ui import business
from ama.ui.controller import show, draft_screen, paginate, navigation
from ama.ui.engine import button as B


def dispatch(db, actor, s, action, p):
    check_admin(db, actor)
    if action == "ops_preflight":
        from ama.preflight import report

        result = report(db)
        items, page = paginate(result["issues"], p.get("page", 0))
        return show(
            db,
            actor,
            s,
            "Проверка готовности: "
            + ("готово" if result["ready"] else "есть незакрытые условия")
            + "\n"
            + "\n".join(x["code"] + " · " + x["object_id"] for x in items),
            navigation(action, page, len(result["issues"])) + [B("Назад", "ops_home")],
            action,
            page=page,
        )
    if action == "ops_test":
        draft = business.make_draft(db, actor, "ops:mark_test", "Отметить тестовую запись", {})
        return draft_screen(db, actor, s, {"draft_id": draft.id})
    if action == "ops_new":
        kind = p["kind"]
        if kind not in {"backup", "verify_restore"}:
            raise BusinessError("Неверная процедура.")
        draft = business.make_draft(
            db,
            actor,
            "ops:" + kind,
            "Резервная копия" if kind == "backup" else "Проверка восстановления в изолированном контуре",
            {},
        )
        return draft_screen(db, actor, s, {"draft_id": draft.id})
    rows = list(
        db.scalars(
            select(OperationsJob)
            .where(OperationsJob.bot_id == settings().bot_id)
            .order_by(OperationsJob.created_at.desc())
            .limit(100)
        )
    )
    items, page = paginate(rows, p.get("page", 0))
    text = (
        "Эксплуатация. Запросы исполняет операторский runner на сервере; без него остаются в очереди. Docker-сокет боту не передаётся. Сырые копии и секреты в чат не отправляются.\n"
        + "\n".join(j.id + " · " + j.kind + " · " + j.status for j in items)
    )
    return show(
        db,
        actor,
        s,
        text,
        [
            B("Проверить готовность", "ops_preflight"),
            B("Отметить тестовую запись…", "ops_test"),
            B("Запросить резервную копию…", "ops_new", kind="backup"),
            B("Проверить восстановление копии…", "ops_new", kind="verify_restore"),
        ]
        + navigation("ops_home", page, len(rows))
        + [B("Настройки", "settings")],
        "ops_home",
        page=page,
    )

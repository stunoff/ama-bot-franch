"""Короткие запросы ФП: шаги сохраняются по человеку и чату, не в памяти процесса."""

import json
from pathlib import Path
from sqlalchemy import select
from ama.auth import require
from ama.models import Dialog, Binding, Entity, EstablishmentBrand
from ama.errors import BusinessError, AccessDenied
from ama.service import create_work, versioned

TREE = json.loads((Path(__file__).resolve().parent.parent / "data/requests.json").read_text())


def options(db, actor, form):
    d = form.data
    if form.stage == "resume_prompt":
        return "Есть незавершённый черновик. Продолжить его или начать новый?", [
            {"text": "Продолжить черновик", "value": "resume"},
            {"text": "Начать новый (убрать прежний черновик)", "value": "new"},
        ]
    if form.stage == "paused":
        return "Черновик сохранён, файлы никому не отправлены.", [{"text": "Продолжить черновик", "value": "resume"}]
    if form.stage == "enterprise":
        b = db.get(Binding, actor.binding_id)
        rows = list(
            db.scalars(
                select(Entity)
                .join(EstablishmentBrand, EstablishmentBrand.establishment_id == Entity.id)
                .where(
                    Entity.kind == "establishment",
                    Entity.partner_id == b.partner_id,
                    EstablishmentBrand.brand_id == b.brand_id,
                    EstablishmentBrand.active.is_(True),
                )
            )
        )
        return "К какому предприятию относится вопрос?", [{"text": e.name, "value": e.id} for e in rows] + [
            {"text": "По бренду в целом", "value": None}
        ]
    if form.stage == "branch":
        return "С чем связан запрос?", [
            {"text": "Маркетинг и реклама", "value": "marketing"},
            {"text": "Другой вопрос", "value": "support"},
        ]
    if form.stage == "category":
        return "Выберите тему", [{"text": c["title"], "value": i} for i, c in enumerate(TREE[d["branch"]])]
    if form.stage == "question":
        question = TREE[d["branch"]][d["category"]]["questions"][d["question"]]
        return question["text"], [{"text": v, "value": v} for v in question["options"]]
    if form.stage == "preview":
        c = TREE[d["branch"]][d["category"]]
        e = db.get(Entity, d.get("establishment_id")) if d.get("establishment_id") else None
        text = f"Проверьте запрос\n{e.name if e else 'По бренду в целом'}\n{c['title']}\n" + "\n".join(
            f"{a['question']}: {a['answer']}" for a in d.get("answers", [])
        )
        if d.get("comment"):
            text += "\nКомментарий: " + d["comment"]
        if d.get("file_ids"):
            from ama.models import IntakeFile

            text += "\nФайлы: " + ", ".join(
                db.get(IntakeFile, x).filename
                + " ("
                + {
                    "pending": "сохраняется",
                    "stored": "сохранён",
                    "failed": "ошибка, нужна повторная загрузка",
                    "blocked": "сохранение заблокировано",
                }.get(db.get(IntakeFile, x).status, db.get(IntakeFile, x).status)
                + ")"
                for x in d["file_ids"]
            )
        return text, [
            {"text": "Отправить", "value": "confirm"},
            {"text": "Добавить комментарий", "value": "comment"},
            {"text": "Добавить фото/файл", "value": "upload"},
            {"text": "Изменить", "value": "edit"},
            *([{"text": "Убрать выбранные файлы", "value": "remove_files"}] if d.get("file_ids") else []),
            {"text": "Сохранить черновик", "value": "pause"},
            {"text": "Отмена", "value": "cancel"},
        ]
    if form.stage == "upload":
        return "Отправьте фото/файл. Они останутся в черновике до подтверждения запроса.", [
            {"text": "Готово, к предпросмотру", "value": "preview"}
        ]
    if form.stage == "edit":
        return "Что изменить? Для другого предприятия сначала уберите прежние вложения.", [
            {"text": "Предприятие", "value": "enterprise"},
            {"text": "Тему запроса", "value": "category"},
            {"text": "Ответы заново", "value": "question"},
            {"text": "Назад", "value": "preview"},
        ]
    if form.stage == "comment":
        return "Напишите короткое пояснение следующим сообщением.", []
    return "Запрос сохранён. Открыть меню: /start", []


def screen(db, actor, form):
    text, choices = options(db, actor, form)
    buttons = [[o["text"], f"af:q:{form.id}:{form.version}:{i}"] for i, o in enumerate(choices)]
    return text, buttons


def start(db, actor):
    require(db, actor, {"partner"})
    b = db.get(Binding, actor.binding_id)
    if b.purpose != "client":
        raise BusinessError("Создайте запрос из клиентского брендового чата.")
    f = db.scalar(
        select(Dialog)
        .where(Dialog.person_id == actor.person_id, Dialog.binding_id == actor.binding_id)
        .with_for_update()
    )
    if not f:
        f = Dialog(person_id=actor.person_id, binding_id=actor.binding_id)
        db.add(f)
        db.flush()
    if f.data and f.stage not in {"done", "cancelled"}:
        previous = f.data.get("_resume_stage") or f.stage
        f.data = {**f.data, "_resume_stage": previous}
        f.stage = "resume_prompt"
        f.version += 1
        return screen(db, actor, f)
    f.stage, f.data, f.version = "enterprise", {}, f.version + 1
    return screen(db, actor, f)


def advance(db, actor, form_id, expected, index, command_key):
    require(db, actor, {"partner"})
    f = db.scalar(select(Dialog).where(Dialog.id == form_id).with_for_update())
    if not f or f.person_id != actor.person_id or f.binding_id != actor.binding_id:
        raise AccessDenied("Форма принадлежит другому человеку или чату.")
    versioned(f, expected)
    _, choices = options(db, actor, f)
    if not 0 <= index < len(choices):
        raise BusinessError("Недействительный вариант ответа.")
    value = choices[index]["value"]
    d = dict(f.data)
    if f.stage in {"resume_prompt", "paused"}:
        if value == "resume":
            f.stage = d.pop("_resume_stage", "preview")
        else:
            from ama.models import IntakeFile

            for ident in d.get("file_ids", []):
                old = db.get(IntakeFile, ident)
                if old and not old.work_id:
                    old.status = "cancelled"
            d = {}
            f.stage = "enterprise"
    elif f.stage == "enterprise":
        d["establishment_id"], f.stage = value, "preview" if d.pop("editing_enterprise", False) else "branch"
    elif f.stage == "branch":
        d["branch"], f.stage = value, "category"
    elif f.stage == "category":
        d["category"], d["question"], d["answers"] = value, 0, []
        category = TREE[d["branch"]][value]
        f.stage = "question" if category["questions"] else "preview"
    elif f.stage == "question":
        questions = TREE[d["branch"]][d["category"]]["questions"]
        d["answers"] = d["answers"] + [{"question": questions[d["question"]]["text"], "answer": value}]
        d["question"] += 1
        f.stage = "question" if d["question"] < len(questions) else "preview"
        if f.stage == "preview" and TREE[d["branch"]][d["category"]]["title"] == "Другое":
            f.stage = "comment"
    elif f.stage == "upload":
        f.stage = "preview"
    elif f.stage == "edit":
        if value == "enterprise" and d.get("file_ids"):
            raise BusinessError("Уберите выбранные файлы перед сменой предприятия.")
        if value == "enterprise":
            d["editing_enterprise"] = True
        if value == "question":
            d["question"], d["answers"] = 0, []
        f.stage = "comment" if value == "question" and not TREE[d["branch"]][d["category"]]["questions"] else value
    elif f.stage == "preview":
        if value in {"comment", "upload", "edit"}:
            f.stage = value
        elif value == "pause":
            d["_resume_stage"] = "preview"
            f.stage = "paused"
        elif value == "remove_files":
            from ama.models import IntakeFile

            for ident in d.get("file_ids", []):
                file = db.get(IntakeFile, ident)
                if file and not file.work_id:
                    file.status = "cancelled"
                    file.version += 1
            d["file_ids"] = []
        elif value == "cancel":
            f.stage = "cancelled"
        else:
            c = TREE[d["branch"]][d["category"]]
            w = create_work(
                db,
                actor,
                {
                    "title": (c["title"] + (": " + d.get("comment", "") if d.get("comment") else ""))[:250],
                    "kind": d["branch"],
                    "establishment_id": d.get("establishment_id"),
                    "data": {
                        "answers": d.get("answers", []),
                        "comment": d.get("comment"),
                        "suggested_route": c.get("suggested_route"),
                        "paid_possible": c.get("paid_possible", False),
                    },
                },
                command_key,
            )
            from ama.intake import confirm_dialog

            confirm_dialog(db, actor, f, w)
            d["work_id"], f.stage = w.id, "done"
    f.version, f.data = f.version + 1, d
    if f.stage == "done":
        return f"Запрос создан: {d['work_id']}.\nФото/файл можно добавить с подписью /file {d['work_id']}", [
            ["Открыть запрос", f"af:w:{d['work_id']}"]
        ]
    return screen(db, actor, f)


def comment(db, actor, text):
    f = db.scalar(
        select(Dialog)
        .where(Dialog.person_id == actor.person_id, Dialog.binding_id == actor.binding_id, Dialog.stage == "comment")
        .with_for_update()
    )
    if not f:
        return None
    require(db, actor, {"partner"})
    if not text.strip() or len(text) > 2000:
        raise BusinessError("Комментарий должен содержать от 1 до 2000 символов.")
    f.data, f.stage, f.version = {**f.data, "comment": text.strip()}, "preview", f.version + 1
    return screen(db, actor, f)

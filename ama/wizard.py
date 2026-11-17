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
    if form.stage == "enterprise":
        b = db.get(Binding, actor.binding_id)
        rows = list(db.scalars(select(Entity).join(EstablishmentBrand, EstablishmentBrand.establishment_id == Entity.id).where(
            Entity.kind == "establishment", Entity.partner_id == b.partner_id, EstablishmentBrand.brand_id == b.brand_id,
            EstablishmentBrand.active.is_(True))))
        return "К какому предприятию относится вопрос?", [{"text": e.name, "value": e.id} for e in rows] + [{"text": "По бренду в целом", "value": None}]
    if form.stage == "branch":
        return "С чем связан запрос?", [{"text": "Маркетинг и реклама", "value": "marketing"}, {"text": "Другой вопрос", "value": "support"}]
    if form.stage == "category":
        return "Выберите тему", [{"text": c["title"], "value": i} for i, c in enumerate(TREE[d["branch"]])]
    if form.stage == "question":
        question = TREE[d["branch"]][d["category"]]["questions"][d["question"]]
        return question["text"], [{"text": v, "value": v} for v in question["options"]]
    if form.stage == "preview":
        c = TREE[d["branch"]][d["category"]]
        e = db.get(Entity, d.get("establishment_id")) if d.get("establishment_id") else None
        text = f"Проверьте запрос\n{e.name if e else 'По бренду в целом'}\n{c['title']}\n" + "\n".join(f"{a['question']}: {a['answer']}" for a in d.get("answers", []))
        if d.get("comment"):
            text += "\nКомментарий: " + d["comment"]
        return text, [{"text": "Отправить", "value": "confirm"}, {"text": "Добавить комментарий", "value": "comment"}, {"text": "Отмена", "value": "cancel"}]
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
    f = db.scalar(select(Dialog).where(Dialog.person_id == actor.person_id, Dialog.binding_id == actor.binding_id).with_for_update())
    if not f:
        f = Dialog(person_id=actor.person_id, binding_id=actor.binding_id)
        db.add(f)
        db.flush()
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
    if f.stage == "enterprise":
        d["establishment_id"], f.stage = value, "branch"
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
    elif f.stage == "preview":
        if value == "comment":
            f.stage = "comment"
        elif value == "cancel":
            f.stage = "cancelled"
        else:
            c = TREE[d["branch"]][d["category"]]
            w = create_work(db, actor, {"title": (c["title"] + (": " + d.get("comment", "") if d.get("comment") else ""))[:250],
                            "kind": d["branch"], "establishment_id": d.get("establishment_id"),
                            "data": {"answers": d.get("answers", []), "comment": d.get("comment"), "suggested_route": c.get("suggested_route"),
                                     "paid_possible": c.get("paid_possible", False)}}, command_key)
            d["work_id"], f.stage = w.id, "done"
    f.version, f.data = f.version + 1, d
    if f.stage == "done":
        return f"Запрос создан: {d['work_id']}.\nФото/файл можно добавить с подписью /file {d['work_id']}", [["Открыть запрос", f"af:w:{d['work_id']}"]]
    return screen(db, actor, f)


def comment(db, actor, text):
    f = db.scalar(select(Dialog).where(Dialog.person_id == actor.person_id, Dialog.binding_id == actor.binding_id, Dialog.stage == "comment").with_for_update())
    if not f:
        return None
    require(db, actor, {"partner"})
    if not text.strip() or len(text) > 2000:
        raise BusinessError("Комментарий должен содержать от 1 до 2000 символов.")
    f.data, f.stage, f.version = {**f.data, "comment": text.strip()}, "preview", f.version + 1
    return screen(db, actor, f)

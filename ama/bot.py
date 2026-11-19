from ama.config import settings
import json
import hashlib
from sqlalchemy import select
from ama.adapters import normalize
from ama.auth import identify, require, roles_for
from ama.errors import BusinessError, AccessDenied, RetryLater
from ama.models import (Binding, Work, Outbox, Record, Entity, Account, Identity, Lead, Attachment,
                        EstablishmentBrand)
from ama.service import queue, create_work, transition, attach, STATUSES
from ama import admin, sales, projects, broadcasts, operations, wizard, reminders

MENU = [["Предприятия", "af:enterprises"], ["Мои задачи", "af:tasks"], ["Ознакомление", "af:training"],
        ["База знаний", "af:knowledge"], ["Задать вопрос", "af:new"], ["Мои проекты", "af:projects"], ["Сетевые обновления", "af:packets"], ["Цифровое подключение", "af:digital"], ["Помощь", "af:help"]]
HELP = """АМА Франчайзинг
/start — меню; /tasks — задачи; /enterprises — предприятия
/training — персональное ознакомление; /knowledge — материалы
/request {"establishment_id":"ID","kind":"support","title":"Ваш вопрос"}
/work ID — открыть карточку; /files ID — получить вложения
Файл отправляйте с подписью /file ID_ПУНКТА. Без ID бот запросит выбор пункта.
Для сотрудников: /create, /act, /project, /sales, /contract, /deal, /broadcast принимают JSON.
Пример: /act {"id":"ID","version":1,"action":"take"}
/af_settings — настройки (только администратор управляющего чата).
Полный формат команд — docs/COMMANDS.md."""


def send(db, event, text, buttons=None, suffix="answer"):
    binding = db.scalar(select(Binding).where(Binding.bot_id == settings().bot_id, Binding.platform == event.platform, Binding.chat_id == event.chat_id,
                                             Binding.topic_key == event.topic))
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
    for i, chunk in enumerate(chunks):
        db.add(Outbox(dedupe_key=f"reply:{event.key}:{suffix}:{i}", platform=event.platform, chat_id=event.chat_id,
                      topic_key=event.topic, binding_id=binding.id if binding else None,
                      binding_version=binding.version if binding else None,
                      payload={"text": chunk, "buttons": (buttons or []) if i == len(chunks) - 1 else []}))


def json_arg(text):
    try:
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError) as e:
        raise BusinessError("После команды нужен JSON-объект. Форматы — /help.") from e


def card(db, actor, w):
    roles = roles_for(db, actor, w)
    if not roles:
        raise AccessDenied("Нет доступа к карточке.")
    text = f"{w.title}\n{STATUSES[w.status]}\nID: {w.id}\nВерсия: {w.version}"
    buttons = []
    if actor.person_id == w.owner_id:
        if w.status in {"new", "rework", "waiting_uk"}:
            buttons.append(["Взять в работу", f"af:a:{w.id}:{w.version}:take"])
        if w.status in {"in_progress", "rework", "materials"}:
            buttons.append(["Результат готов", f"af:a:{w.id}:{w.version}:submit"])
    if actor.person_id == w.reviewer_id and w.status == "review":
        if actor.person_id==w.partner_person_id and w.data.get("partner_request"):
            buttons.append(["Проблема не решена…",f"af:return:{w.id}"])
        buttons.append(["Принять", f"af:a:{w.id}:{w.version}:accept"])
    if actor.person_id == w.pending_owner_id and actor.binding_id == w.pending_binding_id:
        buttons.append(["Принять передачу", f"af:a:{w.id}:{w.version}:handoff_accept"])
    if db.get(Binding, actor.binding_id).purpose != "client":
        text += f"\nОтветственный: {w.owner_id}\nПроверяющий: {w.reviewer_id}\nСрок (UTC): {w.due_at:%Y-%m-%d %H:%M}"
        if w.data.get("waiting"):
            text += "\nОжидаем: " + json.dumps(w.data["waiting"], ensure_ascii=False)
        if w.data.get("rejection"):
            text += "\nЗамечание: " + w.data["rejection"]
    return text, buttons


def public_entry(db, event):
    if not event.private or event.files:
        raise AccessDenied("Чат не подключён. Обратитесь к куратору.")
    consent = db.scalar(select(Record).where(Record.kind == "consent", Record.published.is_(True)))
    if not consent:
        send(db, event, "Приём заявок ещё не активирован: владелец не опубликовал согласие на обработку данных.")
        return
    if event.callback != f"af:consent:{consent.key}":
        send(db, event, consent.data["text"] + "\n" + consent.data["privacy_url"],
             [["Согласен, продолжить", f"af:consent:{consent.key}"]])
        return
    account = db.scalar(select(Account).where(Account.platform == event.platform, Account.external_id == event.user_id))
    if account and not account.active:
        raise AccessDenied("Учётная запись отозвана. Обратитесь к куратору.")
    if not account:
        person = Identity(name="Лид")
        db.add(person)
        db.flush()
        account = Account(person_id=person.id, platform=event.platform, external_id=event.user_id)
        db.add(account)
        db.flush()
    lead = sales.lead_entry(db, account.person_id, "bot", consent.key)
    reminders.react(db, lead, {"platform": event.platform, "chat_id": event.chat_id})
    route = db.scalar(select(Binding).where(Binding.purpose == "sales", Binding.active.is_(True), Binding.verified.is_(True)))
    if route:
        queue(db, route, f"lead:{lead.id}", f"Новый лид {lead.id}. Для принятия: /sales " +
              json.dumps({"id": lead.id, "version": lead.version, "action": "take"}, ensure_ascii=False))
    send(db, event, "Согласие сохранено. Напишите имя, контакт, город и интересующий бренд; консультант продолжит диалог.")


def handle_lead_message(db, event, account):
    lead = db.scalar(select(Lead).where(Lead.person_id == account.person_id).with_for_update())
    if not lead:
        return public_entry(db, event)
    # Содержательное сообщение отменяет будущие касания, но явный отказ сам не снимает.
    from ama.models import now
    if event.text in {"/start", "/menu"}:
        send(db, event, "Напишите вопрос или /stop, чтобы прекратить контакты.")
        return
    reminders.react(db, lead, {"platform": event.platform, "chat_id": event.chat_id})
    lead.last_reaction_at, lead.version = now(), lead.version + 1
    if event.text.startswith("/stop"):
        lead.no_contact, lead.result = True, "refused"
        send(db, event, "Автоматические контакты отключены.")
        return
    route = db.scalar(select(Binding).where(Binding.purpose == "sales", Binding.active.is_(True), Binding.verified.is_(True)))
    if route:
        queue(db, route, f"lead-message:{event.key}", f"Лид {lead.id}:\n{event.text[:3000]}")
    send(db, event, "Сообщение сохранено для консультанта." if route else "Сообщение получено. Продажи временно не подключены.")


def handle(db, platform, raw):
    event = normalize(platform, raw)
    if not event:
        return
    # Чужие callbacks не обрабатываются и не запускают меню.
    if event.callback and not event.callback.startswith("af:"):
        return
    try:
        actor = identify(db, event.platform, event.user_id, event.chat_id, event.topic)
    except AccessDenied:
        if event.private:
            from ama.prospects import handle as handle_prospect
            return handle_prospect(db, event)
        account = db.scalar(select(Account).where(Account.platform == event.platform, Account.external_id == event.user_id, Account.active.is_(True)))
        if event.private and account and not event.callback:
            return handle_lead_message(db, event, account)
        return public_entry(db, event)
    binding = db.get(Binding, actor.binding_id)
    from ama.ui import engine as ui, access as ui_access
    internal = binding.purpose != "client"
    if event.callback and event.callback.startswith("af:ui:"):
        ui.reply(db, actor, event, ui.callback(db, actor, event.callback))
        return
    if event.files:
        from ama.packets import asset_receive
        if asset_receive(db,actor,event):
            send(db,event,"Файлы пакета приняты на сохранение. После сохранения откройте предпросмотр; отправка получателям ещё не выполнялась.")
            return
        from ama.models import MediaGroup
        parts = event.text.split()
        work_id = parts[1] if len(parts) > 1 and parts[0] == "/file" else ui.attachment_context(db, actor)
        for i, f in enumerate(event.files):
            group = None
            group_key = None
            if f.get("album_id"):
                group_key = hashlib.sha256(f"{event.platform}:{event.chat_id}:{event.topic}:{f['album_id']}".encode()).hexdigest()
                group = db.get(MediaGroup, group_key)
                if group:
                    if group.author_id != actor.person_id or group.binding_id != actor.binding_id or (work_id and work_id != group.work_id):
                        raise AccessDenied("Контекст альбома не совпадает с отправителем или пунктом.")
                    work_id = group.work_id
                elif not work_id:
                    raise RetryLater("Для альбома не найден пункт. Отправьте его с подписью /file ID_ПУНКТА.")
            if not work_id:
                send(db, event, "Выберите пункт: отправьте файл с подписью /file ID_ПУНКТА. Без связи файл никуда не передан.")
                return
            attach(db, actor, work_id, event.platform, f["file_id"], f["filename"], f"{event.key}:{i}", f["album_id"])
            if group_key and not group:
                db.add(MediaGroup(id=group_key, work_id=work_id, author_id=actor.person_id, binding_id=actor.binding_id))
        if not event.files[0].get("album_id") or (len(parts) > 1 and parts[0] == "/file"):
            send(db, event, "Вложение принято на сохранение. Статус приёмки пункта не изменён.")
        return
    text = event.text or ""
    if text and not text.startswith("/") and not event.callback:
        response = ui.input_text(db, actor, text)
        if response:
            ui.reply(db, actor, event, response)
            return
        response = wizard.comment(db, actor, text)
        if response:
            send(db, event, *response)
            return
    if event.callback:
        p = event.callback.split(":")
        if p[1] == "pack":
            ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),"pk_view",{"revision_id":p[2]}))
            return
        if p[1] == "return":
            ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),"sla_client_return",{"work_id":p[2]}))
            return
        if internal and p[1] in {"w", "a"}:
            ui_access.require_staff(db, actor)
            screen = ui.session(db, actor)
            params = {"work_id": p[2]}
            if p[1] == "a":
                params.update(expected=int(p[3]), action=p[4])
            ui.reply(db, actor, event, ui.dispatch(db, actor, screen, "work" if p[1] == "w" else "work_action", params))
            return
        if p[1] == "q":
            response = wizard.advance(db, actor, p[2], int(p[3]), int(p[4]), event.key)
            send(db, event, *response)
            return
        if p[1] == "w":
            text = f"/work {p[2]}"
        elif p[1] == "a":
            text = "/act " + json.dumps({"id": p[2], "version": int(p[3]), "action": p[4]})
        elif p[1] == "t":
            text = "/training " + json.dumps({"action": p[2], "version": int(p[3]), "course": "call_center" if len(p) > 4 and p[4] == "c" else "general"})
        else:
            text = "/" + p[1]
    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower()
    if command == "/finance" and not arg:
        ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),"fi_home"))
        return
    if command == "/broadcast" and not arg:
        ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),"pk_list"))
        return
    if (command == "/digital" and not arg) or text == "af:digital":
        ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),"dg_list"))
        return
    if command == "/packets" and not arg:
        ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),"pk_list"))
        return
    if command == "/sla" and not arg:
        ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),"sla_list"))
        return
    if command == "/projects" and not arg:
        ui.reply(db, actor, event, ui.dispatch(db,actor,ui.session(db,actor),"pp_list"))
        return
    if command == "/af_settings" and not arg:
        ui.reply(db, actor, event, ui.dispatch(db, actor, ui.session(db, actor), "settings"))
        return
    ui_commands = {"/start": "home", "/menu": "home", "/workspace": "workspace", "/help": "staff_help",
                   "/tasks": "works", "/work": "work", "/enterprises": "enterprises", "/create": "new",
                   "/project": "new", "/broadcast": "new", "/sales": "leads", "/deal": "deals",
                   "/contract": "deals", "/op": "works", "/reports": "new", "/drafts": "drafts"}
    if internal and command in ui_commands and (not arg or command == "/work"):
        action = ui_commands[command]
        params = {"work_id": arg.strip()} if command == "/work" else {"kind": {
            "/create": "create_work", "/project": "project", "/broadcast": "broadcast", "/reports": "export"}[command]} if action == "new" else {}
        ui.reply(db, actor, event, ui.dispatch(db, actor, ui.session(db, actor), action, params))
        return
    if command in {"/start", "/menu"}:
        require(db, actor, {"partner", "observer", "curator", "support_head", "specialist", "marketing_manager", "admin", "consultant", "contract", "chief_marketer"})
        send(db, event, "АМА Франчайзинг · Главное меню", MENU)
    elif command == "/help":
        send(db, event, HELP)
    elif command in {"/tasks", "/work"}:
        if command == "/work":
            works = [db.get(Work, arg.strip())]
        else:
            works = list(db.scalars(select(Work).where(Work.status != "done").order_by(Work.due_at).limit(200)))
        rows = []
        for w in works:
            if not w:
                continue
            try:
                if roles_for(db, actor, w):
                    rows.append(w)
            except AccessDenied:
                continue
        if not rows:
            send(db, event, "Доступных работ нет.")
        elif command == "/work":
            text, buttons = card(db, actor, rows[0])
            send(db, event, text, buttons)
        else:
            send(db, event, "Текущие работы (первые 30):", [[w.title[:70], f"af:w:{w.id}"] for w in rows[:30]])
    elif command == "/enterprises":
        require(db, actor, {"partner", "observer", "curator", "support_head", "marketing_manager"})
        query = select(Entity).where(Entity.kind == "establishment")
        if binding.partner_id:
            query = query.where(Entity.partner_id == binding.partner_id)
        if binding.brand_id:
            query = query.join(EstablishmentBrand, EstablishmentBrand.establishment_id == Entity.id).where(EstablishmentBrand.brand_id == binding.brand_id, EstablishmentBrand.active.is_(True))
        # Для внутреннего списка также фильтруем области действующих назначений.
        from types import SimpleNamespace
        rows = []
        for e in db.scalars(query):
            scope = SimpleNamespace(binding_id=binding.id, client_binding_id=None, partner_id=e.partner_id,
                                    brand_id=binding.brand_id, establishment_id=e.id)
            if roles_for(db, actor, scope):
                rows.append(f"{e.name}: {e.id}")
        send(db, event, "Предприятия:\n" + ("\n".join(rows) or "Пока нет подключённых предприятий."))
    elif command == "/new":
        send(db, event, *wizard.start(db, actor))
    elif command in {"/request", "/create"}:
        d = json_arg(arg)
        w = create_work(db, actor, d, event.key)
        send(db, event, f"Работа зарегистрирована: {w.id}", [["Открыть", f"af:w:{w.id}"]])
    elif command == "/act":
        d = json_arg(arg)
        w = transition(db, actor, d["id"], d["version"], d["action"], d.get("data", {}), event.key)
        text, buttons = card(db, actor, w)
        send(db, event, text, buttons)
    elif command in {"/training","/learning","/knowledge"}:
        d=json_arg(arg) if arg and command=="/training" else {}
        action="lr_materials" if command=="/knowledge" else "lr_course" if command=="/training" else "lr_home"
        params={"course":d.get("course","general"),"op":d.get("action","open"),"expected":d.get("version")} if command=="/training" else {}
        ui.reply(db,actor,event,ui.dispatch(db,actor,ui.session(db,actor),action,params))
    elif command == "/files":
        w = db.get(Work, arg.strip())
        if not w:
            raise BusinessError("Работа не найдена.")
        require(db, actor, {"curator", "support_head", "marketing_manager", "specialist", "contract", "partner", "observer"}, w)
        # Наружу допускаются только клиентские материалы. Внутренние файлы — только в рабочем контуре.
        for a in db.scalars(select(Attachment).where(Attachment.work_id == w.id, Attachment.status == "stored")):
            if binding.purpose == "client" and a.visibility != "client":
                continue
            queue(db, binding, f"files:{event.key}:{a.id}", f"Материал пункта {w.id}", work=w, attachment_id=a.id)
        send(db, event, "Доступные сохранённые материалы поставлены в очередь доставки.")
    elif command == "/af_settings":
        if arg:
            result = admin.apply(db, actor, json_arg(arg))
        else:
            result = admin.inspect(db, actor)
        send(db, event, json.dumps(result, ensure_ascii=False, indent=2))
    elif command == "/op":
        d = json_arg(arg)
        w = operations.operation(db, actor, d["id"], d["version"], d["action"], d.get("data", {}))
        send(db, event, f"Сохранено. ID: {w.id}. Версия: {w.version}")
    elif command == "/project":
        result = projects.configure(db, actor, json_arg(arg), event.key)
        send(db, event, json.dumps(result, ensure_ascii=False))
    elif command in {"/sales", "/contract", "/deal"}:
        d = json_arg(arg)
        if command == "/sales":
            obj = sales.update_lead(db, actor, d["id"], d["version"], d["action"], d.get("data", {}))
        elif command == "/contract":
            obj = sales.start_contract(db, actor, d["id"], d["version"], d.get("data", {}))
        else:
            obj = sales.contract_action(db, actor, d["id"], d["version"], d["action"], d.get("data", {}), event.key)
        send(db, event, f"Сохранено. ID: {obj.id}. Версия: {obj.version}")
    elif command == "/lead_reply":
        sales.sales_role(db, actor, {"consultant"})
        d = json_arg(arg)
        lead = db.get(Lead, d["id"])
        if not lead or lead.owner_id != actor.person_id or lead.no_contact:
            raise AccessDenied("Нет права отправки лиду или установлен запрет контактов.")
        a = db.scalar(select(Account).where(Account.person_id == lead.person_id, Account.active.is_(True)))
        if not a:
            raise BusinessError("Нет активного аккаунта лида.")
        contact = lead.data.get("contact") or ({"platform": a.platform, "chat_id": a.external_id} if a.platform == "telegram" else None)
        if not contact:
            raise BusinessError("Нет подтверждённого личного чата лида.")
        db.add(Outbox(dedupe_key=f"lead-reply:{event.key}", platform=contact["platform"], chat_id=contact["chat_id"],
                      payload={"text": d["text"], "lead_id": lead.id, "touch_reason": d.get("touch_reason", "consultant_reply"),
                               "silence_epoch": lead.data.get("silence", {}).get("epoch")}))
        send(db, event, "Ответ поставлен в очередь. Автоматически внутренние заметки не отправляются.")
    elif command == "/broadcast":
        d = json_arg(arg)
        if d.get("action") == "publish":
            b = broadcasts.publish(db, actor, d["id"], d["version"])
            result = {"id": b.id, "published": True}
        else:
            b = broadcasts.preview(db, actor, d["text"], d.get("brand_id"))
            result = {"id": b.id, "version": b.version, "chats": len(b.binding_ids), "text": b.text, "published": False}
        send(db, event, json.dumps(result, ensure_ascii=False))
    else:
        send(db, event, "Выберите действие из меню или /help.", MENU)

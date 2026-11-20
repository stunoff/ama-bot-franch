"""Human-confirmed tax and Sberbank setup; no bank credentials or automatic account opening."""

from copy import deepcopy
import re
from sqlalchemy import select
from ama.models import Binding, ProjectPlan, Work, now
from ama.auth import active_person_role
from ama.config import settings
from ama.errors import AccessDenied, BusinessError
from ama.service import queue, parse_date, stamp

TAX_NOTICE = "Важно выбрать и оформить налоговый режим. При отсутствии необходимых действий есть риск остаться на общей системе (ОСНО). Применимость и сроки уточните с профильным специалистом. Бот не выбирает налоговый режим за ФП."


def client_notice(db, p, w, key, text, buttons=None):
    b = db.get(Binding, p.client_binding_id)
    if (
        not b
        or b.bot_id != p.bot_id
        or not b.active
        or not b.verified
        or b.purpose != "client"
        or b.partner_id != p.partner_id
        or b.brand_id != p.brand_id
    ):
        return
    row = queue(db, b, key, text, buttons=buttons)
    row.payload = {
        **row.payload,
        "legal_setup_notice": {"work_id": w.id, "project_id": p.id, "person_id": p.partner_person_id},
        "partner_window": True,
    }


def on_launch(db, p, b, w):
    if b.code == "tax":
        w.data = {**w.data, "tax_notice": TAX_NOTICE}
        client_notice(db, p, w, "tax-warning:" + w.id, TAX_NOTICE, [["Требуется консультация", "af:tax:" + w.id]])


def tax_consultation(db, actor, work_id):
    w = db.scalar(select(Work).where(Work.id == work_id).with_for_update())
    p = db.get(ProjectPlan, w.project_id) if w else None
    if (
        not p
        or p.bot_id != settings().bot_id
        or actor.binding_id != p.client_binding_id
        or actor.person_id != p.partner_person_id
        or not active_person_role(db, actor.person_id, {"partner"}, p)
    ):
        raise AccessDenied("Консультацию запрашивает назначенный ФП в своём чате.")
    if not w.data.get("tax_notice") or w.status == "done":
        raise BusinessError("Налоговый пункт недоступен.")
    if w.data.get("tax_consultation_requested"):
        return
    w.data = {**w.data, "tax_consultation_requested": {"actor_id": actor.person_id, "at": now().isoformat()}}
    w.version += 1
    queue(
        db,
        db.get(Binding, w.binding_id),
        "tax-consultation:" + w.id,
        "ФП запросил консультацию по налоговому режиму. Передайте вопрос профильному специалисту в этой же цепочке.\nПункт: "
        + w.id,
        work=w,
    )
    stamp(db, actor, w, "TAX_CONSULTATION_REQUESTED", w.version - 1, role="partner")


def details(db, actor, w, block, data):
    if block.code not in {"bank", "tax"}:
        return False
    p = db.get(ProjectPlan, block.project_id)
    source = db.get(Binding, actor.binding_id)
    reviewer = (
        actor.person_id == p.curator_id
        and active_person_role(db, actor.person_id, {"curator", "support_head"}, p)
        and source.purpose == "support"
    )
    executor = (
        actor.person_id == w.executor_id
        and source.id == w.binding_id
        and active_person_role(db, actor.person_id, {"specialist", "curator"}, w)
    )
    if not (reviewer or executor):
        raise AccessDenied("Форму фиксирует назначенный исполнитель / куратор.")
    action = data["step"]
    if block.code == "tax":
        if (
            action != "tax_regime"
            or data.get("regime") not in {"usn", "psn", "other", "consultation"}
            or not data.get("evidence")
        ):
            raise BusinessError("Укажите налоговый режим / консультацию и подтверждение ФП.")
        w.data = {
            **w.data,
            "tax_history": [*w.data.get("tax_history", []), deepcopy(w.data.get("tax_regime"))],
            "tax_regime": {
                "regime": data["regime"],
                "evidence": data["evidence"],
                "actor_id": actor.person_id,
                "at": now().isoformat(),
            },
        }
        return True
    if action == "bank_form":
        if w.data.get("bank_sent"):
            raise BusinessError(
                "Отправленная форма сохраняется. Изменения согласуйте со специалистом отдельным уточнением."
            )
        form = {
            k: str(data.get(k) or "").strip()
            for k in ("company_type", "company_name", "inn", "ogrn", "contact_name", "phone", "consent_evidence")
        }
        expected = {"ip": (12, 15), "llc": (10, 13)}.get(form["company_type"])
        if (
            not expected
            or not all(form.values())
            or not re.fullmatch(r"\d{" + str(expected[0]) + "}", form["inn"])
            or not re.fullmatch(r"\d{" + str(expected[1]) + "}", form["ogrn"])
            or not re.fullmatch(r"\+\d{10,15}", form["phone"])
        ):
            raise BusinessError(
                "Заполните форму ИП/ООО, ИНН/ОГРН нужной длины, контакт, телефон +код и подтверждение согласия ФП."
            )
        previous = w.data.get("bank_form")
        w.data = {
            **w.data,
            "bank_form_history": [*w.data.get("bank_form_history", []), *([previous] if previous else [])],
            "bank_form": {
                **form,
                "version": (previous or {}).get("version", 0) + 1,
                "actor_id": actor.person_id,
                "at": now().isoformat(),
            },
        }
    elif action == "bank_sent":
        form = w.data.get("bank_form")
        if (
            not executor
            or source.purpose != "registrations"
            or not form
            or data.get("form_version") != form["version"]
            or not data.get("evidence")
            or not data.get("next_check")
        ):
            raise BusinessError(
                "Отправку фиксирует исполнитель регистраций: актуальная версия формы, подтверждение передачи банку и дата контроля обязательны."
            )
        at = parse_date(data["at"]) if data.get("at") else now()
        if at > now() or parse_date(data["next_check"]) <= now():
            raise BusinessError("Нужны фактическая дата отправки и будущая проверка.")
        if w.data.get("bank_sent"):
            raise BusinessError("Передача этой формы уже подтверждена.")
        w.data = {
            **w.data,
            "bank_sent": {
                "form_version": form["version"],
                "evidence": data["evidence"],
                "at": at.isoformat(),
                "actor_id": actor.person_id,
            },
        }
        from ama.service import transition

        transition(
            db,
            actor,
            w.id,
            w.version,
            "wait_external",
            {
                "party": "Сбербанк",
                "required": "Звонок банка и подтверждение результата открытия счёта",
                "sent_information": "Согласованная форма ИП/ООО версии " + str(form["version"]),
                "next_check": data["next_check"],
            },
            "bank-wait:" + w.id + ":" + str(form["version"]),
        )
        client_notice(
            db,
            p,
            w,
            "bank-call:" + w.id + ":" + str(form["version"]),
            "Форма передана специалистом в Сбербанк. Ожидайте звонка банка по согласованному контакту. Открытие счёта будет подтверждено отдельно.",
        )
    elif action == "bank_result":
        if not w.data.get("bank_sent") or not data.get("evidence"):
            raise BusinessError("Нужны подтверждённая передача формы и фактический результат банка.")
        w.data = {
            **w.data,
            "bank_result": {"evidence": data["evidence"], "actor_id": actor.person_id, "at": now().isoformat()},
        }
    else:
        raise BusinessError("Выберите шаг формы банка / налогового режима.")
    return True


def before_transition(w, code):
    if code == "tax" and (not w.data.get("tax_regime") or w.data["tax_regime"]["regime"] == "consultation"):
        raise BusinessError("Нужен подтверждённый налоговый режим; запрос консультации не равен результату.")
    if code == "bank" and not w.data.get("bank_result"):
        raise BusinessError("Передача формы не равна открытому счёту. Нужен подтверждённый результат банка.")


def deliverable(db, row):
    data = row.payload.get("legal_setup_notice")
    if not data:
        return True
    p = db.get(ProjectPlan, data["project_id"])
    w = db.get(Work, data["work_id"])
    return bool(
        p
        and w
        and p.bot_id == settings().bot_id
        and p.client_binding_id == row.binding_id
        and p.partner_person_id == data["person_id"]
        and active_person_role(db, p.partner_person_id, {"partner"}, p)
    )

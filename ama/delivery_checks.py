"""Identical probe identity checks for sending and manual delivery confirmation."""

from ama.config import settings


def current_probe(binding, row):
    return bool(
        binding
        and binding.bot_id == settings().bot_id
        and binding.version == row.binding_version
        and (binding.platform, binding.chat_id, binding.topic_key) == (row.platform, row.chat_id, row.topic_key)
    )


def confirm_probe(db, row):
    if row.status != "sent" or not row.payload.get("probe"):
        return
    from sqlalchemy import select
    from ama.models import Binding

    b = db.scalar(
        select(Binding).where(Binding.id == row.binding_id).with_for_update().execution_options(populate_existing=True)
    )
    if current_probe(b, row):
        b.verified = True

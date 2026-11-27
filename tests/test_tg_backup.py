# -*- coding: utf-8 -*-
"""Резервная копия заказов рекламы в Telegram: хостинг стёр data/ — бот восстанавливает всё."""
import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Bot:
    def __init__(self):
        self.pinned = None
        self.files = {}
        self.calls = []

    async def get_chat(self, chat_id):
        return NS(pinned_message=self.pinned)

    async def send_document(self, chat_id, file, caption=None, **kw):
        self.calls.append("send")
        self.files["f1"] = file.data
        return NS(message_id=77)

    async def pin_chat_message(self, chat_id, mid, **kw):
        self.calls.append("pin")
        self.pinned = NS(message_id=mid, document=NS(file_name="ad_orders_backup.json", file_id="f1"))

    async def edit_message_media(self, chat_id, message_id, media):
        self.calls.append("edit")
        self.files["f1"] = media.media.data

    async def get_file(self, file_id):
        return NS(file_path=file_id)

    async def download_file(self, path):
        return io.BytesIO(self.files[path])


def test_backup_and_restore(tmp_path, monkeypatch):
    import modules.ad_orders as ad
    import modules.notifications as notif
    import utils.tg_backup as b
    from config.settings import config_manager

    monkeypatch.setattr(ad, "STATE_PATH", tmp_path / "ad.json")
    monkeypatch.setattr(config_manager.settings.telegram, "admin_ids", [111])
    bot = Bot()
    monkeypatch.setattr(notif, "_bot_ref", bot)
    monkeypatch.setattr(b, "_msg_id", None)

    ad._save({"A1": {"order_id": "A1", "ts": 9e9, "status": "waiting", "post": {"stage": "queued", "publish_at": 1}}})
    assert b._dirty
    assert asyncio.run(b.upload()) and bot.calls == ["send", "pin"]
    ad.update("A1", buyer="Vasya")
    assert asyncio.run(b.upload()) and bot.calls[-1] == "edit"          # правится то же сообщение

    (tmp_path / "ad.json").unlink()                                      # хостинг стёр data/
    monkeypatch.setattr(b, "_msg_id", None)
    assert asyncio.run(b.restore_if_needed()) == 1
    e = ad.get("A1")
    assert e["buyer"] == "Vasya" and e["post"]["stage"] == "queued"
    assert asyncio.run(b.restore_if_needed()) == 0                       # уже есть — повторно не трогаем

    ad.update("A1", buyer="Petya")                                       # локальные данные свежее копии
    assert asyncio.run(b.restore_if_needed()) == 0 and ad.get("A1")["buyer"] == "Petya"

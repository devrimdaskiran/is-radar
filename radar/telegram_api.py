"""Kütüphanesiz, senkron Telegram Bot API istemcisi (GitHub Actions çalıştırıcısı için).

Hata mesajlarında token asla görünmez.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.token = re.sub(r"\s+", "", token or "")
        self.client = client or httpx.Client(timeout=40)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def call(self, method: str, **params):
        if not self.enabled:
            raise TelegramError("TELEGRAM_BOT_TOKEN tanımlı değil.")
        try:
            r = self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
        except httpx.HTTPError as e:
            raise TelegramError(f"Telegram'a ulaşılamadı ({method}): {type(e).__name__}") from None
        try:
            d = r.json()
        except ValueError:
            d = {}
        if r.status_code != 200 or not d.get("ok"):
            hint = " (token yanlış olabilir)" if r.status_code in (401, 404) else ""
            raise TelegramError(f"Telegram {method} başarısız: {r.status_code} {d.get('description', '')}{hint}")
        return d["result"]

    # --- kısayollar
    def send(self, chat_id, text: str, buttons: list[list[dict]] | None = None) -> int | None:
        last = None
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        for i, ch in enumerate(chunks):
            params = {"chat_id": chat_id, "text": ch, "parse_mode": "HTML", "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = {"inline_keyboard": buttons}
            last = self.call("sendMessage", **params)
        return last.get("message_id") if last else None

    def updates(self, offset: int | None) -> list[dict]:
        params = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if offset:
            params["offset"] = offset
        return self.call("getUpdates", **params)

    def answer_callback(self, cb_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=cb_id, text=text[:190])
        except TelegramError:
            pass  # eski butonlara (15 dk+) cevap verilemez; önemli değil

    def edit_buttons(self, chat_id, message_id, buttons) -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                      reply_markup={"inline_keyboard": buttons})
        except TelegramError:
            pass

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        try:
            self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])
        except TelegramError as e:
            log.info("Komut menüsü ayarlanamadı: %s", e)

    def username(self) -> str:
        try:
            return self.call("getMe").get("username", "?")
        except TelegramError:
            return "?"


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)

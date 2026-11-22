"""Komut satırı.

  python -m radar run          Telegram botu + otomatik tarama (sürekli çalışır)
  python -m radar scan         Tek seferlik tarama, sonuç özetini yazar (bildirim göndermez)
  python -m radar check        Ayarları ve kaynak bağlantılarını kontrol eder
  python -m radar whoami       Telegram kullanıcı ID'ni öğrenmek için botu dinler
"""
from __future__ import annotations

import argparse
import logging
import sys

from .app import App, setup_logging


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="radar")
    ap.add_argument("cmd", choices=["run", "scan", "check", "whoami"])
    a = ap.parse_args(argv)
    setup_logging()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if a.cmd == "whoami":
        from telegram import Update
        from telegram.ext import ApplicationBuilder, MessageHandler, filters

        from .config import load_secrets
        sec = load_secrets()
        if not sec.telegram_token:
            print("Önce .env dosyasına TELEGRAM_BOT_TOKEN yaz.")
            return 1

        async def echo(update: Update, ctx):
            uid = update.effective_user.id
            print(f"\n>>> Telegram kullanıcı ID'n: {uid}\n.env dosyasına TELEGRAM_ALLOWED_USER_IDS={uid} yaz, sonra Ctrl+C.\n")
            await update.effective_message.reply_text(f"Kullanıcı ID'n: {uid}")
        tg = ApplicationBuilder().token(sec.telegram_token).build()
        tg.add_handler(MessageHandler(filters.ALL, echo))
        print("Telegram'da botuna herhangi bir mesaj gönder…")
        tg.run_polling()
        return 0

    app = App()
    if a.cmd == "check":
        s = app.secrets
        rows = [("Telegram bot tokenı", bool(s.telegram_token)), ("Yetkili kullanıcı ID", bool(s.allowed_user_ids)),
                ("Jooble API anahtarı", bool(s.jooble_api_key)), ("Gmail adresi + uygulama şifresi", bool(s.gmail_address and s.gmail_app_password)),
                ("Anthropic API anahtarı (isteğe bağlı)", bool(s.anthropic_api_key))]
        for name, ok in rows:
            print(f"[{'OK' if ok else '--'}] {name}")
        print(f"Profil: {len(app.profile.diplomas)} diploma, {len(app.profile.skills)} yetkinlik yüklendi.")
        print("Yapay zekâ:", "AÇIK" if app.analyzer().enabled else "KAPALI (kural tabanlı mod)")
        return 0

    if a.cmd == "scan":
        from .pipeline import scan
        with app.Session() as s:
            rep = scan(s, app.due_collectors(force=True), app.profile, app.settings(), app.analyzer())
        from .bot import RadarBot
        print(RadarBot.scan_summary(rep))
        return 0

    from .bot import RadarBot
    bot = RadarBot(app)
    tg = bot.build()
    logging.getLogger(__name__).info("AI Career Radar başladı.")
    tg.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

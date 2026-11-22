"""Telegram botu: komutlar, bildirim kartları, butonlar, doğal dil araması ve zamanlanmış işler.

Bot yalnızca .env'deki TELEGRAM_ALLOWED_USER_IDS listesindeki kullanıcılara yanıt verir.
Bot internete kendisi çıkmaz; tüm yanıtlar veritabanından gelir. Tarama zamanlanmış işle yapılır.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application as TgApp, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .app import App
from .db import APPLICATION_STATES, Application, Feedback, Job, Notification, Source, set_setting
from .pipeline import scan
from .queries import JobFilter, find_jobs, parse_query, stats

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Istanbul")


from .formatting import E, card, fmt_date, in_quiet_hours, keyboard_rows, short  # noqa: E402,F401


def keyboard(job: Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(**b) for b in row] for row in keyboard_rows(job)])


# ------------------------------------------------------------------ bot
class RadarBot:
    def __init__(self, app: App):
        self.app = app
        self.allowed = set(app.secrets.allowed_user_ids)

    def authorized(self, update: Update) -> bool:
        u = update.effective_user
        ok = bool(u and u.id in self.allowed)
        if not ok:
            log.warning("Yetkisiz erişim denemesi: user_id=%s", u.id if u else None)
        return ok

    def guard(fn):  # noqa: N805
        async def wrapper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not self.authorized(update):
                return
            return await fn(self, update, ctx)
        return wrapper

    async def reply(self, update: Update, text: str, **kw):
        msg = update.effective_message
        for i in range(0, len(text), 3900):
            await msg.reply_text(text[i:i + 3900], parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw)

    async def send_list(self, update, title: str, rows):
        if not rows:
            await self.reply(update, f"{title}\n\nKayıt bulunamadı.")
            return
        await self.reply(update, f"<b>{E(title)}</b>\n\n" + "\n".join(short(j, e) for j, e in rows) +
                         "\n\nAyrıntı için ilan bağlantısına (/ilan_N) dokun.")

    # ---------------- komutlar
    @guard
    async def start(self, update, ctx):
        await self.reply(update, "👋 <b>AI Career Radar</b> çalışıyor.\nYeni ve uygun ilanları buraya göndereceğim. "
                                 "Komutlar için /help yaz. Doğal dilde de sorabilirsin: "
                                 "<i>“Gaziantep'te deneyim istemeyen işler”</i>")

    @guard
    async def help(self, update, ctx):
        await self.reply(update, "\n".join([
            "<b>Komutlar</b>", "/jobs – uygun ilanlar", "/best – en yüksek puanlılar", "/today – bugün bulunanlar",
            "/week – son 7 gün", "/search &lt;kelime/şehir&gt; – arama", "/career – kariyer fırsatları",
            "/favorites – favoriler", "/applications – başvurularım", "/profile – profilim", "/stats – istatistikler",
            "/sources – kaynak durumu", "/settings – ayarlar", "/scan – şimdi tara", "/pause – taramayı duraklat",
            "/resume – devam et", "", "Doğal dil örnekleri: “Son 3 günde açılan otomasyon ilanları”, "
            "“En yüksek puanlı 10 ilan”, “Savunma sanayisindeki tekniker ilanları”, “AutoCAD isteyen firmalar”"]))

    @guard
    async def profile(self, update, ctx):
        p = self.app.profile
        dip = "\n".join(f"• {d['program']} – {d['seviye'].replace('_', ' ')} ({d['durum']})" for d in p.diplomas + p.studying)
        sk = ", ".join(f"{s.name} ({s.level})" for s in p.skills)
        await self.reply(update, f"<b>Profil</b>\n{dip}\n\n<b>Yetkinlikler:</b> {E(sk)}\n"
                                 f"<b>İş deneyimi:</b> {p.experience_years:g} yıl · <b>Staj:</b> {len(p.internships)}\n"
                                 f"<b>Askerlik:</b> {p.military.get('durum')} ({p.military.get('tecil_bitis')})\n"
                                 f"<b>Ehliyet:</b> {', '.join(p.licenses)} · <b>Vardiya:</b> {'uygun' if p.shift_ok else 'hayır'}\n"
                                 f"<i>Değiştirmek için configs/profile.yaml dosyasını düzenle.</i>")

    async def _list(self, update, title, f: JobFilter):
        with self.app.Session() as s:
            rows = find_jobs(s, f)
            await self.send_list(update, title, rows)

    @guard
    async def jobs(self, update, ctx):
        await self._list(update, "Uygun ilanlar", JobFilter(min_score=self.app.settings()["bildirim"]["min_puan"], limit=15))

    @guard
    async def best(self, update, ctx):
        await self._list(update, "En yüksek puanlı ilanlar", JobFilter(limit=10))

    @guard
    async def today(self, update, ctx):
        await self._list(update, "Bugün bulunan ilanlar", JobFilter(since_days=1, sort="date", limit=20))

    @guard
    async def week(self, update, ctx):
        await self._list(update, "Son 7 günün ilanları", JobFilter(since_days=7, limit=20))

    @guard
    async def career(self, update, ctx):
        with self.app.Session() as s:
            rows = find_jobs(s, JobFilter(career=True, limit=20))
            if not rows:
                await self.reply(update, "Henüz kariyer fırsatı kaydı yok.")
                return
            groups: dict[str, list] = {}
            for j, e in rows:
                groups.setdefault(e.career_track, []).append(short(j, e))
            await self.reply(update, "<b>Kariyer Fırsatları</b>\n" +
                             "\n".join(f"\n<b>{E(k)}</b>\n" + "\n".join(v) for k, v in groups.items()))

    @guard
    async def search(self, update, ctx):
        q = " ".join(ctx.args or [])
        if not q:
            await self.reply(update, "Kullanım: /search Gaziantep mekatronik")
            return
        f = parse_query(q)
        if not f.keywords and not f.city:
            f.keywords = q.split()
        await self._list(update, f"Arama: {q}", f)

    @guard
    async def show_job(self, update, ctx):
        try:
            jid = int(update.effective_message.text.split("_", 1)[1].split("@")[0])
        except (IndexError, ValueError):
            return
        with self.app.Session() as s:
            job = s.get(Job, jid)
            if not job or not job.latest_eval:
                await self.reply(update, "İlan bulunamadı.")
                return
            await update.effective_message.reply_text(card(job, job.latest_eval), parse_mode=ParseMode.HTML,
                                                      reply_markup=keyboard(job), disable_web_page_preview=True)

    @guard
    async def favorites(self, update, ctx):
        with self.app.Session() as s:
            favs = s.scalars(select(Feedback).where(Feedback.favorite.is_(True))).all()
            rows = [(s.get(Job, f.job_id), s.get(Job, f.job_id).latest_eval) for f in favs]
            await self.send_list(update, "Favoriler", [r for r in rows if r[1]])

    @guard
    async def applications(self, update, ctx):
        with self.app.Session() as s:
            apps = s.scalars(select(Application).order_by(Application.applied_at.desc())).all()
            if not apps:
                await self.reply(update, "Henüz başvuru kaydın yok. Bildirimdeki “Başvuru Yapıldı” butonuyla eklenir.")
                return
            lines = [f"• <b>{E(a.job.title)}</b> — {E(a.job.company or '?')}\n  {E(a.state)} · {fmt_date(a.applied_at)} "
                     f"/durum_{a.id}" for a in apps]
            await self.reply(update, "<b>Başvurularım</b>\n" + "\n".join(lines))

    @guard
    async def app_state(self, update, ctx):
        try:
            aid = int(update.effective_message.text.split("_", 1)[1].split("@")[0])
        except (IndexError, ValueError):
            return
        kb = [[InlineKeyboardButton(st, callback_data=f"st:{aid}:{i}")] for i, st in enumerate(APPLICATION_STATES)]
        await update.effective_message.reply_text("Yeni durumu seç:", reply_markup=InlineKeyboardMarkup(kb))

    @guard
    async def stats_cmd(self, update, ctx):
        with self.app.Session() as s:
            st = stats(s)
        cities = ", ".join(f"{c} ({n})" for c, n in st["sehirler"]) or "-"
        await self.reply(update, f"<b>İstatistikler</b>\nToplam ilan: {st['toplam']}\nBugün: {st['bugun']} · 7 gün: {st['hafta']}\n"
                                 f"Başvurulabilir: {st['basvurulabilir']} · Uygun (70+): {st['uygun']} · Çok yüksek (80+): {st['cok_yuksek']}\n"
                                 f"Kariyer fırsatı: {st['kariyer']} · Favori: {st['favori']}\n"
                                 f"Başvuru: {st['basvuru']} · Görüşme bekleyen: {st['gorusme']}\nEn çok ilan: {E(cities)}")

    @guard
    async def sources(self, update, ctx):
        with self.app.Session() as s:
            lines = []
            for src in s.scalars(select(Source)):
                icon = {"çalışıyor": "🟢", "hata": "🔴", "yapılandırılmadı": "🟡", "pasif": "⚪"}.get(src.status, "⚪")
                lines.append(f"{icon} <b>{E(src.name)}</b> – {E(src.status)}\n   {E(src.method)}\n"
                             f"   Son tarama: {src.last_run:%d.%m %H:%M}" if src.last_run else
                             f"{icon} <b>{E(src.name)}</b> – {E(src.status)}\n   {E(src.method)}")
                if src.last_success:
                    lines[-1] += f" · Son başarılı: {src.last_success:%d.%m %H:%M} · Bulunan: {src.last_count}"
                if src.last_error:
                    lines[-1] += f"\n   ⚠️ {E(src.last_error[:200])}"
        await self.reply(update, "<b>Kaynaklar</b>\n" + "\n".join(lines) +
                         "\n\n<i>Yalnızca entegre edilen kaynaklar taranır; tüm internet taranmaz.</i>")

    def settings_view(self):
        st = self.app.settings()["bildirim"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Bildirim: {'AÇIK' if st['aktif'] else 'KAPALI'}", callback_data="set:aktif")],
            [InlineKeyboardButton(f"Anlık mod (sessiz saat yok): {'AÇIK' if st['anlik'] else 'KAPALI'}", callback_data="set:anlik")],
            [InlineKeyboardButton("Min puan −5", callback_data="set:min-"),
             InlineKeyboardButton(f"Min puan: {st['min_puan']}", callback_data="noop"),
             InlineKeyboardButton("+5", callback_data="set:min+")]])
        text = (f"<b>Ayarlar</b>\nSessiz saatler: {st['sessiz_baslangic']}–{st['sessiz_bitis']} "
                f"(bu aralıkta bulunanlar sabah gelir)\nGünlük özet: {st['gunluk_ozet_saati']}")
        return text, kb

    @guard
    async def settings_cmd(self, update, ctx):
        text, kb = self.settings_view()
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    @guard
    async def pause(self, update, ctx):
        self.app.paused = True
        await self.reply(update, "⏸ Otomatik tarama duraklatıldı. /resume ile devam ettir.")

    @guard
    async def resume(self, update, ctx):
        self.app.paused = False
        await self.reply(update, "▶️ Otomatik tarama devam ediyor.")

    @guard
    async def scan_now(self, update, ctx):
        await self.reply(update, "🔎 Tarama başladı…")
        rep = await self.run_scan(force=True)
        await self.reply(update, self.scan_summary(rep))

    @guard
    async def natural(self, update, ctx):
        text = update.effective_message.text or ""
        f = parse_query(text)
        with self.app.Session() as s:
            if "kac" in text.lower().replace("ç", "c"):
                rows = find_jobs(s, JobFilter(since_days=f.since_days or 7, limit=500, city=f.city, keywords=f.keywords))
                await self.reply(update, f"Bu kriterlere uyan başvurulabilir ilan sayısı: <b>{len(rows)}</b>")
                return
            rows = find_jobs(s, f)
            await self.send_list(update, "Sonuçlar", rows)

    # ---------------- butonlar
    async def on_button(self, update: Update, ctx):
        q = update.callback_query
        if not self.authorized(update):
            await q.answer()
            return
        data = q.data or ""
        with self.app.Session() as s:
            if data.startswith(("fav:", "ni:", "app:", "sim:")):
                kind, jid = data.split(":")
                job = s.get(Job, int(jid))
                if not job:
                    await q.answer("İlan bulunamadı")
                    return
                if kind in ("fav", "ni"):
                    fb = s.get(Feedback, job.id) or Feedback(job_id=job.id)
                    if kind == "fav":
                        fb.favorite = not fb.favorite
                        msg = "⭐ Favorilere eklendi" if fb.favorite else "Favorilerden çıkarıldı"
                    else:
                        fb.not_interested = True
                        msg = "Bu ilan bir daha gönderilmeyecek"
                    fb.updated = datetime.now()
                    s.merge(fb)
                    s.commit()
                    await q.answer(msg)
                elif kind == "app":
                    a = s.scalar(select(Application).where(Application.job_id == job.id))
                    if not a:
                        a = Application(job_id=job.id, applied_at=datetime.now(), state="Başvuru yapıldı",
                                        reminder_at=datetime.now() + timedelta(days=5))
                        s.add(a)
                        s.commit()
                    await q.answer("✅ Başvuru kaydedildi; 5 gün sonra hatırlatacağım")
                else:
                    await q.answer()
                    words = [w for w in job.title.split() if len(w) > 3][:2]
                    rows = [r for r in find_jobs(s, JobFilter(keywords=words, limit=8)) if r[0].id != job.id]
                    await self.send_list(update, f"Benzer ilanlar: {job.title}", rows)
            elif data.startswith("st:"):
                _, aid, idx = data.split(":")
                a = s.get(Application, int(aid))
                if a:
                    a.state = APPLICATION_STATES[int(idx)]
                    a.last_contact = datetime.now()
                    s.commit()
                await q.answer("Durum güncellendi")
                await q.edit_message_text(f"Durum: {APPLICATION_STATES[int(idx)]}")
            elif data.startswith("set:"):
                st = self.app.settings()["bildirim"]
                key = data[4:]
                if key in ("aktif", "anlik"):
                    set_setting(s, f"bildirim.{key}", not st[key])
                elif key in ("min-", "min+"):
                    set_setting(s, "bildirim.min_puan", max(0, min(100, st["min_puan"] + (5 if key == "min+" else -5))))
                s.commit()
                await q.answer("Kaydedildi")
                text, kb = self.settings_view()
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await q.answer()

    # ---------------- zamanlanmış işler
    async def run_scan(self, force: bool = False):
        def work():
            cols = self.app.due_collectors(force=force)
            with self.app.Session() as s:
                return scan(s, cols, self.app.profile, self.app.settings(), self.app.analyzer())
        return await asyncio.to_thread(work)

    @staticmethod
    def scan_summary(rep) -> str:
        t = (f"Tarama bitti: {rep.sources_ok} kaynak çalıştı, {rep.sources_failed} hatalı.\n"
             f"Alınan: {rep.fetched} · Yeni: {rep.new_jobs} · Güncellenen: {rep.updated_jobs} · Bildirime eklenen: {rep.queued}")
        if rep.errors:
            t += "\n" + "\n".join(f"⚠️ {E(e[:200])}" for e in rep.errors)
        return t

    async def job_scan(self, ctx):
        if self.app.paused:
            return
        try:
            rep = await self.run_scan()
            log.info("Tarama: %s", self.scan_summary(rep).replace("\n", " | "))
        except Exception:  # noqa: BLE001
            log.exception("Tarama işi hata verdi")

    async def job_notify(self, ctx):
        st = self.app.settings()
        if not st["bildirim"]["aktif"] or in_quiet_hours(st, datetime.now(TZ)):
            return
        with self.app.Session() as s:
            pending = s.scalars(select(Notification).where(Notification.sent_at.is_(None)).order_by(Notification.id).limit(10)).all()
            for n in pending:
                job = s.get(Job, n.job_id)
                fb = s.get(Feedback, n.job_id)
                if not job or not job.latest_eval or (fb and fb.not_interested):
                    n.sent_at = datetime.now()
                    continue
                for uid in self.allowed:
                    m = await ctx.bot.send_message(uid, card(job, job.latest_eval, n.reason), parse_mode=ParseMode.HTML,
                                                   reply_markup=keyboard(job), disable_web_page_preview=True)
                    n.telegram_message_id = m.message_id
                n.sent_at = datetime.now()
                s.commit()

    async def job_daily(self, ctx):
        with self.app.Session() as s:
            st = stats(s)
            top = find_jobs(s, JobFilter(since_days=1, limit=5))
        txt = (f"📊 <b>AI CAREER RADAR | GÜNLÜK RAPOR</b>\nYeni ilan: {st['bugun']}\nBaşvurulabilir (toplam): {st['basvurulabilir']}\n"
               f"Çok yüksek uyum: {st['cok_yuksek']}\nKariyer fırsatı: {st['kariyer']}\nFavori: {st['favori']}\n")
        if top:
            txt += "\n<b>Bugünün en iyileri</b>\n" + "\n".join(short(j, e) for j, e in top)
        for uid in self.allowed:
            await ctx.bot.send_message(uid, txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    async def job_reminders(self, ctx):
        with self.app.Session() as s:
            due = s.scalars(select(Application).where(Application.reminder_sent.is_(False),
                                                      Application.reminder_at <= datetime.now())).all()
            for a in due:
                days = (datetime.now() - (a.applied_at or datetime.now())).days
                for uid in self.allowed:
                    await ctx.bot.send_message(uid, f"⏰ {E(a.job.company or '')} – {E(a.job.title)} ilanına {days} gün önce "
                                                    f"başvurdun. Durumu güncellemek ister misin? /durum_{a.id}",
                                               parse_mode=ParseMode.HTML)
                a.reminder_sent = True
            s.commit()

    async def job_backup(self, ctx):
        await asyncio.to_thread(self.app.backup)

    # ---------------- kurulum
    def build(self) -> TgApp:
        if not self.app.secrets.telegram_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN .env dosyasında yok.")
        if not self.allowed:
            raise SystemExit("TELEGRAM_ALLOWED_USER_IDS .env dosyasında yok (güvenlik için zorunlu).")
        tg = ApplicationBuilder().token(self.app.secrets.telegram_token).build()
        for name, fn in [("start", self.start), ("help", self.help), ("profile", self.profile), ("jobs", self.jobs),
                         ("best", self.best), ("today", self.today), ("week", self.week), ("search", self.search),
                         ("career", self.career), ("favorites", self.favorites), ("applications", self.applications),
                         ("stats", self.stats_cmd), ("sources", self.sources), ("settings", self.settings_cmd),
                         ("pause", self.pause), ("resume", self.resume), ("scan", self.scan_now)]:
            tg.add_handler(CommandHandler(name, fn))
        tg.add_handler(MessageHandler(filters.Regex(r"^/ilan_\d+"), self.show_job))
        tg.add_handler(MessageHandler(filters.Regex(r"^/durum_\d+"), self.app_state))
        tg.add_handler(CallbackQueryHandler(self.on_button))
        tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.natural))
        st = self.app.settings()
        jq = tg.job_queue
        jq.run_repeating(self.job_scan, interval=60 * 5, first=10)        # kaynak bazlı sıklık due_collectors'da
        jq.run_repeating(self.job_notify, interval=60, first=30)
        jq.run_repeating(self.job_reminders, interval=3600, first=120)
        jq.run_daily(self.job_daily, time=time.fromisoformat(st["bildirim"]["gunluk_ozet_saati"]).replace(tzinfo=TZ))
        jq.run_daily(self.job_backup, time=time(3, 30, tzinfo=TZ))
        return tg

"""GitHub Actions çalıştırıcısı: her çalıştırmada bir "tur" yapar ve kapanır.

Bir tur:
 1. Şifreli durum veritabanını açar (state/radar.db.enc → data/radar.db).
 2. Telegram'dan gelen mesajları/buton tıklamalarını işler (komutlara cevap verir).
 3. Zamanı gelen kaynakları tarar, ilanları değerlendirir.
 4. Uygun yeni ilanları Telegram'a gönderir (sessiz saatlere uyarak).
 5. Günlük/haftalık raporu ve başvuru hatırlatmalarını gönderir.
 6. Veritabanını tekrar şifreleyip kaydeder.
Bilgisayarın açık olması gerekmez; her şey GitHub sunucularında çalışır.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import DATA_DIR, ROOT, load_secrets
from .db import (APPLICATION_STATES, Application, Evaluation, Feedback, Job, JobLink, Notification, Setting, Source,
                 set_setting)
from .formatting import E, card, fmt_date, in_quiet_hours, keyboard_rows, short
from .queries import JobFilter, find_jobs, latest_evals, parse_query, stats
from .telegram_api import Telegram, TelegramError

log = logging.getLogger("radar.runner")
TZ = ZoneInfo("Europe/Istanbul")
STATE_ENC = ROOT / "state" / "radar.db.enc"
DB = DATA_DIR / "radar.db"

COMMANDS = [("jobs", "Uygun özel sektör ilanları"), ("kamu", "Kamu ilanları (KPSS, POMEM, MSB, belediye...)"), ("best", "En yüksek puanlılar"), ("today", "Bugün bulunanlar"),
            ("week", "Son 7 gün"), ("career", "Kariyer fırsatları"), ("search", "Ara: /search Gaziantep bakım"),
            ("favorites", "Favoriler"), ("applications", "Başvurularım"), ("stats", "İstatistikler"),
            ("sources", "Kaynak durumu"), ("settings", "Ayarlar"), ("profile", "Profilim"), ("pause", "Duraklat"),
            ("resume", "Devam et"), ("help", "Yardım")]


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def getset(s, key, default=None):
    row = s.get(Setting, key)
    return row.value if row else default


# ---------------------------------------------------------------- durum dosyası
def open_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_ENC.exists():
        from .secure import decrypt_file
        decrypt_file(str(STATE_ENC), str(DB))
        log.info("Durum veritabanı açıldı.")
    else:
        log.info("Durum dosyası yok: ilk çalıştırma, yeni veritabanı oluşturulacak.")


def save_state(engine) -> None:
    from .secure import encrypt_file
    engine.dispose()
    import sqlite3
    con = sqlite3.connect(DB)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("VACUUM")
    con.close()
    STATE_ENC.parent.mkdir(parents=True, exist_ok=True)
    encrypt_file(str(DB), str(STATE_ENC))
    log.info("Durum şifrelenip kaydedildi (%d KB).", STATE_ENC.stat().st_size // 1024)


def prune(s, days: int = 75) -> None:
    """Eski ilanların uzun metinlerini silerek veritabanını küçük tutar (kayıtlar ve geçmiş kalır)."""
    cut = now() - timedelta(days=days)
    for j in s.scalars(select(Job).where(Job.first_seen < cut, Job.description != "")):
        j.description = ""
        if j.status == "aktif":
            j.status = "süresi_geçti"
    for l in s.scalars(select(JobLink).join(Job).where(Job.first_seen < cut)):
        l.raw = {}
    old = now() - timedelta(days=30)
    for j in s.scalars(select(Job).where(Job.first_seen < old)):
        for ev in j.evaluations[:-1]:      # yalnızca son değerlendirme kalsın
            s.delete(ev)


# ---------------------------------------------------------------- Telegram etkileşimi
class Runner:
    def __init__(self, app, tg: Telegram):
        self.app, self.tg = app, tg
        self.chat = None

    # sahip sohbeti: TELEGRAM_CHAT_ID secret'ı > kayıtlı sohbet > ilk /start yazan
    def owner(self, s):
        return self.app.secrets.telegram_chat_id or getset(s, "tg.chat_id")

    def send(self, text, buttons=None):
        if self.chat:
            return self.tg.send(self.chat, text, buttons)

    def handle_updates(self, s) -> int:
        offset = getset(s, "tg.offset")
        try:
            ups = self.tg.updates(offset)
        except TelegramError as e:
            log.error("%s", e)
            return 0
        n = 0
        for u in ups:
            set_setting(s, "tg.offset", u["update_id"] + 1)
            msg = u.get("message")
            cb = u.get("callback_query")
            frm = (msg or cb or {}).get("from", {})
            chat_id = str((msg or (cb or {}).get("message") or {}).get("chat", {}).get("id", ""))
            owner = self.owner(s)
            if not owner and msg and (msg.get("text") or "").startswith("/start"):
                set_setting(s, "tg.chat_id", chat_id)
                owner = chat_id
                log.info("Telegram sohbeti bağlandı.")
            if not owner or chat_id != str(owner) or frm.get("is_bot"):
                continue  # yetkisiz kullanıcı: sessizce yok sayılır
            self.chat = owner
            try:
                if cb:
                    self.on_button(s, cb)
                elif msg and msg.get("text"):
                    self.on_text(s, msg["text"].strip())
                n += 1
            except Exception:  # noqa: BLE001 - bir mesajdaki hata turu bozmasın
                log.error("Mesaj işlenemedi:\n%s", traceback.format_exc())
                self.send("⚠️ Bu isteği işlerken bir hata oldu; kayıtlara yazıldı.")
            s.commit()
        return n

    # ---- komutlar
    def list_msg(self, s, title, f: JobFilter):
        rows = find_jobs(s, f, now=now())
        if not rows:
            return f"<b>{E(title)}</b>\n\nKayıt bulunamadı."
        return f"<b>{E(title)}</b>\n\n" + "\n".join(short(j, e) for j, e in rows) + "\n\nAyrıntı için /ilan_N bağlantısına dokun."

    def on_text(self, s, text: str):
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        args = text.split()[1:]
        st = self.app.settings(s)
        if cmd == "/start":
            self.send(self.welcome(s))
            set_setting(s, "tg.welcomed", True)
        elif cmd == "/help":
            self.send("<b>Komutlar</b>\n" + "\n".join(f"/{c} – {d}" for c, d in COMMANDS) +
                      "\n\nDoğal dilde de yazabilirsin: “Gaziantep'te deneyim istemeyen işler”, “son 3 günde otomasyon ilanları”."
                      "\n\n<i>Not: Bot GitHub üzerinde dönemsel çalıştığı için cevaplar birkaç dakika gecikebilir.</i>")
        elif cmd == "/jobs":
            self.send(self.list_msg(s, "Uygun özel sektör ilanları", JobFilter(min_score=st["bildirim"]["min_puan"], limit=15, sector="ozel")))
        elif cmd == "/kamu":
            self.send(self.list_msg(s, "Kamu ilanları", JobFilter(min_score=st["bildirim"].get("kamu_min_puan", 55), limit=15,
                                                                  sector="kamu", sort="date")))
        elif cmd == "/best":
            self.send(self.list_msg(s, "En yüksek puanlı ilanlar", JobFilter(limit=10)))
        elif cmd == "/today":
            self.send(self.list_msg(s, "Bugün bulunanlar", JobFilter(since_days=1, sort="date", limit=20)))
        elif cmd == "/week":
            self.send(self.list_msg(s, "Son 7 gün", JobFilter(since_days=7, limit=20)))
        elif cmd == "/career":
            rows = find_jobs(s, JobFilter(career=True, limit=25), now=now())
            groups: dict[str, list] = {}
            for j, e in rows:
                groups.setdefault(e.career_track, []).append(short(j, e))
            self.send("<b>Kariyer Fırsatları</b>\n" + ("\n".join(f"\n<b>{E(k)}</b>\n" + "\n".join(v) for k, v in groups.items())
                                                     or "\nHenüz kayıt yok."))
        elif cmd == "/search":
            q = " ".join(args)
            f = parse_query(q)
            if not f.keywords and not f.city:
                f.keywords = args
            self.send(self.list_msg(s, f"Arama: {q}", f) if q else "Kullanım: /search Gaziantep bakım")
        elif cmd.startswith("/ilan_"):
            j = s.get(Job, int(re.sub(r"\D", "", cmd) or 0))
            if j and j.latest_eval:
                self.send(card(j, j.latest_eval), keyboard_rows(j))
            else:
                self.send("İlan bulunamadı.")
        elif cmd == "/favorites":
            favs = [s.get(Job, f.job_id) for f in s.scalars(select(Feedback).where(Feedback.favorite.is_(True)))]
            self.send("<b>Favoriler</b>\n\n" + ("\n".join(short(j, j.latest_eval) for j in favs if j and j.latest_eval)
                                                or "Henüz favori yok."))
        elif cmd == "/applications":
            apps = s.scalars(select(Application).order_by(Application.id.desc())).all()
            self.send("<b>Başvurularım</b>\n" + ("\n".join(
                f"• <b>{E(a.job.title)}</b> — {E(a.job.company or '?')}\n  {E(a.state)} · {fmt_date(a.applied_at)} /durum_{a.id}"
                for a in apps) or "\nHenüz yok. Bildirimdeki “Başvuru Yapıldı” butonuyla eklenir."))
        elif cmd.startswith("/durum_"):
            aid = int(re.sub(r"\D", "", cmd) or 0)
            self.send("Yeni durumu seç:", [[{"text": stt, "callback_data": f"st:{aid}:{i}"}] for i, stt in enumerate(APPLICATION_STATES)])
        elif cmd == "/stats":
            self.send(self.stats_text(s))
        elif cmd == "/sources":
            self.send(self.sources_text(s))
        elif cmd == "/settings":
            self.send(*self.settings_view(s))
        elif cmd == "/profile":
            p = self.app.profile
            dip = "\n".join(f"• {d['program']} – {d['seviye'].replace('_', ' ')} ({d['durum']})" for d in p.diplomas + p.studying)
            self.send(f"<b>Profil</b>\n{E(dip)}\n<b>Yetkinlikler:</b> {E(', '.join(x.name for x in p.skills))}\n"
                      f"<b>Askerlik:</b> {E(str(p.military.get('durum')))} ({E(str(p.military.get('tecil_bitis')))}) · "
                      f"<b>Ehliyet:</b> {E(', '.join(p.licenses))}")
        elif cmd == "/pause":
            set_setting(s, "radar.paused", True)
            self.send("⏸ Otomatik tarama duraklatıldı. /resume ile devam.")
        elif cmd == "/resume":
            set_setting(s, "radar.paused", False)
            self.send("▶️ Tarama devam ediyor.")
        elif cmd:
            self.send("Bu komutu tanımıyorum. /help yaz.")
        else:
            f = parse_query(text)
            low = text.lower()
            if "kaç" in low or "kac" in low:
                rows = find_jobs(s, JobFilter(since_days=f.since_days or 7, limit=999, city=f.city, keywords=f.keywords), now=now())
                self.send(f"Bu kriterlere uyan başvurulabilir ilan sayısı: <b>{len(rows)}</b>")
            else:
                self.send(self.list_msg(s, "Sonuçlar", f))

    def settings_view(self, s=None):
        b = self.app.settings(s)["bildirim"]
        kb = [[{"text": f"Bildirim: {'AÇIK' if b['aktif'] else 'KAPALI'}", "callback_data": "set:aktif"}],
              [{"text": f"Gece de gönder: {'AÇIK' if b['anlik'] else 'KAPALI'}", "callback_data": "set:anlik"}],
              [{"text": "Min puan −5", "callback_data": "set:min-"}, {"text": f"Min: {b['min_puan']}", "callback_data": "noop"},
               {"text": "+5", "callback_data": "set:min+"}]]
        return (f"<b>Ayarlar</b>\nSessiz saatler: {b['sessiz_baslangic']}–{b['sessiz_bitis']} (gece bulunanlar sabah gelir)\n"
                f"Günlük özet: {b['gunluk_ozet_saati']}"), kb

    def on_button(self, s, cb):
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        reply = ""
        if data.startswith(("fav:", "ni:", "app:", "sim:")):
            kind, jid = data.split(":")
            job = s.get(Job, int(jid))
            if not job:
                return self.tg.answer_callback(cb["id"], "İlan bulunamadı")
            if kind in ("fav", "ni"):
                fb = s.get(Feedback, job.id) or Feedback(job_id=job.id)
                if kind == "fav":
                    fb.favorite = not fb.favorite
                    reply = "⭐ Favorilere eklendi" if fb.favorite else "Favorilerden çıkarıldı"
                else:
                    fb.not_interested = True
                    reply = "🙅 Bu ilan bir daha gönderilmeyecek"
                fb.updated = now()
                s.merge(fb)
            elif kind == "app":
                if not s.scalar(select(Application).where(Application.job_id == job.id)):
                    s.add(Application(job_id=job.id, applied_at=now(), state="Başvuru yapıldı",
                                      reminder_at=now() + timedelta(days=5)))
                reply = "✅ Başvuru kaydedildi; 5 gün sonra hatırlatacağım"
            else:
                words = [w for w in job.title.split() if len(w) > 3][:2]
                rows = [r for r in find_jobs(s, JobFilter(keywords=words, limit=8), now=now()) if r[0].id != job.id]
                self.send(f"<b>Benzer ilanlar: {E(job.title)}</b>\n\n" + ("\n".join(short(j, e) for j, e in rows) or "Bulunamadı."))
            if reply:
                self.send(f"{reply}: <i>{E(job.title)}</i>")
        elif data.startswith("st:"):
            _, aid, idx = data.split(":")
            a = s.get(Application, int(aid))
            if a:
                a.state = APPLICATION_STATES[int(idx)]
                a.last_contact = now()
                reply = f"Durum: {a.state}"
                self.send(f"📌 {E(a.job.title)} → <b>{E(a.state)}</b>")
        elif data.startswith("set:"):
            b = self.app.settings(s)["bildirim"]
            key = data[4:]
            if key in ("aktif", "anlik"):
                set_setting(s, f"bildirim.{key}", not b[key])
            elif key in ("min-", "min+"):
                set_setting(s, "bildirim.min_puan", max(0, min(100, b["min_puan"] + (5 if key == "min+" else -5))))
            s.flush()
            reply = "Kaydedildi"
            text, kb = self.settings_view(s)
            self.tg.edit_buttons(msg.get("chat", {}).get("id"), msg.get("message_id"), kb)
            self.send(text)
        self.tg.answer_callback(cb["id"], reply)

    # ---- metinler
    def stats_text(self, s) -> str:
        st = stats(s, now=now())
        cities = ", ".join(f"{c} ({n})" for c, n in st["sehirler"]) or "-"
        return (f"<b>İstatistikler</b>\nToplam ilan: {st['toplam']} · Bugün: {st['bugun']} · 7 gün: {st['hafta']}\n"
                f"Başvurulabilir: {st['basvurulabilir']} · Uygun (70+): {st['uygun']} · Çok yüksek (80+): {st['cok_yuksek']}\n"
                f"Kariyer fırsatı: {st['kariyer']} · Favori: {st['favori']} · Başvuru: {st['basvuru']} · Görüşme: {st['gorusme']}\n"
                f"En çok ilan: {E(cities)}")

    def sources_text(self, s) -> str:
        out = []
        for src in s.scalars(select(Source)):
            icon = {"çalışıyor": "🟢", "hata": "🔴", "yapılandırılmadı": "🟡", "pasif": "⚪"}.get(src.status, "⚪")
            line = f"{icon} <b>{E(src.name)}</b> – {E(src.status)}"
            if src.last_success:
                line += f"\n   Son başarılı: {src.last_success:%d.%m %H:%M} · Son bulunan: {src.last_count} · Toplam: {src.total_found}"
            if src.last_error:
                line += f"\n   ⚠️ {E(src.last_error[:180])}"
            out.append(line)
        return "<b>Kaynaklar</b>\n" + "\n".join(out) + "\n\n<i>Yalnızca bu kaynaklar taranır; tüm internet taranmaz.</i>"

    def welcome(self, s) -> str:
        return ("👋 <b>AI Career Radar bağlandı!</b>\n\nArtık GitHub sunucularında kendi kendine çalışıyorum. "
                "Gündüz her 30 dakikada bir özel sektör ilanlarını, 2 saatte bir kamu ilanlarını (KPSS, POMEM, MSB, MİT, "
                "belediye, üniversite, İŞKUR işçi alımları) tarayıp profiline uyanları buraya göndereceğim. "
                "Gece bulunanlar sabah 08:00'de gelir.\n\n" + self.sources_text(s) + "\n\nKomutlar için /help")

    # ---- bildirim / raporlar
    def send_notifications(self, s) -> int:
        st = self.app.settings(s)
        if not st["bildirim"]["aktif"] or in_quiet_hours(st, now()):
            return 0
        sent = 0
        pending = s.scalars(select(Notification).where(Notification.sent_at.is_(None)).order_by(Notification.id)).all()
        pending.sort(key=lambda n: -(s.get(Job, n.job_id).latest_eval.score if s.get(Job, n.job_id).latest_eval else 0))
        for n in pending[:12]:
            job = s.get(Job, n.job_id)
            fb = s.get(Feedback, n.job_id)
            if job and job.latest_eval and not (fb and fb.not_interested) and job.status == "aktif":
                n.telegram_message_id = self.send(card(job, job.latest_eval, n.reason), keyboard_rows(job))
                sent += 1
            n.sent_at = now()
            s.commit()
        if len(pending) > 12:
            self.send(f"…ve {len(pending) - 12} ilan daha sırada; bir sonraki turda gelecek. Hepsi için /jobs")
        return sent

    def daily(self, s) -> None:
        st = self.app.settings(s)
        hh, mm = (int(x) for x in st["bildirim"]["gunluk_ozet_saati"].split(":"))
        t = now()
        today = t.date().isoformat()
        if getset(s, "tg.last_daily") == today or (t.hour, t.minute) < (hh, mm):
            return
        set_setting(s, "tg.last_daily", today)
        stt = stats(s, now=t)
        top = find_jobs(s, JobFilter(since_days=1, limit=5), now=t)
        bad = [src.name for src in s.scalars(select(Source)) if src.status == "hata"]
        txt = (f"📊 <b>AI CAREER RADAR | GÜNLÜK RAPOR</b>\nBugün bulunan ilan: {stt['bugun']}\n"
               f"Başvurulabilir (aktif): {stt['basvurulabilir']} · Çok yüksek uyum: {stt['cok_yuksek']}\n"
               f"Kariyer fırsatı: {stt['kariyer']} · Favori: {stt['favori']} · Başvuru: {stt['basvuru']}")
        if top:
            txt += "\n\n<b>Bugünün en iyileri</b>\n" + "\n".join(short(j, e) for j, e in top)
        if bad:
            txt += f"\n\n⚠️ Hata veren kaynak: {E(', '.join(bad))} (/sources)"
        if t.weekday() == 6:
            txt += "\n\n" + self.weekly(s)
        self.send(txt)

    def weekly(self, s) -> str:
        t = now()
        rows = [(j, e) for j, e in latest_evals(s) if j.first_seen >= t - timedelta(days=7)]
        cities, comps, missing = {}, {}, {}
        for j, e in rows:
            if j.city:
                cities[j.city] = cities.get(j.city, 0) + 1
            if e.gate != "Başvurulamaz" and j.company:
                comps[j.company] = max(comps.get(j.company, 0), e.score)
            for m in e.explanation.get("eksik_beceriler", []):
                missing[m] = missing.get(m, 0) + 1
        top = lambda d, n=3: ", ".join(f"{k} ({v:g})" for k, v in sorted(d.items(), key=lambda x: -x[1])[:n]) or "-"
        apps = s.query(Application).count()
        return (f"🗓 <b>HAFTALIK KARİYER RAPORU</b>\nBu hafta ilan: {len(rows)}\nEn çok ilan açan şehirler: {E(top(cities))}\n"
                f"Sana en uygun şirketler: {E(top(comps))}\nEn çok istenen ama sende olmayan beceriler: {E(top(missing))}\n"
                f"Toplam başvuru: {apps}")

    def reminders(self, s) -> None:
        for a in s.scalars(select(Application).where(Application.reminder_sent.is_(False), Application.reminder_at <= now())):
            days = (now() - (a.applied_at or now())).days
            self.send(f"⏰ {E(a.job.company or '')} – {E(a.job.title)} ilanına {days} gün önce başvurdun. "
                      f"Durumu güncellemek ister misin? /durum_{a.id}")
            a.reminder_sent = True


def main() -> int:
    from .app import App, setup_logging
    setup_logging()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    secrets = load_secrets()
    if not secrets.telegram_token:
        log.error("TELEGRAM_BOT_TOKEN secret'ı eksik. Repo → Settings → Secrets and variables → Actions.")
        return 1
    if secrets.radar_key:
        os.environ["RADAR_KEY"] = secrets.radar_key
    open_state()
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{DB}")
    app = App(secrets)
    tg = Telegram(secrets.telegram_token)
    r = Runner(app, tg)
    exit_code = 0
    try:
        def talk(s, final: bool = False):
            """Telegram'la konuş: mesajları cevapla, hoş geldin ve bekleyen ilanları gönder."""
            n = r.handle_updates(s)
            r.chat = r.owner(s)
            sent = 0
            if r.chat:
                if not getset(s, "tg.welcomed"):
                    r.send(r.welcome(s))
                    set_setting(s, "tg.welcomed", True)
                sent = r.send_notifications(s)
                if final:
                    r.daily(s)
                    r.reminders(s)
            s.commit()
            return n, sent

        with app.Session() as s:
            if not getset(s, "tg.commands_set"):
                tg.set_commands(COMMANDS)
                set_setting(s, "tg.commands_set", True)
                s.commit()
            handled, sent0 = talk(s)            # önce hızlı cevap: tarama beklenmez
            if not r.chat:
                log.warning("Telegram sohbeti henüz bağlı değil: Telegram'da botuna /start yaz.")
            paused = bool(getset(s, "radar.paused", False))
        if not paused:
            from .pipeline import scan
            budget = float(os.environ.get("RADAR_SCAN_MINUTES", "14"))
            with app.Session() as s:
                rep = scan(s, app.due_collectors(), app.profile, app.settings(), app.analyzer(), today=now(),
                           deadline=datetime.now() + timedelta(minutes=budget))
            log.info("Tarama: kaynak ok=%d hata=%d alınan=%d yeni=%d sıraya=%d", rep.sources_ok, rep.sources_failed,
                     rep.fetched, rep.new_jobs, rep.queued)
            for e in rep.errors or []:
                log.warning("Kaynak: %s", e)
        with app.Session() as s:
            handled2, sent = talk(s, final=True)    # tarama sırasında gelen mesajlar + yeni ilanlar
            log.info("Telegram: %d mesaj işlendi, %d ilan gönderildi.", handled + handled2, sent0 + sent)
            prune(s)
            s.commit()
    except TelegramError as e:
        log.error("%s", e)
        exit_code = 1
    except Exception:  # noqa: BLE001
        log.error("Tur hata verdi:\n%s", traceback.format_exc())
        exit_code = 1
        try:
            if r.chat:
                r.send("⚠️ AI Career Radar bu turda hata verdi; ayrıntı GitHub Actions kayıtlarında. Sistem bir sonraki turda tekrar dener.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        save_state(app.engine)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

"""Telegram bildirim biçimi, yetkilendirme, sessiz saatler, doğal dil sorguları, başvuru takibi."""
from datetime import datetime
from types import SimpleNamespace

from radar.bot import RadarBot, card, in_quiet_hours, keyboard
from radar.collectors.base import RawPosting
from radar.config import Secrets
from radar.db import Application, Feedback, Job, Source
from radar.pipeline import scan
from radar.queries import JobFilter, find_jobs, parse_query, stats

from .conftest import TODAY
from .test_pipeline import GOOD_DESC, FakeCollector


def fill(session, profile, settings):
    session.add(Source(name="a", kind="t"))
    session.commit()
    items = [
        RawPosting("kariyer.net", "1", "https://www.kariyer.net/is-ilani/x-1", "Elektrik Otomasyon Teknisyeni", "Özmermer",
                   "Gaziantep", description=GOOD_DESC + "\n• Deneyimli / deneyimsiz\n• AutoCAD bilen\n• Personel servisi"),
        RawPosting("kariyer.net", "2", "https://www.kariyer.net/is-ilani/x-2", "Mekatronik Teknikeri", "ASELSAN Gaziantep",
                   "Gaziantep", description="• Meslek Yüksekokullarının Mekatronik bölümünden mezun\n• En az 2 yıl deneyimli"),
        RawPosting("linkedin", "3", "https://www.linkedin.com/jobs/view/3", "Otomasyon Teknikeri", "ABC", "Çanakkale",
                   description="• Meslek Yüksekokullarının Mekatronik bölümünden mezun"),
    ]
    scan(session, [FakeCollector("a", items)], profile, settings, today=TODAY)


def test_card_format_and_buttons(session, profile, settings):
    fill(session, profile, settings)
    job = session.query(Job).filter(Job.title.like("Elektrik%")).one()
    txt = card(job, job.latest_eval)
    for part in ("YENİ İŞ FIRSATI", "Pozisyon:", "Şirket:", "Lokasyon:", "Uygunluk:", "NEDEN SANA UYGUN?", "Son Başvuru:"):
        assert part in txt
    assert "AutoCAD" in txt and "servis" in txt
    kb = keyboard(job).inline_keyboard
    labels = [b.text for row in kb for b in row]
    assert len(labels) == 6 and any("İlgilenmiyorum" in x for x in labels)
    assert kb[0][0].url.startswith("https://www.kariyer.net")


def test_html_escaping_against_injection(session, profile, settings):
    session.add(Source(name="a", kind="t"))
    session.commit()
    p = RawPosting("x", "9", "https://x.test", "Teknisyen <b>ACİL</b>", "<script>", "Gaziantep", description=GOOD_DESC)
    scan(session, [FakeCollector("a", [p])], profile, settings, today=TODAY)
    txt = card(session.query(Job).one(), session.query(Job).one().latest_eval)
    assert "&lt;script&gt;" in txt and "<script>" not in txt


def test_authorization_whitelist():
    app = SimpleNamespace(secrets=Secrets(allowed_user_ids=[111]))
    bot = RadarBot(app)
    assert bot.authorized(SimpleNamespace(effective_user=SimpleNamespace(id=111)))
    assert not bot.authorized(SimpleNamespace(effective_user=SimpleNamespace(id=222)))
    assert not bot.authorized(SimpleNamespace(effective_user=None))


def test_quiet_hours(settings):
    assert in_quiet_hours(settings, datetime(2026, 11, 21, 3, 0))
    assert not in_quiet_hours(settings, datetime(2026, 11, 21, 9, 0))
    settings["bildirim"]["anlik"] = True
    assert not in_quiet_hours(settings, datetime(2026, 11, 21, 3, 0))


def test_natural_language_queries():
    f = parse_query("Gaziantep'teki bana uygun işleri bul.")
    assert f.city == "Gaziantep"
    f = parse_query("Son 3 günde açılan otomasyon ilanlarını göster.")
    assert f.since_days == 3 and "otomasyon" in f.keywords
    assert parse_query("En yüksek puanlı 10 ilanı getir.").limit == 10
    assert parse_query("Sadece deneyim istemeyen işleri göster.").no_experience
    f = parse_query("Savunma sanayisindeki tekniker ilanlarını bul.")
    assert "aselsan" in f.keywords and "tekniker" in f.keywords
    assert "autocad" in parse_query("AutoCAD bilen eleman arayan firmaları göster.").keywords
    f = parse_query("Çanakkale'de mekatronik işi var mı?")
    assert f.city == "Çanakkale" and "mekatronik" in f.keywords


def test_find_jobs_filters(session, profile, settings):
    fill(session, profile, settings)
    titles = lambda rows: {j.title for j, _ in rows}
    assert titles(find_jobs(session, parse_query("Gaziantep işleri"), now=TODAY)) == {"Elektrik Otomasyon Teknisyeni"}
    assert "Mekatronik Teknikeri" not in titles(find_jobs(session, JobFilter(), now=TODAY))   # 2 yıl zorunlu deneyim
    assert titles(find_jobs(session, JobFilter(career=True), now=TODAY)) == {"Mekatronik Teknikeri"}
    assert titles(find_jobs(session, parse_query("deneyim istemeyen"), now=TODAY)) == {"Elektrik Otomasyon Teknisyeni"}
    j = session.query(Job).filter(Job.title.like("Elektrik%")).one()
    session.add(Feedback(job_id=j.id, not_interested=True))
    session.commit()
    assert "Elektrik Otomasyon Teknisyeni" not in titles(find_jobs(session, JobFilter(), now=TODAY))


def test_application_tracking_and_stats(session, profile, settings):
    fill(session, profile, settings)
    j = session.query(Job).first()
    session.add(Application(job_id=j.id, applied_at=TODAY, state="Görüşme bekleniyor"))
    session.commit()
    st = stats(session, now=TODAY)
    assert st["toplam"] == 3 and st["basvuru"] == 1 and st["gorusme"] == 1 and st["kariyer"] >= 1

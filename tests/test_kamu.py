"""Kamu ilanı izleme ve değerlendirme testleri."""
from datetime import datetime

import httpx

from radar.collectors.announcements import AnnouncementCollector
from radar.collectors.base import PoliteClient
from radar.config import Secrets
from radar.extract import extract_fields
from radar.kamu import evaluate_kamu, find_deadline

TODAY = datetime(2026, 11, 22, 12, 0)

ISCI = """Gaziantep Büyükşehir Belediyesi 12 Sürekli İşçi Alacak. Son başvuru tarihi 5 Aralık 2026
Elektrikçi (Bakım Onarım) 4 kişi: Meslek Yüksekokullarının Elektrik, Elektronik Teknolojisi veya Mekatronik programlarından mezun olmak.
Askerlik hizmetini yapmış, muaf veya tecilli olmak.
Başvurular İŞKUR üzerinden alınacaktır."""
LISANS = """Sayıştay 16 sözleşmeli personel alacak. Son başvuru tarihi 21 Aralık 2026
Adayların en az dört yıllık lisans eğitimi veren fakültelerin Bilgisayar Mühendisliği bölümünden mezun olması gerekmektedir."""
POMEM = """34. Dönem POMEM Giriş Sınavı. Lisans mezunu 6.800 erkek ve önlisans mezunu 1.700 erkek aday alınacaktır.
Önlisans mezunları için 2026 KPSS P93 puan türünden en az 65 puan almış olmak.
Başvuru tarihi itibarıyla 28 yaşını doldurmamış olmak. Son başvuru tarihi 15.01.2027"""
ILGISIZ = """Ankara Üniversitesi Öğretim Üyesi Alım İlanı. Doçent kadrosu için ilgili mevzuat şartları aranır."""


def ev(text, profile, settings):
    title, _, body = text.partition("\n")
    return evaluate_kamu(title, body, extract_fields(title, body, TODAY), profile, settings, today=TODAY)


def test_deadline_parsing():
    assert find_deadline("Son başvuru tarihi 5 Aralık 2026") == "2026-12-05"
    assert find_deadline("son başvuru: 15.01.2027") == "2027-01-15"


def test_relevant_worker_ad(profile, settings):
    r = ev(ISCI, profile, settings)
    st = {c.name: c.status for c in r.checks}
    assert r.extra["ilgililik"] >= 50 and st["Eğitim"] == "Karşılıyor"
    assert r.gate != "Başvurulamaz" and r.score >= 55


def test_lisans_only_rejected(profile, settings):
    r = ev(LISANS, profile, settings)
    assert {c.name: c.status for c in r.checks}["Eğitim"] == "Karşılamıyor"
    assert r.gate == "Başvurulamaz"


def test_pomem_kpss_and_age_need_verification(profile, settings):
    from radar.profile import load_profile
    empty = load_profile({**profile.raw, "kamu": {}})
    r = ev(POMEM, empty, settings)
    st = {c.name: c.status for c in r.checks}
    assert st["KPSS"] == "Doğrulanmalı" and st["Yaş"] == "Doğrulanmalı"
    assert r.gate == "Doğrulanmalı" and r.score >= 55
    p2 = profile.raw.copy()
    from radar.profile import load_profile
    p2 = load_profile({**profile.raw, "kamu": {"kpss_p93": 60, "dogum_tarihi": "2003-05-01"}})
    r2 = ev(POMEM, p2, settings)
    st2 = {c.name: c.status for c in r2.checks}
    assert st2["KPSS"] == "Karşılamıyor" and st2["Yaş"] == "Karşılıyor" and r2.career_track == "KPSS puanı yükseltilmeli"


def test_irrelevant_ad_has_zero_relevance(profile, settings):
    assert ev(ILGISIZ, profile, settings).extra["ilgililik"] == 0


LIST = """<html><body><a href="/ilan/87100/gaziantep-buyuksehir-belediyesi-12-isci-alacak.html">Gaziantep Büyükşehir Belediyesi 12 İşçi Alacak</a>
<a href="/ilan/87034/adli-tip-kurumu-181-sozlesmeli-personel-alacak.html">Adli Tıp Kurumu 181 sözleşmeli personel alacak</a>
<a href="/kategori/genel/">Genel ilanlar kategorisi</a></body></html>"""
DETAIL = "<html><body><div class='ilan-detay'>" + "<p>" + ISCI.replace("\n", "</p><p>") + "</p>" + "<p>" + ("Ek açıklama. " * 40) + "</p></div></body></html>"


def mock(map_):
    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /ilan/yonet/\n")
        for k, v in map_.items():
            if k in str(req.url):
                return httpx.Response(200, text=v)
        return httpx.Response(404)
    return PoliteClient(delay_s=0, client=httpx.Client(transport=httpx.MockTransport(h)))


def test_announcement_collector_reads_new_only():
    http = mock({"87100": DETAIL, "87034": DETAIL, "kategori/kpss": LIST})
    cfg = {"max_detay": 1, "sayfalar": [{"ad": "memurlar", "kaynak": "memurlar.net",
                                         "url": "https://ilan.memurlar.net/kategori/kpss-b-ilanlari/",
                                         "ilan_deseni": r"ilan\.memurlar\.net/ilan/(\d+)/"}]}
    col = AnnouncementCollector(cfg, Secrets(), client=http, known={"memurlar.net:99999"})
    posts = col.fetch()
    assert [p.external_id for p in posts] == ["87100"]           # kota 1: ikincisi sonraki tura kaldı
    assert posts[0].sector == "kamu" and "Mekatronik" in posts[0].description and posts[0].detail_level == "tam"
    posts2 = AnnouncementCollector(cfg, Secrets(), client=http, known=col.known).fetch()
    assert [p.external_id for p in posts2] == ["87034"]


def test_other_department_onlisans_not_notified(profile, settings):
    txt = """Devlet Hastanesi 5 sözleşmeli personel alacak.
Ön lisans mezunu olmak ve Tıbbi Dokümantasyon ve Sekreterlik programından mezun olmak."""
    r = ev(txt, profile, settings)
    assert r.score < settings["bildirim"].get("kamu_min_puan", 55)


def test_kamu_end_to_end_queue_and_card(session, profile, settings):
    from radar.collectors.base import RawPosting
    from radar.db import Job, Source
    from radar.formatting import card
    from radar.pipeline import scan
    from radar.queries import JobFilter, find_jobs, parse_query
    from .test_pipeline import FakeCollector
    session.add(Source(name="k", kind="t"))
    session.commit()
    title, _, body = ISCI.partition("\n")
    items = [RawPosting("memurlar.net", "87100", "https://ilan.memurlar.net/ilan/87100/x.html", title, sector="kamu",
                        description=body),
             RawPosting("memurlar.net", "87101", "https://ilan.memurlar.net/ilan/87101/y.html", "Ankara Üniversitesi Öğretim Üyesi Alım İlanı",
                        sector="kamu", description="Doçent kadrosu için ilgili mevzuat şartları aranır." * 3)]
    rep = scan(session, [FakeCollector("k", items)], profile, settings, today=TODAY)
    assert rep.queued == 1
    job = session.query(Job).filter(Job.title.like("Gaziantep%")).one()
    txt = card(job, job.latest_eval)
    assert "KAMU İLANI" in txt and "05.12.2026" in txt
    assert parse_query("kamu ilanları").sector == "kamu"
    assert [j.title for j, _ in find_jobs(session, JobFilter(sector="kamu"), now=TODAY)] == [job.title]

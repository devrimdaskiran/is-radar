"""İlan toplama, tekrar önleme, kaynak hatası, bildirim kuyruğu, yeniden başlatma testleri."""
from datetime import datetime

from sqlalchemy import select

from radar.collectors.base import Collector, CollectorError, RawPosting
from radar.db import Feedback, Job, JobLink, Notification, Source, make_engine, make_session_factory
from radar.pipeline import fingerprint, scan

from .conftest import TODAY

GOOD_DESC = ("• Meslek Lisesi veya Meslek Yüksek Okullarının Elektrik-Elektronik, Mekatronik bölümlerinden mezun,\n"
             "• Vardiyalı çalışmaya uyum sağlayabilecek.")


class FakeCollector(Collector):
    def __init__(self, name, items=None, error=None):
        super().__init__({}, None)
        self.name, self.items, self.error = name, items or [], error

    def fetch(self):
        if self.error:
            raise self.error
        return list(self.items)


def post(src, ext, title="Elektrik Bakım Teknisyeni", company="Merinos Halı A.Ş.", city="Gaziantep", desc=GOOD_DESC):
    return RawPosting(source=src, external_id=ext, url=f"https://ornek.test/{src}/{ext}", title=title, company=company,
                      city=city, description=desc, published_at=TODAY)


def seed(s, *names):
    for n in names:
        s.add(Source(name=n, kind="test"))
    s.commit()


def test_same_job_on_two_sites_is_merged(session, profile, settings):
    seed(session, "a", "b")
    cols = [FakeCollector("a", [post("kariyer.net", "1")]),
            FakeCollector("b", [post("jooble", "x9", company="MERİNOS HALI A.Ş")])]
    rep = scan(session, cols, profile, settings, today=TODAY)
    assert rep.new_jobs == 1
    assert session.query(Job).count() == 1
    assert session.query(JobLink).count() == 2


def test_different_city_or_title_not_merged(session, profile, settings):
    seed(session, "a")
    items = [post("kariyer.net", "1"), post("kariyer.net", "2", city="Adana"),
             post("kariyer.net", "3", title="Mekatronik Teknikeri")]
    scan(session, [FakeCollector("a", items)], profile, settings, today=TODAY)
    assert session.query(Job).count() == 3


def test_unknown_company_never_merges_across_sources(profile):
    a = post("kariyer.net", "1", company=None)
    b = post("linkedin", "77", company=None)
    assert fingerprint(a) != fingerprint(b)


def test_failing_source_does_not_stop_others(session, profile, settings):
    seed(session, "bozuk", "saglam", "cokuyor")
    cols = [FakeCollector("bozuk", error=CollectorError("Erişim engellendi (HTTP 403)")),
            FakeCollector("cokuyor", error=RuntimeError("beklenmeyen")),
            FakeCollector("saglam", [post("kariyer.net", "1")])]
    rep = scan(session, cols, profile, settings, today=TODAY)
    assert rep.sources_failed == 2 and rep.sources_ok == 1 and rep.new_jobs == 1
    src = {x.name: x for x in session.scalars(select(Source))}
    assert src["bozuk"].status == "hata" and "403" in src["bozuk"].last_error
    assert src["saglam"].status == "çalışıyor" and src["saglam"].last_success
    assert src["bozuk"].last_success is None      # çalışmayan kaynak çalışıyor gibi gösterilmez


def test_notification_queued_once_and_not_interested_respected(session, profile, settings):
    seed(session, "a")
    col = FakeCollector("a", [post("kariyer.net", "1"), post("kariyer.net", "2", title="Mekatronik Teknikeri")])
    rep1 = scan(session, [col], profile, settings, today=TODAY)
    assert rep1.queued == 2
    rep2 = scan(session, [col], profile, settings, today=TODAY)
    assert rep2.queued == 0 and session.query(Notification).count() == 2
    job = session.scalar(select(Job).where(Job.title == "Mekatronik Teknikeri"))
    session.add(Feedback(job_id=job.id, not_interested=True))
    session.commit()
    col.items = [post("kariyer.net", "2", title="Mekatronik Teknikeri", desc=GOOD_DESC + "\n• B sınıfı ehliyet sahibi.")]
    rep3 = scan(session, [col], profile, settings, today=TODAY)
    assert rep3.queued == 0


def test_requirement_change_triggers_update_notification(session, profile, settings):
    seed(session, "a")
    col = FakeCollector("a", [post("kariyer.net", "1")])
    scan(session, [col], profile, settings, today=TODAY)
    col.items = [post("kariyer.net", "1", desc=GOOD_DESC + "\n• Son başvuru tarihi: 15.12.2026")]
    rep = scan(session, [col], profile, settings, today=TODAY)
    reasons = [n.reason for n in session.scalars(select(Notification).order_by(Notification.id))]
    assert reasons == ["yeni", "güncelleme"] and rep.queued == 1


def test_inapplicable_job_not_notified_but_stored(session, profile, settings):
    seed(session, "a")
    desc = "• Mekatronik bölümünden mezun\n• Askerlik hizmetini tamamlamış"
    rep = scan(session, [FakeCollector("a", [post("kariyer.net", "1", desc=desc)])], profile, settings, today=TODAY)
    assert rep.new_jobs == 1 and rep.queued == 0
    job = session.scalar(select(Job))
    assert job.latest_eval.gate == "Başvurulamaz" and job.latest_eval.career_track == "Askerlik sonrası uygun"


def test_expired_job_marked_and_not_notified(session, profile, settings):
    seed(session, "a")
    desc = GOOD_DESC + "\n• Son başvuru tarihi: 01.11.2026"
    rep = scan(session, [FakeCollector("a", [post("kariyer.net", "1", desc=desc)])], profile, settings, today=TODAY)
    job = session.scalar(select(Job))
    assert job.status == "süresi_geçti" and rep.queued == 0


def test_no_reanalysis_when_unchanged(session, profile, settings):
    seed(session, "a")
    col = FakeCollector("a", [post("kariyer.net", "1")])
    scan(session, [col], profile, settings, today=TODAY)
    rep = scan(session, [col], profile, settings, today=TODAY)
    assert rep.evaluated == 0


def test_data_survives_restart(tmp_path, profile, settings):
    url = f"sqlite:///{tmp_path / 'radar.db'}"
    S = make_session_factory(make_engine(url))
    with S() as s:
        seed(s, "a")
        scan(s, [FakeCollector("a", [post("kariyer.net", "1")])], profile, settings, today=TODAY)
    S2 = make_session_factory(make_engine(url))   # "yeniden başlatma"
    with S2() as s:
        assert s.query(Job).count() == 1 and s.query(Notification).count() == 1
        rep = scan(s, [FakeCollector("a", [post("kariyer.net", "1")])], profile, settings, today=TODAY)
        assert rep.queued == 0                       # yeniden başlatma sonrası tekrar bildirim yok

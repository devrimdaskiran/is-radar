"""Kaynak modülleri: Jooble API eşleme, e-posta alarm ayrıştırma, JSON-LD kariyer sayfaları, robots.txt."""
import json
from email.message import EmailMessage

import httpx
import pytest

from radar.collectors.base import CollectorError, PoliteClient
from radar.collectors.career_jsonld import parse_jobposting_html
from radar.collectors.imap_alerts import ImapAlertsCollector, match_link, parse_alert_html
from radar.collectors.jooble import JoobleCollector
from radar.config import Secrets

ALERT_HTML = """
<html><body><table>
<tr><td><a href="https://click.kariyer.net/c?u=https%3A%2F%2Fwww.kariyer.net%2Fis-ilani%2Fozmermer-yem-san-a-s-elektrik-otomasyon-teknisyeni-4506722%3Futm_source%3Dalarm">Elektrik Otomasyon Teknisyeni</a>
<div>Özmermer Yem San. A.Ş.</div><div>Gaziantep (Şehitkamil)</div>
<a href="https://www.kariyer.net/is-ilani/ozmermer-yem-san-a-s-elektrik-otomasyon-teknisyeni-4506722">Başvur</a></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4123456789/?trackingId=abc">Mekatronik Teknikeri</a>
<p>ABC Otomasyon</p><p>Kahramanmaraş, Türkiye</p></td></tr>
<tr><td><a href="https://tr.indeed.com/rc/clk?jk=a1b2c3d4e5f6a7b8&from=ja">Bakım Teknisyeni</a>
<span>XYZ Tekstil</span><span>Gaziantep</span></td></tr>
<tr><td><a href="https://www.kariyer.net/is-ilanlari">Tüm ilanlar</a></td></tr>
</table></body></html>"""


def test_alert_email_parsing():
    posts = parse_alert_html(ALERT_HTML)
    by = {p.source: p for p in posts}
    assert set(by) == {"kariyer.net", "linkedin", "indeed"}
    k = by["kariyer.net"]
    assert k.external_id == "4506722" and k.title == "Elektrik Otomasyon Teknisyeni"
    assert k.company.startswith("Özmermer") and k.city == "Gaziantep"
    assert k.url == "https://www.kariyer.net/is-ilani/ozmermer-yem-san-a-s-elektrik-otomasyon-teknisyeni-4506722"
    assert by["linkedin"].external_id == "4123456789" and by["linkedin"].city == "Kahramanmaraş"
    assert by["indeed"].url == "https://tr.indeed.com/viewjob?jk=a1b2c3d4e5f6a7b8"
    assert all(p.detail_level == "özet" for p in posts)


def test_non_job_links_ignored():
    assert match_link("https://www.kariyer.net/is-ilanlari") is None
    assert match_link("https://www.linkedin.com/feed/") is None


class FakeIMAP:
    def __init__(self, raw):
        self.raw, self.selected_readonly = raw, None

    def login(self, u, p):
        if p != "dogru":
            import imaplib
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, box, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def search(self, charset, crit):
        return "OK", [b"1"] if "kariyer.net" in crit else [b""]

    def fetch(self, num, what):
        assert "PEEK" in what            # okunmadı işareti değişmemeli
        return "OK", [(b"1", self.raw)]

    def logout(self):
        pass


def make_mail():
    m = EmailMessage()
    m["From"] = "Kariyer.net <alarm@kariyer.net>"
    m["Date"] = "Sat, 21 Nov 2026 03:05:00 +0300"
    m["Subject"] = "Yeni ilanlar"
    m.set_content("düz metin")
    m.add_alternative(ALERT_HTML, subtype="html")
    return m.as_bytes()


def test_imap_collector_readonly_end_to_end():
    fake = FakeIMAP(make_mail())
    cfg = {"gonderenler": {"kariyer.net": ["kariyer.net"], "linkedin": ["linkedin.com"]}, "gun_geriye": 3}
    col = ImapAlertsCollector(cfg, Secrets(gmail_address="a@gmail.com", gmail_app_password="dogru"), imap_factory=lambda: fake)
    posts = col.fetch()
    assert fake.selected_readonly is True
    assert {p.external_id for p in posts} >= {"4506722"}
    assert posts[0].published_at.year == 2026


def test_imap_bad_password_is_reported():
    col = ImapAlertsCollector({"gonderenler": {}}, Secrets(gmail_address="a@gmail.com", gmail_app_password="yanlis"),
                              imap_factory=lambda: FakeIMAP(b""))
    with pytest.raises(CollectorError, match="Gmail'e bağlanılamadı"):
        col.fetch()


def test_imap_missing_config():
    with pytest.raises(CollectorError, match="tanımlı değil"):
        ImapAlertsCollector({}, Secrets()).fetch()


def test_jooble_mapping_and_errors():
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        assert req.url.path.endswith("/api/ANAHTAR")
        return httpx.Response(200, json={"totalCount": 1, "jobs": [{
            "title": "<b>Mekatronik</b> Teknikeri", "location": "Şehitkamil, Gaziantep", "snippet": "Meslek yüksekokulu&nbsp;Mekatronik mezunu...",
            "salary": "", "source": "kariyer.net", "type": "Tam zamanlı", "link": "https://jooble.org/desc/123",
            "company": "ABC", "updated": "2026-11-21T03:00:00.0000000", "id": 123, "_q": body["keywords"]}]})
    cli = httpx.Client(transport=httpx.MockTransport(handler))
    col = JoobleCollector({"aramalar": [{"kelime": "mekatronik", "konum": "Gaziantep"}]}, Secrets(jooble_api_key="ANAHTAR"), client=cli)
    [p] = col.fetch()
    assert p.title == "Mekatronik Teknikeri" and p.city == "Gaziantep" and p.district == "Şehitkamil"
    assert p.detail_level == "özet" and "Mekatronik mezunu" in p.description

    with pytest.raises(CollectorError, match="JOOBLE_API_KEY"):
        JoobleCollector({}, Secrets()).fetch()
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(CollectorError, match="reddedildi"):
        JoobleCollector({"aramalar": [{"kelime": "x"}]}, Secrets(jooble_api_key="k"), client=bad).fetch()


def test_jsonld_jobposting():
    page = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"JobPosting",
    "title":"Test Teknisyeni","hiringOrganization":{"name":"Örnek Savunma"},"datePosted":"2026-11-20",
    "validThrough":"2026-12-20T23:59","description":"<p>Meslek Yüksekokullarının <b>Mekatronik</b> bölümünden mezun</p>",
    "jobLocation":{"address":{"addressLocality":"Gaziantep"}},"identifier":{"value":"TS-42"}}]}</script></head></html>"""
    [p] = parse_jobposting_html(page, "https://kariyer.ornek.com/ilan/42")
    assert p.title == "Test Teknisyeni" and p.company == "Örnek Savunma" and p.city == "Gaziantep"
    assert p.external_id == "TS-42" and p.deadline.day == 20 and "Mekatronik" in p.description


def test_robots_and_captcha_respected():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /gizli\n")
        if req.url.path == "/captcha":
            return httpx.Response(200, text="<html>Please complete the CAPTCHA</html>")
        return httpx.Response(200, text="ok")
    pc = PoliteClient(delay_s=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert pc.get("https://site.test/acik").text == "ok"
    with pytest.raises(CollectorError, match="robots"):
        pc.get("https://site.test/gizli/ilan")
    with pytest.raises(CollectorError, match="CAPTCHA"):
        pc.get("https://site.test/captcha")

"""GitHub Actions çalıştırıcısı, Telegram etkileşimi, şifreleme, site listeleri ve Gemini testleri."""
import json
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from radar.collectors.base import PoliteClient, RawPosting
from radar.collectors.gemini_search import GeminiSearchCollector
from radar.collectors.site_listing import SiteListingCollector, extract_detail_text
from radar.config import Secrets
from radar.db import Application, Feedback, Job, Notification, Source, make_engine, make_session_factory
from radar.llm.providers import GeminiProvider
from radar.pipeline import scan
from radar.runner import Runner, getset
from radar.secure import KeyMissing, decrypt_bytes, encrypt_bytes, new_key

from .conftest import TODAY
from .test_pipeline import GOOD_DESC, FakeCollector

LIST_HTML = """<html><body><nav><a href="/is-ilanlari/elektrik">Elektrik İlanları</a></nav>
<div class="list">
<a href="/is-ilani/ornek-a-s-mekatronik-teknikeri-4601111?ref=x" title="Mekatronik Teknikeri">Mekatronik Teknikeri</a>
<a href="https://www.kariyer.net/is-ilani/ornek-ltd-satis-temsilcisi-4602222">Satış Temsilcisi</a>
<a href="/is-ilani/kapali-firma-bakim-teknisyeni-4603333">Bakım Teknisyeni</a>
</div></body></html>"""
DETAIL_HTML = """<html><body><header>Menü Elektrik Mühendisi İlanları Gaziantep İstanbul</header>
<h1>Mekatronik Teknikeri</h1><div class="detail"><div><h2>Genel Nitelikler</h2><ul>
<li>Meslek Yüksekokullarının Mekatronik bölümünden mezun,</li><li>Deneyimli / deneyimsiz,</li>
<li>Vardiyalı çalışmaya uygun.</li></ul></div></div><footer>Mühendislik ilanları</footer></body></html>"""
CLOSED_HTML = "<html><body><h1>Bakım Teknisyeni</h1><p>Bu ilan başvuruları artık kabul etmiyor.</p></body></html>"


def mock_site(handler_map):
    def h(req: httpx.Request):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /filtre\n")
        for k, v in handler_map.items():
            if k in str(req.url):
                return httpx.Response(200, text=v)
        return httpx.Response(404, text="yok")
    return PoliteClient(delay_s=0, client=httpx.Client(transport=httpx.MockTransport(h)))


def test_site_listing_collector(profile):
    http = mock_site({"is-ilanlari/gaziantep": LIST_HTML, "4601111": DETAIL_HTML, "4603333": CLOSED_HTML})
    cfg = {"iller": ["gaziantep"], "siteler": [{"ad": "kariyer.net", "liste_url": "https://www.kariyer.net/is-ilanlari/{il}",
                                                 "sayfa": 1, "ilan_deseni": r"kariyer\.net/is-ilani/[a-z0-9\-]+-(\d{5,})$"}]}
    col = SiteListingCollector(cfg, Secrets(), client=http, profile=profile)
    posts = col.fetch()
    assert [p.external_id for p in posts] == ["4601111"]          # satış elendi, kapalı ilan atlandı
    p = posts[0]
    assert p.detail_level == "tam" and "Mekatronik bölümünden mezun" in p.description
    assert "Menü" not in p.description and p.city == "Gaziantep"


def test_detail_text_and_closed_detection():
    text, closed = extract_detail_text(DETAIL_HTML)
    assert "Genel Nitelikler" in text and not closed
    assert extract_detail_text(CLOSED_HTML)[1] is True


def test_site_listing_warns_when_structure_changes(profile):
    http = mock_site({"is-ilanlari/gaziantep": "<html><body>yeni tasarım</body></html>"})
    cfg = {"iller": ["gaziantep"], "siteler": [{"ad": "kariyer.net", "liste_url": "https://www.kariyer.net/is-ilanlari/{il}",
                                                 "ilan_deseni": r"is-ilani/.*-(\d+)$"}]}
    col = SiteListingCollector(cfg, Secrets(), client=http, profile=profile)
    assert col.fetch() == [] and "yapısı değişmiş" in col.warnings[0]


def test_gemini_search_verifies_links():
    class FakeGem:
        def generate(self, body, timeout=0):
            assert body["tools"] == [{"google_search": {}}]
            txt = json.dumps({"ilanlar": [
                {"url": "https://www.kariyer.net/is-ilani/ornek-a-s-mekatronik-teknikeri-4601111", "baslik": "Mekatronik Teknikeri"},
                {"url": "https://www.kariyer.net/is-ilani/uydurma-9999999", "baslik": "Uydurma"},
                {"url": "https://kotu-site.example/ilan/1", "baslik": "İzinsiz alan"}]})
            return {"candidates": [{"content": {"parts": [{"text": txt}]}}]}, "gemini-x"
    http = mock_site({"4601111": DETAIL_HTML})
    col = GeminiSearchCollector({"aramalar": ["test"], "izinli_alanlar": ["kariyer.net"]}, Secrets(), provider=FakeGem(), client=http)
    posts = col.fetch()
    assert len(posts) == 1 and "Mekatronik" in posts[0].description   # 404 veren uydurma bağlantı atıldı


def test_gemini_provider_model_pick_and_fallback():
    calls = []

    def h(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-9.0-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-9.0-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-9.0-flash-image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding", "supportedGenerationMethods": ["embedContent"]}]})
        calls.append(req.url.path)
        if "flash-lite" not in req.url.path:
            return httpx.Response(429, text="quota")
        assert req.headers["x-goog-api-key"] == "ANAHTAR"
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"gereksinimler": []}'}]}}],
                                         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})
    gp = GeminiProvider(" ANAH TAR ", client=httpx.Client(transport=httpx.MockTransport(h)))
    assert gp.models() == ["gemini-9.0-flash", "gemini-9.0-flash-lite"]
    c = gp.complete("sys", "user")
    assert c.text == '{"gereksinimler": []}' and c.model == "gemini-9.0-flash-lite"


def test_encryption_roundtrip(monkeypatch):
    monkeypatch.setenv("RADAR_KEY", new_key())
    blob = encrypt_bytes("gizli profil".encode())
    assert b"gizli" not in blob and decrypt_bytes(blob).decode() == "gizli profil"
    monkeypatch.setenv("RADAR_KEY", new_key())
    with pytest.raises(KeyMissing):
        decrypt_bytes(blob)


def test_encrypted_profile_loads(monkeypatch, tmp_path):
    import shutil

    from radar import config
    import yaml
    original = config.load_yaml("profile.yaml")          # düz ya da şifreli kaynaktan
    key = new_key()
    monkeypatch.setenv("RADAR_KEY", key)
    shutil.copy(config.CONFIG_DIR / "settings.yaml", tmp_path)
    (tmp_path / "profile.yaml.enc").write_bytes(encrypt_bytes(yaml.safe_dump(original, allow_unicode=True).encode()))
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    assert config.load_yaml("profile.yaml")["aday"]["sehir"] == "Gaziantep"


# ------------------------------------------------------------------ Telegram çalıştırıcısı
class FakeTG:
    def __init__(self, updates=None):
        self.ups, self.sent, self.answered = updates or [], [], []

    def updates(self, offset):
        return [u for u in self.ups if not offset or u["update_id"] >= offset]

    def send(self, chat, text, buttons=None):
        self.sent.append((chat, text, buttons))
        return len(self.sent)

    def answer_callback(self, cid, text=""):
        self.answered.append(text)

    def edit_buttons(self, *a):
        pass


class FakeApp:
    def __init__(self, Session, profile, settings, chat_secret=""):
        self.Session, self.profile, self._st = Session, profile, settings
        self.secrets = Secrets(telegram_chat_id=chat_secret)

    def settings(self, s=None):
        from radar.config import deep_merge
        from radar.db import get_setting_overrides
        return deep_merge(self._st, get_setting_overrides(s) if s is not None else {})


def msg(uid, text, chat=None, n=[100]):
    n[0] += 1
    return {"update_id": n[0], "message": {"from": {"id": uid}, "chat": {"id": chat or uid}, "text": text}}


@pytest.fixture()
def env(profile, settings):
    S = make_session_factory(make_engine("sqlite:///:memory:"))
    with S() as s:
        s.add(Source(name="a", kind="t"))
        s.commit()
        p = RawPosting("kariyer.net", "1", "https://www.kariyer.net/is-ilani/x-1", "Elektrik Bakım Teknisyeni", "Merinos",
                       "Gaziantep", description=GOOD_DESC)
        scan(s, [FakeCollector("a", [p])], profile, settings, today=TODAY)
    return S, FakeApp(S, profile, settings)


def test_first_start_binds_owner_and_ignores_strangers(env):
    S, app = env
    tg = FakeTG([msg(555, "/jobs"), msg(42, "/start"), msg(555, "/start"), msg(42, "/stats")])
    r = Runner(app, tg)
    with S() as s:
        r.handle_updates(s)
        assert getset(s, "tg.chat_id") == "42"
        assert getset(s, "tg.offset") == tg.ups[-1]["update_id"] + 1
    chats = {c for c, _, _ in tg.sent}
    assert chats == {"42"}                                   # 555 hiçbir cevap almadı
    assert any("İstatistikler" in t for _, t, _ in tg.sent)


def test_commands_and_natural_language(env):
    S, app = env
    tg = FakeTG([msg(7, "/start"), msg(7, "/jobs"), msg(7, "Gaziantep'te deneyim istemeyen işler"), msg(7, "/sources"),
                 msg(7, "/ilan_1"), msg(7, "/bilinmeyen")])
    r = Runner(app, tg)
    with S() as s:
        r.handle_updates(s)
    texts = [t for _, t, _ in tg.sent]
    assert any("Elektrik Bakım Teknisyeni" in t for t in texts[1:3])
    assert any("Kaynaklar" in t for t in texts)
    card_msg = next(x for x in tg.sent if "YENİ İŞ FIRSATI" in x[1])
    assert card_msg[2][0][0]["url"].startswith("https://www.kariyer.net")
    assert "tanımıyorum" in texts[-1]


def test_buttons_feedback_and_application(env):
    S, app = env
    cb = lambda n, data: {"update_id": n, "callback_query": {"id": f"c{n}", "data": data, "from": {"id": 7},
                                                            "message": {"chat": {"id": 7}, "message_id": 1}}}
    tg = FakeTG([msg(7, "/start"), cb(900, "fav:1"), cb(901, "app:1"), cb(902, "ni:1"), cb(903, "st:1:4")])
    r = Runner(app, tg)
    with S() as s:
        r.handle_updates(s)
        fb = s.get(Feedback, 1)
        a = s.scalar(select(Application))
        assert fb.favorite and fb.not_interested
        assert a.state == "Görüşme bekleniyor" and a.reminder_at > a.applied_at
    assert "⭐ Favorilere eklendi" in tg.answered


def test_notifications_sent_once_respecting_quiet_hours(env, monkeypatch):
    S, app = env
    tg = FakeTG()
    r = Runner(app, tg)
    r.chat = "7"
    import radar.runner as rr
    monkeypatch.setattr(rr, "now", lambda: datetime(2026, 11, 22, 3, 0))
    with S() as s:
        assert r.send_notifications(s) == 0              # gece: bekletilir
    monkeypatch.setattr(rr, "now", lambda: datetime(2026, 11, 22, 9, 0))
    with S() as s:
        assert r.send_notifications(s) == 1
        assert r.send_notifications(s) == 0              # tekrar gönderilmez
        assert s.scalar(select(Notification)).sent_at is not None
    assert "YENİ İŞ FIRSATI" in tg.sent[0][1]


def test_daily_report_once_per_day(env, monkeypatch):
    S, app = env
    tg = FakeTG()
    r = Runner(app, tg)
    r.chat = "7"
    import radar.runner as rr
    monkeypatch.setattr(rr, "now", lambda: datetime(2026, 11, 22, 21, 0))   # Pazar → haftalık rapor da eklenir
    with S() as s:
        r.daily(s)
        r.daily(s)
    assert len(tg.sent) == 1 and "GÜNLÜK RAPOR" in tg.sent[0][1] and "HAFTALIK" in tg.sent[0][1]


def test_telegram_client_hides_token():
    from radar.telegram_api import Telegram, TelegramError
    cli = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})))
    t = Telegram("123456:GIZLI-TOKEN", client=cli)
    with pytest.raises(TelegramError) as e:
        t.send("1", "merhaba")
    assert "GIZLI" not in str(e.value) and "token yanlış" in str(e.value)

"""E-posta iş alarmı toplayıcısı (Gmail IMAP, salt okunur).

Kariyer.net, LinkedIn, Indeed vb. sitelerde kayıtlı aramaların e-postalarını okur ve
içindeki ilan bağlantılarını çıkarır. Siteye bot trafiği gitmez, site şifresi kullanılmaz.
Mesajlar BODY.PEEK ile okunur: okunmadı işaretleri değişmez, hiçbir e-posta silinmez/taşınmaz.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta
from email import policy
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from ..textnorm import fold
from .base import Collector, CollectorError, RawPosting

log = logging.getLogger(__name__)

# (site, URL deseni, kimlik grubu, kanonik URL şablonu)
LINK_RULES = [
    ("kariyer.net", re.compile(r"kariyer\.net/is-ilani/([a-z0-9\-]+-(\d+))"), 2, "https://www.kariyer.net/is-ilani/{full}"),
    ("linkedin", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?]*?-)?(\d{6,})"), 1, "https://www.linkedin.com/jobs/view/{id}"),
    ("indeed", re.compile(r"indeed\.com/(?:rc/clk|viewjob|pagead/clk)[^\"']*?[?&]jk=([0-9a-f]{10,})"), 1, "https://tr.indeed.com/viewjob?jk={id}"),
    ("eleman.net", re.compile(r"eleman\.net/is-ilani/([a-z0-9\-]+-i(\d+))"), 2, "https://www.eleman.net/is-ilani/{full}"),
    ("secretcv", re.compile(r"secretcv\.com/is-ilanlari/([a-z0-9\-]+-(\d+))"), 2, "https://www.secretcv.com/is-ilanlari/{full}"),
    ("yenibiris", re.compile(r"yenibiris\.com/is-ilanlari/([a-z0-9\-]+-(\d+))"), 2, "https://www.yenibiris.com/is-ilanlari/{full}"),
]
GENERIC_ANCHOR = {"basvur", "hemen basvur", "ilani incele", "ilani gor", "tumunu gor", "tum ilanlar", "detay",
                  "view job", "apply", "see all jobs", "goruntule", "daha fazla", "incele"}


def _candidate_urls(href: str) -> list[str]:
    """Takip (tracking) yönlendirmelerinin içindeki gerçek adresi de dener."""
    out = [href]
    dec = unquote(unquote(href))
    if dec != href:
        out.append(dec)
    return out


def match_link(href: str):
    for site, pat, gid, tmpl in LINK_RULES:
        for u in _candidate_urls(href):
            m = pat.search(u)
            if m:
                ext = m.group(gid)
                full = m.group(1)
                return site, ext, tmpl.format(id=ext, full=full)
    return None


def _block_lines(a) -> list[str]:
    node = a
    for _ in range(4):
        if node.parent is None:
            break
        node = node.parent
        txt = node.get_text("\n", strip=True)
        if len(txt.splitlines()) >= 3:
            break
    return [ln.strip() for ln in node.get_text("\n", strip=True).splitlines() if ln.strip()]


def parse_alert_html(html: str, from_header: str = "", provinces: list[str] | None = None) -> list[RawPosting]:
    from ..extract import PROVINCES  # döngüsel içe aktarmayı önlemek için
    provinces = provinces or PROVINCES
    soup = BeautifulSoup(html or "", "html.parser")
    seen: dict[str, RawPosting] = {}
    for a in soup.find_all("a", href=True):
        hit = match_link(a["href"])
        if not hit:
            continue
        site, ext, url = hit
        text = a.get_text(" ", strip=True)
        key = f"{site}:{ext}"
        if key in seen:
            continue
        if not text or fold(text) in GENERIC_ANCHOR or len(text) < 4:
            # Başlık bu bağlantıda değilse, aynı ilanın başka bir bağlantısında olabilir
            continue
        lines = _block_lines(a)
        company = city = None
        try:
            i = lines.index(text)
            after = lines[i + 1:i + 5]
        except ValueError:
            after = lines[:4]
        for ln in after:
            fl = fold(ln)
            prov = next((p for p in provinces if re.search(r"(?<![a-z])" + re.escape(fold(p)) + r"(?![a-z])", fl)), None)
            if prov and not city:
                city = prov
            elif not company and len(ln) < 120 and not prov and fold(ln) not in GENERIC_ANCHOR:
                company = ln
        if not city:
            slug = fold(urlparse(url).path.replace("-", " "))
            city = next((p for p in provinces if re.search(r"(?<![a-z])" + re.escape(fold(p)) + r"(?![a-z])", slug)), None)
        seen[key] = RawPosting(source=site, external_id=ext, url=url, title=text, company=company, city=city,
                               description="", detail_level="özet",
                               raw={"alert_from": from_header, "block": " | ".join(lines[:8])})
    return list(seen.values())


class ImapAlertsCollector(Collector):
    name = "eposta_alarmlari"
    method = "Gmail IMAP (salt okunur) - iş alarmı e-postaları"

    def __init__(self, cfg, secrets, imap_factory=None):
        super().__init__(cfg, secrets)
        self.imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL("imap.gmail.com", 993))

    def fetch(self) -> list[RawPosting]:
        s = self.secrets
        if not (s.gmail_address and s.gmail_app_password):
            raise CollectorError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD tanımlı değil (.env). Kaynak pasif kaldı.")
        since = (datetime.now() - timedelta(days=int(self.cfg.get("gun_geriye", 3)))).strftime("%d-%b-%Y")
        out: list[RawPosting] = []
        try:
            M = self.imap_factory()
            M.login(s.gmail_address, s.gmail_app_password)
        except (imaplib.IMAP4.error, OSError) as e:
            raise CollectorError(f"Gmail'e bağlanılamadı: {e}. Uygulama şifresini kontrol et.") from e
        try:
            M.select("INBOX", readonly=True)
            for site, domains in (self.cfg.get("gonderenler") or {}).items():
                for dom in domains:
                    typ, data = M.search(None, f'(SINCE "{since}" FROM "{dom}")')
                    if typ != "OK":
                        continue
                    for num in data[0].split()[-50:]:
                        typ, msg_data = M.fetch(num, "(BODY.PEEK[])")
                        if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                            continue
                        msg = email.message_from_bytes(msg_data[0][1], policy=policy.default)
                        body = msg.get_body(preferencelist=("html", "plain"))
                        html = body.get_content() if body else ""
                        posts = parse_alert_html(html, str(msg.get("From", "")))
                        date = msg.get("Date")
                        for p in posts:
                            try:
                                p.published_at = email.utils.parsedate_to_datetime(date).replace(tzinfo=None) if date else None
                            except (TypeError, ValueError):
                                pass
                        out.extend(posts)
        finally:
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
        return out

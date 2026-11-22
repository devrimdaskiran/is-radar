"""İş sitelerinin il bazlı ilan listelerini okuyan toplayıcı (Kariyer.net, Eleman.net, ...).

Çalışma şekli:
1. Liste sayfasını açar (robots.txt kontrolüyle, istekler arasında bekleyerek).
2. İlan bağlantılarını site-özel desenle bulur; başlığı bağlantı metninden alır.
3. Yalnızca unvanı profille ilgili görünen ilanların detay sayfasını açar (gereksiz istek yok).
4. Detayda JSON-LD JobPosting varsa onu, yoksa "Nitelikler / İş Tanımı" bölümünün metnini alır.
Kapanmış ilanlar ("başvuruları artık kabul etmiyor") atlanır.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..textnorm import fold, strip_html
from .base import Collector, CollectorError, PoliteClient, RawPosting
from .career_jsonld import parse_jobposting_html

log = logging.getLogger(__name__)

SECTION_HINTS = r"genel nitelik|aranan nitelik|is tanimi|nitelikler|aday kriter|is ilani detay|pozisyon hakkinda|gorev tanimi|is aciklamasi"
CLOSED_HINTS = r"basvurulari artik kabul etmiyor|ilan yayindan kaldirilmis|bu ilan kapanmistir|ilan suresi dolmus|aradiginiz ilan bulunamadi"


def extract_detail_text(html: str) -> tuple[str, bool]:
    """Detay sayfasından ilan metnini ve 'kapalı mı' bilgisini çıkarır."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form", "iframe"]):
        t.decompose()
    full = soup.get_text("\n", strip=True)
    closed = bool(re.search(CLOSED_HINTS, fold(full)))
    best = None
    for h in soup.find_all(re.compile(r"^(h1|h2|h3|h4|strong|b|div|span|p)$")):
        txt = h.get_text(" ", strip=True)
        if 3 < len(txt) < 60 and re.search(SECTION_HINTS, fold(txt)):
            node = h
            for _ in range(4):  # başlığın bulunduğu bölümü genişlet
                if node.parent is None:
                    break
                node = node.parent
                if len(node.get_text(" ", strip=True)) > 250:
                    break
            cand = node.get_text("\n", strip=True)
            if best is None or len(cand) > len(best):
                best = cand
    if not best:
        main = soup.find("main") or soup.find("article") or soup.body or soup
        best = main.get_text("\n", strip=True)
    lines = [ln for ln in best.splitlines() if ln.strip()]
    return strip_html("\n".join(lines))[:8000], closed


class SiteListingCollector(Collector):
    name = "site_listeleri"
    method = "İş sitelerinin il bazlı ilan listeleri (robots.txt'ye uyarak)"

    def __init__(self, cfg, secrets, client: PoliteClient | None = None, profile=None):
        super().__init__(cfg, secrets)
        self.http = client or PoliteClient(delay_s=float(cfg.get("bekleme_sn", 3)))
        self.profile = profile

    def _relevant(self, title: str) -> bool:
        if not self.profile:
            return True
        t = fold(title)
        if any(b in t for b in self.profile.blocked_titles):
            return False
        kws = [fold(k) for k in self.profile.title_keywords + self.profile.targets]
        return any(re.search(r"(?<![a-z])" + re.escape(k), t) for k in kws)

    def fetch(self) -> list[RawPosting]:
        out: list[RawPosting] = []
        errors: list[str] = []
        self.warnings = []
        ok_sites = 0
        max_detail = int(self.cfg.get("max_detay", 25))
        seen: set[str] = set()
        for site in self.cfg.get("siteler", []):
            if not site.get("aktif", True):
                continue
            pat = re.compile(site["ilan_deseni"])
            details = 0
            site_ok = False
            fails = 0
            for city in self.cfg.get("iller", []):
                if fails >= 2 and not site_ok:
                    errors.append(f"{site['ad']}: üst üste erişilemedi, bu tur atlandı")
                    break
                for page in range(1, int(site.get("sayfa", 1)) + 1):
                    url = site["liste_url"].format(il=city, sayfa=page)
                    if page == 1 and "{sayfa}" in site["liste_url"] and site.get("ilk_sayfa_url"):
                        url = site["ilk_sayfa_url"].format(il=city)
                    try:
                        r = self.http.get(url)
                    except CollectorError as e:
                        errors.append(f"{site['ad']}: {e}")
                        fails += 1
                        break
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{site['ad']} {city}: {type(e).__name__}: {str(e)[:120]}")
                        fails += 1
                        break
                    site_ok = True
                    soup = BeautifulSoup(r.text, "html.parser")
                    links = {}
                    for a in soup.find_all("a", href=True):
                        href = urljoin(url, a["href"].split("?")[0])
                        m = pat.search(href)
                        if not m:
                            continue
                        title = a.get("title") or a.get_text(" ", strip=True)
                        if href not in links or len(title) > len(links[href][1]):
                            links[href] = (m.group(1), title.strip())
                    if not links and page == 1:
                        self.warnings.append(f"{site['ad']}/{city}: sayfa açıldı ama ilan bağlantısı bulunamadı (site yapısı değişmiş olabilir)")
                    for href, (ext, title) in links.items():
                        key = f"{site['ad']}:{ext}"
                        if key in seen or not title:
                            continue
                        seen.add(key)
                        if not self._relevant(title):
                            continue
                        post = RawPosting(source=site["ad"], external_id=ext, url=href, title=title[:200],
                                          city=city.replace("-", " ").title() if city.isascii() else city,
                                          detail_level="özet", raw={"liste": url})
                        if details < max_detail:
                            details += 1
                            try:
                                d = self.http.get(href)
                                jl = parse_jobposting_html(d.text, href)
                                if jl:
                                    j = jl[0]
                                    j.source, j.external_id, j.url, j.detail_level = site["ad"], ext, href, "tam"
                                    j.title = j.title or post.title
                                    j.city = j.city or post.city
                                    post = j
                                else:
                                    text, closed = extract_detail_text(d.text)
                                    if closed:
                                        continue
                                    post.description, post.detail_level = text, "tam"
                                    h1 = BeautifulSoup(d.text, "html.parser").find("h1")
                                    if h1 and h1.get_text(strip=True):
                                        post.title = h1.get_text(" ", strip=True)[:200]
                            except CollectorError as e:
                                errors.append(f"{site['ad']} detay: {e}")
                            except Exception as e:  # noqa: BLE001
                                log.warning("Detay okunamadı %s: %s", href, e)
                        out.append(post)
            ok_sites += site_ok
        if not ok_sites:
            raise CollectorError("Hiçbir siteye erişilemedi: " + "; ".join(errors[:4]) if errors else "Site tanımlı değil.")
        self.warnings += errors
        if self.warnings:
            log.warning("Site listesi uyarıları: %s", self.warnings[:6])
        return out

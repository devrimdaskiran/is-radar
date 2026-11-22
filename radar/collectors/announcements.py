"""Duyuru / ilan sayfası izleyici (kamu ilanları için).

Bir liste sayfasını (memurlar.net kategorileri, Polis Akademisi duyuruları, MSB Personel Temin, MİT Kariyer,
Kariyer Kapısı, ...) açar; ilan bağlantılarını desen veya anahtar kelimeyle bulur. Daha önce görülmemiş
her ilanın sayfasını (gerekirse ekli PDF'i) okuyup metnini çıkarır. robots.txt'ye uyar.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..textnorm import fold
from .base import Collector, CollectorError, PoliteClient, RawPosting
from .site_listing import extract_detail_text

log = logging.getLogger(__name__)

DEFAULT_KEYWORDS = ["alim", "alacak", "ilan", "temin", "personel", "pomem", "pmyo", "astsubay", "tekniker",
                    "teknisyen", "isci", "memur", "sozlesmeli", "basvuru"]


def pdf_text(data: bytes, max_pages: int = 25) -> str:
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages[:max_pages])
    except Exception as e:  # noqa: BLE001
        log.info("PDF okunamadı: %s", e)
        return ""


def page_body(html: str) -> str:
    """memurlar.net gibi sayfalarda ilan gövdesini bulur; bulunamazsa genel metin çıkarıcıya düşer."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "iframe"]):
        t.decompose()
    best = None
    for el in soup.find_all(["article", "div", "section"]):
        cls = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if re.search(r"(content|detail|icerik|haber|ilan|article|body|text)", cls, re.I):
            txt = el.get_text("\n", strip=True)
            if len(txt) > 400 and (best is None or len(txt) > len(best)):
                best = txt
    if best and len(best) < 60000:
        return best
    return extract_detail_text(html)[0]


class AnnouncementCollector(Collector):
    name = "kamu_duyurulari"
    method = "Kamu ilan/duyuru sayfaları (memurlar.net, Polis Akademisi, MSB, MİT, Kariyer Kapısı)"

    def __init__(self, cfg, secrets, client: PoliteClient | None = None, known: set[str] | None = None):
        super().__init__(cfg, secrets)
        self.http = client or PoliteClient(delay_s=float(cfg.get("bekleme_sn", 2)))
        self.known = known or set()
        self.warnings: list[str] = []

    def fetch(self) -> list[RawPosting]:
        out, errors, ok = [], [], 0
        max_new = int(self.cfg.get("max_detay", 40))
        opened = 0
        for page in self.cfg.get("sayfalar", []):
            if not page.get("aktif", True):
                continue
            try:
                r = self.http.get(page["url"])
            except Exception as e:  # noqa: BLE001 - bir sayfanın hatası diğerlerini durdurmaz
                errors.append(f"{page['ad']}: {str(e)[:150]}")
                continue
            ok += 1
            pat = re.compile(page["ilan_deseni"]) if page.get("ilan_deseni") else None
            kws = [fold(k) for k in page.get("anahtar", DEFAULT_KEYWORDS)]
            soup = BeautifulSoup(r.text, "html.parser")
            found = 0
            for a in soup.find_all("a", href=True):
                href = urljoin(page["url"], a["href"]).split("#")[0]
                title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
                if len(title) < 8:
                    continue
                if pat:
                    m = pat.search(href)
                    if not m:
                        continue
                    ext = m.group(1) if m.groups() else href
                else:
                    if not any(k in fold(title) for k in kws) or href.rstrip("/") == page["url"].rstrip("/"):
                        continue
                    ext = hashlib.sha1(href.encode()).hexdigest()[:16]
                src = page.get("kaynak", page["ad"])
                key = f"{src}:{ext}"
                if key in self.known:
                    continue
                if page.get("detay", True) and opened >= max_new:
                    continue          # detay kotası doldu: bu ilan bir sonraki turda tam metniyle alınacak
                self.known.add(key)
                found += 1
                post = RawPosting(source=src, external_id=str(ext), url=href, title=title[:250], sector="kamu",
                                  company=page.get("kurum"), city=None, detail_level="özet",
                                  raw={"liste": page["url"]})
                if page.get("detay", True) and opened < max_new:
                    opened += 1
                    try:
                        if href.lower().endswith(".pdf"):
                            post.description = pdf_text(self.http.get(href).content)[:20000]
                        else:
                            d = self.http.get(href)
                            text = page_body(d.text)
                            if len(text) < 600:   # metin kısa ise ekli PDF'e bak
                                ds = BeautifulSoup(d.text, "html.parser")
                                pdfs = [urljoin(href, x["href"]) for x in ds.find_all("a", href=True)
                                        if x["href"].lower().split("?")[0].endswith(".pdf")]
                                if pdfs:
                                    text += "\n" + pdf_text(self.http.get(pdfs[0]).content)
                            post.description = text[:20000]
                            h1 = BeautifulSoup(d.text, "html.parser").find("h1")
                            if h1 and len(h1.get_text(strip=True)) > 8:
                                post.title = h1.get_text(" ", strip=True)[:250]
                        post.detail_level = "tam" if len(post.description) > 300 else "özet"
                    except Exception as e:  # noqa: BLE001
                        log.info("Duyuru detayı okunamadı %s: %s", href, e)
                out.append(post)
            if found == 0 and len(r.text) > 0 and not any(
                    (pat.search(urljoin(page["url"], a["href"])) if pat else False) for a in soup.find_all("a", href=True)):
                errors.append(f"{page['ad']}: sayfa açıldı ama ilan bağlantısı bulunamadı (sayfa JavaScript ile yükleniyor olabilir)")
        if not ok:
            raise CollectorError("Hiçbir kamu sayfasına erişilemedi: " + "; ".join(errors[:4]))
        self.warnings = errors
        return out

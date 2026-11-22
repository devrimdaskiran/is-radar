"""Gemini + Google Arama ile ilan keşfi (yedek kaynak).

Gemini'nin "Google Search grounding" özelliğiyle yeni ilanları arar. Modelin verdiği her bağlantı
gerçekten açılıp doğrulanır; açılmayan ya da ilan sayfası olmayan bağlantılar atılır (uydurma bağlantı gelmez).
Ücretsiz katmanın günlük arama kotası sınırlı olduğu için seyrek çalışır.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date

from ..llm.providers import GeminiProvider, ProviderError
from ..textnorm import fold
from .base import Collector, CollectorError, PoliteClient, RawPosting
from .career_jsonld import parse_jobposting_html
from .site_listing import extract_detail_text

log = logging.getLogger(__name__)

PROMPT = """Bugün {today}. Türkiye'de şu aramaya uyan, YAYINDA OLAN ve son {days} gün içinde yayınlanmış özel sektör iş ilanlarını Google'da ara:
"{query}"
Yalnızca tek bir ilanın kendi sayfasına giden bağlantıları ver (liste/arama sayfası değil). Kariyer.net, Eleman.net, Secretcv, Yenibiris, İşin Olsun, İŞKUR ve şirket kariyer sayfaları olabilir.
Yanıtı yalnızca şu JSON biçiminde ver: {{"ilanlar":[{{"url":"...","baslik":"...","sirket":"...","sehir":"..."}}]}}"""


def _domain_ok(url: str, allowed: list[str]) -> bool:
    return any(d in url for d in allowed)


class GeminiSearchCollector(Collector):
    name = "gemini_arama"
    method = "Gemini + Google Arama (bağlantılar tek tek doğrulanır)"

    def __init__(self, cfg, secrets, provider: GeminiProvider | None = None, client: PoliteClient | None = None):
        super().__init__(cfg, secrets)
        self.provider = provider
        self.http = client or PoliteClient(delay_s=2)

    def fetch(self) -> list[RawPosting]:
        if self.provider is None:
            if not getattr(self.secrets, "gemini_api_key", ""):
                raise CollectorError("GEMINI_API_KEY tanımlı değil. Kaynak pasif kaldı.")
            self.provider = GeminiProvider(self.secrets.gemini_api_key)
        allowed = self.cfg.get("izinli_alanlar", [])
        found: dict[str, dict] = {}
        errors = []
        for q in self.cfg.get("aramalar", []):
            body = {"contents": [{"role": "user", "parts": [{"text": PROMPT.format(
                        today=date.today().strftime("%d.%m.%Y"), days=self.cfg.get("gun", 3), query=q)}]}],
                    "tools": [{"google_search": {}}],
                    "generationConfig": {"temperature": 0.1}}
            try:
                d, _ = self.provider.generate(body, timeout=120)
            except ProviderError as e:
                errors.append(str(e))
                continue
            parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            m = re.search(r"\{.*\}", text, re.S)
            try:
                items = json.loads(m.group(0)).get("ilanlar", []) if m else []
            except json.JSONDecodeError:
                items = []
            for it in items:
                url = str(it.get("url", "")).strip()
                if url.startswith("http") and (not allowed or _domain_ok(url, allowed)):
                    found.setdefault(url.split("?")[0], it)
        if errors and not found:
            raise CollectorError("Gemini araması başarısız: " + "; ".join(errors[:2]))
        out = []
        for url, it in list(found.items())[: int(self.cfg.get("max_dogrulama", 15))]:
            try:
                r = self.http.get(url)
            except Exception as e:  # noqa: BLE001 - açılmayan bağlantı atlanır
                log.info("Doğrulanamayan bağlantı atlandı: %s (%s)", url, e)
                continue
            jl = parse_jobposting_html(r.text, url)
            if jl:
                p = jl[0]
                p.source, p.url = "gemini_arama", url
                p.external_id = re.sub(r"\W+", "-", url)[-120:]
                out.append(p)
                continue
            text, closed = extract_detail_text(r.text)
            if closed or len(text) < 80:
                continue
            title = it.get("baslik") or ""
            if fold(title)[:15] and fold(title)[:15] not in fold(r.text):
                title = (re.search(r"<title>(.*?)</title>", r.text, re.S | re.I) or [None, title])[1] or title
            out.append(RawPosting(source="gemini_arama", external_id=re.sub(r"\W+", "-", url)[-120:], url=url,
                                  title=title.strip()[:200], company=it.get("sirket") or None, city=it.get("sehir") or None,
                                  description=text, detail_level="tam"))
        return out

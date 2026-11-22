"""Toplayıcı (collector) ortak arayüzü."""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)
USER_AGENT = "AICareerRadar/1.0 (kisisel is arama asistani; dusuk hizli)"


@dataclass
class RawPosting:
    source: str
    external_id: str
    url: str
    title: str
    company: str | None = None
    city: str | None = None
    district: str | None = None
    description: str = ""
    published_at: datetime | None = None
    deadline: datetime | None = None
    salary: str | None = None
    work_model: str | None = None
    sector: str | None = None
    detail_level: str = "tam"          # tam | özet (yalnızca başlık/özet var)
    raw: dict = field(default_factory=dict)


class CollectorError(Exception):
    """Kaynağa erişilemedi / yapılandırma eksik. Mesaj Türkçe ve kullanıcıya gösterilir."""


class Collector:
    name: str = "base"
    method: str = ""

    def __init__(self, cfg: dict, secrets):
        self.cfg = cfg
        self.secrets = secrets

    def fetch(self) -> list[RawPosting]:  # pragma: no cover - arayüz
        raise NotImplementedError


class PoliteClient:
    """robots.txt'ye uyan, istekler arasında bekleyen HTTP istemcisi."""

    def __init__(self, delay_s: float = 2.0, timeout: float = 20.0, client: httpx.Client | None = None):
        self.delay_s = delay_s
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last = 0.0

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[base] = rp
        return rp.can_fetch(USER_AGENT, url)

    def get(self, url: str) -> httpx.Response:
        if not self.allowed(url):
            raise CollectorError(f"robots.txt bu adrese izin vermiyor: {url}")
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.client.get(url)
        if r.status_code in (401, 403, 429):
            raise CollectorError(f"Erişim engellendi (HTTP {r.status_code}); kaynak durduruldu: {url}")
        lowered = r.text[:5000].lower()
        if "captcha" in lowered or "cf-challenge" in lowered:
            raise CollectorError(f"CAPTCHA / bot kontrolü görüldü; aşılmaya çalışılmadı: {url}")
        r.raise_for_status()
        return r

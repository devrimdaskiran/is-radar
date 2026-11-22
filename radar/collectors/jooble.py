"""Jooble resmi REST API toplayıcısı.

Belge: https://jooble.org/api/about  (anahtar ücretsiz, form ile alınır)
İstek: POST https://jooble.org/api/{anahtar}  gövde: {"keywords": "...", "location": "..."}
Not: Jooble açıklamaları kısaltılmış "snippet" olarak döner; detay_level = "özet".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx

from ..textnorm import strip_html
from .base import Collector, CollectorError, RawPosting

log = logging.getLogger(__name__)
API = "https://jooble.org/api/{key}"


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _split_location(loc: str | None) -> tuple[str | None, str | None]:
    if not loc:
        return None, None
    parts = [p.strip() for p in re.split(r"[,/]", loc) if p.strip()]
    if len(parts) >= 2:
        return parts[-1], parts[0]
    return parts[0], None


def map_job(j: dict) -> RawPosting:
    city, district = _split_location(j.get("location"))
    return RawPosting(
        source="jooble",
        external_id=str(j.get("id") or j.get("link")),
        url=j.get("link", ""),
        title=strip_html(j.get("title", "")).strip(),
        company=(j.get("company") or None),
        city=city,
        district=district,
        description=strip_html(j.get("snippet", "")),
        published_at=_parse_date(j.get("updated")),
        salary=j.get("salary") or None,
        work_model=j.get("type") or None,
        detail_level="özet",
        raw=j,
    )


class JoobleCollector(Collector):
    name = "jooble"
    method = "Resmi REST API"

    def __init__(self, cfg, secrets, client: httpx.Client | None = None):
        super().__init__(cfg, secrets)
        self.client = client or httpx.Client(timeout=30)

    def fetch(self) -> list[RawPosting]:
        key = self.secrets.jooble_api_key
        if not key:
            raise CollectorError("JOOBLE_API_KEY tanımlı değil (.env). Kaynak pasif kaldı.")
        out: list[RawPosting] = []
        errors = []
        for q in self.cfg.get("aramalar", []):
            body = {"keywords": q.get("kelime", ""), "location": q.get("konum", "")}
            try:
                r = self.client.post(API.format(key=key), json=body)
                if r.status_code in (401, 403):
                    raise CollectorError(f"Jooble anahtarı reddedildi (HTTP {r.status_code}). Kota bitmiş olabilir.")
                r.raise_for_status()
                for j in r.json().get("jobs", []):
                    out.append(map_job(j))
            except CollectorError:
                raise
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{body['keywords']}/{body['location']}: {e}")
        if errors and not out:
            raise CollectorError("Jooble aramaları başarısız: " + "; ".join(errors[:3]))
        return out

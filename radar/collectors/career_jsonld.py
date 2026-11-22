"""Şirket kariyer sayfası toplayıcısı (schema.org JobPosting / JSON-LD).

Birçok kariyer sayfası, Google'da görünmek için ilanlarını JSON-LD olarak sayfaya gömer.
Bu modül liste sayfasından ilan bağlantılarını bulur, her ilan sayfasındaki JobPosting
verisini okur. robots.txt her alan adı için kontrol edilir; CAPTCHA görülürse durur.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..textnorm import strip_html
from .base import Collector, CollectorError, PoliteClient, RawPosting

log = logging.getLogger(__name__)


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _iter_jobpostings(obj):
    if isinstance(obj, list):
        for o in obj:
            yield from _iter_jobpostings(o)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            yield obj
        for k in ("@graph", "itemListElement"):
            if k in obj:
                yield from _iter_jobpostings(obj[k])


def parse_jobposting_html(html: str, url: str, company_hint: str | None = None) -> list[RawPosting]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "{}")
        except json.JSONDecodeError:
            continue
        def first(x):
            while isinstance(x, list):
                x = x[0] if x else {}
            return x if isinstance(x, dict) else ({"name": x} if isinstance(x, str) else {})

        for jp in _iter_jobpostings(data):
            org = first(jp.get("hiringOrganization"))
            loc = first(jp.get("jobLocation"))
            addr = first(loc.get("address")) if loc else {}
            ident = jp.get("identifier")
            if isinstance(ident, list):
                ident = ident[0] if ident else None
            desc = strip_html(str(jp.get("description", "")))
            for extra in ("educationRequirements", "experienceRequirements", "qualifications"):
                v = jp.get(extra)
                if isinstance(v, dict):
                    v = v.get("credentialCategory") or v.get("description") or ""
                if v:
                    desc += f"\n{strip_html(str(v))}"
            salary = jp.get("baseSalary")
            out.append(RawPosting(
                source="kariyer_sayfasi",
                external_id=str(ident.get("value") if isinstance(ident, dict) else (ident or url)),
                url=jp.get("url") or url,
                title=strip_html(str(jp.get("title", ""))),
                company=(org.get("name") if isinstance(org, dict) else None) or company_hint,
                city=addr.get("addressLocality") or addr.get("addressRegion"),
                description=desc,
                published_at=_dt(jp.get("datePosted")),
                deadline=_dt(jp.get("validThrough")),
                salary=json.dumps(salary, ensure_ascii=False) if salary else None,
                work_model=("Uzaktan" if jp.get("jobLocationType") == "TELECOMMUTE" else jp.get("employmentType")),
                detail_level="tam",
                raw={"jsonld": True},
            ))
    return out


class CareerJsonLdCollector(Collector):
    name = "kariyer_sayfalari"
    method = "Şirket kariyer sayfası (JSON-LD, robots.txt'ye uyarak)"

    def __init__(self, cfg, secrets, client: PoliteClient | None = None):
        super().__init__(cfg, secrets)
        self.http = client or PoliteClient()

    def fetch(self) -> list[RawPosting]:
        companies = [c for c in self.cfg.get("sirketler", []) if c.get("liste_url")]
        if not companies:
            raise CollectorError("Hiçbir şirket için liste_url girilmemiş (configs/sources.yaml).")
        out, errors = [], []
        for c in companies:
            try:
                r = self.http.get(c["liste_url"])
                found = parse_jobposting_html(r.text, c["liste_url"], c["ad"])
                if not found and c.get("ilan_link_deseni"):
                    pat = re.compile(c["ilan_link_deseni"])
                    links = {urljoin(c["liste_url"], a["href"]) for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True)
                             if pat.search(a["href"])}
                    for link in list(links)[:30]:
                        found += parse_jobposting_html(self.http.get(link).text, link, c["ad"])
                out += found
            except Exception as e:  # noqa: BLE001 - bir şirketin hatası diğerlerini durdurmaz
                errors.append(f"{c['ad']}: {e}")
        if errors and not out:
            raise CollectorError("; ".join(errors[:5]))
        if errors:
            log.warning("Kariyer sayfası hataları: %s", errors)
        return out

"""Veritabanı sorguları ve doğal dil → filtre çevirisi (internete çıkmaz, LLM gerektirmez)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Application, Evaluation, Feedback, Job
from .extract import PROVINCES
from .textnorm import fold


@dataclass
class JobFilter:
    city: str | None = None
    since_days: float | None = None
    keywords: list[str] = field(default_factory=list)
    min_score: float | None = None
    no_experience: bool = False
    applicable_only: bool = True
    limit: int = 10
    sort: str = "score"            # score | date
    career: bool = False
    company: str | None = None


SECTOR_WORDS = {
    "savunma": ["aselsan", "tusas", "roketsan", "havelsan", "baykar", "tei", "savunma"],
    "otomasyon": ["otomasyon"], "mekatronik": ["mekatronik"], "elektrik": ["elektrik"], "elektronik": ["elektronik"],
    "bakim": ["bakim"], "robot": ["robot"], "cnc": ["cnc"], "teknik destek": ["teknik destek"], "enerji": ["enerji", "gunes"],
    "tekniker": ["tekniker"], "teknisyen": ["teknisyen"], "kalite": ["kalite"], "uretim": ["uretim"], "montaj": ["montaj"],
}
SKILL_WORDS = ["autocad", "arduino", "python", "plc", "proteus", "fusion", "esp32", "c#", "solidworks"]


def parse_query(text: str) -> JobFilter:
    t = fold(text)
    f = JobFilter()
    for p in PROVINCES + ["Nurdağı", "İslahiye", "Nizip", "Şehitkamil", "Şahinbey"]:
        if re.search(r"(?<![a-z])" + re.escape(fold(p)), t):
            f.city = p
            break
    m = re.search(r"son (\d+) (gun|hafta|ay)", t)
    if m:
        n = int(m.group(1))
        f.since_days = n * {"gun": 1, "hafta": 7, "ay": 30}[m.group(2)]
    elif "bugun" in t:
        f.since_days = 1
    elif re.search(r"(son )?bir hafta|bu hafta|haftalik", t):
        f.since_days = 7
    m = re.search(r"(?:en yuksek|en iyi|ilk) (\d+)", t)
    if m:
        f.limit = min(30, int(m.group(1)))
    if re.search(r"deneyim (?:istemeyen|aranmayan|sarti olmayan)|deneyimsiz", t):
        f.no_experience = True
    if re.search(r"kariyer firsat", t):
        f.career = True
        f.applicable_only = False
    for k, words in SECTOR_WORDS.items():
        if re.search(r"(?<![a-z])" + re.escape(k), t):
            f.keywords.extend(words)
    for sk in SKILL_WORDS:
        if sk in t:
            f.keywords.append(sk)
    if re.search(r"yeni|en son|son acilan", t):
        f.sort = "date"
    return f


def latest_evals(s: Session):
    """Her ilanın en son değerlendirmesi."""
    rows = s.execute(select(Job, Evaluation).join(Evaluation, Evaluation.job_id == Job.id).order_by(Evaluation.id)).all()
    latest: dict[int, tuple[Job, Evaluation]] = {}
    for job, ev in rows:
        latest[job.id] = (job, ev)
    return list(latest.values())


def find_jobs(s: Session, f: JobFilter, now: datetime | None = None) -> list[tuple[Job, Evaluation]]:
    now = now or datetime.now()
    hidden = {fb.job_id for fb in s.scalars(select(Feedback).where(Feedback.not_interested.is_(True)))}
    out = []
    for job, ev in latest_evals(s):
        if job.id in hidden or not ev.explanation.get("on_eleme"):
            continue
        if f.career:
            if not ev.career_track or ev.career_track == "Doğrudan uygun":
                continue
        elif f.applicable_only and ev.gate == "Başvurulamaz":
            continue
        if job.status != "aktif":
            continue
        if f.city and fold(f.city) not in fold(f"{job.city or ''} {job.district or ''} {job.title}"):
            continue
        if f.since_days is not None and job.first_seen < now - timedelta(days=f.since_days):
            continue
        hay = fold(f"{job.title} {job.company or ''} {job.description or ''}")
        if f.keywords and not any(fold(k) in hay for k in f.keywords):
            continue
        if f.min_score is not None and ev.score < f.min_score:
            continue
        if f.no_experience:
            d = (job.fields or {}).get("deneyim") or {}
            if d.get("tur") not in ("aranmiyor", "tercihen"):
                continue
        if f.company and fold(f.company) not in fold(job.company or ""):
            continue
        out.append((job, ev))
    key = (lambda x: x[1].score) if f.sort == "score" else (lambda x: x[0].first_seen)
    out.sort(key=key, reverse=True)
    return out[: f.limit]


def stats(s: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    le = latest_evals(s)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ok = [(j, e) for j, e in le if e.explanation.get("on_eleme") and e.gate != "Başvurulamaz" and j.status == "aktif"]
    cities: dict[str, int] = {}
    for j, _ in le:
        if j.city:
            cities[j.city] = cities.get(j.city, 0) + 1
    return {
        "toplam": s.query(Job).count(),
        "bugun": s.query(Job).filter(Job.first_seen >= today0).count(),
        "hafta": s.query(Job).filter(Job.first_seen >= now - timedelta(days=7)).count(),
        "uygun": len([1 for _, e in ok if e.score >= 70]),
        "cok_yuksek": len([1 for _, e in ok if e.score >= 80]),
        "basvurulabilir": len(ok),
        "kariyer": len([1 for _, e in le if e.career_track and e.career_track != "Doğrudan uygun"]),
        "favori": s.query(Feedback).filter(Feedback.favorite.is_(True)).count(),
        "basvuru": s.query(Application).count(),
        "gorusme": s.query(Application).filter(Application.state == "Görüşme bekleniyor").count(),
        "sehirler": sorted(cities.items(), key=lambda x: -x[1])[:3],
    }

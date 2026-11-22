"""Tarama hattı: topla → normalleştir → tekrar önle → ön eleme → şart + puan → bildirim kuyruğu."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .collectors.base import Collector, CollectorError, RawPosting
from .db import Evaluation, Feedback, Job, JobLink, Notification, ScrapeRun, Source
from .extract import extract_fields
from .llm.analyzer import Analyzer, BudgetExceeded
from .llm.providers import ProviderError
from .matching import VERIFY, Check, evaluate, prefilter
from .profile import Profile
from .textnorm import fold

log = logging.getLogger(__name__)
REQ_KEYS = ("egitim", "deneyim", "askerlik", "ehliyet", "son_basvuru", "ikamet", "yas", "vardiya")


def _clean_title(t: str) -> str:
    t = re.sub(r"\(.*?\)", " ", fold(t))
    t = re.sub(r"\s-\s.*$", " ", t)  # "Yol Yardım Teknisyeni - Gaziantep"
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())


def fingerprint(p: RawPosting) -> str:
    """Aynı şirket + aynı unvan + aynı şehir = aynı ilan. Şirket bilinmiyorsa kaynağa özgü kimlik eklenir
    (yanlışlıkla farklı ilanları birleştirmemek için)."""
    comp = re.sub(r"[.,]", " ", fold(p.company or ""))
    comp = re.sub(r"\b(a ?s|ltd|sti|san|tic|ve|anonim|sirketi|limited|sanayi|ticaret)\b", " ", comp)
    comp = " ".join(comp.split())
    base = f"{comp}|{_clean_title(p.title)}|{fold(p.city or '')}"
    if not comp:
        base += f"|{p.source}:{p.external_id}"
    return hashlib.sha256(base.encode()).hexdigest()[:32]


def req_hash(fields: dict) -> str:
    sub = {k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "kanit"})
           for k, v in fields.items() if k in REQ_KEYS}
    return hashlib.sha256(json.dumps(sub, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:32]


def profile_version(profile: Profile, settings: dict) -> str:
    return hashlib.sha256(json.dumps([profile.raw, settings.get("puan_agirliklari")], sort_keys=True,
                                     ensure_ascii=False, default=str).encode()).hexdigest()[:16]


@dataclass
class ScanReport:
    sources_ok: int = 0
    sources_failed: int = 0
    fetched: int = 0
    new_jobs: int = 0
    updated_jobs: int = 0
    evaluated: int = 0
    queued: int = 0
    errors: list | None = None


def upsert(s: Session, p: RawPosting) -> tuple[Job, str]:
    """İlanı kaydeder. Dönüş: (ilan, 'yeni' | 'güncellendi' | 'aynı')."""
    link = s.scalar(select(JobLink).where(JobLink.source == p.source, JobLink.external_id == p.external_id))
    fp = fingerprint(p)
    job = link.job if link else s.scalar(select(Job).where(Job.fingerprint == fp))
    status = "aynı"
    if job is None:
        job = Job(fingerprint=fp, title=p.title, company=p.company, city=p.city, district=p.district,
                  work_model=p.work_model, salary=p.salary, description=p.description, sector=p.sector,
                  published_at=p.published_at, deadline=p.deadline, fields={"_detay": p.detail_level})
        s.add(job)
        s.flush()
        status = "yeni"
    else:
        # Daha zengin bilgi geldiyse (ör. özet yerine tam metin) güncelle
        if len(p.description or "") > len(job.description or ""):
            job.description = p.description
            job.fields = {**(job.fields or {}), "_detay": p.detail_level}
            status = "güncellendi"
        for attr in ("company", "city", "district", "salary", "work_model", "published_at", "deadline"):
            if getattr(job, attr) in (None, "") and getattr(p, attr):
                setattr(job, attr, getattr(p, attr))
        job.last_checked = datetime.now()
    if not link:
        s.add(JobLink(job_id=job.id, source=p.source, external_id=p.external_id, url=p.url,
                      raw={k: v for k, v in (p.raw or {}).items() if k != "block"} | {"block": (p.raw or {}).get("block", "")[:500]}))
    return job, status


def evaluate_job(s: Session, job: Job, profile: Profile, settings: dict, analyzer: Analyzer | None,
                 today: datetime | None = None) -> Evaluation | None:
    if job.sector != "kamu":
        from .textnorm import split_title_company
        t2, comp = split_title_company(job.title)
        if comp:
            job.title, job.company = t2, job.company or comp
    detail = (job.fields or {}).get("_detay", "tam")
    fields = extract_fields(job.title, job.description or "", today)
    fields["_detay"] = detail
    is_kamu = job.sector == "kamu"
    if is_kamu:
        from .kamu import find_deadline
        dl = fields.get("son_basvuru") or find_deadline(f"{job.title}\n{job.description or ''}")
        if dl:
            fields["son_basvuru"] = dl
        ok, why = True, "Kamu ilanı"
    else:
        ok, why = prefilter(job.title, job.description or "", profile, fields, job.published_at,
                            int(settings["tarama"]["ilan_max_yas_gun"]), today)
    new_hash = req_hash(fields) + hashlib.sha256((job.description or "").encode()).hexdigest()[:8]
    job.fields = fields
    if fields.get("son_basvuru") and not job.deadline:
        job.deadline = datetime.fromisoformat(fields["son_basvuru"])
    if job.deadline and job.deadline < (today or datetime.now()).replace(hour=0, minute=0):
        job.status = "süresi_geçti"
    pv = profile_version(profile, settings)
    if job.latest_eval and job.content_hash == new_hash and job.latest_eval.profile_version == pv:
        return None  # değişiklik yok: tekrar analiz yapılmaz
    job.content_hash = new_hash
    if is_kamu:
        from .kamu import evaluate_kamu
        r = evaluate_kamu(job.title, job.description or "", fields, profile, settings, today=today, detail_level=detail)
        if r.extra.get("ilgililik", 0) == 0:
            ok, why = False, "Kamu ilanında senin alanınla ilgili kadro görünmüyor"
    else:
        r = evaluate(job.title, job.description or "", fields, profile, settings, city=job.city, work_model=job.work_model,
                     published=job.published_at, detail_level=detail, today=today)
    engine = "kural"
    if not ok:
        r.category = f"Ön elemede elendi: {why}"
    elif analyzer and analyzer.enabled and r.location_tier in ("ev", "yakin", "uzaktan", "bilinmiyor", "uzak_izlenen"):
        try:
            ai = analyzer.analyze(s, job_text=f"{job.title}\n{job.company or ''}\n{job.description}",
                                  content_hash=new_hash, profile_summary=profile.anonymous_summary(), profile_version=pv,
                                  kamu=is_kamu)
        except (BudgetExceeded, ProviderError) as e:
            ai = None
            r.explanation["dikkat"].append(f"Yapay zekâ analizi yapılamadı ({e}); kural tabanlı sonuç gösteriliyor.")
        if ai:
            engine = f"kural+{analyzer.provider.name}"
            known = {fold(c.evidence or "") for c in r.checks}
            for g in ai.get("gereksinimler", []):
                if fold(g["alinti"]) not in known and g.get("tur") in ("egitim", "deneyim", "askerlik", "ehliyet", "yas", "belge"):
                    r.checks.append(Check(f"AI: {g['tur']}", VERIFY, g.get("aciklama", "Yapay zekânın bulduğu şart"), g["alinti"]))
            for key, ai_key in (("pozisyon", "pozisyon_uyumu"), ("teknik", "teknik_uyum")):
                if isinstance(ai.get(ai_key), (int, float)):
                    r.subscores[key] = round((r.subscores[key] + ai[ai_key]) / 2)
            w = settings["puan_agirliklari"]
            r.score = round(sum(r.subscores[k] * float(w[k]) for k in r.subscores), 1)
            if r.gate == "Uygun" and any(c.status == VERIFY for c in r.checks):
                r.gate = "Doğrulanmalı"
            r.explanation["ai_yorum"] = ai.get("yorum", "")
            if is_kamu and ai.get("uygun_kadrolar"):
                r.explanation["uygun_kadrolar"] = [k for k in ai["uygun_kadrolar"] if isinstance(k, dict)][:5]
            r.explanation["dikkat"] += [d for d in ai.get("dikkat", []) if isinstance(d, str)][:4]
    ev = Evaluation(job_id=job.id, engine=engine, profile_version=pv, score=r.score, subscores=r.subscores, gate=r.gate,
                    category=r.category, career_track=r.career_track if ok else None,
                    checks=[c.as_dict() for c in r.checks],
                    explanation={**r.explanation, "konum_seviyesi": r.location_tier, "on_eleme": ok, "on_eleme_nedeni": why,
                                 "eksik_beceriler": r.extra.get("eksik_beceriler", [])})
    s.add(ev)
    job.evaluations.append(ev)
    return ev


def maybe_queue(s: Session, job: Job, ev: Evaluation, settings: dict, change: str) -> bool:
    fb = s.get(Feedback, job.id)
    if fb and fb.not_interested:
        return False
    if not ev.explanation.get("on_eleme") or ev.gate == "Başvurulamaz" or job.status != "aktif":
        return False
    min_score = settings["bildirim"].get("kamu_min_puan", 55) if job.sector == "kamu" else settings["bildirim"]["min_puan"]
    if ev.score < float(min_score):
        return False
    prev = s.scalars(select(Notification).where(Notification.job_id == job.id).order_by(Notification.id)).all()
    if not prev:
        s.add(Notification(job_id=job.id, reason="yeni", content_hash=job.content_hash))
        return True
    if change == "güncellendi" and prev[-1].content_hash[:32] != job.content_hash[:32]:
        s.add(Notification(job_id=job.id, reason="güncelleme", content_hash=job.content_hash))
        return True
    return False


def run_collector(s: Session, col: Collector) -> tuple[list[RawPosting], str | None]:
    src = s.scalar(select(Source).where(Source.name == col.name))
    run = ScrapeRun(source_id=src.id)
    s.add(run)
    src.last_run = datetime.now()
    try:
        items = col.fetch()
        run.ok, run.found = True, len(items)
        src.last_success, src.last_count, src.last_error = datetime.now(), len(items), None
        src.success_runs += 1
        src.total_found += len(items)
        src.status = "çalışıyor"
        warns = getattr(col, "warnings", None)
        if warns:
            src.last_error = "Uyarı (kaynak çalışıyor): " + "; ".join(warns[:3])
        return items, None
    except CollectorError as e:
        msg = str(e)
    except Exception as e:  # noqa: BLE001 - beklenmeyen hata da diğer kaynakları durdurmaz
        msg = f"Beklenmeyen hata: {e}"
        log.error("Toplayıcı %s çöktü:\n%s", col.name, traceback.format_exc())
    run.ok, run.error = False, msg
    src.last_error = msg
    src.status = "yapılandırılmadı" if "tanımlı değil" in msg or "girilmemiş" in msg else "hata"
    return [], msg
    # (run.finished finally'de atanır)


def scan(s: Session, collectors: list[Collector], profile: Profile, settings: dict, analyzer: Analyzer | None = None,
         today: datetime | None = None, deadline: datetime | None = None) -> ScanReport:
    """deadline: bu zamandan sonra yeni kaynağa başlanmaz, yapay zekâ analizi atlanır (tur süresini sınırlamak için).
    Atlanan kaynaklar bir sonraki turda zamanı geldiği için yeniden denenir."""
    rep = ScanReport(errors=[])
    for col in collectors:
        if deadline and datetime.now() > deadline:
            rep.errors.append(f"{col.name}: tur süresi doldu, bir sonraki turda taranacak")
            continue
        items, err = run_collector(s, col)
        if err:
            rep.sources_failed += 1
            rep.errors.append(f"{col.name}: {err}")
            s.commit()
            continue
        rep.sources_ok += 1
        rep.fetched += len(items)
        new_count = 0
        for p in items:
            if not p.title:
                continue
            with s.no_autoflush:
                job, change = upsert(s, p)
            s.flush()
            if change == "yeni":
                rep.new_jobs += 1
                new_count += 1
            elif change == "güncellendi":
                rep.updated_jobs += 1
            use_ai = analyzer if not (deadline and datetime.now() > deadline) else None
            ev = evaluate_job(s, job, profile, settings, use_ai, today)
            if ev:
                rep.evaluated += 1
                if maybe_queue(s, job, ev, settings, change):
                    rep.queued += 1
        last = s.scalar(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(1))
        if last:
            last.new, last.finished = new_count, datetime.now()
        s.commit()
    return rep

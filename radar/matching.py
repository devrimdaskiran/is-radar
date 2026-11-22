"""Ön eleme, zorunlu şart kapısı, ağırlıklı uyum puanı ve açıklama.

Zorunlu şartlar puandan AYRI değerlendirilir: bir şart "Karşılamıyor" ise ilan puanı ne olursa
olsun "Başvurulamaz" olur ve bildirim gitmez.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .extract import LEVEL_RANK, LEVEL_TR, find_skills
from .profile import Profile
from .textnorm import fold

OK, NO, VERIFY, UNKNOWN = "Karşılıyor", "Karşılamıyor", "Doğrulanmalı", "Belirtilmemiş"
STATUS_SCORE = {OK: 100, UNKNOWN: 80, VERIFY: 50, NO: 0}

# Profildeki diploma programlarının ilanlardaki karşılıkları
PROGRAM_CANON = {
    "mekatronik": "Mekatronik",
    "elektrik elektronik teknolojisi": "Elektrik-Elektronik",
    "elektrik-elektronik teknolojisi": "Elektrik-Elektronik",
    "elektrik elektronik": "Elektrik-Elektronik",
    "bilgisayar programciligi": "Bilgisayar",
}
# Diploma ile "yakın" sayılan ilan bölümleri: diploma yerine geçmez, Doğrulanmalı üretir
NEAR = {
    "Elektrik-Elektronik": {"Elektrik", "Elektronik", "Elektronik ve Haberleşme"},
    "Mekatronik": {"Otomasyon", "Kontrol ve Otomasyon", "Endüstriyel Otomasyon", "Robotik", "Endüstriyel Bakım"},
}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    evidence: str | None = None

    def as_dict(self):
        return {"ad": self.name, "durum": self.status, "aciklama": self.detail, "kanit": self.evidence}


@dataclass
class Result:
    score: float
    subscores: dict
    gate: str
    category: str
    career_track: str | None
    checks: list[Check]
    explanation: dict
    prefilter_ok: bool = True
    prefilter_reason: str = ""
    location_tier: str = "bilinmiyor"
    extra: dict = field(default_factory=dict)


def canon_program(p: str) -> str:
    return PROGRAM_CANON.get(fold(p).replace("-", " "), PROGRAM_CANON.get(fold(p), p))


def location_tier(city: str | None, profile: Profile, fields: dict, work_model: str | None) -> str:
    wm = fold(work_model or fields.get("calisma_modeli") or "")
    if "uzaktan" in wm:
        return "uzaktan"
    cands = [city] if city else []
    cands += fields.get("metindeki_iller", []) if not city else []
    for c in cands:
        fc = fold(c or "")
        for tier in ("ev", "yakin", "uzak_izlenen"):
            if any(fold(x) in fc or fc in fold(x) for x in profile.regions.get(tier, []) if fc):
                return tier
    return "diger" if cands else "bilinmiyor"


def prefilter(title: str, description: str, profile: Profile, fields: dict, published: datetime | None,
              max_age_days: int = 45, today: datetime | None = None) -> tuple[bool, str]:
    t = fold(title)
    if any(b in t for b in profile.blocked_titles):
        return False, "Engellenen unvan"
    today = today or datetime.now()
    if published and (today - published).days > max_age_days:
        return False, f"İlan {max_age_days} günden eski"
    if fields.get("son_basvuru"):
        try:
            if datetime.fromisoformat(fields["son_basvuru"]) < today.replace(hour=0, minute=0, second=0, microsecond=0):
                return False, "Son başvuru tarihi geçmiş"
        except ValueError:
            pass
    kw = [fold(k) for k in profile.title_keywords] + [fold(x) for x in profile.targets]
    if any(re.search(r"(?<![a-z])" + re.escape(k), t) for k in kw):
        return True, "Unvan eşleşti"
    edu = fields.get("egitim") or {}
    mine = {canon_program(d["program"]) for d in profile.diplomas} | set(profile.related_departments)
    if any(d["ad"] in mine and not d["muhendislik"] for d in edu.get("bolumler", [])):
        return True, "Aranan bölüm eğitiminle örtüşüyor"
    return False, "Unvan ve bölüm ilgisiz"


def check_education(fields: dict, profile: Profile) -> tuple[Check, float, list[str]]:
    edu = fields.get("egitim")
    matched_notes: list[str] = []
    if not edu:
        return Check("Eğitim", UNKNOWN, "İlanda eğitim şartı belirtilmemiş."), 65, matched_notes
    min_lv = edu.get("min_seviye")
    min_rank = LEVEL_RANK.get(min_lv, 0)
    depts = edu.get("bolumler", [])
    nonEng = {d["ad"] for d in depts if not d["muhendislik"]}
    engOnly = {d["ad"] for d in depts if d["muhendislik"]} - nonEng
    related = edu.get("ilgili_bolum_ifadesi")
    best = None  # (status, score, detail, evidence)
    for dip in profile.diplomas:
        rank = LEVEL_RANK.get(dip["seviye"], 0)
        prog = canon_program(dip["program"])
        lvl_ok = rank >= min_rank if min_lv else True
        if not lvl_ok:
            cand = (NO, 0, f"İlan en az {LEVEL_TR[min_lv]} istiyor; {dip['program']} diploman {LEVEL_TR[dip['seviye']]}.",
                    edu.get("kanit"))
        elif not depts:
            cand = (OK, 85, f"{LEVEL_TR[dip['seviye']].capitalize()} {dip['program']} diploman seviye şartını karşılıyor; "
                    f"bölüm belirtilmemiş.", edu.get("kanit"))
        elif prog in nonEng:
            ev = next(d["kanit"] for d in depts if d["ad"] == prog and not d["muhendislik"])
            cand = (OK, 100, f"İlan {prog} bölümünü açıkça kabul ediyor; {LEVEL_TR[dip['seviye']]} {dip['program']} diploman var.", ev)
        elif nonEng & NEAR.get(prog, set()) or (related and (prog in nonEng | engOnly or nonEng)):
            near = ", ".join(sorted(nonEng & NEAR.get(prog, set()))) or "ilgili bölümler"
            cand = (VERIFY, 55, f"İlan {near} istiyor; {dip['program']} diplomanın kabul edildiği açıkça yazmıyor. "
                    f"Diploma uygunluğu doğrulanmalı.", related or edu.get("kanit"))
        elif prog in engOnly:
            cand = (NO, 0, f"İlan {prog} Mühendisliği (lisans) istiyor; ön lisans/lise {prog} bunun yerine geçmez.", edu.get("kanit"))
        else:
            cand = (NO, 0, f"İlanın aradığı bölümler ({', '.join(sorted(nonEng | engOnly))}) diplomalarınla örtüşmüyor.",
                    edu.get("kanit"))
        if best is None or STATUS_SCORE[cand[0]] + cand[1] > STATUS_SCORE[best[0]] + best[1]:
            best = cand
        if cand[0] == OK:
            matched_notes.append(cand[2])
    status, score, detail, ev = best
    return Check("Eğitim", status, detail, ev), score, matched_notes


def evaluate(title: str, description: str, fields: dict, profile: Profile, settings: dict, *,
             city: str | None = None, work_model: str | None = None, published: datetime | None = None,
             detail_level: str = "tam", today: datetime | None = None) -> Result:
    w = settings["puan_agirliklari"]
    text = f"{title}\n{description}"
    checks: list[Check] = []
    explanation = {"neden_uygun": [], "egitim_ortusen": [], "beceri_ortusen": [], "karsilanmayan": [],
                   "eksik_bilgi": [], "basvuru": "", "dikkat": [], "kanitlar": []}

    # Eğitim
    edu_check, E, edu_notes = check_education(fields, profile)
    checks.append(edu_check)
    explanation["egitim_ortusen"] = edu_notes

    # Deneyim
    exp = fields.get("deneyim")
    if not exp:
        D = 75
        checks.append(Check("Deneyim", UNKNOWN, "Deneyim şartı belirtilmemiş."))
    elif exp["tur"] == "aranmiyor":
        D = 100
        checks.append(Check("Deneyim", OK, "Deneyim şartı aranmıyor.", exp["kanit"]))
    elif exp["tur"] == "tercihen":
        D = max(35, 90 - 15 * exp["yil"])
        checks.append(Check("Deneyim", OK, f"{exp['yil']} yıl deneyim 'tercihen' isteniyor (zorunlu değil); "
                            "2 stajın artı sayılabilir.", exp["kanit"]))
        explanation["dikkat"].append(f"Tercihen {exp['yil']} yıl deneyim isteniyor; stajlarını öne çıkar.")
    elif exp["tur"] == "zorunlu":
        if profile.experience_years >= exp["yil"]:
            D = 100
            checks.append(Check("Deneyim", OK, f"En az {exp['yil']} yıl isteniyor.", exp["kanit"]))
        else:
            D = 5
            checks.append(Check("Deneyim", NO, f"En az {exp['yil']} yıl iş deneyimi zorunlu; senin iş deneyimin yok "
                                "(stajlar iş deneyimi sayılmaz).", exp["kanit"]))
    else:
        D = 50
        checks.append(Check("Deneyim", VERIFY, "'Deneyimli' isteniyor ama süre yazmıyor; stajlarının yeterli olup "
                            "olmadığı doğrulanmalı.", exp["kanit"]))

    # Askerlik
    ask = fields.get("askerlik")
    if ask:
        left = profile.military_deferral_years_left((today or datetime.now()).date())
        status = profile.military.get("durum")
        if status in ("yapildi", "muaf"):
            checks.append(Check("Askerlik", OK, "Askerlik şartı karşılanıyor.", ask["kanit"]))
        elif ask["tecil_yil"] is not None and left is not None:
            ok = left >= ask["tecil_yil"]
            checks.append(Check("Askerlik", OK if ok else NO,
                                f"En az {ask['tecil_yil']} yıl tecil isteniyor; tecilinin bitmesine yaklaşık {left:.1f} yıl var.",
                                ask["kanit"]))
        elif ask["tamamlanmis_sart"] and not ask["tecil_kabul"]:
            checks.append(Check("Askerlik", NO, f"Askerliğini tamamlamış olmak şartı var; sen "
                                f"{profile.military.get('tecil_bitis')} tarihine kadar tecillisin.", ask["kanit"]))
        elif ask["iliski_olmayan"] or ask["tamamlanmis_sart"]:
            checks.append(Check("Askerlik", VERIFY, "Askerlik şartı var; tecilin kabul edilip edilmediğini firmaya sor.",
                                ask["kanit"]))
        else:
            checks.append(Check("Askerlik", VERIFY, "Askerlikle ilgili bir ifade var; durumunu doğrula.", ask["kanit"]))
    else:
        checks.append(Check("Askerlik", UNKNOWN, "Askerlik şartı belirtilmemiş."))

    # Ehliyet
    eh = fields.get("ehliyet")
    if eh:
        need = set(eh["siniflar"]) or {"B"}
        have = set(profile.licenses)
        missing = need - have
        if not missing:
            checks.append(Check("Ehliyet", OK, f"Ehliyet ({', '.join(sorted(need))}) var.", eh["kanit"]))
        else:
            checks.append(Check("Ehliyet", VERIFY if eh["tercihen"] else NO,
                                f"{', '.join(sorted(missing))} sınıfı ehliyet isteniyor; sende yok.", eh["kanit"]))
    # Vardiya
    if fields.get("vardiya"):
        checks.append(Check("Vardiya", OK if profile.shift_ok else NO,
                            "Vardiyalı çalışma var; vardiyaya uygun olduğunu belirttin." if profile.shift_ok
                            else "Vardiyalı çalışma var; tercihin değil.", fields["vardiya"]["kanit"]))
    # İkamet
    ik = fields.get("ikamet")
    if ik and ik.get("il"):
        same = fold(ik["il"]) == fold(profile.city)
        checks.append(Check("İkamet", OK if same else (VERIFY if ik["kabul_edebilecek"] else NO),
                            f"{ik['il']}'de ikamet şartı var.", ik["kanit"]))
    # Yaş
    if fields.get("yas"):
        checks.append(Check("Yaş", VERIFY, f"Yaş şartı var ({fields['yas']['aralik']}); profilinde yaş yok.", fields["yas"]["kanit"]))
    # Dil
    if fields.get("dil"):
        checks.append(Check("Yabancı dil", OK if fields["dil"]["tercihen"] else VERIFY,
                            "İngilizce seviyen 'iyi' (beyan, belge yok)." + ("" if fields["dil"]["tercihen"]
                                                                            else " Zorunluysa sınav yapılabilir."),
                            fields["dil"]["kanit"]))
    # Belge (MYK vb.)
    if fields.get("belge") and not fields["belge"]["alternatif"]:
        checks.append(Check("Belge/Sertifika", VERIFY, "Bir belge/sertifika isteniyor; sende olup olmadığını kontrol et.",
                            fields["belge"]["kanit"]))

    # Beceriler
    have_sk = find_skills(text, profile.skills)
    miss_sk = find_skills(text, profile.watch_missing)
    explanation["beceri_ortusen"] = [f"{s['ad']} ({s['seviye']})" for s in have_sk]
    m, n = len(have_sk), len(miss_sk)
    T = 60 if m + n == 0 else max(20, min(100, round(100 * (m + 0.3) / (m + n + 0.3))))
    for s in miss_sk:
        explanation["dikkat"].append(f"{s['ad']} isteniyor/geçiyor; profilinde yok.")
        explanation["kanitlar"].append(s["kanit"])

    # Pozisyon
    t = fold(title)
    P = 30
    for tg in profile.targets:
        ft = fold(tg)
        if ft in t:
            P = 100
            break
        words = [x for x in re.split(r"[\s\-/]+", ft) if len(x) > 2]
        overlap = sum(1 for x in words if x in t) / max(1, len(words))
        P = max(P, round(40 + 50 * overlap) if overlap >= 0.5 else P)
    if P < 65 and any(re.search(r"(?<![a-z])" + re.escape(fold(k)), t) for k in profile.title_keywords):
        P = 65

    # Konum
    tier = location_tier(city, profile, fields, work_model)
    K = {"ev": 100, "yakin": 75, "uzak_izlenen": 45, "diger": 25, "uzaktan": 90, "bilinmiyor": 50}[tier]
    perks = [p["ad"] for p in fields.get("yan_haklar", [])]
    if tier in ("uzak_izlenen", "diger") and any(p in perks for p in ("lojman", "konaklama", "servis")):
        K += 15
    if tier in ("diger", "uzak_izlenen") and not profile.relocate_private:
        explanation["dikkat"].append("İlan yakın bölgende değil; özel sektörde taşınmak istemediğini belirttin.")

    others = [c for c in checks if c.name not in ("Eğitim", "Deneyim")]
    O = round(sum(STATUS_SCORE[c.status] for c in others) / len(others)) if others else 80

    sub = {"egitim": E, "teknik": T, "pozisyon": P, "deneyim": D, "konum": K, "diger": O}
    score = round(sum(sub[k] * float(w[k]) for k in sub), 1)

    statuses = [c.status for c in checks]
    gate = "Başvurulamaz" if NO in statuses else ("Doğrulanmalı" if VERIFY in statuses else "Uygun")
    category = ("Mükemmel uyum" if score >= 90 else "Çok yüksek uyum" if score >= 80 else "Uygun" if score >= 70
                else "Kısmen uygun" if score >= 50 else "Düşük uyum")
    if gate == "Başvurulamaz":
        category = "Başvurulamaz (zorunlu şart)"

    # Kariyer fırsatı sınıflandırması
    failed = {c.name for c in checks if c.status == NO}
    track = None
    if gate != "Başvurulamaz" and score >= 70 and P >= 65:
        track = "Doğrudan uygun"
    elif failed == {"Deneyim"} and E >= 55:
        track = "Deneyim kazanılması gerekiyor"
    elif failed == {"Askerlik"} and E >= 55:
        track = "Askerlik sonrası uygun"
    elif not failed and n and P >= 65:
        track = "Ek sertifika gerekiyor" if fields.get("belge") else "Küçük bir eğitimle geçiş mümkün"
    elif failed == {"Eğitim"} and P >= 65:
        track = "Uzun vadeli kariyer fırsatı"

    # Açıklama
    if edu_check.status == OK:
        explanation["neden_uygun"].append(edu_check.detail)
    if have_sk:
        explanation["neden_uygun"].append("İlanda geçen becerilerin: " + ", ".join(s["ad"] for s in have_sk[:6]) + ".")
    if P >= 85:
        explanation["neden_uygun"].append("Pozisyon kariyer hedeflerinle doğrudan örtüşüyor.")
    if tier == "ev":
        explanation["neden_uygun"].append("İş yeri senin bölgende.")
    explanation["karsilanmayan"] = [c.detail for c in checks if c.status == NO]
    explanation["eksik_bilgi"] = [c.name for c in checks if c.status == UNKNOWN]
    for c in checks:
        if c.status == VERIFY:
            explanation["dikkat"].append(c.detail)
        if c.evidence:
            explanation["kanitlar"].append(c.evidence)
    if detail_level != "tam":
        explanation["dikkat"].append("İlanın tam metni alınamadı (yalnızca başlık/özet); şartları bağlantıdan kontrol et.")
    explanation["basvuru"] = (
        "Başvurma: zorunlu şartlardan en az biri karşılanmıyor." if gate == "Başvurulamaz" else
        "Başvurmaya değer; işaretli noktaları önce doğrula." if gate == "Doğrulanmalı" and score >= 60 else
        "Başvurmaya değer." if gate == "Uygun" and score >= 70 else
        "Kısmen uygun; ilgini çekiyorsa değerlendir.")
    explanation["kanitlar"] = list(dict.fromkeys(e for e in explanation["kanitlar"] if e))[:8]
    explanation["dikkat"] = list(dict.fromkeys(explanation["dikkat"]))

    return Result(score=score, subscores=sub, gate=gate, category=category, career_track=track, checks=checks,
                  explanation=explanation, location_tier=tier, extra={"eksik_beceriler": [s["ad"] for s in miss_sk]})

"""Kamu ilanı değerlendirmesi.

Kamu ilanları özel sektör ilanlarından farklıdır: tek ilanda onlarca kadro, KPSS puan türü, yaş sınırı,
"ön lisans programlarının herhangi birinden mezun" gibi geniş şartlar olur. Bu yüzden ayrı kurallarla
değerlendirilir. Konum elemesi yapılmaz (kamuda taşınmaya açık).
"""
from __future__ import annotations

import re
from datetime import date, datetime

from .extract import LEVEL_RANK
from .matching import NO, OK, UNKNOWN, VERIFY, Check, Result, check_education
from .profile import Profile
from .textnorm import fold, sentences

# Adayın diplomalarının kamu ilanlarında geçebilecek adları
PROGRAM_PATTERNS = [
    ("Mekatronik", r"mekatronik"),
    ("Elektrik-Elektronik", r"elektrik[\s\-/]*(?:ve\s+)?elektronik|elektrik elektronik teknolojisi"),
    ("Elektrik", r"(?<![a-z])elektrik(?![a-z\s\-/]*elektronik)"),
    ("Elektronik", r"(?<![a-z])elektronik(?: teknolojisi)?(?![a-z])"),
    ("Otomasyon", r"kontrol ve otomasyon|endustriyel otomasyon|otomasyon"),
    ("Elektronik Haberleşme", r"elektronik haberlesme"),
]
BROAD_ONLISANS = (r"(?:on ?lisans|onlisans)[^.\n]{0,80}(?:herhangi bir|tum|butun)[^.\n]{0,40}(?:bolum|program|alan)|"
                  r"(?:herhangi bir|tum|butun) (?:on ?lisans|onlisans) program|bolum (?:sarti|ayrimi) (?:aranmaz|yoktur|olmaksizin)|"
                  r"(?:on ?lisans|onlisans) mezunu (?:[\d.]+ (?:erkek|kadin))")
SPECIAL = [
    ("POMEM (Polis Meslek Eğitim Merkezi)", r"pomem|polis meslek egitim"),
    ("Astsubay temini", r"astsubay"),
    ("Uzman erbaş / sözleşmeli er", r"uzman erbas|sozlesmeli er(?![a-z])"),
    ("Teknisyen/Tekniker kadrosu", r"(?<![a-z])(?:tekniker|teknisyen)(?![a-z])"),
    ("Elektrik/bakım işçisi", r"elektrikci|elektrik ustasi|elektrik bakim|bakim onarim"),
]
TR_MONTHS = {"ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7, "agustos": 8,
             "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12}


def find_deadline(text: str) -> str | None:
    f = fold(text)
    m = re.search(r"son basvuru(?: tarihi)?[^0-9]{0,25}(\d{1,2})[ .](ocak|subat|mart|nisan|mayis|haziran|temmuz|agustos|eylul|ekim|kasim|aralik)[ .](\d{4})", f)
    if m:
        try:
            return date(int(m.group(3)), TR_MONTHS[m.group(2)], int(m.group(1))).isoformat()
        except ValueError:
            return None
    m = re.search(r"son basvuru(?: tarihi)?[^0-9]{0,25}(\d{1,2})[./](\d{1,2})[./](\d{4})", f)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


EDU_CTX = r"mezun|program|bolum|teknolojisi|teknikeri|teknikerligi|teknisyen|on ?lisans|onlisans|meslek yuksek|lise|alan(?:in|i)dan"
NOT_DEPT = r"elektronik (?:ortam|posta|imza|basvuru|yolla|sistem|tebligat|belge)|e-?devlet|elektrik (?:fatura|kesinti|abone)"


def program_hits(text: str) -> list[tuple[str, str]]:
    """Adayın programlarının ilanda EĞİTİM ŞARTI olarak geçtiği cümleler (mühendislik bölümleri sayılmaz)."""
    hits = []
    for name, pat in PROGRAM_PATTERNS:
        for s in sentences(text):
            fs = fold(s)
            if not re.search(EDU_CTX, fs):
                continue
            # lisans / mühendislik bölümlerini sayan cümleler (ön lisans geçmiyorsa) senin için değil
            if re.search(r"en az (?:dort|4) yillik|muhendislik|fakultelerin|(?<!on )(?<![a-z])lisans(?![a-z])", fs) and \
                    not re.search(r"on ?lisans|onlisans|meslek yuksek|myo|teknikerlik", fs):
                continue
            clean = re.sub(NOT_DEPT, " ", fs)
            for m in re.finditer(pat, clean):
                tail = clean[m.end():m.end() + 25]
                if re.match(r"\s*(?:ve [a-z]+ )?(?:muhendis|bolumu \(lisans|lisans)", tail):
                    continue
                hits.append((name, s.strip()[:300]))
                break
            if hits and hits[-1][0] == name:
                break
    return hits


def _quote(text: str, pattern: str) -> str | None:
    for s in sentences(text):
        if re.search(pattern, fold(s)):
            return s.strip()[:300]
    return None


def kamu_profile(profile: Profile) -> dict:
    return profile.raw.get("kamu", {}) or {}


def evaluate_kamu(title: str, description: str, fields: dict, profile: Profile, settings: dict, *,
                  today: datetime | None = None, detail_level: str = "tam") -> Result:
    today = today or datetime.now()
    text = f"{title}\n{description}"
    ft = fold(text)
    kp = kamu_profile(profile)
    checks: list[Check] = []
    ex = {"neden_uygun": [], "egitim_ortusen": [], "beceri_ortusen": [], "karsilanmayan": [], "eksik_bilgi": [],
          "basvuru": "", "dikkat": [], "kanitlar": []}

    # --- İlgililik
    programs = program_hits(text)
    broad = re.search(BROAD_ONLISANS, ft)
    specials = [(n, _quote(text, p)) for n, p in SPECIAL if re.search(p, ft)]
    rel = 0
    if programs:
        rel += 50
        ex["neden_uygun"].append("İlanda senin alanın geçiyor: " + ", ".join(n for n, _ in programs[:4]) + ".")
        ex["kanitlar"] += [q for _, q in programs if q][:3]
    if broad:
        specific_elsewhere = re.search(r"program(?:in|lar)?(?:in)?dan|bolum(?:u|ler)?(?:n)?den", ft) and not programs
        strong = re.search(r"herhangi bir|tum (?:on ?lisans|bolum|program)|butun|bolum (?:sarti|ayrimi)|pomem|polis meslek", ft)
        rel += 35 if (strong or not specific_elsewhere) else 10
        ex["neden_uygun"].append("Ön lisans mezunları için bölüm ayrımı olmayan kadro var.")
        q = _quote(text, BROAD_ONLISANS)
        if q:
            ex["kanitlar"].append(q)
    if specials:
        rel += 20
        ex["neden_uygun"].append("Kadro türü: " + ", ".join(n for n, _ in specials[:3]) + ".")
        ex["kanitlar"] += [q for _, q in specials if q][:2]

    # --- Eğitim seviyesi
    edu = fields.get("egitim") or {}
    levels = set(edu.get("seviyeler", []))
    onlisans_kadro = re.search(r"(?:on ?lisans|onlisans|meslek yuksek ?okul|myo)[^.\n]{0,60}(?:mezun|program|kadro|pozisyon|\))|"
                               r"\((?:on ?lisans|onlisans)\)|(?:programci|tekniker|teknisyen)[^.\n]{0,15}\(on ?lisans", ft)
    explicit_lisans = re.search(r"en az (?:dort|4) yillik|en az lisans|(?:4|dort) yillik (?:lisans )?(?:egitim veren|fakulte)", ft)
    if explicit_lisans and not onlisans_kadro and not broad:
        checks.append(Check("Eğitim", NO, "İlan en az 4 yıllık (lisans) mezuniyet istiyor; ön lisans diploman yetmiyor.",
                            _quote(text, r"en az (?:dort|4) yillik|en az lisans|(?:4|dort) yillik")))
    elif levels and levels <= {"lisans", "yuksek_lisans"}:
        checks.append(Check("Eğitim", NO, "İlandaki kadrolar yalnızca lisans mezunlarına açık görünüyor.", edu.get("kanit")))
    elif "on_lisans" in levels or broad:
        if not programs and not broad:
            rel = min(rel, 15)   # ön lisans kadrosu var ama senin bölümün geçmiyor: düşük öncelik
        checks.append(Check("Eğitim", OK if programs or broad else VERIFY,
                            "Ön lisans mezunlarına kadro var." + ("" if programs or broad else " Bölümünün kabul edildiğini doğrula."),
                            edu.get("kanit")))
    elif "lise" in levels:
        checks.append(Check("Eğitim", OK, "Lise/ortaöğretim mezunlarına kadro var (ön lisans diploman üst öğrenim).",
                            edu.get("kanit")))
    else:
        checks.append(Check("Eğitim", UNKNOWN, "Öğrenim şartı metinde net değil (ekli tabloda olabilir)."))

    # --- KPSS
    if re.search(r"kpss", ft):
        if re.search(r"kpss (?:sarti|puani) (?:aranmaz|aranmayacak|yoktur)|kpss sartsiz", ft):
            checks.append(Check("KPSS", OK, "KPSS şartı aranmıyor.", _quote(text, r"kpss (?:sarti|puani) (?:aranmaz|aranmayacak|yoktur)|kpss sartsiz")))
        else:
            ptypes = sorted(set(re.findall(r"kpss\s*p\s?(\d{1,3})|p(9[34]|3)(?![0-9])", ft)) - {("", "")})
            pts = sorted({a or b for a, b in ptypes})
            mins = re.findall(r"(\d{2})(?:[,.]\d+)? (?:ve uzeri )?puan", ft)
            mine = kp.get("kpss_p93")
            q = _quote(text, r"kpss")
            if mine is None:
                checks.append(Check("KPSS", VERIFY, f"KPSS isteniyor{(' (P' + ', P'.join(pts) + ')') if pts else ''}; "
                                    "profilinde KPSS puanın yok, kendin kontrol et.", q))
            else:
                need = min((int(x) for x in mins if 40 <= int(x) <= 100), default=None)
                if need and float(mine) < need:
                    checks.append(Check("KPSS", NO, f"En az {need} KPSS puanı isteniyor; senin P93 puanın {mine}.", q))
                else:
                    checks.append(Check("KPSS", OK if need else VERIFY,
                                        f"KPSS P93 puanın {mine}" + (f"; istenen en az {need}." if need else "; istenen puan türünü kontrol et."), q))

    # --- Yaş
    m = re.search(r"(\d{2}) yasini (?:doldurmamis|tamamlamamis|bitirmemis|gecmemis)|(\d{2}) yasindan (?:gun almamis|buyuk olmamak)", ft)
    if m:
        lim = int(m.group(1) or m.group(2))
        dob = kp.get("dogum_tarihi")
        q = _quote(text, r"yasini|yasindan")
        if dob:
            y, mo, d = (int(x) for x in str(dob).split("-"))
            age = (today.date() - date(y, mo, d)).days / 365.25
            checks.append(Check("Yaş", OK if age < lim else NO, f"Yaş sınırı {lim}; sen {int(age)} yaşındasın.", q))
        else:
            checks.append(Check("Yaş", VERIFY, f"{lim} yaş sınırı var; profilinde doğum tarihi yok.", q))

    # --- Deneyim / askerlik / ehliyet: genel kurallar
    from .matching import evaluate as _ev_private
    priv = _ev_private(title, description, fields, profile, settings, city=None, today=today, detail_level=detail_level)
    for c in priv.checks:
        if c.name in ("Deneyim", "Askerlik", "Ehliyet") and c.status != UNKNOWN:
            checks.append(c)

    # --- Son başvuru
    dl = fields.get("son_basvuru") or find_deadline(text)
    if dl:
        fields["son_basvuru"] = dl
        if date.fromisoformat(dl) < today.date():
            checks.append(Check("Son başvuru", NO, f"Son başvuru tarihi geçmiş ({dl})."))

    statuses = [c.status for c in checks]
    gate = "Başvurulamaz" if NO in statuses else ("Doğrulanmalı" if VERIFY in statuses else "Uygun")
    edu_s = {OK: 100, VERIFY: 60, UNKNOWN: 55, NO: 0}[next(c.status for c in checks if c.name == "Eğitim")]
    score = round(min(100, 0.55 * min(100, rel) + 0.45 * edu_s), 1) if rel else round(0.2 * edu_s, 1)
    sub = {"egitim": edu_s, "teknik": min(100, rel), "pozisyon": min(100, rel), "deneyim": priv.subscores["deneyim"],
           "konum": 100, "diger": 80}
    category = "Kamu – " + ("Başvurulamaz (zorunlu şart)" if gate == "Başvurulamaz" else
                            "Çok uygun" if score >= 80 else "Uygun" if score >= 60 else "İlgili olabilir" if score >= 40 else "İlgisiz")
    failed = {c.name for c in checks if c.status == NO}
    track = None
    if rel >= 50 and failed and failed <= {"Deneyim", "Askerlik", "KPSS", "Yaş"}:
        track = {"Deneyim": "Deneyim kazanılması gerekiyor", "Askerlik": "Askerlik sonrası uygun",
                 "KPSS": "KPSS puanı yükseltilmeli", "Yaş": "Yaş şartı"}[sorted(failed)[0]]
    ex["karsilanmayan"] = [c.detail for c in checks if c.status == NO]
    ex["dikkat"] = [c.detail for c in checks if c.status == VERIFY]
    ex["eksik_bilgi"] = [c.name for c in checks if c.status == UNKNOWN]
    ex["kanitlar"] = list(dict.fromkeys([q for q in ex["kanitlar"] if q] + [c.evidence for c in checks if c.evidence]))[:8]
    if detail_level != "tam":
        ex["dikkat"].append("İlan metninin tamamı okunamadı; kadro tablosunu bağlantıdan kontrol et.")
    ex["basvuru"] = ("Başvurulamaz: zorunlu şartlardan biri karşılanmıyor." if gate == "Başvurulamaz" else
                     "Kadro tablosunda senin bölümünü/şartlarını kontrol edip başvurmayı değerlendir.")
    return Result(score=score, subscores=sub, gate=gate, category=category, career_track=track, checks=checks,
                  explanation=ex, location_tier="kamu", extra={"eksik_beceriler": [], "ilgililik": rel})

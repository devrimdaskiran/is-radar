"""İlan metninden kural tabanlı alan çıkarma.

Her bulunan şart, ilandaki ORİJİNAL cümleyle (kanıt) birlikte saklanır. Bir şey bulunamazsa
alan boş kalır ve arayüzde "Belirtilmemiş" görünür; hiçbir bilgi tahmin edilmez.
"""
from __future__ import annotations

import re
from datetime import datetime

from .textnorm import fold, sentences

PROVINCES = [
    "Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Amasya", "Ankara", "Antalya", "Artvin", "Aydın", "Balıkesir",
    "Bilecik", "Bingöl", "Bitlis", "Bolu", "Burdur", "Bursa", "Çanakkale", "Çankırı", "Çorum", "Denizli",
    "Diyarbakır", "Edirne", "Elazığ", "Erzincan", "Erzurum", "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane",
    "Hakkari", "Hatay", "Isparta", "Mersin", "İstanbul", "İzmir", "Kars", "Kastamonu", "Kayseri", "Kırklareli",
    "Kırşehir", "Kocaeli", "Konya", "Kütahya", "Malatya", "Manisa", "Kahramanmaraş", "Mardin", "Muğla", "Muş",
    "Nevşehir", "Niğde", "Ordu", "Rize", "Sakarya", "Samsun", "Siirt", "Sinop", "Sivas", "Tekirdağ", "Tokat",
    "Trabzon", "Tunceli", "Şanlıurfa", "Uşak", "Van", "Yozgat", "Zonguldak", "Aksaray", "Bayburt", "Karaman",
    "Kırıkkale", "Batman", "Şırnak", "Bartın", "Ardahan", "Iğdır", "Yalova", "Karabük", "Kilis", "Osmaniye", "Düzce",
]

LEVEL_PATTERNS = [
    ("lise", r"meslek lise|teknik lise|endustri meslek|anadolu teknik|mesleki ve teknik anadolu|lise mezun|(?<![a-z])lise(?![a-z])"),
    ("on_lisans", r"meslek yuksek ?okul|(?<![a-z])myo(?![a-z])|on ?lisans|onlisans|yuksekokul|2 yillik"),
    ("lisans", r"(?<!on )(?<!on)(?<![a-z])lisans(?![a-z])|muhendislik fakulte|fakultelerin|4 yillik|[a-z]+ muhendisligi"),
]
LEVEL_RANK = {"lise": 1, "on_lisans": 2, "lisans": 3, "yuksek_lisans": 4}
LEVEL_TR = {"lise": "lise", "on_lisans": "ön lisans", "lisans": "lisans", "yuksek_lisans": "yüksek lisans"}

# (kanonik ad, desen) - bileşik adlar önce gelir
DEPARTMENTS = [
    ("Elektrik-Elektronik", r"elektrik[\s\-/]*(?:ve\s+)?elektronik"),
    ("Elektronik ve Haberleşme", r"elektronik (?:ve )?haberlesme"),
    ("Kontrol ve Otomasyon", r"kontrol (?:ve )?otomasyon"),
    ("Endüstriyel Otomasyon", r"endustriyel otomasyon"),
    ("Endüstriyel Bakım", r"endustriyel bakim|makine bakim"),
    ("Yenilenebilir Enerji", r"yenilenebilir enerji"),
    ("Mekatronik", r"mekatronik"),
    ("Otomasyon", r"otomasyon"),
    ("Elektrik", r"elektrik"),
    ("Elektronik", r"elektronik"),
    ("Robotik", r"robotik"),
    ("Makine", r"makine|makina"),
    ("Bilgisayar", r"bilgisayar"),
    ("CNC", r"(?<![a-z])cnc(?![a-z])"),
    ("Otomotiv", r"otomotiv|motor(?:lu)? (?:arac|teknoloji)|(?<![a-z])motor(?![a-z])"),
    ("Enerji", r"enerji"),
    ("İnşaat", r"insaat"),
    ("Gıda", r"gida"),
    ("Kimya", r"kimya"),
    ("Endüstri", r"endustri muhendis"),
    ("Biyoloji", r"biyolo"),
    ("İklimlendirme", r"iklimlendirme|sogutma"),
]
EDU_CONTEXT = r"mezun|bolum|okul|universite|lise|yuksekokul|egitim|fakulte|ogrenim|lisans"
RELATED_CLAUSE = r"ilgili (?:teknik )?(?:bolum|alan|program)|benzeri bolum|ilgili mezun|vb\.? bolum"


def _first(pattern: str, folded: str):
    return re.search(pattern, folded)


def _ev(sent: str) -> str:
    return sent.strip()[:300]


def extract_fields(title: str, description: str, today: datetime | None = None) -> dict:
    """İlan metninden şartları çıkarır. Dönen her alan: {"deger": ..., "kanit": "...", ...}"""
    today = today or datetime.now()
    text = f"{title}\n{description or ''}"
    sents = sentences(description or "")
    fs = [(s, fold(s)) for s in sents]
    ftext = fold(text)
    f: dict = {}

    # ---------- Eğitim ----------
    levels: dict[str, str] = {}
    depts: dict[str, dict] = {}
    min_explicit = None
    related = None
    ctx_idx = [i for i, (_, x) in enumerate(fs) if re.search(EDU_CONTEXT, x)]
    for i, (s, x) in enumerate(fs):
        # Madde madde yazılmış bölüm listeleri: eğitim cümlesine 3 satır uzaklıktaki satırlar da sayılır
        if not any(abs(i - j) <= 3 for j in ctx_idx):
            continue
        m = re.search(r"en az ((?:meslek |teknik |endustri meslek )?lise|on ?lisans|lisans|meslek yuksek ?okul)", x)
        if m:
            lv = "lise" if "lise" in m.group(1) else ("on_lisans" if ("on" in m.group(1) or "yuksek" in m.group(1)) else "lisans")
            min_explicit = (lv, _ev(s))
        for lv, pat in LEVEL_PATTERNS:
            if re.search(pat, x) and lv not in levels:
                if lv == "lisans" and re.search(r"muhendisligi", x) and not re.search(r"(?<!on )(?<![a-z])lisans", x):
                    levels.setdefault("lisans", _ev(s))
                elif lv == "lisans":
                    levels.setdefault("lisans", _ev(s))
                else:
                    levels[lv] = _ev(s)
        rest = x
        for name, pat in DEPARTMENTS:
            for m2 in re.finditer(pat, rest):
                tail = rest[m2.end():m2.end() + 14]
                eng = bool(re.match(r"\s*(?:ve [a-z]+ )?muhendis", tail))
                key = name + (" Mühendisliği" if eng else "")
                depts.setdefault(key, {"ad": name, "muhendislik": eng, "kanit": _ev(s)})
            rest = re.sub(pat, " ### ", rest)
        if re.search(RELATED_CLAUSE, x) and not related:
            related = _ev(s)
    if levels or depts or min_explicit:
        if min_explicit:
            min_level = min_explicit[0]
        elif levels:
            min_level = min(levels, key=lambda k: LEVEL_RANK[k])
        else:
            min_level = None
        f["egitim"] = {
            "seviyeler": sorted(levels, key=lambda k: LEVEL_RANK[k]),
            "min_seviye": min_level,
            "bolumler": list(depts.values()),
            "ilgili_bolum_ifadesi": related,
            "kanit": min_explicit[1] if min_explicit else (next(iter(levels.values()), None) or
                                                            (next(iter(depts.values()))["kanit"] if depts else None)),
        }

    # ---------- Deneyim ----------
    for s, x in fs:
        if re.search(r"deneyimsiz|deneyim (?:sarti )?(?:aranmamaktadir|aranmaz|yoktur|sarti yok)|tecrube sarti (?:aranmaz|yok)|"
                     r"yeni mezun|yeni baslayan|deneyimli\s*(?:/|veya|ya da)\s*deneyimsiz|is deneyimi olmayan", x):
            f["deneyim"] = {"tur": "aranmiyor", "yil": 0, "kanit": _ev(s)}
            break
    if "deneyim" not in f:
        for s, x in fs:
            m = re.search(r"(?:en az|minimum|min\.?)\s*(\d+)\s*(?:yil|sene)|(\d+)\s*(?:-\s*\d+\s*)?(?:yil|sene)(?:lik)?\s*(?:ve uzeri\s*)?"
                          r"(?:is |sektor |alan |saha )?(?:deneyim|tecrube)", x)
            if m and re.search(r"deneyim|tecrube|calismis", x):
                yrs = int(m.group(1) or m.group(2))
                pref = bool(re.search(r"tercihen|tercih sebebi|tercih edilir|olmasi tercih", x))
                f["deneyim"] = {"tur": "tercihen" if pref else "zorunlu", "yil": yrs, "kanit": _ev(s)}
                break
    if "deneyim" not in f:
        for s, x in fs:
            if re.search(r"(?<![a-z])(?:deneyimli|tecrubeli|deneyim sahibi|tecrube sahibi)(?![a-z])", x) and not re.search(r"tercihen", x):
                f["deneyim"] = {"tur": "belirsiz", "yil": None, "kanit": _ev(s)}
                break
    for s, x in fs:
        if re.search(r"staj", x) and re.search(r"deneyim|yapmis|tamamlamis", x):
            f["staj_ifadesi"] = {"kanit": _ev(s)}
            break

    # ---------- Askerlik ----------
    for s, x in fs:
        if "askerlik" not in x and "askerlig" not in x and "tecil" not in x:
            continue
        m = re.search(r"en az (\d+) yil (?:sureyle )?tecil", x)
        completed = re.search(r"(?:askerlik (?:hizmetini |gorevini |yukumlulugunu )?|askerligini )(?:tamamlamis|yapmis|bitirmis)|"
                              r"askerlik durumu:? yapildi|askerlikten muaf (?:veya|ya da) tamamlamis|askerligini tamamla", x)
        no_tie = re.search(r"askerlik(?:le)? (?:ile )?ilisi[gk]i (?:bulunmayan|olmayan|yok)", x)
        f["askerlik"] = {
            "tamamlanmis_sart": bool(completed),
            "iliski_olmayan": bool(no_tie),
            "tecil_yil": int(m.group(1)) if m else None,
            "tecil_kabul": bool(re.search(r"tecil", x)),
            "kanit": _ev(s),
        }
        break

    # ---------- Ehliyet ----------
    for s, x in fs:
        if not re.search(r"ehliyet|surucu belge", x):
            continue
        classes = set(re.findall(r"(?<![a-z0-9])(a1|a2|b1|be|c1|ce|d1|[abcdfgm])(?:\s*(?:ve|,|/|-)\s*(?:a1|a2|b1|[abcdfgm]))*\s*sinif", x))
        classes |= {c for c in re.findall(r"(?<![a-z0-9])(a1|a2|b1|c1|ce|[abcde])(?![a-z0-9])", x.split("ehliyet")[0][-25:])}
        f["ehliyet"] = {"siniflar": sorted(c.upper() for c in classes), "tercihen": bool(re.search(r"tercihen", x)), "kanit": _ev(s)}
        break

    # ---------- Vardiya / seyahat / ikamet / yaş / dil ----------
    for s, x in fs:
        if re.search(r"vardiya|7'?li sistem|gece calis|00-08|nobet", x):
            f["vardiya"] = {"var": True, "kanit": _ev(s)}
            break
    for s, x in fs:
        if re.search(r"seyahat (?:engeli|edebilecek|engel)|sehir disi gorev", x):
            f["seyahat"] = {"var": True, "kanit": _ev(s)}
            break
    for s, x in fs:
        m = re.search(r"([a-z]+)(?:'?(?:de|da|te|ta))?(?: ili(?:nde)?)?(?: ve (?:cevresinde|ilcelerinde))? ikamet (?:eden|ediyor|etmesi)", x)
        if m:
            prov = next((p for p in PROVINCES if fold(p) in x[:m.end()]), None)
            f["ikamet"] = {"il": prov, "kabul_edebilecek": bool(re.search(r"edebilecek|edecek|tasinabilecek", x)), "kanit": _ev(s)}
            break
    for s, x in fs:
        m = re.search(r"(\d{2})\s*[-–]\s*(\d{2})\s*yas", x) or re.search(r"(\d{2})\s*yasini (?:gecmemis|asmamis)", x)
        if m:
            f["yas"] = {"aralik": m.group(0), "kanit": _ev(s)}
            break
    for s, x in fs:
        if re.search(r"ingilizce|almanca|yabanci dil|english", x):
            f["dil"] = {"tercihen": bool(re.search(r"tercihen|tercih", x)), "kanit": _ev(s)}
            break

    # ---------- Yan haklar / çalışma modeli / maaş / tarihler ----------
    perks = []
    for word, label in [(r"servis", "servis"), (r"lojman", "lojman"), (r"konaklama", "konaklama"),
                        (r"yol (?:ucreti|yardimi)|ulasim (?:imkani|destegi)", "ulaşım desteği"), (r"yemek", "yemek")]:
        for s, x in fs:
            if re.search(word, x):
                perks.append({"ad": label, "kanit": _ev(s)})
                break
    if perks:
        f["yan_haklar"] = perks
    if re.search(r"uzaktan|remote", ftext):
        f["calisma_modeli"] = "Uzaktan" if not re.search(r"hibrit|hybrid", ftext) else "Hibrit"
    elif re.search(r"hibrit|hybrid", ftext):
        f["calisma_modeli"] = "Hibrit"
    elif re.search(r"is yerinde|ofiste|sahada|fabrikada", ftext):
        f["calisma_modeli"] = "İş yerinde"
    m = re.search(r"₺\s?[\d\.]+\s*[-–]\s*₺?\s?[\d\.]+(?:\s*/\s*\w+)?(?:,\s*\w+)?|[\d\.]{5,}\s*(?:tl|₺)\s*[-–]\s*[\d\.]{5,}\s*(?:tl|₺)",
                  description or "", re.I)
    if m:
        f["maas"] = m.group(0).strip()
    m = re.search(r"closingdate:\s*(\d{2}\.\d{2}\.\d{4})|son basvuru(?: tarihi)?\s*:?\s*(\d{2}[./]\d{2}[./]\d{4})", ftext)
    if m:
        d = (m.group(1) or m.group(2)).replace("/", ".")
        try:
            f["son_basvuru"] = datetime.strptime(d, "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass
    m = re.search(r"lastpublishdate:\s*(\d{2}\.\d{2}\.\d{4})", ftext)
    if m:
        f["yayin_bitis"] = datetime.strptime(m.group(1), "%d.%m.%Y").date().isoformat()
    for s, x in fs:
        if re.search(r"myk|mesleki yeterlilik|sertifika|belgesine sahip", x):
            f["belge"] = {"kanit": _ev(s), "alternatif": bool(re.search(r"\bya da\b|\bveya\b", x))}
            break

    # ---------- Şehir (metinden) ----------
    tf = fold(title)
    f["metindeki_iller"] = [p for p in PROVINCES if re.search(r"(?<![a-z])" + re.escape(fold(p)) + r"(?![a-z])", tf + " " + ftext[:400])]
    return f


def find_skills(text: str, skills) -> list[dict]:
    """Profil yetkinliklerinden ilanda geçenleri kanıt cümlesiyle döndürür."""
    out = []
    sents = sentences(text)
    fsents = [(s, fold(s)) for s in sents] or [(text, fold(text))]
    for sk in skills:
        for s, x in fsents:
            hit = None
            for syn in sk.synonyms:
                fsyn = fold(syn)
                if fsyn and re.search(r"(?<![a-z0-9])" + re.escape(fsyn) + r"(?![a-z0-9])", x):
                    hit = syn
                    break
            if hit:
                out.append({"ad": sk.name, "seviye": sk.level, "kanit": s.strip()[:300]})
                break
    return out

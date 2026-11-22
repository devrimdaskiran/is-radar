"""Türkçe metin normalleştirme yardımcıları.

Eşleştirme her zaman `fold()` edilmiş metin üzerinde yapılır:
küçük harf (Türkçe kurallarıyla), aksan katlama (ç->c, ş->s ...), tek boşluk.
"""
from __future__ import annotations

import html
import re
import unicodedata

_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def tr_lower(s: str) -> str:
    return (s or "").translate(_TR_LOWER).lower()


def fold(s: str) -> str:
    s = tr_lower(s).translate(_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _WS.sub(" ", s).strip()


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    lines = [_WS.sub(" ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def sentences(text: str) -> list[str]:
    """Metni madde/cümle parçalarına böler (kanıt alıntısı için)."""
    parts = re.split(r"(?:\n+|(?<=[.!?])\s+|•|\s-\s|\*)", text or "")
    return [p.strip(" -•*\t") for p in parts if p and len(p.strip()) > 2]


def contains_any(folded_text: str, needles: list[str]) -> str | None:
    """İlk bulunan iğneyi döndürür (iğneler de fold edilir, kelime sınırına dikkat edilir)."""
    for n in needles:
        fn = fold(n)
        if not fn:
            continue
        pat = r"(?<![a-z0-9])" + re.escape(fn.strip()) + (r"(?![a-z0-9])" if not fn.endswith(" ") else "")
        if re.search(pat, folded_text):
            return n
    return None


ROLE_NOUNS = r"(personeli|elemani|teknisyeni|teknikeri|iscisi|operatoru|uzmani|muhendisi|sorumlusu|ustasi|gorevlisi|" \
             r"temsilcisi|sefi|muduru|asistani|sekreteri|teknisyen|tekniker|isci|operator|yardimcisi|kalfasi|" \
             r"elektrikcisi|elektrikci|montajcisi|stajyeri|danismani|koordinatoru|amiri|calisani|egitmeni|egitmen|tasarimci|" \
             r"tasarimcisi|sorumlu|muhasebeci|surucu|sofor|soforu|asci|garson|kasiyer|depocu|tekniker|programci|" \
             r"gelistirici|analisti|uzman|muhendis)"
COMPANY_HINT = r"(a\.? ?s\.?|ltd|sti|san\.?|tic\.?|sanayi|ticaret|holding|grup|limited|anonim|as)(?![a-z])"


def split_title_company(title: str) -> tuple[str, str | None]:
    """'Bakım Onarım Elemanı TANDOĞAN PLASTİK ... LTD ŞTİ' → ('Bakım Onarım Elemanı', 'TANDOĞAN PLASTİK ... LTD ŞTİ').
    Şirket eki yoksa unvana dokunmaz."""
    words = (title or "").split()
    if len(words) < 3 or not re.search(COMPANY_HINT, fold(title)):
        return title, None
    cut = None
    for i, w in enumerate(words[:-1]):
        if re.fullmatch(ROLE_NOUNS, fold(w).strip(".,;:()")):
            cut = i + 1
    if cut and cut < len(words):
        return " ".join(words[:cut]), " ".join(words[cut:])
    return title, None

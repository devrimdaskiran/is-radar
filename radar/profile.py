"""Aday profili: YAML'dan okunur, eşleştirme için hazır hale getirilir."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import load_yaml
from .textnorm import fold

LEVEL_RANK = {"lise": 1, "on_lisans": 2, "lisans": 3, "yuksek_lisans": 4}


@dataclass
class Skill:
    name: str
    level: str
    synonyms: list[str]


@dataclass
class Profile:
    raw: dict
    diplomas: list[dict]                 # yalnızca mezun olunanlar
    studying: list[dict]                 # devam edenler
    related_departments: list[str]
    skills: list[Skill]
    watch_missing: list[Skill]
    targets: list[str]
    title_keywords: list[str]
    blocked_titles: list[str]
    regions: dict
    experience_years: float
    internships: list[dict]
    military: dict
    licenses: list[str]
    shift_ok: bool
    remote_ok: bool
    relocate_private: bool
    city: str
    extra: dict = field(default_factory=dict)

    @property
    def highest_level(self) -> int:
        return max((LEVEL_RANK.get(d["seviye"], 0) for d in self.diplomas), default=0)

    def diploma_programs_at_or_below(self, max_rank: int | None = None) -> list[dict]:
        return [d for d in self.diplomas if max_rank is None or LEVEL_RANK.get(d["seviye"], 0) <= max_rank]

    def military_deferral_years_left(self, today: date | None = None) -> float | None:
        end = self.military.get("tecil_bitis")
        if self.military.get("durum") != "tecilli" or not end:
            return None
        today = today or date.today()
        y, m, d = (int(x) for x in str(end).split("-"))
        return max(0.0, (date(y, m, d) - today).days / 365.25)

    def anonymous_summary(self) -> str:
        """Yapay zekâya gönderilebilecek, kimlik bilgisi içermeyen profil özeti."""
        dip = "; ".join(f"{d['seviye']} {d['program']} ({d['durum']})" for d in self.diplomas + self.studying)
        sk = ", ".join(f"{s.name} ({s.level})" for s in self.skills)
        return (
            f"Diplomalar: {dip}\nYetkinlikler: {sk}\n"
            f"İş deneyimi: {self.experience_years} yıl; staj: {len(self.internships)} adet (her biri 30 iş günü)\n"
            f"Askerlik: {self.military.get('durum')} (bitiş {self.military.get('tecil_bitis')})\n"
            f"Ehliyet: {', '.join(self.licenses)}; Vardiya: {'uygun' if self.shift_ok else 'değil'}\n"
            f"Sahip OLMADIĞI yetkinlikler: {', '.join(s.name for s in self.watch_missing)}"
        )


def load_profile(data: dict | None = None) -> Profile:
    p = data or load_yaml("profile.yaml")
    edu = p.get("egitim", [])
    skills = [Skill(s["ad"], s.get("seviye", ""), [s["ad"]] + s.get("es", [])) for s in p.get("yetkinlikler", [])]
    watch = [Skill(s["ad"], "yok", [s["ad"]] + s.get("es", [])) for s in p.get("eksik_izlenecek", [])]
    kon = p.get("konum", {})
    tr = p.get("tercihler", {})
    return Profile(
        raw=p,
        diplomas=[e for e in edu if e.get("durum") == "mezun"],
        studying=[e for e in edu if e.get("durum") == "devam"],
        related_departments=p.get("ilgili_bolumler", []),
        skills=skills,
        watch_missing=watch,
        targets=p.get("kariyer_hedefleri", []),
        title_keywords=p.get("unvan_anahtar_kelimeleri", []),
        blocked_titles=[fold(x) for x in p.get("engellenen_unvanlar", [])],
        regions=kon.get("bolgeler", {}),
        experience_years=float(p.get("deneyim", {}).get("is_deneyimi_yil", 0)),
        internships=p.get("deneyim", {}).get("stajlar", []),
        military=p.get("askerlik", {}),
        licenses=[x.upper() for x in p.get("ehliyet", [])],
        shift_ok=bool(tr.get("vardiya", False)),
        remote_ok=bool(kon.get("uzaktan_calisma", False)),
        relocate_private=bool(kon.get("ozel_sektor_tasinma", False)),
        city=p.get("aday", {}).get("sehir", ""),
    )

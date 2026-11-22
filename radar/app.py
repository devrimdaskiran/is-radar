"""Uygulama bağlamı: ayarlar, profil, veritabanı, toplayıcılar ve analiz katmanını bir araya getirir."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from sqlalchemy import select

from .collectors.base import Collector
from .collectors.career_jsonld import CareerJsonLdCollector
from .collectors.gemini_search import GeminiSearchCollector
from .collectors.site_listing import SiteListingCollector
from .collectors.imap_alerts import ImapAlertsCollector
from .collectors.jooble import JoobleCollector
from .config import DATA_DIR, LOG_DIR, Secrets, deep_merge, load_secrets, load_yaml
from .db import Source, get_setting_overrides, make_engine, make_session_factory
from .llm.analyzer import Analyzer
from .llm.providers import PROVIDERS, ProviderError
from .profile import Profile, load_profile

COLLECTORS = {"jooble_api": JoobleCollector, "imap_alerts": ImapAlertsCollector, "career_jsonld": CareerJsonLdCollector,
              "site_listing": SiteListingCollector, "gemini_search": GeminiSearchCollector}


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "radar.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [fh, sh]
    logging.getLogger("httpx").setLevel(logging.WARNING)


class App:
    def __init__(self, secrets: Secrets | None = None, db_url: str | None = None):
        self.secrets = secrets or load_secrets()
        self.engine = make_engine(db_url)
        self.Session = make_session_factory(self.engine)
        self.sources_cfg = load_yaml("sources.yaml").get("kaynaklar", [])
        self.base_settings = load_yaml("settings.yaml")
        self.profile: Profile = load_profile()
        self.paused = False
        self._seed_sources()

    # ---- ayarlar ----
    def settings(self, s=None) -> dict:
        """Geçerli ayarlar. Açık bir oturum verilirse onu kullanır (aynı işlem içindeki değişiklikleri görür)."""
        if s is not None:
            return deep_merge(self.base_settings, get_setting_overrides(s))
        with self.Session() as s2:
            return deep_merge(self.base_settings, get_setting_overrides(s2))

    def reload_profile(self) -> None:
        self.profile = load_profile()

    # ---- kaynaklar ----
    def _seed_sources(self) -> None:
        with self.Session() as s:
            for c in self.sources_cfg:
                src = s.scalar(select(Source).where(Source.name == c["ad"]))
                cls = COLLECTORS.get(c["tur"])
                if not src:
                    src = Source(name=c["ad"], kind=c["tur"])
                    s.add(src)
                src.method = cls.method if cls else c["tur"]
                src.active = bool(c.get("aktif", True))
                src.interval_min = int(c.get("siklik_dakika", 30))
                if not src.active:
                    src.status = "pasif"
            s.commit()

    def due_collectors(self, force: bool = False) -> list[Collector]:
        out = []
        with self.Session() as s:
            for c in self.sources_cfg:
                cls = COLLECTORS.get(c["tur"])
                src = s.scalar(select(Source).where(Source.name == c["ad"]))
                if not cls or not src or not src.active:
                    continue
                if not force and src.last_run and datetime.now() - src.last_run < timedelta(minutes=src.interval_min - 1):
                    continue
                col = cls(c, self.secrets, profile=self.profile) if cls is SiteListingCollector else cls(c, self.secrets)
                col.name = c["ad"]
                out.append(col)
        return out

    def analyzer(self) -> Analyzer:
        st = self.settings()
        ai = st.get("yapay_zeka", {})
        provider = None
        name = ai.get("saglayici", "gemini")
        key = {"gemini": self.secrets.gemini_api_key, "claude": self.secrets.anthropic_api_key}.get(name, "")
        if ai.get("aktif") and key:
            try:
                provider = PROVIDERS[name](key, ai.get("model") or None)
            except (ProviderError, KeyError):
                provider = None
        return Analyzer(provider, st)

    # ---- yedekleme ----
    def backup(self) -> str | None:
        db = DATA_DIR / "radar.db"
        if not db.exists():
            return None
        bdir = DATA_DIR / "backups"
        bdir.mkdir(exist_ok=True)
        target = bdir / f"radar-{datetime.now():%Y%m%d}.db"
        import sqlite3
        src = sqlite3.connect(db)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        src.close(), dst.close()
        keep = int(self.settings().get("yedekleme", {}).get("saklama_gun", 14))
        for f in sorted(bdir.glob("radar-*.db"))[:-keep]:
            f.unlink(missing_ok=True)
        return str(target)

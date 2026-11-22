"""Veritabanı modelleri (SQLAlchemy 2).

İlke: ilanın gerçek verisi (Job), yapay zekâ/kural yorumu (Evaluation) ve kullanıcının
kararları (Feedback, Application) ayrı tablolardadır.
v1 SQLite kullanır; VPS'te DATABASE_URL ile PostgreSQL'e geçilir.
"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
                        create_engine, event, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import DATA_DIR


def now() -> datetime:
    return datetime.now()


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    method: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_min: Mapped[int] = mapped_column(Integer, default=30)
    last_run: Mapped[datetime | None] = mapped_column(DateTime)
    last_success: Mapped[datetime | None] = mapped_column(DateTime)
    last_count: Mapped[int] = mapped_column(Integer, default=0)
    total_found: Mapped[int] = mapped_column(Integer, default=0)
    success_runs: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="henüz çalışmadı")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    started: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished: Mapped[datetime | None] = mapped_column(DateTime)
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str | None] = mapped_column(String(300))
    sector: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    district: Mapped[str | None] = mapped_column(String(100))
    work_model: Mapped[str | None] = mapped_column(String(60))
    salary: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    fields: Mapped[dict] = mapped_column(JSON, default=dict)      # çıkarılan şartlar + kanıtlar
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_checked: Mapped[datetime] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String(30), default="aktif")  # aktif | süresi_geçti | kapandı
    links: Mapped[list["JobLink"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="job", cascade="all, delete-orphan",
                                                           order_by="Evaluation.id")

    @property
    def latest_eval(self) -> "Evaluation | None":
        return self.evaluations[-1] if self.evaluations else None

    @property
    def primary_url(self) -> str | None:
        return self.links[0].url if self.links else None


class JobLink(Base):
    __tablename__ = "job_links"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    source: Mapped[str] = mapped_column(String(80))
    external_id: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    found_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    job: Mapped[Job] = relationship(back_populates="links")


class Evaluation(Base):
    __tablename__ = "evaluations"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=now)
    engine: Mapped[str] = mapped_column(String(60))          # "kural" | "kural+claude-..."
    profile_version: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column(Float)
    subscores: Mapped[dict] = mapped_column(JSON)
    gate: Mapped[str] = mapped_column(String(30))             # Uygun | Doğrulanmalı | Başvurulamaz
    category: Mapped[str] = mapped_column(String(40))
    career_track: Mapped[str | None] = mapped_column(String(60))
    checks: Mapped[list] = mapped_column(JSON)                # zorunlu şart kontrolleri
    explanation: Mapped[dict] = mapped_column(JSON)
    job: Mapped[Job] = relationship(back_populates="evaluations")


class Feedback(Base):
    __tablename__ = "feedback"
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    not_interested: Mapped[bool] = mapped_column(Boolean, default=False)
    updated: Mapped[datetime] = mapped_column(DateTime, default=now)


APPLICATION_STATES = ["Yeni bulundu", "İnceleniyor", "Başvurulacak", "Başvuru yapıldı", "Görüşme bekleniyor",
                      "Mülakat yapıldı", "Olumlu sonuçlandı", "Olumsuz sonuçlandı", "Arşivlendi"]


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    state: Mapped[str] = mapped_column(String(40), default="Başvuru yapıldı")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    interview_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_contact: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str] = mapped_column(Text, default="")
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    job: Mapped[Job] = relationship()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    reason: Mapped[str] = mapped_column(String(30))           # yeni | güncelleme
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class EgressLog(Base):
    """Kişisel veya ilan verisinin hangi dış servise gönderildiğinin kaydı."""
    __tablename__ = "egress_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
    service: Mapped[str] = mapped_column(String(80))
    what: Mapped[str] = mapped_column(String(300))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)


def make_engine(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL") or f"sqlite:///{DATA_DIR / 'radar.db'}"
    if url.startswith("sqlite:///") and ":memory:" not in url:
        os.makedirs(os.path.dirname(url.replace("sqlite:///", "")) or ".", exist_ok=True)
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragma(dbapi_con, _):
            cur = dbapi_con.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def get_setting_overrides(s: Session) -> dict:
    out: dict = {}
    for row in s.scalars(select(Setting)):
        cur = out
        parts = row.key.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = row.value
    return out


def set_setting(s: Session, key: str, value) -> None:
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    s.flush()

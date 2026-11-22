"""Ayarlar ve gizli bilgiler.

Gizli bilgiler yalnızca .env dosyasından okunur. YAML dosyaları kişisel profil ve
davranış ayarlarını tutar. Telegram /settings ile değişen ayarlar veritabanında
saklanır ve `effective_settings()` ile YAML'ın üzerine bindirilir.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("RADAR_CONFIG_DIR", ROOT / "configs"))
DATA_DIR = Path(os.environ.get("RADAR_DATA_DIR", ROOT / "data"))
LOG_DIR = Path(os.environ.get("RADAR_LOG_DIR", ROOT / "logs"))


@dataclass
class Secrets:
    telegram_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    jooble_api_key: str = ""
    gmail_address: str = ""
    gmail_app_password: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    telegram_chat_id: str = ""
    radar_key: str = ""


def load_secrets(env_file: Path | None = None) -> Secrets:
    load_dotenv(env_file or ROOT / ".env")
    os.environ["TELEGRAM_BOT_TOKEN"] = re.sub(r"\s+", "", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    ids = [int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()]
    return Secrets(
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        allowed_user_ids=ids,
        jooble_api_key=os.environ.get("JOOBLE_API_KEY", "").strip(),
        gmail_address=os.environ.get("GMAIL_ADDRESS", "").strip(),
        gmail_app_password=os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        gemini_api_key=re.sub(r"\s+", "", os.environ.get("GEMINI_API_KEY", "")),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        radar_key=os.environ.get("RADAR_KEY", "").strip(),
    )


def load_yaml(name: str) -> dict:
    """YAML okur. Dosyanın şifreli sürümü (name + '.enc') varsa ve RADAR_KEY tanımlıysa onu çözer.
    Herkese açık depoda kişisel profil yalnızca şifreli olarak durur."""
    plain = CONFIG_DIR / name
    enc = CONFIG_DIR / (name + ".enc")
    if not plain.exists() and enc.exists():
        from .secure import decrypt_bytes
        return yaml.safe_load(decrypt_bytes(enc.read_bytes()).decode("utf-8")) or {}
    with open(plain, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out

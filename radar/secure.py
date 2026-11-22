"""Şifreleme: herkese açık GitHub deposunda kişisel veriler (profil, durum veritabanı) şifreli saklanır.

Anahtar yalnızca GitHub Secrets'taki RADAR_KEY'dedir. Anahtar olmadan dosyalar okunamaz.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class KeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = os.environ.get("RADAR_KEY", "").strip()
    if not key:
        raise KeyMissing("RADAR_KEY tanımlı değil: şifreli profil/durum dosyaları açılamıyor.")
    return Fernet(key.encode())


def new_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    try:
        return _fernet().decrypt(data)
    except InvalidToken as e:
        raise KeyMissing("RADAR_KEY yanlış: şifreli dosya çözülemedi.") from e


def encrypt_file(src: str, dst: str) -> None:
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(encrypt_bytes(data))


def decrypt_file(src: str, dst: str) -> None:
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(decrypt_bytes(data))

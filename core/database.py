"""
Зашифрованная база данных приватных ключей и прокси.

Алгоритм:
  Пароль → PBKDF2HMAC (SHA-256, 480 000 итераций) → Fernet (AES-128-CBC + HMAC-SHA256)
  Данные: input/database.enc  |  Соль: input/database.salt
"""

import base64
import json
import os
from getpass import getpass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

DB_FILE   = Path("input/database.enc")
SALT_FILE = Path("input/database.salt")


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def db_exists() -> bool:
    return DB_FILE.exists() and SALT_FILE.exists()


def save_db(password: str, private_keys: list[str], proxies: list[str]) -> None:
    salt      = os.urandom(16)
    key       = _derive_key(password, salt)
    data      = json.dumps({"private_keys": private_keys, "proxies": proxies}, ensure_ascii=False)
    encrypted = Fernet(key).encrypt(data.encode("utf-8"))
    DB_FILE.parent.mkdir(exist_ok=True)
    SALT_FILE.write_bytes(salt)
    DB_FILE.write_bytes(encrypted)


def load_db(password: str) -> tuple[list[str], list[str]]:
    try:
        salt      = SALT_FILE.read_bytes()
        key       = _derive_key(password, salt)
        encrypted = DB_FILE.read_bytes()
        data      = json.loads(Fernet(key).decrypt(encrypted).decode("utf-8"))
    except InvalidToken:
        raise ValueError("Неверный пароль")
    return data["private_keys"], data["proxies"]


def unlock() -> tuple[list[str], list[str]]:
    """Запрашивает пароль и расшифровывает базу. 3 попытки — затем выход."""
    print()
    for attempt in range(3):
        pwd = getpass("  Пароль: ")
        try:
            keys, proxies = load_db(pwd)
            print(f"  Загружено: {len(keys)} ключей, {len(proxies)} прокси\n")
            return keys, proxies
        except ValueError:
            if attempt < 2:
                print("  Неверный пароль, попробуй снова.\n")
    raise SystemExit("\n  Неверный пароль.")

# -*- coding: utf-8 -*-
"""Dashboard foydalanuvchilari: fayl, parol xeshi va tekshirish.

Har kimga o'z logini. Ma'lumotni hamma ko'radi - chegaralash yo'q, lekin
kirish alohida: odam ishdan ketsa faqat uniki o'chiriladi, qolganlar
ishlayveradi.

Saqlash: <DATA_DIR>/users.json. Parol OCHIQ saqlanmaydi - faqat
pbkdf2-sha256 xeshi va har kimga alohida tuz (salt).
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets

ITERATIONS = 200_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,30}$")

# Chalkashadigan belgilar olib tashlangan: 0/O, 1/l/I
ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_password(length=12):
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("ascii"), ITERATIONS)
    return "pbkdf2$%d$%s$%s" % (ITERATIONS, salt,
                                base64.b64encode(dk).decode("ascii"))


def verify_password(password, stored):
    """Xesh mos keladimi. Format buzuq bo'lsa - False, istisno emas."""
    try:
        algo, iters, salt, digest = str(stored or "").split("$", 3)
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("ascii"), int(iters))
        return hmac.compare_digest(base64.b64encode(dk).decode("ascii"), digest)
    except Exception:
        return False


def path_of(data_dir):
    return os.path.join(data_dir, "users.json")


def load(data_dir):
    try:
        with open(path_of(data_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(data_dir, users):
    os.makedirs(data_dir, exist_ok=True)
    tmp = path_of(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(users, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path_of(data_dir))
    try:
        os.chmod(path_of(data_dir), 0o600)
    except Exception:
        pass


def ensure_admin(data_dir, password):
    """Birinchi ishga tushirishda `admin` yaratiladi.

    Paroli - .env dagi DASHBOARD_PASSWORD. Shunda eski parol ishlayveradi
    va hech kim tizimdan tushib qolmaydi.
    """
    users = load(data_dir)
    if users or not password:
        return users
    users["admin"] = {
        "hash": hash_password(password),
        "member": "",
        "admin": True,
        "created": datetime.date.today().isoformat(),
    }
    save(data_dir, users)
    return users


def check(data_dir, name, password):
    """-> (login, user) yoki (None, None)."""
    name = (name or "").strip().lower()
    user = load(data_dir).get(name)
    if not user or not verify_password(password or "", user.get("hash")):
        return None, None
    return name, user


def add(data_dir, name, member="", admin=False, password=None):
    """-> (login, ochiq parol). Parol FAQAT shu yerda ko'rinadi."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("Логин 2-31 белги: кичик ҳарф, рақам, . _ -")
    users = load(data_dir)
    if name in users:
        raise ValueError("Бундай логин аллақачон бор: " + name)
    password = password or new_password()
    users[name] = {
        "hash": hash_password(password),
        "member": (member or "").strip(),
        "admin": bool(admin),
        "created": datetime.date.today().isoformat(),
    }
    save(data_dir, users)
    return name, password


def reset(data_dir, name):
    users = load(data_dir)
    if name not in users:
        raise ValueError("Бундай логин йўқ: " + name)
    password = new_password()
    users[name]["hash"] = hash_password(password)
    save(data_dir, users)
    return password


def delete(data_dir, name):
    """Oxirgi adminni o'chirib bo'lmaydi - aks holda hech kim kira olmaydi."""
    users = load(data_dir)
    if name not in users:
        raise ValueError("Бундай логин йўқ: " + name)
    if users[name].get("admin"):
        admins = [k for k, v in users.items() if v.get("admin")]
        if len(admins) <= 1:
            raise ValueError("Охирги бошқарувчини ўчириб бўлмайди")
    users.pop(name)
    save(data_dir, users)


def fingerprint(user):
    """Cookie imzosiga qo'shiladigan parol izi.

    Parol almashsa yoki odam o'chirilsa, eski cookie darhol yaroqsiz
    bo'lishi uchun kerak.
    """
    return hashlib.sha256((user or {}).get("hash", "").encode("utf-8")
                          ).hexdigest()[:12]

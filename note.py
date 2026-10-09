#!/usr/bin/env python3
"""claude-note-win - Claude Code va Codex sessiyalaridan bilim bazasi.

Oqim:
  SessionEnd hook -> note.py --hook (stdin: JSON) -> fon jarayon -> transkript
  -> Claude yoki Codex sintezi -> vault/notes/*.md + INDEX.md
"""

import argparse
import datetime
import difflib
import glob
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import traceback
import time
import unicodedata
import zipfile
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
GUARD_ENV = "CLAUDE_NOTE_ACTIVE"

WINDOWS = sys.platform == "win32"

# Windows jarayon bayroqlari (macOS/Linux da ishlatilmaydi)
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def quiet_kwargs():
    """Fon jarayonida konsol oynasi chiqmasin.

    Windows da claude.cmd bat shim bo'lgani uchun unga ko'rinmas konsol
    kerak. POSIX da bunday muammo yo'q - qo'shimcha hech narsa shart emas.
    """
    return {"creationflags": CREATE_NO_WINDOW} if WINDOWS else {}


def detach_kwargs():
    """Fon jarayoni ota jarayon (Claude Code) yopilganda o'lib qolmasin.

    Windows: NEW_PROCESS_GROUP - Ctrl+C merosga o'tmaydi.
    POSIX:   start_new_session - yangi sessiya, SIGHUP tegmaydi.
    """
    if WINDOWS:
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}

DEFAULTS = {
    "mode": "route",
    "vault": os.path.join(BASE, "vault"),
    "state": os.path.join(BASE, "state"),
    "language": "uz-Cyrl",
    "model": "claude-sonnet-5",
    # Kuchli model: brifing ixchamlash va imlosi buzilgan sintez javobini qayta
    # so'rash uchun (sonnet o'zbek kirill harflarini buzadi, opus - yo'q).
    "strong_model": "claude-opus-5",
    "synthesis_provider": "auto",
    "codex_model": "gpt-5.6-sol",
    "min_user_messages": 3,
    "max_digest_chars": 1500000,
    "max_tool_result_chars": 400,
    "max_tool_input_chars": 150,
    "keep_only_error_results": True,
    "max_chunks_per_run": 4,
    "claude_timeout_sec": 300,
    "codex_timeout_sec": 300,
    "dedup_ratio": 0.86,
    "keep_raw_log": True,
    "skip_projects": [],
    # --- jamoa telemetriyasi: standart holatda O'CHIQ ---
    "telemetry_enabled": False,
    "telemetry_url": "",
    "team_member": "",
    "telemetry_send_summary": True,
    "telemetry_timeout_sec": 10,
    "telemetry_max_outbox": 500,
    # --- jamoa serveri (faqat `note paket` ko'rsatma chiqarishi uchun) ---
    "server_ssh": "",                        # masalan "my-vps" (~/.ssh/config)
    "server_dir": "/opt/claude-note-server",
    # `note scan` papkasi; bo'sh bo'lsa - foydalanuvchi uy papkasi
    "scan_root": "",
}

# Windows konsoli cp1251 bo'lgani uchun kirill matn buzilmasin
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SECTIONS = ["Learnings", "Decisions", "Patterns", "Open Questions"]
RELATED_HEADING = "## Боғлиқ эслатмалар"   # Obsidian [[wikilink]] lari shu yerda
LANG_NAME = {
    "uz-Cyrl": ("O'ZBEK TILIDA, FAQAT KIRILL YOZUVIDA yoz (masalan: "
                "'Модуль нархлаш стратегияси келишилди'). Lotin yozuvida yozma. "
                "IMLO: қ, ғ, ў, ҳ - oddiy kichik harflar; so'z o'rtasida hech qachon "
                "bosh harf yozma ('фақат', 'ҳар', 'йўқ', 'алоҳида' - to'g'ri; "
                "'фаҲат', 'ҲаР', 'йУҲ', 'алоҲида' - xato). Kirish matnida shunday "
                "buzilgan so'z uchrasa, uni takrorlama - to'g'ri yoz."),
    "uz-Latn": "o'zbek tilida, lotin yozuvida yoz",
    "ru": "rus tilida yoz",
    "en": "ingliz tilida yoz",
}


# ---------------------------------------------------------------- infra

FS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(text, limit=70):
    """Sarlavhadan fayl nomi. Windows taqiqlagan belgilar olib tashlanadi."""
    t = FS_BAD.sub(" ", str(text or "")).replace("[", "(").replace("]", ")")
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:limit].strip(" .")


def session_note_name(date, project, session_id, title=None):
    """Sessiya sahifasining nomi.

    Sarlavha bo'lsa - o'qiladigan nom (`2026-09-07 — 1С каталог майдонлари`),
    aks holda eski uslub (sana-loyiha-id). Nom bir marta tanlanadi va state da
    saqlanadi: keyingi bo'laklarda sarlavha o'zgarsa ham fayl ikkilanmasin.
    """
    clean = safe_filename(title) if title else ""
    if clean:
        return "%s — %s" % (date, clean)
    return "%s-%s-%s" % (date, project, str(session_id)[:8])


def session_note_path(cfg, project, name):
    """Sessiyalar loyiha bo'yicha alohida papkalarda turadi."""
    folder = os.path.join(cfg["vault"], "sessions", safe_filename(project, 60))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name + ".md")


LOCAL_CONFIG = "config.local.json"


def load_config(overrides=None):
    """Sozlama uch qatlamdan yig'iladi:

        DEFAULTS  ->  config.json  ->  state/config.local.json  ->  CLI

    Uchinchi qatlam nima uchun kerak: config.json GitHub repoda turadi.
    Unga yozilgan har narsa - ism, telemetriya yoqilgani - hamma klonlarga
    tarqaydi, va lokal o'zgartirish tasodifan commit bo'lib ketishi mumkin.
    state/ esa .gitignore da: shu yerdagi sozlama faqat shu kompyuterniki.
    """
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    for key in ("vault", "state"):
        cfg[key] = os.path.abspath(cfg[key])
    try:
        with open(os.path.join(cfg["state"], LOCAL_CONFIG), encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    for key in ("vault", "state"):
        cfg[key] = os.path.abspath(cfg[key])
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    for sub in ("notes", "log", "sessions", "projects"):
        os.makedirs(os.path.join(cfg["vault"], sub), exist_ok=True)
    os.makedirs(cfg["state"], exist_ok=True)
    return cfg


def log(cfg, msg):
    line = "%s  %s" % (datetime.datetime.now().isoformat(timespec="seconds"), msg)
    try:
        with open(os.path.join(cfg["state"], "note.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    if os.environ.get("CLAUDE_NOTE_VERBOSE"):
        print(line, file=sys.stderr)


def state_path(cfg):
    return os.path.join(cfg["state"], "processed.json")


def load_state(cfg):
    try:
        with open(state_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(cfg, st):
    tmp = state_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, state_path(cfg))


def slugify(text, fallback="eslatma"):
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace(" ", "-")
    text = re.sub(r"[^a-z0-9a-яё-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:60] or fallback


def cwd_of_transcript(path):
    """Transkript yozuvlaridagi `cwd` maydoni - loyihaning haqiqiy papkasi."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh):
                if i > 40:
                    break
                try:
                    rec = json.loads(ln)
                    c = rec.get("cwd") or (rec.get("payload") or {}).get("cwd")
                except Exception:
                    continue
                if c:
                    return c
    except Exception:
        pass
    return ""


CODEX_LIKE = ("codex", "antigravity")

AGENT_LABELS = {
    "codex": "Codex",
    "antigravity": "Antigravity",
    "claude": "Claude Code",
}

SYNTHESIS_AGENT_LABELS = {
    "codex": "ChatGPT Codex",
    "antigravity": "Google Antigravity",
    "claude": "Claude Code",
}


def transcript_kind(path):
    """Transkript Claude Code, Codex yoki Antigravity formatidaligini aniqlaydi."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(10):
                raw = fh.readline()
                if not raw:
                    break
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if rec.get("type") == "session_meta":
                    payload = rec.get("payload") or {}
                    if payload.get("originator") == "antigravity":
                        return "antigravity"
                    return "codex"
                if "payload" in rec:
                    return "codex"
                if rec.get("type") in ("user", "assistant", "system"):
                    return "claude"
    except Exception:
        pass
    return "claude"


def session_id_of_transcript(path):
    """Fayl nomidan emas, mavjud bo'lsa transkript metadatasidan sessiya ID."""
    if transcript_kind(path) in CODEX_LIKE:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for _ in range(10):
                    rec = json.loads(fh.readline())
                    if rec.get("type") == "session_meta":
                        p = rec.get("payload") or {}
                        return p.get("id") or p.get("session_id")
        except Exception:
            pass
    return os.path.splitext(os.path.basename(path))[0]


def project_of(transcript=None, cwd=None):
    """Loyiha nomi. Avval transkript papkasi (.claude/projects/<kodlangan-yo'l>) -
    u to'liq yo'lni saqlaydi; bo'lmasa cwd ning oxirgi ikki bo'lagi.
    D:/demo-project/docs -> demo-project_docs (faqat 'docs' emas)."""
    if transcript and transcript_kind(transcript) in CODEX_LIKE:
        cwd = cwd or cwd_of_transcript(transcript)
        transcript = None
    if transcript:
        folder = os.path.basename(os.path.dirname(os.path.normpath(transcript)))
        if folder and folder.lower() != "projects":
            name = re.sub(r"^[a-zA-Z]--", "", folder)
            return name.replace("-", "_") or "umumiy"
    if cwd:
        parts = [p for p in os.path.normpath(cwd).replace("\\", "/").split("/") if p and ":" not in p]
        if parts:
            return "_".join(parts[-2:]) if len(parts) > 1 else parts[-1]
    return "umumiy"


# ---------------------------------------------------------------- transkript

SYNTH_MARKER = "Sen tajribali texnik kotibsan"


def is_own_synthesis(transcript):
    """Transkript bizning `claude -p` sintez chaqiruvimizmi?

    Ular ham oddiy sessiya kabi transkript yozadi. Endi cwd BASE ga
    yo'naltirilgan, lekin ilgari yaratilganlari loyiha papkalarida qolgan -
    ularni ishlab yuborsak, o'z promptimizni "bilim" deb yozib qo'yardik.
    """
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh):
                if i > 5:
                    break
                if SYNTH_MARKER in ln:
                    return True
    except Exception:
        pass
    return False


def _text_of(block):
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    return block.get("text") or ""


# Xatoni bildiruvchi so'zlar: `is_error` bayrog'i faqat 2.6% holatda qo'yiladi,
# ko'p nosozlik esa oddiy matn bo'lib qaytadi (masalan bash chiqishi).
ERROR_WORDS = re.compile(
    r"error|xato|хато|ошибка|traceback|exception|failed|failure|fatal|denied|"
    r"refused|not found|no such|cannot|can't|unable to|permission|timeout|"
    r"undefined|is not defined|modulenotfound|importerror|syntaxerror",
    re.I)


def _keep_result(body, is_error, cfg):
    """Qaysi tool natijasini digestga kiritish kerak.

    O'lchandi: tool chaqiruvlari + natijalari digestning 76% ini egallaydi va
    katta sessiyalarda foydali suhbatni siqib chiqaradi (171 MB sessiyadan
    atigi 2% o'qilardi). Muvaffaqiyatli natijalar bilim uchun deyarli hech
    narsa bermaydi - XATOLAR esa eng qimmatli qism, ular brifingdagi
    "Тузоқлар" bo'limiga aylanadi. Shuning uchun standart holatda faqat
    xatolar saqlanadi.
    """
    if not cfg.get("keep_only_error_results", True):
        return True
    if is_error:
        return True
    return bool(ERROR_WORDS.search(body[:2000]))


def parse_transcript(path, cfg, start_line=0):
    """.jsonl -> (digest, user xabarlari soni, birinchi savol, tugagan qator, davomi_bormi)

    start_line - shu qatordan keyingisini o'qiydi. Kompakt oldidan yoki sessiya
    o'rtasida chaqirilganda faqat YANGI qism ishlanadi: bir xil suhbat uchun
    ikki marta pul to'lanmaydi va takror fikr yozilmaydi.

    Chegaraga yetganda matnning O'RTASI kesilmaydi (avval shunday edi va
    171 MB sessiyadan atigi 2% ko'rinardi) - o'sha joyda TO'XTAYDI va
    "davomi bor" deb qaytaradi. Qolganini chaqiruvchi keyingi bo'lak
    sifatida ishlaydi.
    """
    lines = []
    n_user = 0
    first_ask = ""
    budget = int(cfg["max_digest_chars"])
    used = 0
    stopped_at = 0
    cap = int(cfg["max_tool_result_chars"])
    icap = int(cfg.get("max_tool_input_chars") or 150)
    line_no = 0

    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line_no += 1
            if line_no <= start_line:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if used >= budget:          # bo'lak to'ldi - shu yerda to'xtaymiz
                stopped_at = line_no - 1
                break

            rtype = rec.get("type")
            if rtype in ("response_item", "event_msg"):
                payload = rec.get("payload") or {}
                ptype = payload.get("type")
                before = len(lines)
                if rtype == "response_item" and ptype == "message":
                    role = payload.get("role")
                    parts = [_text_of(b) for b in (payload.get("content") or [])]
                    text = "\n".join(p.strip() for p in parts if p and p.strip())
                    if role == "user" and text and not text.lstrip().startswith(
                            ("<environment_context>", "<system-reminder>")):
                        n_user += 1
                        if not first_ask:
                            first_ask = text[:300]
                        lines.append("[USER] " + text)
                    elif role == "assistant" and text:
                        lines.append("[CODEX] " + text)
                elif rtype == "response_item" and ptype in (
                        "custom_tool_call", "function_call"):
                    inp = payload.get("input") or payload.get("arguments") or ""
                    if not isinstance(inp, str):
                        inp = json.dumps(inp, ensure_ascii=False)
                    lines.append("[TOOL] %s %s" % (
                        payload.get("name") or "?", inp[:icap]))
                elif rtype == "response_item" and ptype in (
                        "custom_tool_call_output", "function_call_output"):
                    body = payload.get("output")
                    if not isinstance(body, str):
                        body = json.dumps(body, ensure_ascii=False)
                    body = (body or "").strip().replace("\n", " ")
                    if body and _keep_result(body, False, cfg):
                        lines.append("[RESULT] " + body[:cap])
                used += sum(len(x) + 1 for x in lines[before:])
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            before = len(lines)

            if rtype == "user":
                parts, results = [], []
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            body = b.get("content")
                            if isinstance(body, list):
                                body = " ".join(_text_of(x) for x in body)
                            body = str(body or "").strip().replace("\n", " ")
                            if body and _keep_result(body, b.get("is_error"), cfg):
                                tag = "[XATO] " if b.get("is_error") else "[RESULT] "
                                results.append(tag + body[:cap])
                        else:
                            t = _text_of(b)
                            if t:
                                parts.append(t)
                text = "\n".join(p.strip() for p in parts if p.strip())
                if text and not text.lstrip().startswith("<system-reminder>"):
                    n_user += 1
                    if not first_ask:
                        first_ask = text[:300]
                    lines.append("[USER] " + text)
                lines.extend(results)

            elif rtype == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and (b.get("text") or "").strip():
                        lines.append("[CLAUDE] " + b["text"].strip())
                    elif b.get("type") == "tool_use":
                        inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                        lines.append("[TOOL] %s %s" % (b.get("name"), inp[:icap]))

            used += sum(len(x) + 1 for x in lines[before:])

    more = stopped_at > 0
    end_line = stopped_at if more else line_no
    return "\n".join(lines), n_user, first_ask, end_line, more


# ---------------------------------------------------------------- claude -p

def claude_bin():
    """claude CLI ni topadi.

    macOS/Linux da PATH dagi `claude` ning o'zi - oddiy bajariladigan fayl,
    boshqa hech narsa kerak emas.

    Windows da esa PATH dagi `claude.cmd` shunchaki bat shim: u ichida
    node_modules/@anthropic-ai/claude-code/bin/claude.exe ni chaqiradi.
    Fon jarayonda o'sha bat shim cmd.exe ni ochib osilib qoladi, shuning
    uchun .exe ni to'g'ridan-to'g'ri chaqiramiz.
    """
    if not WINDOWS:
        return shutil.which("claude")
    for name in ("claude.exe", "claude.cmd", "claude"):
        found = shutil.which(name)
        if not found:
            continue
        if found.lower().endswith(".exe"):
            return found
        real = os.path.join(os.path.dirname(found), "node_modules", "@anthropic-ai",
                            "claude-code", "bin", "claude.exe")
        if os.path.exists(real):
            return real
        return found
    return None


def run_claude(prompt, cfg):
    exe = claude_bin()
    if not exe:
        raise RuntimeError("claude CLI topilmadi (PATH da yo'q)")
    env = dict(os.environ)
    env[GUARD_ENV] = "1"   # rekursiya himoyasi: bola sessiya hook ni qayta ishlatmaydi
    cmd = [exe, "-p", "--output-format", "json", "--model", cfg["model"]]
    # claude.cmd - bat fayl, uni ishga tushirish uchun cmd.exe ga konsol kerak.
    # Fon jarayonda konsol yo'q, shuning uchun CREATE_NO_WINDOW bilan
    # ko'rinmas konsol beramiz - aks holda STATUS_CONTROL_C_EXIT bilan yiqiladi.
    # cwd MUHIM: `claude -p` ham oddiy sessiya kabi transkript yozadi va u
    # ishlagan papkaning loyihasiga tushadi. cwd berilmasa, sintez chaqiruvlari
    # foydalanuvchining loyiha ro'yxatida "Sen tajribali texnik kotibsan..."
    # bo'lib ko'rinardi va --backfill ularni bilim deb ishlab yuborishi mumkin
    # edi. BASE ga yo'naltiramiz - u skip_projects da.
    proc = subprocess.run(
        cmd, input=prompt, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=int(cfg["claude_timeout_sec"]),
        cwd=BASE, **quiet_kwargs()
    )
    if proc.returncode != 0:
        raise RuntimeError("claude -p xato (%s): %s"
                           % (proc.returncode, (proc.stderr or proc.stdout or "")[:600]))
    out = (proc.stdout or "").strip()
    meta = {}
    try:
        env_json = json.loads(out)
        usage = env_json.get("usage") or {}
        meta = {
            "cost_usd": float(env_json.get("total_cost_usd") or 0.0),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "duration_ms": int(env_json.get("duration_ms") or 0),
            "model": cfg["model"],
            "provider": "claude",
        }
        return (env_json.get("result") or ""), meta
    except Exception:
        return out, meta


def codex_bin():
    """Codex CLI executable faylini topadi."""
    return shutil.which("codex.exe") or shutil.which("codex")


def run_codex(prompt, cfg):
    """ChatGPT hisobidagi Codex CLI orqali headless sintez qiladi.

    --ephemeral ichki sintez sessiyasini tarixga yozmaydi; read-only esa
    kotib vazifasiga fayllarni o'zgartirish huquqi bermaydi.
    """
    exe = codex_bin()
    if not exe:
        raise RuntimeError("codex CLI topilmadi (PATH da yo'q)")
    fd, output_path = tempfile.mkstemp(prefix="claude-note-codex-", suffix=".txt")
    os.close(fd)
    cmd = [exe, "exec", "--ignore-user-config", "--ephemeral",
           "--skip-git-repo-check", "--sandbox", "read-only",
           "--cd", BASE, "--json", "--output-last-message", output_path]
    if cfg.get("codex_model"):
        cmd += ["--model", str(cfg["codex_model"])]
    cmd.append("-")
    env = dict(os.environ)
    env[GUARD_ENV] = "1"
    started = datetime.datetime.now()
    try:
        proc = subprocess.run(
            cmd, input=prompt, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=int(cfg.get("codex_timeout_sec") or 300),
            creationflags=CREATE_NO_WINDOW, cwd=BASE,
        )
        if proc.returncode != 0:
            raise RuntimeError("codex exec xato (%s): %s" % (
                proc.returncode, (proc.stderr or proc.stdout or "")[-1000:]))
        with open(output_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
        model = cfg.get("codex_model") or "codex-default"
        exec_usage = {}
        # `codex exec --json` oxirida turn.completed usage qaytaradi.
        # Eng katta yozuv olinadi: kelajakdagi CLI versiyasi oraliq usage
        # yozsa ham bir chaqiruvni ikki marta sanamaymiz.
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            usage = event.get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict) and int(usage.get("input_tokens") or 0) >= \
                    int(exec_usage.get("input_tokens") or 0):
                exec_usage = usage
        priced = codex_usage_price(model, exec_usage) if exec_usage else {}
        meta = {
            "cost_usd": round(float(priced.get("cost_usd") or 0), 6),
            "duration_ms": int((datetime.datetime.now() - started).total_seconds() * 1000),
            "model": model,
            "provider": "codex",
            "input_tokens": int(priced.get("in") or 0),
            "output_tokens": int(priced.get("out") or 0),
            "cache_write": int(priced.get("cw5") or 0),
            "cache_read": int(priced.get("cr") or 0),
        }
        return text, meta
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


def run_synthesis(prompt, cfg, source="claude"):
    """Tanlangan yoki transkriptga mos sintez provayderini ishga tushiradi."""
    provider = str(cfg.get("synthesis_provider") or "auto").lower()
    if provider == "auto":
        provider = source if source in ("claude", "codex") else "claude"
    if provider == "codex":
        if codex_bin():
            return run_codex(prompt, cfg)
        if claude_bin():
            return run_claude(prompt, cfg)
    elif provider == "claude":
        if claude_bin():
            return run_claude(prompt, cfg)
        if codex_bin():
            return run_codex(prompt, cfg)
    raise RuntimeError("sintez uchun na claude, na codex CLI topildi")


def extract_json(text):
    """Javobdan JSON obyektni ajratadi (code fence yoki matn ichidan)."""
    text = (text or "").strip()
    fence = text.find("```")
    if fence != -1:
        rest = text[fence + 3:]
        if rest[:4].lower().startswith("json"):
            rest = rest[4:]
        end = rest.find("```")
        text = rest[:end] if end != -1 else rest
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("javobda JSON topilmadi: " + text[:300])
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------- vault

FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


def note_path(cfg, slug):
    return os.path.join(cfg["vault"], "notes", slug + ".md")


def _front_of(path):
    try:
        with open(path, encoding="utf-8") as fh:
            m = FRONT_RE.match(fh.read(2000))
        if not m:
            return {}
        out = {}
        for ln in m.group(1).splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                out[k.strip()] = v.strip()
        return out
    except Exception:
        return {}


def existing_notes(cfg):
    out = []
    for path in sorted(glob.glob(os.path.join(cfg["vault"], "notes", "*.md"))):
        slug = os.path.splitext(os.path.basename(path))[0]
        fm = _front_of(path)
        out.append({
            "slug": slug,
            "title": fm.get("title") or slug,
            "desc": fm.get("desc") or "",
            "updated": fm.get("updated") or "",
            "projects": fm.get("projects") or "",
        })
    return out


def normalize(s):
    s = re.sub(r"_\(.*?\)_\s*$", "", str(s))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def existing_bullets(path):
    bullets = []
    if not os.path.exists(path):
        return bullets
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln.startswith("- "):
                bullets.append(normalize(ln[2:]))
    return bullets


def is_duplicate(text, bullets, ratio):
    n = normalize(text)
    if not n:
        return True
    for b in bullets:
        if not b:
            continue
        if n == b or difflib.SequenceMatcher(None, n, b).ratio() >= ratio:
            return True
    return False


def append_entry(cfg, entry, project, date, dry=False, agent="claude"):
    """Bitta eslatma fayliga bulletlarni qo'shadi -> (qo'shilgan soni, slug)."""
    slug = slugify(entry.get("note") or entry.get("note_title"))
    path = note_path(cfg, slug)
    section = entry.get("section") if entry.get("section") in SECTIONS else "Learnings"
    bullets = [str(b).strip() for b in (entry.get("bullets") or []) if str(b).strip()]
    if not bullets:
        return 0, slug

    known = existing_bullets(path)
    fresh = []
    for b in bullets:
        if is_duplicate(b, known + [normalize(x) for x in fresh], float(cfg["dedup_ratio"])):
            continue
        fresh.append(b)
    if not fresh:
        return 0, slug

    if dry:
        print("  -> %s.md  [%s]  +%d" % (slug, section, len(fresh)))
        for b in fresh:
            print("     - " + b[:170])
        return len(fresh), slug

    if not os.path.exists(path):
        head = [
            "---",
            "title: " + (entry.get("note_title") or slug),
            "desc: " + (entry.get("note_desc") or ""),
            "created: " + date,
            "updated: " + date,
            "projects: " + project,
            "tags: [" + project + "]",   # Obsidian teglari
            "---",
            "",
            "# " + (entry.get("note_title") or slug),
            "",
        ]
        body = "\n".join(head) + "\n".join("## " + s + "\n\n" for s in SECTIONS)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    def _front_fix(m):
        lines, seen_proj, seen_tags = [], False, False
        for ln in m.group(1).splitlines():
            if ln.startswith("updated:"):
                ln = "updated: " + date
            elif ln.startswith("projects:"):
                seen_proj = True
                vals = [v.strip() for v in ln.split(":", 1)[1].split(",") if v.strip()]
                if project not in vals:
                    vals.append(project)
                ln = "projects: " + ", ".join(vals)
            elif ln.startswith("tags:"):
                seen_tags = True
                vals = [v.strip() for v in ln.split(":", 1)[1].strip(" []").split(",") if v.strip()]
                if project not in vals:
                    vals.append(project)
                ln = "tags: [" + ", ".join(vals) + "]"
            lines.append(ln)
        if not seen_proj:
            lines.append("projects: " + project)
        if not seen_tags:
            lines.append("tags: [" + project + "]")
        return "---\n" + "\n".join(lines) + "\n---\n"

    text = FRONT_RE.sub(_front_fix, text, count=1)

    agent_label = AGENT_LABELS.get(agent, "Claude Code")
    block = "".join("- %s _(%s, %s, %s)_\n" %
                    (b, project, date, agent_label) for b in fresh)
    heading = "## " + section
    if heading in text:
        idx = text.index(heading) + len(heading)
        nxt = text.find("\n## ", idx)
        cut = nxt if nxt != -1 else len(text)
        chunk = text[idx:cut].rstrip("\n")
        text = text[:idx] + chunk + "\n" + block + "\n" + text[cut:]
    else:
        text = text.rstrip("\n") + "\n\n" + heading + "\n" + block

    text = merge_related(text, entry.get("related") or [], slug)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return len(fresh), slug


def merge_related(text, related, self_slug):
    """Obsidian [[wikilink]] larini 'Боғлиқ эслатмалар' bo'limiga qo'shadi.
    Graf ko'rinishi va backlink'lar shu havolalar hisobiga ishlaydi."""
    wanted = []
    for r in related:
        s = slugify(r)
        if s and s != self_slug and s not in wanted:
            wanted.append(s)
    if not wanted:
        return text

    have = set(re.findall(r"\[\[([^\]]+)\]\]", text))
    new = [s for s in wanted if s not in have]
    if not new:
        return text

    block = "".join("- [[%s]]\n" % s for s in new)
    if RELATED_HEADING in text:
        idx = text.index(RELATED_HEADING) + len(RELATED_HEADING)
        nxt = text.find("\n## ", idx)
        cut = nxt if nxt != -1 else len(text)
        chunk = text[idx:cut].rstrip("\n")
        return text[:idx] + chunk + "\n" + block + "\n" + text[cut:]
    return text.rstrip("\n") + "\n\n" + RELATED_HEADING + "\n" + block


def rebuild_index(cfg):
    notes = sorted(existing_notes(cfg), key=lambda n: n["updated"], reverse=True)
    out = [
        "# Билим базаси (claude-note)",
        "",
        "Автомат яратилади. Барча лойиҳалар учун ЯГОНА база.",
        "Тўлиқ ёзувлар: `" + os.path.join(cfg["vault"], "notes") + "`",
        "",
        "| Эслатма | Нима ҳақида | Лойиҳалар | Янгиланди |",
        "|---|---|---|---|",
    ]
    for n in notes:
        out.append("| [%s](notes/%s.md) | %s | %s | %s |"
                   % (n["title"], n["slug"], n["desc"] or "-",
                      n["projects"] or "-", n["updated"] or "-"))
    out.append("")
    out.append("_Жами: %d эслатма._" % len(notes))

    # loyihalar kesimi - har birining yig'ma sahifasiga havola
    projects = {}
    for r in merge_ledger_by_session(read_ledger(cfg)):
        p = r.get("project") or "umumiy"
        d = projects.setdefault(p, {"sessions": 0, "work": 0.0, "note": 0.0})
        d["sessions"] += 1
        d["work"] += work_cost(r)
        d["note"] += float(r.get("cost_usd") or 0)
    for n in notes:
        for p in [v.strip() for v in (n["projects"] or "").split(",") if v.strip()]:
            projects.setdefault(p, {"sessions": 0, "work": 0.0, "note": 0.0})
    if projects:
        out += ["", "## Лойиҳалар", "",
                "| Лойиҳа | Сессия | Иш нархи | Эслатмага ёзиш |", "|---|--:|--:|--:|"]
        for p, d in sorted(projects.items(), key=lambda kv: kv[1]["work"], reverse=True):
            out.append("| [[%s]] | %d | $%.4f | $%.4f |"
                       % (p, d["sessions"], d["work"], d["note"]))
        out.append("")

    with open(os.path.join(cfg["vault"], "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return len(notes)


# ------------------------------------------------- sessiya va loyiha sahifalari

def write_session_note(cfg, data, project, date, session_id, touched, work_cost,
                       note_cost, name=None, usage=None, tools=None,
                       tool_cost=None, agent="claude", sub=None):
    """Har bir suhbat uchun alohida sahifa: nima qilindi, qaysi eslatmalarga tushdi, narxi.

    Mavzu eslatmalari sessiyalarni aralashtirib yuboradi - bu sahifa esa
    "o'sha kuni o'sha suhbatda nima bo'ldi" savoliga javob beradi.
    """
    name = name or session_note_name(date, project, session_id, data.get("title"))
    path = session_note_path(cfg, project, name)
    agent_label = AGENT_LABELS.get(agent, "Claude Code")
    title = data.get("title") or "Сессия"
    # Yordamchi agentlar: ish narxiga qo'shiladi, lekin alohida ko'rsatiladi
    sub_usage, sub_cost, sub_agents = sub or ({}, 0.0, [])
    main_cost = work_cost
    work_cost = main_cost + float(sub_cost or 0)

    lines = [
        "---",
        "title: " + title,
        "date: " + date,
        "project: " + project,
        "session: " + str(session_id),
        "agent: " + agent,
        "work_cost_usd: %.4f" % work_cost,
        "note_cost_usd: %.4f" % note_cost,
        "tags: [" + project + ", sessiya]",
        "---",
        "",
        "# " + title,
        "",
        "**Agent:** " + agent_label,
        "",
        "**Лойиҳа:** [[%s|%s]] · **Сана:** %s · **Иш нархи:** $%.4f · **Эслатма:** $%.4f"
        % (project, project, date, work_cost, note_cost),
        "",
        data.get("summary") or "",
        "",
    ]
    if sub_cost:
        lines.insert(lines.index("tags: [" + project + ", sessiya]"),
                     "subagent_cost_usd: %.4f" % sub_cost)

    if sub_agents:
        lines += ["## Ёрдамчи агентлар", "",
                  "Иш нархи $%.4f = бош агент $%.4f + ёрдамчилар $%.4f."
                  % (work_cost, main_cost, sub_cost), "",
                  "| Ёрдамчи | Тури | Сўров | Энг катта контекст | Нарх |",
                  "|---|---|--:|--:|--:|"]
        for a in sorted(sub_agents, key=lambda x: -float(x.get("cost_usd") or 0)):
            lines.append("| %s | %s | %s | %s | $%.4f |"
                         % (cell(a.get("name"), 60), cell(a.get("type"), 20),
                            thousands(a.get("requests")),
                            thousands(a.get("peak_context")),
                            float(a.get("cost_usd") or 0)))
        lines.append("")

    if touched:
        lines += ["## Қайси эслатмаларга тушди", ""]
        for slug, section, bullets in touched:
            lines.append("### [[%s]] — %s" % (slug, section))
            for b in bullets:
                lines.append("- " + str(b))
            lines.append("")
    else:
        lines += ["_Бу сессиядан янги фикр чиқмади (ҳаммаси такрор эди)._", ""]

    ttbl = tool_cost_table(tools, tool_cost)
    if ttbl:
        lines += ["## Пул қайси ишга кетди", ""] + ttbl
        lines += ["", "_Тахминий: битта сўровнинг нархи ўша сўровда "
                      "чақирилган воситалар орасида тенг бўлинади. Аниқ "
                      "бўлиши мумкин эмас — пулни контекст ейди, восита эмас._",
                  ""]

    # Jadval jami ish narxiga teng chiqishi uchun yordamchilar ham qo'shiladi
    usage = merge_usage(merge_usage({}, usage or {}), sub_usage or {})
    tbl = usage_table(usage)
    if tbl:
        lines += ["## Иш нархи қаердан чиқди", ""]
        lines += tbl
        lines += ["", "Ишлатилган нархлар:", ""] + price_note(usage)
        lines += ["", "_Нарх = токенлар × расмий нарх. Обуна билан ишлаганда "
                      "ҳақиқий пул ечилмайди._", ""]

    lines += ["## Хом транскрипт", "",
              "`" + os.path.join(cfg["vault"], "log", name + ".md") + "`", ""]

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return name


def analyze_session(cfg, transcript):
    """Sessiya nimaga qimmatga tushganini ko'rsatadi va nima qilish kerakligini aytadi.

    Uzoq sessiyada narxning katta qismi kesh o'qishga ketadi: har bir so'rov
    (har bir tool chaqiruvi ham) butun kontekstni qaytadan o'qiydi.
    """
    seen, ctx, tools, per_model = set(), [], {}, {}
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tools[b.get("name")] = tools.get(b.get("name"), 0) + 1
            usage = msg.get("usage")
            if not usage:
                continue
            key = rec.get("requestId") or msg.get("id")
            model = msg.get("model") or "?"
            if not key or key in seen or model.startswith("<"):
                continue
            seen.add(key)
            ctx.append(int(usage.get("cache_read_input_tokens") or 0))
            per_model[model] = per_model.get(model, 0) + 1

    if not ctx:
        print("Bu transkriptda so'rov topilmadi.")
        return

    n = len(ctx)
    s = sorted(ctx)
    avg = sum(ctx) // n
    pm, total = session_usage(transcript)
    cr_cost = sum(d["cr"] * price_of(m)["cr"] for m, d in pm.items()) / 1e6
    per_req = avg * price_of(max(per_model, key=per_model.get))["cr"] / 1e6

    print("СЕССИЯ ТАҲЛИЛИ: " + os.path.basename(transcript))
    print("-" * 62)
    print("Сўровлар            : %s" % "{:,}".format(n))
    print("Контекст (мин/мед/макс): %s / %s / %s токен"
          % ("{:,}".format(s[0]), "{:,}".format(s[n // 2]), "{:,}".format(s[-1])))
    print("Умумий нарх         : $%.2f" % total)
    print("Шундан кэш ўқиш     : $%.2f  (%.0f%%)"
          % (cr_cost, 100.0 * cr_cost / total if total else 0))
    print("Битта сўров нархи   : ~$%.4f (фақат контекстни қайта ўқиш)" % per_req)
    print()
    print("Модел бўйича сўровлар:")
    for m, c in sorted(per_model.items(), key=lambda kv: -kv[1]):
        print("   %-20s %5d" % (m, c))
    print()
    print("Энг кўп ишлатилган tool (ҳар бири = 1 сўров):")
    for t, c in sorted(tools.items(), key=lambda kv: -kv[1])[:8]:
        print("   %-18s %5d  ~$%.2f" % (t, c, c * per_req))
    print()
    print("ТАВСИЯЛАР")
    print("-" * 62)
    if s[n // 2] > 150_000:
        for target in (50_000, 100_000):
            print("  Медиан контекст %s бўлганда кэш ўқиш: $%.2f (ҳозир $%.2f)"
                  % ("{:,}".format(target), n * target * price_of(
                      max(per_model, key=per_model.get))["cr"] / 1e6, cr_cost))
        print("  -> Мавзу орасида /clear қилинг: контекст нолдан бошланади.")
    if n > 500:
        print("  -> %s та сўров кўп. Bash буйруқларини битта чақирувга "
              "бирлаштириш сўров сонини камайтиради." % "{:,}".format(n))
    if any("opus" in m for m in per_model):
        opus = sum(c for m, c in per_model.items() if "opus" in m)
        print("  -> %d сўров Opus да. Оддий ишларда Sonnet 5 кэш ўқишда "
              "2.5 баробар арзон ($0.20 vs $0.50 / 1M)." % opus)


BRIEF_SECTIONS = ["Тузилиш", "Технологиялар", "Қоидалар", "Тузоқлар", "Муҳим йўллар"]


def brief_path(cfg, project):
    return os.path.join(cfg["vault"], "projects", project + "-brief.md")


PROJECT_MARKERS = (".git", "package.json", "requirements.txt", "pyproject.toml",
                   "go.mod", "docker-compose.yml", "Cargo.toml", "pom.xml",
                   "manage.py", "pubspec.yaml", "*.sln")
SCAN_SKIP = {"node_modules", ".venv", "venv", "__pycache__", "target", "dist",
             "build", ".next", "vendor", "site-packages", ".git", "extracted_files",
             "tools", "misc"}


def project_name_for_path(path):
    """Claude Code transkript papkasini qanday nomlasa, shunday nom beradi.

    D:\\demo-project\\app -> d--demo-project-app -> demo-project_app
    """
    parts = [p for p in os.path.normpath(path).replace("\\", "/").split("/")
             if p and ":" not in p]
    return "_".join(parts) or "umumiy"


def scan_projects(root=None, max_depth=3):
    """Diskdagi loyiha papkalarini topadi (marker fayllar bo'yicha).

    Marker topilgan papkaning ichiga kirilmaydi - u loyiha ildizi hisoblanadi.
    """
    root = root or os.path.expanduser("~")
    found = []

    def walk(d, depth):
        if depth > max_depth:
            return
        try:
            names = os.listdir(d)
        except Exception:
            return
        nameset = set(names)
        hit = [m for m in PROJECT_MARKERS if m in nameset]
        if not hit and any(n.endswith(".sln") for n in names):
            hit = ["*.sln"]
        if hit:
            found.append({"path": os.path.normpath(d), "markers": ",".join(hit)})
            return
        for n in names:
            if n in SCAN_SKIP or n.startswith("."):
                continue
            sub = os.path.join(d, n)
            if os.path.isdir(sub):
                walk(sub, depth + 1)

    walk(root, 0)
    return found


def registry_path(cfg):
    return os.path.join(cfg["state"], "registry.json")


def load_registry(cfg):
    try:
        with open(registry_path(cfg), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def resolve_project(cfg, transcript, cwd):
    """Loyiha nomini ishonchli aniqlaydi.

    SessionEnd payloadida transcript_path bo'ladi va nom undan chiqadi
    (masalan `demo-project`), SessionStart da esa u bo'lmasligi mumkin - u holda
    cwd dan boshqa nom chiqib ketardi (`demo-project_app`) va brifing
    topilmasdi. Shuning uchun brifinglarning `path:` maydoni bo'yicha ham
    qidiramiz.
    """
    name = project_of(transcript, cwd)
    if os.path.exists(brief_path(cfg, name)):
        return name
    if cwd:
        target = os.path.normcase(os.path.normpath(cwd))
        best, best_len = None, -1
        for f in glob.glob(os.path.join(cfg["vault"], "projects", "*-brief.md")):
            recorded = (_front_of(f).get("path") or "").strip()
            if not recorded:
                continue
            rec = os.path.normcase(os.path.normpath(recorded))
            # aynan o'sha papka yoki uning ichidagi papka
            if target == rec or target.startswith(rec + os.sep):
                if len(rec) > best_len:
                    best, best_len = os.path.basename(f)[:-len("-brief.md")], len(rec)
        if best:
            return best
        # brifing yo'q bo'lsa - skanerdan chiqqan ro'yxatga qaraymiz
        best, best_len = None, -1
        for rec_path, rec_name in (load_registry(cfg) or {}).items():
            rec = os.path.normcase(os.path.normpath(rec_path))
            if target == rec or target.startswith(rec + os.sep):
                if len(rec) > best_len:
                    best, best_len = rec_name, len(rec)
        if best:
            return best
    return name


def cmd_scan(cfg, root=None):
    """Diskdagi loyihalarni ro'yxatga oladi.

    Hook lar allaqachon global - har qanday papkada ishlagan sessiya avtomat
    hisobga tushadi. Ro'yxat esa nom chalkashmasligi uchun kerak: loyihaning
    ichki papkasidan sessiya ochilsa ham xarajat va brifing bitta nom ostida
    yig'iladi.
    """
    if not root or root == "auto":
        root = cfg.get("scan_root") or os.path.expanduser("~")
    found = scan_projects(root)
    claude_dirs = {os.path.basename(d).lower()
                   for d in glob.glob(os.path.join(os.path.expanduser("~"),
                                                   ".claude", "projects", "*"))}
    reg, rows = {}, []
    for f in found:
        path = f["path"]
        name = project_name_for_path(path)
        reg[path] = name
        drive = os.path.splitdrive(path)[0].rstrip(":").lower()
        encoded = "%s--%s" % (drive, name.replace("_", "-"))
        used = encoded.lower() in claude_dirs
        try:
            mtime = datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()
        except Exception:
            mtime = "-"
        rows.append({"path": path, "name": name, "markers": f["markers"],
                     "used": used, "mtime": mtime})

    with open(registry_path(cfg), "w", encoding="utf-8") as fh:
        json.dump(reg, fh, ensure_ascii=False, indent=1)

    rows.sort(key=lambda r: (not r["used"], r["path"].lower()))
    out = [
        "# Лойиҳалар рўйхати",
        "",
        "`note scan` билан автомат яратилади. Диск: `%s`" % root,
        "",
        "«Claude ишлатилган» — ўша папкада аввал Claude Code сессияси бўлганми.",
        "Ҳисоб бугундан бошланади: қайси папкада ишласангиз, сарф ўша лойиҳа "
        "номи остида йиғилади.",
        "",
        "| Лойиҳа | Йўл | Белгилар | Claude ишлатилган | Охирги ўзгариш |",
        "|---|---|---|:-:|---|",
    ]
    for r in rows:
        out.append("| `%s` | `%s` | %s | %s | %s |"
                   % (r["name"], r["path"], r["markers"],
                      "✅" if r["used"] else "—", r["mtime"]))
    out += ["", "_Жами: %d лойиҳа, шундан %d тасида Claude ишлатилган._"
            % (len(rows), sum(1 for r in rows if r["used"])), ""]
    with open(os.path.join(cfg["vault"], "PROJECTS.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return rows


def read_brief(cfg, project, limit=25000):
    """Sintez uchun mavjud brifing matni (takror yozmasligi uchun)."""
    try:
        with open(brief_path(cfg, project), encoding="utf-8") as fh:
            return fh.read()[:limit]
    except Exception:
        return "(brifing hali yo'q)"


def _brief_insert(text, section, line):
    """Brifing matnida `## section` bo'limi oxiriga `- line` qatorini qo'shadi."""
    heading = "## " + section
    block = "- %s\n" % line
    if heading in text:
        idx = text.index(heading) + len(heading)
        nxt = text.find("\n## ", idx)
        cut = nxt if nxt != -1 else len(text)
        chunk = text[idx:cut].rstrip("\n")
        return text[:idx] + chunk + "\n" + block + "\n" + text[cut:]
    return text.rstrip("\n") + "\n\n" + heading + "\n" + block


def write_brief(cfg, project, items, date, cwd=""):
    """Loyihaning doimiy brifingi - /clear dan keyin ishni tez tiklash uchun.

    Maqsad: yangi sessiya 350k kontekstdan emas, shu qisqa hujjatdan boshlasin.
    """
    if not items:
        return 0
    path = brief_path(cfg, project)
    if not os.path.exists(path):
        head = [
            "---",
            "title: " + project + " — брифинг",
            "desc: Янги сессия учун: лойиҳа ҳақида билиш шарт бўлган доимий маълумот",
            "project: " + project,
            "path: " + (cwd or ""),
            "updated: " + date,
            "tags: [" + project + ", brifing]",
            "---",
            "",
            "# " + project + " — брифинг",
            "",
            "> Бу файл автомат тўлдирилади. `/clear` дан кейин ёки янги сессияда "
            "аввал шуни ўқинг — узоқ тушунтиришга вақт кетмайди.",
            "",
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(head) + "\n"
                     + "\n".join("## " + s + "\n\n" for s in BRIEF_SECTIONS))

    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    known = [normalize(l[2:]) for l in text.splitlines() if l.strip().startswith("- ")]
    added = 0
    for it in items:
        section = it.get("section") if it.get("section") in BRIEF_SECTIONS else "Тузилиш"
        line = str(it.get("text") or "").strip()
        if not line or is_duplicate(line, known, float(cfg["dedup_ratio"])):
            continue
        known.append(normalize(line))
        text = _brief_insert(text, section, line)
        added += 1

    # Almashtirish matni lambda orqali beriladi: Windows yo'llaridagi teskari
    # slesh (D:\demo-project\app) re.sub uchun escape bo'lib ketardi
    # va "bad escape \l" xatosi bilan butun ishlov yiqilardi.
    text = re.sub(r"^updated: .*$", lambda m: "updated: " + date, text, count=1, flags=re.M)
    if cwd:
        text = re.sub(r"^path: .*$", lambda m: "path: " + cwd, text, count=1, flags=re.M)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return added


# Brifing SessionStart hook orqali additionalContext bo'lib beriladi. Claude Code
# ~10 000 belgidan uzun kontekstni faylga olib qo'yib, modelga faqat ~2 KB
# boshini ko'rsatadi - ya'ni katta brifing amalda yo'qoladi. Shuning uchun:
#   - hook butun kontekstni SESSION_CONTEXT_MAX dan oshirmaydi: brifing sig'sa -
#     to'liq, sig'masa - bo'limlar muhimlik tartibida, butun bo'lim bo'lib, qolgani
#     uchun fayl yo'li va hajmi ko'rsatiladi (brief_for_context);
#   - brifing BRIEF_COMPACT_AT dan oshsa yoki imlosi buzilsa, LLM uni "yo'qotishsiz
#     minimal" holga keltiradi (hajm maqsadi yo'q); eski to'liq matn
#     <name>-brief.full.md arxivida qoladi.
SESSION_CONTEXT_MAX = 9900
BRIEF_COMPACT_AT = 9000
BRIEF_CONTEXT_PRIORITY = ["Қоидалар", "Тузоқлар", "Тузилиш", "Муҳим йўллар"]
BRIEF_COMPACT_RETRY_GROWTH = 3000


def brief_full_path(cfg, project):
    return os.path.join(cfg["vault"], "projects", project + "-brief.full.md")


def cut_at_boundary(text, limit):
    """Matnni `limit` belgigacha QATOR chegarasida kesadi.

    Markdown jadval o'rtasida yoki bo'lim sarlavhasidan keyin osilib qolmaydi:
    jadval bo'lsa uning boshigacha, oxirgi qator sarlavha bo'lsa undan oldin
    kesiladi. Bitta qatorning o'zi limitdan uzun bo'lsa - so'z chegarasida.
    """
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    out, size = [], 0
    for ln in lines:
        add = len(ln) + (1 if out else 0)
        if size + add > limit:
            break
        out.append(ln)
        size += add
    if not out:                                  # birinchi qatorning o'zi juda uzun
        head = text[:limit]
        sp = head.rfind(" ")
        return (head[:sp] if sp > limit // 2 else head).rstrip()
    nxt = lines[len(out)] if len(out) < len(lines) else ""
    if out[-1].lstrip().startswith("|") and nxt.lstrip().startswith("|"):
        while out and out[-1].lstrip().startswith("|"):
            out.pop()                            # chala jadvalni to'liq tashlaymiz
    while out and (not out[-1].strip() or out[-1].startswith("#")):
        out.pop()                                # osilib qolgan sarlavha/bo'sh qator
    return "\n".join(out).rstrip()


def brief_for_context(body, room, bpath):
    """SessionStart uchun brifing matni - `room` belgiga sig'adigan qilib.

    Sig'sa - to'liq, o'z tartibida. Sig'masa - bo'limlar MUHIMLIK tartibida
    (BRIEF_CONTEXT_PRIORITY, keyin qolganlari) faqat butun bo'lim bo'lib
    olinadi; sig'maganlari ro'yxati va fayl yo'li oxirida ko'rsatiladi.
    Eng muhim bo'limning o'zi sig'masa - u qator chegarasida qisman beriladi.
    """
    if len(body) <= room:
        return body
    first = body.find("\n## ")
    if first == -1:
        return cut_at_boundary(body, room)
    intro = body[:first].strip()
    secs = []
    for chunk in re.split(r"\n(?=## )", body[first + 1:]):
        chunk = chunk.strip()
        if chunk:
            secs.append((chunk.split("\n", 1)[0][3:].strip(), chunk))
    rank = {name: i for i, name in enumerate(BRIEF_CONTEXT_PRIORITY)}
    order = sorted(range(len(secs)), key=lambda i: (rank.get(secs[i][0], 99), i))

    def footer(left):
        if not left:
            return ""
        return ("\n\n_Брифинг давоми: `%s` (%d бўлим, %d белги: %s) — вазифага тегишли "
                "бўлса Read қил._" % (bpath, len(left), sum(len(c) for _n, c in left),
                                     ", ".join(n for n, _c in left)))
    reserve = len(footer(secs)) + 20             # eng yomon holat - hammasi qoldi
    out, left, size = [intro] if intro else [], [], len(intro)
    for i in order:
        name, chunk = secs[i]
        if size + 2 + len(chunk) + reserve <= room:
            out.append(chunk)
            size += 2 + len(chunk)
        elif len(out) <= (1 if intro else 0) and i == order[0]:
            part = cut_at_boundary(chunk, room - size - reserve - 2)
            out.append(part)
            size += 2 + len(part)
            left.append((name + " (қисман)", chunk[len(part):]))
        else:
            left.append((name, chunk))
    return "\n\n".join(out) + footer(left)


# Brifingni bir nechta SessionStart hookiga bo'lib berish (--part N).
# ~10 000 belgi chegarasi HAR BIR hook chiqishiga alohida qo'llanadi (2026-10-05
# sinovda isbotlangan: 3 ta ~8 800 belgilik hook - uchalasi ham to'liq ko'rindi).
# Hooklar parallel ishlaydi va kontekstga IXTIYORIY tartibda tushadi - shuning
# uchun har qism o'zini tanishtiradi, bo'lim faqat ## chegarasida (juda katta
# bo'lim - qator chegarasida, "(давоми)" sarlavhasi bilan) bo'linadi.
BRIEF_PARTS = 3
BRIEF_PART_ORDER = ["Қоидалар", "Тузоқлар", "Тузилиш", "Технологиялар", "Муҳим йўллар"]
BRIEF_SECTION_SPLIT = 9500


def _split_lines(text, limit):
    """Matnni `limit` dan oshmaydigan bo'laklarga QATOR chegarasida bo'ladi."""
    chunks, rest = [], text
    while len(rest) > limit:
        part = cut_at_boundary(rest, limit)
        if not part.strip():                     # chala jadval tashlandi - oddiy qator
            nl = rest.rfind("\n", 0, limit)
            part = rest[:nl] if nl > 0 else rest[:limit]
        chunks.append(part.rstrip())
        rest = rest[len(part):].lstrip("\n")
    if rest.strip():
        chunks.append(rest.rstrip())
    return chunks


def brief_parts(body, rooms, bpath):
    """Brifingni len(rooms) qismga bo'ladi: i-qism rooms[i] belgidan oshmaydi.

    Bo'limlar BRIEF_PART_ORDER tartibida (keyin qolganlari o'z tartibida)
    birinchi sig'adigan qismga joylanadi. BRIEF_SECTION_SPLIT dan (yoki qism
    sig'imidan) katta bo'lim qator chegarasida bo'linadi. Hech qaysi qismga
    sig'maganlari oxirgi qismdagi izohda fayl yo'li bilan ko'rsatiladi.
    Qaytaradi: [qism matni] (bo'sh qism - "").
    """
    pieces = [p.strip() for p in re.split(r"(?m)^(?=## )", body) if p.strip()]
    units = []                                   # (tartib kaliti, nom, matn)
    rank = {name: i for i, name in enumerate(BRIEF_PART_ORDER)}
    biggest = min(BRIEF_SECTION_SPLIT, max(rooms))
    for idx, p in enumerate(pieces):
        if p.startswith("## "):
            name = p.split("\n", 1)[0][3:].strip()
            key = (rank.get(name, len(rank)), idx)
        else:
            name, key = "", (-1, idx)            # kirish qismi - har doim birinchi
        if len(p) <= biggest:
            units.append((key, name, p))
            continue
        head = "## %s (давоми)\n" % name if name else ""
        for j, ch in enumerate(_split_lines(p, biggest - len(head))):
            units.append((key + (j,), name, ch if j == 0 else head + ch))
    units.sort(key=lambda u: u[0])

    def pack(caps):
        parts, left = [[] for _ in caps], []
        sizes = [0] * len(caps)
        for _k, name, text in units:
            for i, cap in enumerate(caps):
                add = len(text) + (2 if parts[i] else 0)
                if sizes[i] + add <= cap:
                    parts[i].append(text)
                    sizes[i] += add
                    break
            else:
                left.append((name or "кириш", text))
        return parts, left

    def footer(left):
        names = []
        for n, _t in left:
            if n not in names:
                names.append(n)
        return ("\n\n_Брифинг давоми (қисмларга сиғмади): `%s` (%d белги: %s) — вазифага "
                "тегишли бўлса Read қил._" % (bpath, sum(len(t) for _n, t in left),
                                             ", ".join(names)))

    caps = list(rooms)
    parts, left = pack(caps)
    if left:                                     # izoh uchun oxirgi qismda joy ochamiz
        caps[-1] -= len(footer(left)) + 40
        parts, left = pack(caps)
    texts = ["\n\n".join(p) for p in parts]
    if left:                                     # caps[-1] aynan shu izoh uchun kamaytirilgan
        texts[-1] = (texts[-1] + footer(left)).strip()
    return texts


def _brief_bullets(text):
    """Brifingdagi '- ' qatorlar: [(bo'lim, matn)] tartib bilan."""
    res, section = [], "Тузилиш"
    for ln in text.splitlines():
        if ln.startswith("## "):
            section = ln[3:].strip()
        elif ln.strip().startswith("- "):
            res.append((section, ln.strip()[2:].strip()))
    return res


def _compacted_lines(cfg, project=None, lines=None):
    """state/brief-compacted.json: loyiha -> oxirgi ixchamlash yozgan qatorlar.
    lines berilsa - shu loyiha uchun yozadi."""
    path = os.path.join(cfg["state"], "brief-compacted.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    if project is not None:
        data[project] = sorted({normalize(t) for t in lines or []})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=0)
    return data


def archive_brief(cfg, project, text):
    """Ixchamlashdan oldingi brifingni <name>-brief.full.md ga saqlaydi.

    Arxiv birinchi marta - to'liq nusxa. Keyingi ixchamlashlarda ichidagi hech
    narsa o'chirilmaydi: faqat arxivda hali yo'q qatorlar o'z bo'limiga
    qo'shiladi. Shunda hech qaysi ixchamlashda ma'lumot yo'qolmaydi.
    Oldingi ixchamlash YOZGAN qatorlar (brief-compacted.json) qo'shilmaydi - ular
    arxivdagi asl qatorlarning qisqa nusxasi, aks holda arxiv takror bilan shishadi.
    """
    done = set(_compacted_lines(cfg).get(project) or [])
    path = brief_full_path(cfg, project)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
    with open(path, encoding="utf-8") as fh:
        arch = fh.read()
    known = {normalize(t) for _s, t in _brief_bullets(arch)}
    for section, line in _brief_bullets(text):
        if normalize(line) not in known and normalize(line) not in done:
            known.add(normalize(line))
            arch = _brief_insert(arch, section, line)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(arch)
    return path


BRIEF_COMPACT_PROMPT = """Сен тажрибали техник котибсан. Қуйида «%(project)s» лойиҳасининг \
брифинги — янги Claude Code сессиясига автомат бериладиган доимий маълумот. У \
кўп сессия давомида қатор-қатор қўшилиб ўсган (%(size)d белги). Уни ЙЎҚОТИШСИЗ \
МИНИМАЛ ҳолга келтир.

Қоидалар:
- Ҳажм мақсади йўқ: натижа табиий равишда қанча чиқса, шунча. Сунъий кесма, \
лекин ортиқча сўз ҳам қолдирма.
- Такрор ёки бир-бирига яқин қаторларни битта аниқ қаторга бирлаштир.
- Эскирган ёки кейинги қатор инкор қилган маълумотни ташла (бўлим ичида пастдаги \
қатор — янгироқ; зид келса, янгисига ишон).
- Кодни очиб осон билиб олинадиган нарсани (файл ичидаги оддий тафсилот, функция ёки \
майдон рўйхати) ташла.
- ҲАЛИ ТЎҒРИ бўлган ҳар бир қарор, фойдаланувчи қоидаси, тузоқ ва унинг ечими, лойиҳа \
тузилиши, муҳим файл/папка йўли, буйруқ, порт, сервер САҚЛАНСИН — биронтасини ҳам \
йўқотма. Аниқ йўл, буйруқ ва номларни ўзгартирма.
- Тарих, «қачон ва ким қилди», бир марталик иш тафсилотлари — ташла ёки бир неча \
сўзга қисқартир (тўлиқ нусха архивда сақланади).
- Ҳар қатор қисқа ва мустақил тушунарли бўлсин. Тил — %(lang)s.
- Имло: ўзбек кирилл ҳарфлари (қ, ў, ҳ, ғ) тўғри, кичик ҳарф билан ёзилсин; сўз ичида \
бош ҳарф бўлмасин. Киришда «фаҲат», «ҲаР», «йУҲ» каби бузилган сўзлар бўлса — тўғрисини \
ёз («фақат», «ҳар», «йўқ»).
- Бўлимлар фақат шулар: %(sections)s. Бўлим бўш бўлса — бўш рўйхат.

ФАҚАТ JSON қайтар, бошқа матнсиз:
{"sections": {"Тузилиш": ["..."], "Технологиялар": ["..."], "Қоидалар": ["..."], \
"Тузоқлар": ["..."], "Муҳим йўллар": ["..."]}}

Брифинг:
<<<
%(body)s
>>>
"""


# ------------------------------------------------ o'zbek kirill imlosi
# claude-sonnet-5 o'zbek kirill matnida ba'zan harflarni buzadi: "фақат" ->
# "фаҲат", "йўқ" -> "йУҲ", "ҳар" -> "ҲаР". Kodirovka emas (xom javob tekshirildi):
# model kirish matnidagi buzilgan so'zlarni takrorlaydi va KUCHAYTIRADI, buzilgan
# eslatma/brifing esa keyingi har bir sintez promptiga qaytib kiradi - o'z-o'zini
# oziqlantiruvchi aylana. claude-opus-5 buzilgan kirishdan ham toza matn yozadi.
# Himoya: (1) promptga beriladigan kontekst lug'at bilan tozalanadi, (2) model
# javobi tekshiriladi - buzilgan bo'lsa kuchli model bilan qayta so'raladi yoki
# lug'at bilan tuzatiladi, (3) --fix-imlo mavjud bazani tozalaydi.
_MIXED_CASE_RE = re.compile(r"[а-яёўқғҳ][А-ЯЁЎҚҒҲ]")
_CYR_WORD_RE = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳ]+")
_ORTHO_OK_WORDS = {"кВт", "мВт", "мА", "кГц", "мГц", "кБ", "мБ", "кН"}
# bosh harf -> ehtimoliy to'g'ri kichik harflar (model ko'pincha қ ni Ҳ/К, ў ni У qiladi)
_ORTHO_ALT = {"Ҳ": "қҳғ", "Қ": "қк", "К": "кқ", "Х": "хҳ", "У": "уў", "Ў": "ўу",
              "Г": "гғ", "Ғ": "ғг", "О": "оў"}
_ORTHO_VOCAB = {}


def mixed_case_rate(text):
    """So'z ichidagi bosh harflar (1000 belgiga). Toza matnda ~0."""
    return len(_MIXED_CASE_RE.findall(text or "")) * 1000.0 / max(len(text or ""), 1)


_UZ_ONLY_RE = re.compile(r"[ЎўҚқҒғҲҳ]")


def _ortho_suspect(word):
    """Buzilgan so'zmi? 1С kodidagi rus CamelCase identifikatorlar (ВидНоменклатуры,
    ЖурналОбмена) - bosh harf bilan boshlanadi va o'zbekcha harfsiz - buzilish emas."""
    if not _MIXED_CASE_RE.search(word) or word in _ORTHO_OK_WORDS:
        return False
    return not (word[0].isupper() and not _UZ_ONLY_RE.search(word))


def ortho_bad_words(text):
    """Kirill so'z ichida kichik harfdan keyin bosh harf kelgan so'zlar.
    Lotin/raqam aralash so'z (API, Go'da) va rus CamelCase hisobga olinmaydi."""
    prose = _CODE_SPAN_RE.sub(" ", text or "")
    return [w for w in _CYR_WORD_RE.findall(prose) if _ortho_suspect(w)]


def ortho_ok(text, per=6000):
    """Matn imlosi yaroqlimi: buzilgan so'z soni ~6000 belgiga 1 tadan oshmasin."""
    return len(ortho_bad_words(text)) <= max(1, len(text or "") // per)


def ortho_vocab(cfg):
    """Bazadagi TOZA kirill so'zlar chastotasi (kichik harfda) - tuzatish lug'ati."""
    key = cfg["vault"]
    if key in _ORTHO_VOCAB:
        return _ORTHO_VOCAB[key]
    vocab = {}
    for path in glob.glob(os.path.join(cfg["vault"], "**", "*.md"), recursive=True):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        for w in _CYR_WORD_RE.findall(text):
            if not _MIXED_CASE_RE.search(w):
                lw = w.lower()
                vocab[lw] = vocab.get(lw, 0) + 1
    _ORTHO_VOCAB[key] = vocab
    return vocab


_ORTHO_LOWER_ALT = {"у": "ў", "к": "қ", "х": "ҳ", "г": "ғ"}
_CODE_SPAN_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)


def _ortho_fix_word(word, vocab):
    """Buzilgan so'z uchun lug'atdagi eng ko'p uchragan to'g'ri variant.
    Avval faqat bosh harflar (va so'z boshi) almashtiriladi; topilmasa - kichik
    у/к/х/г ham ў/қ/ҳ/ғ bo'lishi mumkin deb qaraladi ("йуКлигида" -> "йўқлигида").
    Topilmasa - (oddiy kichik harfli shakl, False)."""
    import itertools
    pos = [i for i in range(1, len(word)) if word[i].isupper()]
    plain = word[0] + "".join(c.lower() if i in pos else c
                              for i, c in enumerate(word) if i > 0)
    first = list(dict.fromkeys(word[0] + "".join(
        c.upper() for c in _ORTHO_ALT.get(word[0], ""))))
    for wide in (False, True):
        slots = [(0, first)] + [(i, _ORTHO_ALT.get(word[i], word[i].lower())) for i in pos]
        if wide:
            slots += [(i, c + _ORTHO_LOWER_ALT[c]) for i, c in enumerate(word)
                      if i > 0 and c in _ORTHO_LOWER_ALT]
        n_combo = 1
        for _i, o in slots:
            n_combo *= len(o)
        if n_combo > 4096:
            continue
        best, best_n = None, 0
        for combo in itertools.product(*[o for _i, o in slots]):
            chars = list(word)
            for (i, _o), c in zip(slots, combo):
                chars[i] = c
            cand = "".join(chars)
            n = vocab.get(cand.lower(), 0)
            if n > best_n:
                best, best_n = cand, n
        if best:
            return best, True
    return plain, False


def ortho_repair(text, cfg):
    """Matndagi buzilgan kirill so'zlarni lug'at bo'yicha tuzatadi (`kod` ichiga
    tegmaydi). Qaytaradi: (matn, tuzatilgan_soni, lug'atda_topilmagan_soni)."""
    if not text or not _MIXED_CASE_RE.search(text):
        return text, 0, 0
    vocab = ortho_vocab(cfg)
    stats = [0, 0]

    def fix(m):
        w = m.group(0)
        if not _ortho_suspect(w):
            return w
        new, found = _ortho_fix_word(w, vocab)
        stats[0 if found else 1] += 1
        # Gap o'rtasida ("... ҲаР куни") birinchi harf ham kichik bo'lishi kerak.
        before = m.string[:m.start()].rstrip(" ")
        if new[0].isupper() and before and (before[-1].isalnum() or before[-1] in ",;(")                 and vocab.get(new.lower()):
            new = new[0].lower() + new[1:]
        return new
    parts = _CODE_SPAN_RE.split(text)
    for k in range(0, len(parts), 2):            # toq indekslar - kod bo'laklari
        parts[k] = _CYR_WORD_RE.sub(fix, parts[k])
    return "".join(parts), stats[0], stats[1]


def _ortho_walk(obj, fn):
    """JSON tuzilmasidagi barcha satrlarga fn qo'llaydi (kalitlarga emas)."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_ortho_walk(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _ortho_walk(v, fn) for k, v in obj.items()}
    return obj


def ortho_data_text(data):
    out = []
    _ortho_walk(data, lambda s: out.append(s) or s)
    return "\n".join(out)


def _brief_sections_text(sections):
    out = []
    for sec in BRIEF_SECTIONS:
        items = [re.sub(r"^-\s+", "", str(x).strip()).strip()
                 for x in (sections.get(sec) or []) if str(x).strip()]
        out.append("## " + sec + "\n" + "".join("- %s\n" % x for x in items))
    return "\n".join(out)


def compact_brief(cfg, project, date=None, source="claude", force=False, dry=False,
                  from_full=False):
    """Brifingni LLM bilan "yo'qotishsiz minimal" holga keltiradi.

    Qaytaradi: (holat, eski_uzunlik, yangi_uzunlik, narx_usd).
    Faqat kerak bo'lganda ishlaydi: brifing BRIEF_COMPACT_AT dan uzun yoki imlosi
    buzilgan bo'lsa (force - har doim). Hajm maqsadi YO'Q: takror, eskirgan va
    koddan ko'rinadigan narsa olib tashlanadi, hali to'g'ri qaror/qoida/tuzoq/yo'l
    saqlanadi - hajm loyihaga qarab tabiiy chiqadi.
    Ixchamlashdan OLDIN to'liq matn arxivga yoziladi. from_full=True - manba
    joriy brifing emas, <name>-brief.full.md arxivi (qayta ixchamlash uchun).
    Model: cfg["strong_model"] (sonnet o'zbek kirillni buzadi). Javob yaroqsiz,
    imlosi buzilgan yoki manbadan uzun bo'lsa - qayta so'raladi (3 urinish).
    """
    path = brief_path(cfg, project)
    full = brief_full_path(cfg, project)
    if not os.path.exists(path):
        return ("yo'q", 0, 0, 0.0)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = FRONT_RE.match(text)
    front = m.group(0) if m else ""
    old_len = len(FRONT_RE.sub("", text, count=1).strip())
    if not force and ortho_ok(text):
        if old_len <= BRIEF_COMPACT_AT:
            return ("kerak emas", old_len, old_len, 0.0)
        # Yo'qotishsiz ixchamlangan katta brifing tabiiy ravishda 9000 dan uzun
        # qolishi mumkin - har sessiyada qayta ixchamlamaslik uchun oxirgi
        # ixchamlashdan keyin qo'shilgan yangi qatorlar yetarli bo'lsagina.
        done = _compacted_lines(cfg).get(project)
        if done is not None:
            done = set(done)
            fresh = sum(len(t) for _s, t in _brief_bullets(text) if normalize(t) not in done)
            if fresh < max(BRIEF_COMPACT_RETRY_GROWTH, old_len // 4):
                return ("kerak emas", old_len, old_len, 0.0)
    if "\n## " not in "\n" + text:
        return ("bo'limsiz", old_len, old_len, 0.0)
    if dry:
        return ("sinov", old_len, old_len, 0.0)
    # Muvaffaqiyatsiz ixchamlash har sessiya oxirida qayta-qayta pul sarflamasin:
    # oxirgi xatodan keyin brifing BRIEF_COMPACT_RETRY_GROWTH belgiga o'smaguncha kutamiz.
    fail_path = os.path.join(cfg["state"], "brief-compact-fail.json")
    try:
        with open(fail_path, encoding="utf-8") as fh:
            fails = json.load(fh)
    except Exception:
        fails = {}
    if not force and project in fails and \
            old_len < int(fails[project].get("len") or 0) + BRIEF_COMPACT_RETRY_GROWTH:
        return ("kutilmoqda", old_len, old_len, 0.0)

    # Avval arxiv - keyin LLM. Arxiv faqat to'ldiriladi: from_full bo'lsa ham
    # joriy brifingdagi arxivda yo'q yangi qatorlar oldin unga qo'shiladi.
    archive_brief(cfg, project, text)
    src_text = text
    if from_full:
        with open(full, encoding="utf-8") as fh:
            src_text = fh.read()
    sm = FRONT_RE.match(src_text)
    body = src_text[len(sm.group(0)) if sm else 0:]
    first = body.find("\n## ")
    intro = body[:first + 1] if first != -1 else body
    sections_body = body[first + 1:] if first != -1 else ""
    if not sections_body.strip():
        return ("bo'limsiz", old_len, old_len, 0.0)
    intro = ortho_repair(intro, cfg)[0]
    # Kirishdagi buzilgan so'zlar lug'at bilan oldindan tuzatiladi - model ularni
    # takrorlamasin. Lug'atda topilmaganini promptdagi imlo qoidasi tuzatadi.
    src = ortho_repair(sections_body.strip(), cfg)[0]
    src_items = len(_brief_bullets(src))
    ccfg = dict(cfg)
    ccfg["model"] = cfg.get("strong_model") or cfg["model"]
    # 30k belgilik brifingni qayta yozish oddiy sintezdan uzoq ketadi (opus bilan
    # katta loyihada 900 s da uzilgan) - kutish chegarasi kamida 1500 s.
    for key in ("claude_timeout_sec", "codex_timeout_sec"):
        ccfg[key] = max(int(cfg.get(key) or 300), 1500)
    prompt = BRIEF_COMPACT_PROMPT % {
        "project": project, "size": len(src),
        "lang": LANG_NAME.get(cfg["language"], cfg["language"]),
        "sections": ", ".join(BRIEF_SECTIONS), "body": src,
    }
    cost, best = 0.0, None
    for _attempt in range(3):
        try:
            out, meta = run_synthesis(prompt, ccfg, source)
        except Exception as exc:
            log(cfg, "BRIEF_COMPACT chaqiruv xatosi (%s): %s" % (project, str(exc)[:300]))
            continue
        cost += float((meta or {}).get("cost_usd") or 0.0)
        try:
            data = extract_json(out)
            cand = _brief_sections_text(data.get("sections") or {})
        except Exception:
            log(cfg, "BRIEF_COMPACT javob yaroqsiz (%s): %s" % (project, (out or "")[:300]))
            continue
        n_items = len(_brief_bullets(cand))
        # Juda kam qator - model ko'p narsani tashlab yuborgan (yo'qotish).
        if n_items < min(3, src_items) or len(cand) < len(src) * 0.1:
            log(cfg, "BRIEF_COMPACT juda qisqa javob (%s): %d qator, %d belgi"
                % (project, n_items, len(cand)))
            continue
        if len(cand) >= len(src):
            log(cfg, "BRIEF_COMPACT javob manbadan uzun (%s): %d >= %d"
                % (project, len(cand), len(src)))
            continue
        if not ortho_ok(cand):
            fixed, n_fix, n_left = ortho_repair(cand, cfg)
            log(cfg, "BRIEF_COMPACT imlo buzilgan (%s): %d so'z, lug'at %d tuzatdi, %d qoldi"
                % (project, n_fix + n_left, n_fix, n_left))
            if n_left or not ortho_ok(fixed):
                continue
            cand = fixed
        best = cand
        break
    if best is None:
        log(cfg, "BRIEF_COMPACT bajarilmadi: %s ($%.4f)" % (project, cost))
        fails[project] = {"len": old_len, "at": datetime.datetime.now().isoformat()}
        try:
            with open(fail_path, "w", encoding="utf-8") as fh:
                json.dump(fails, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return ("xato", old_len, old_len, cost)

    date = date or datetime.date.today().isoformat()
    front = re.sub(r"^updated: .*$", lambda _m: "updated: " + date, front, count=1, flags=re.M)
    if re.search(r"^compacted: ", front, flags=re.M):
        front = re.sub(r"^compacted: .*$", lambda _m: "compacted: " + date, front,
                       count=1, flags=re.M)
    elif front:
        front = front[:-4] + "compacted: " + date + "\n---\n"
    new_text = front + intro.rstrip("\n") + "\n\n" + best
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    new_len = len(FRONT_RE.sub("", new_text, count=1).strip())
    _compacted_lines(cfg, project, [t for _s, t in _brief_bullets(best)])
    if fails.pop(project, None) is not None:
        try:
            with open(fail_path, "w", encoding="utf-8") as fh:
                json.dump(fails, fh, ensure_ascii=False, indent=1)
        except Exception:
            pass
    log(cfg, "BRIEF_COMPACT %s: %d -> %d belgi ($%.4f, %s)"
        % (project, old_len, new_len, cost, ccfg["model"]))
    return ("ok", old_len, new_len, cost)


def cmd_fix_imlo(cfg, dry=False):
    """Bazadagi .md fayllarda buzilgan kirill so'zlarni lug'at bilan tuzatadi.

    Oldin o'zgaradigan fayllar state/imlo-zaxira-<vaqt>.zip ga saqlanadi.
    Lug'atda topilmagan so'z oddiy kichik harfga o'giriladi va ro'yxatda
    ko'rsatiladi - ularni qo'lda ko'rib chiqish mumkin.
    """
    paths = sorted(glob.glob(os.path.join(cfg["vault"], "**", "*.md"), recursive=True))
    changes, total_fix, unresolved = [], 0, {}
    vocab = ortho_vocab(cfg)
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        bad = ortho_bad_words(text)
        if not bad:
            continue
        for w in bad:
            if not _ortho_fix_word(w, vocab)[1]:
                unresolved[w] = _ortho_fix_word(w, vocab)[0]
        new, n_fix, n_left = ortho_repair(text, cfg)
        total_fix += n_fix + n_left
        changes.append((path, text, new, n_fix, n_left))
    for path, _t, _n, n_fix, n_left in changes:
        print("  %-60s %3d tuzatildi, %d lug'atsiz"
              % (os.path.relpath(path, cfg["vault"])[:60], n_fix, n_left))
    if unresolved:
        print("Lug'atda topilmadi (oddiy kichik harfga): " + ", ".join(
            "%s->%s" % kv for kv in sorted(unresolved.items())[:60]))
    print("Jami: %d fayl, %d so'z" % (len(changes), total_fix))
    if dry or not changes:
        return
    zpath = os.path.join(cfg["state"], "imlo-zaxira-%s.zip"
                         % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text, _n, _f, _l in changes:
            zf.writestr(os.path.relpath(path, cfg["vault"]), text)
    for path, _t, new, _f, _l in changes:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
    rebuild_index(cfg)
    _ORTHO_VOCAB.pop(cfg["vault"], None)
    log(cfg, "FIX_IMLO: %d fayl, %d so'z; zaxira %s" % (len(changes), total_fix, zpath))
    print("Zaxira: " + zpath)


def write_advice(cfg, project, advice, date, session_id):
    """Ish USULI bo'yicha tavsiyalar - keyingi safar yaxshiroq ishlash uchun."""
    if not advice:
        return 0
    path = os.path.join(cfg["vault"], "TAVSIYALAR.md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Тавсиялар\n\nСессияларни олиб бориш усули бўйича "
                     "автомат тўпланган маслаҳатлар.\n")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    known = [normalize(l[2:]) for l in text.splitlines() if l.strip().startswith("- ")]
    fresh = []
    for a in advice:
        a = str(a).strip()
        if a and not is_duplicate(a, known + [normalize(x) for x in fresh],
                                  float(cfg["dedup_ratio"])):
            fresh.append(a)
    if not fresh:
        return 0
    with open(path, "a", encoding="utf-8") as fh:
        for a in fresh:
            fh.write("- %s _(%s, %s, `%s`)_\n" % (a, project, date, str(session_id)[:8]))
    return len(fresh)


def ensure_session_stubs(cfg):
    """Daftarda bor, lekin sahifasi yo'q sessiyalar uchun qisqa sahifa yaratadi.

    Sessiya sahifalari keyinroq qo'shilgani uchun eski qatorlarda ular yo'q -
    havolalar uzilib qolmasin. Sintez qayta chaqirilmaydi (bepul).
    """
    made = 0
    for r in merge_ledger_by_session(read_ledger(cfg)):
        proj = r.get("project") or "umumiy"
        name = r.get("session_note") or session_note_name(
            r.get("date", ""), proj, r.get("session", ""), r.get("title"))
        path = session_note_path(cfg, proj, name)
        if os.path.exists(path):
            continue
        title = r.get("title") or ("Сессия " + str(r.get("session", ""))[:8])
        work = work_cost(r)
        note_c = float(r.get("cost_usd") or 0)
        body = [
            "---",
            "title: " + str(title),
            "date: " + str(r.get("date", "")),
            "project: " + proj,
            "session: " + str(r.get("session", "")),
            "work_cost_usd: %.4f" % work,
            "note_cost_usd: %.4f" % note_c,
            "tags: [" + proj + ", sessiya]",
            "---",
            "",
            "# " + str(title),
            "",
            "**Лойиҳа:** [[%s|%s]] · **Сана:** %s · **Иш нархи:** $%.4f · **Эслатма:** $%.4f"
            % (proj, proj, r.get("date", ""), work, note_c),
            "",
            "_Бу сессия сессия-саҳифалари қўшилишидан олдин ишланган — "
            "тафсилот сақланмаган. Қўшилган банд: %d._" % int(r.get("bullets") or 0),
            "",
            "## Хом транскрипт",
            "",
            "`" + os.path.join(cfg["vault"], "log", name + ".md") + "`",
            "",
        ]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
        made += 1
    return made


def rebuild_projects(cfg):
    """Har bir loyiha uchun yig'ma sahifa: eslatmalari, sessiyalari, umumiy narxi."""
    rows = merge_ledger_by_session(read_ledger(cfg))
    notes = existing_notes(cfg)

    projects = set()
    for r in rows:
        projects.add(r.get("project") or "umumiy")
    for n in notes:
        for p in [v.strip() for v in (n["projects"] or "").split(",") if v.strip()]:
            projects.add(p)

    def money(x):
        return "$%.4f" % x if x < 0.01 else "$%.2f" % x

    for proj in sorted(projects):
        my_notes = [n for n in notes
                    if proj in [v.strip() for v in (n["projects"] or "").split(",")]]
        my_rows = [r for r in rows if (r.get("project") or "umumiy") == proj]
        work = sum(work_cost(r) for r in my_rows)
        sub = sum(float(r.get("subagent_cost_usd") or 0) for r in my_rows)
        note_c = sum(float(r.get("cost_usd") or 0) for r in my_rows)
        bullets = sum(int(r.get("bullets") or 0) for r in my_rows)

        out = [
            "---",
            "title: " + proj,
            "tags: [" + proj + ", loyiha]",
            "sessions: %d" % len(my_rows),
            "work_cost_usd: %.4f" % work,
            "note_cost_usd: %.4f" % note_c,
            "---",
            "",
            "# " + proj,
            "",
            "**Сессия:** %d · **Иш нархи:** %s%s · **Эслатмага ёзиш:** %s · **Жами банд:** %d"
            % (len(my_rows), money(work),
               " (шундан ёрдамчи агентлар: %s)" % money(sub) if sub else "",
               money(note_c), bullets),
            "",
            "## Билим (мавзулар бўйича)",
            "",
        ]
        if my_notes:
            out += ["| Эслатма | Нима ҳақида | Янгиланди |", "|---|---|---|"]
            for n in sorted(my_notes, key=lambda x: x["updated"], reverse=True):
                out.append("| [[%s\\|%s]] | %s | %s |"
                           % (n["slug"], n["title"], n["desc"] or "-", n["updated"] or "-"))
        else:
            out.append("_Ҳали эслатма йўқ._")

        out += ["", "## Сессиялар (суҳбатлар)", ""]
        if my_rows:
            out += ["| Сана ва вақт | Сессия | Банд | Иш нархи | Эслатма |",
                    "|---|---|--:|--:|--:|"]
            for r in sorted(my_rows, key=lambda x: x.get("ts", ""), reverse=True):
                sn = r.get("session_note") or session_note_name(
                    r.get("date", ""), proj, r.get("session", ""), r.get("title"))
                out.append("| %s | [[%s\\|%s]] | %d | %s | %s |"
                           % (session_when(r), cell(sn, 90),
                              cell(r.get("title") or str(r.get("session", ""))[:8]),
                              int(r.get("bullets") or 0),
                              money(work_cost(r)),
                              money(float(r.get("cost_usd") or 0))))
        else:
            out.append("_Ҳали сессия ишланмаган._")
        out.append("")

        with open(os.path.join(cfg["vault"], "projects", proj + ".md"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
    return len(projects)


# ---------------------------------------------------------------- sintez

PROMPT = """Sen tajribali texnik kotibsan. Quyida {agent} bilan bo'lgan bitta ish sessiyasining transkripti bor.
Undan uzoq muddat qiymatga ega bilimni ajratib ol va mavjud bilim bazasiga marshrutla.

TIL (MUHIM): chiqaruvchi barcha matnni - title, summary, note_title, note_desc va bullets - {lang}
Bu transkript qaysi tilda bo'lishidan qat'i nazar amal qiladi. Faqat texnik atamalar, fayl yo'llari,
buyruqlar, kod va mahsulot nomlari asl holida (lotin/inglizcha) qoladi. JSON kalitlari va "section"
qiymatlari ham inglizcha qoladi.

LOYIHA: {project}
SANA: {date}

MAVJUD ESLATMALAR (mos kelsa ayni shu slug ga qo'sh, yangi yaratma):
{notes}

BU LOYIHADA ALLAQACHON YOZILGAN BANDLAR - eng muhim qoida: quyidagi faktlarni
BOSHQA SO'Z BILAN QAYTA YOZMA. Ayni fakt boshqacha ifodalangan bo'lsa ham - bu takror.
Yangi band faqat shu ro'yxatda YO'Q bo'lgan narsa haqida bo'lsin. Mavjud bandni
to'ldirish kerak bo'lsa ham, faqat YANGI qismini yoz.
{existing}

QOIDALAR:
1. Faqat kelajakda foydali bo'ladigan narsani yoz. "Faylni o'qidik", "test ishga tushdi" kabi sessiyaga xos maydalikni tashla.
2. Har bir bullet o'zicha tushunarli bo'lsin - "biz uni tuzatdik" emas, balki nima va qanday qilingani.
3. Sessiyada saqlashga arziydigan narsa bo'lmasa - "entries": [] qaytar. Bo'sh qaytarish normal holat.
4. Bir eslatmaga 6 tadan ortiq bullet yozma.
5. section faqat shulardan biri: Learnings, Decisions, Patterns, Open Questions.
6. note (slug) - lotin harflari, kichik, chiziqcha bilan. Mavjud ro'yxatda mos slug bo'lsa, ayni o'shani ishlat.
7. "related" - shu eslatma bilan mazmunan bog'liq BOSHQA eslatmalarning sluglari (yuqoridagi
   mavjud ro'yxatdan yoki shu javobdagi boshqa entry lardan). Bog'liq eslatma bo'lmasa - [].
   Faqat haqiqiy mazmuniy bog'liqlikni ko'rsat, shunchaki bir loyihada bo'lgani uchun emas.

BRIFING ("brief") - eng muhim qism. Bu loyiha bo'yicha YANGI sessiya ishni noldan
tushunishi uchun kerak bo'ladigan doimiy bilim. Maqsad: keyingi safar odam /clear qilganda
uzoq tushuntirishga vaqt ketmasin. Faqat UZOQ MUDDAT o'zgarmaydigan narsalarni yoz:
tizim tuzilishi, texnologiyalar, muhim fayl/papka yo'llari, kelishilgan qoidalar va
cheklovlar, tuzoqlar ("bunga tegma, chunki..."). Bugungi vaqtinchalik holatni ("bugun
X ni tuzatdik") YOZMA - u eslatmalarga ketadi. Yangi doimiy bilim yo'q bo'lsa - [].
Mavjud brifingda bor narsani takrorlama:
{brief}

TAVSIYA ("advice") - shu sessiyaning O'ZI qanday olib borilganiga qarab, keyingi safar
qanday YAXSHIROQ ishlash mumkinligi. Ish natijasi haqida emas, ish USULI haqida.
Masalan: kontekst juda shishgani, bir sessiyada bir necha aloqasiz mavzu aralashgani,
bir xil narsa qayta-qayta so'ralgani, oldindan aytilsa ko'p vaqt tejaladigan ma'lumot.
Aniq va bajariladigan bo'lsin. Yaxshi olib borilgan bo'lsa - [].

FAQAT JSON qaytar, boshqa hech qanday matnsiz:
{{
  "title": "sessiyaning qisqa nomi",
  "summary": "2-3 jumlada nima qilindi",
  "entries": [
    {{"note": "slug", "note_title": "Eslatma sarlavhasi", "note_desc": "bir qatorli izoh",
      "section": "Learnings", "bullets": ["...", "..."], "related": ["boshqa-slug"]}}
  ],
  "brief": [
    {{"section": "Тузилиш", "text": "..."}}
  ],
  "advice": ["keyingi safar ... qilinsa yaxshi bo'ladi"]
}}

brief.section faqat shulardan biri: Тузилиш, Технологиялар, Қоидалар, Тузоқлар, Муҳим йўллар

=== TRANSKRIPT ===
{digest}
=== TRANSKRIPT TUGADI ==="""


def project_notes_content(cfg, project, limit=25000):
    """Shu loyihaga tegishli eslatmalarning mavjud bandlari.

    Faqat slug ro'yxatini berish yetarli emas edi: model ayni faktni boshqacha
    so'z bilan qayta yozar, satr-o'xshashlik dedupi esa uni tutmasdi. Endi
    mavjud bandlar matni ham promptga kiradi - model o'zi takrorlamaydi.
    """
    out, total = [], 0
    notes = [n for n in existing_notes(cfg)
             if project in [v.strip() for v in (n["projects"] or "").split(",")]]
    notes.sort(key=lambda n: n["updated"], reverse=True)
    for n in notes:
        bullets = []
        try:
            with open(note_path(cfg, n["slug"]), encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln.startswith("- ") and not ln.startswith("- [["):
                        bullets.append(re.sub(r"\s*_\(.*?\)_\s*$", "", ln))
        except Exception:
            continue
        if not bullets:
            continue
        block = "### %s\n%s" % (n["slug"], "\n".join(bullets))
        if total + len(block) > limit:
            out.append("...(qolgani promptga sig'madi)")
            break
        out.append(block)
        total += len(block)
    return "\n\n".join(out) or "(bu loyiha bo'yicha hali eslatma yo'q)"


def synthesize(cfg, digest, project, date, source="claude"):
    notes = existing_notes(cfg)
    listing = "\n".join("- %s : %s (%s)" % (n["slug"], n["title"], n["desc"] or "-")
                        for n in notes) or "(baza hozircha bo'sh)"
    # Bazadagi buzilgan so'zlar promptga tushmasin - model ularni takrorlaydi.
    fix = lambda t: ortho_repair(t, cfg)[0]
    prompt = PROMPT.format(
        lang=LANG_NAME.get(cfg["language"], cfg["language"]),
        project=project, date=date, notes=fix(listing), digest=digest,
        agent=SYNTHESIS_AGENT_LABELS.get(source, "Claude Code"),
        brief=fix(read_brief(cfg, project)),
        existing=fix(project_notes_content(cfg, project)),
    )
    text, meta = run_synthesis(prompt, cfg, source)
    data = extract_json(text)
    if not str(cfg.get("language", "")).endswith("Cyrl"):
        return data, meta
    body = ortho_data_text(data)
    if ortho_ok(body):
        return data, meta
    log(cfg, "IMLO sintez javobi buzilgan (%s): %d so'z, masalan %s"
        % (project, len(ortho_bad_words(body)), ", ".join(ortho_bad_words(body)[:6])))
    strong = cfg.get("strong_model")
    if (meta or {}).get("provider") == "claude" and strong and strong != cfg["model"]:
        scfg = dict(cfg)
        scfg["model"] = strong
        scfg["claude_timeout_sec"] = max(int(cfg.get("claude_timeout_sec") or 300), 600)
        try:
            text2, meta2 = run_synthesis(prompt, scfg, source)
            data2 = extract_json(text2)
            merged = dict(meta2 or {})
            for k in ("cost_usd", "input_tokens", "output_tokens", "cache_write",
                      "cache_read", "duration_ms"):
                merged[k] = (meta or {}).get(k, 0) + (meta2 or {}).get(k, 0)
            meta = merged
            if len(ortho_bad_words(ortho_data_text(data2))) <= len(ortho_bad_words(body)):
                data = data2
                log(cfg, "IMLO %s bilan qayta sintez qilindi (%s)" % (strong, project))
        except Exception as exc:
            log(cfg, "IMLO qayta sintez xatosi (%s): %s" % (project, str(exc)[:300]))
    if not ortho_ok(ortho_data_text(data)):
        data = _ortho_walk(data, fix)            # oxirgi chora - lug'at bilan
        log(cfg, "IMLO lug'at bilan tuzatildi (%s), qoldi: %d so'z"
            % (project, len(ortho_bad_words(ortho_data_text(data)))))
    return data, meta


# ---------------------------------------------------------------- narxlar

# $/1M token. Manbalar: Anthropic va OpenAI rasmiy API narxlari
# (OpenAI: 2026-09-10 holatiga).
# Kesh qoidasi: yozish 5m = 1.25x kirish, 1h = 2x kirish, o'qish = 0.1x kirish
# (Fable 5.1 istisno: o'qish 0.025x).
PRICING = {
    "claude-fable-5-1":  {"in": 10.0, "out": 50.0, "cr_mult": 0.025},
    "claude-fable-5":    {"in": 10.0, "out": 50.0},
    "claude-opus-5":     {"in": 5.0,  "out": 25.0},
    "claude-opus-4-8":   {"in": 5.0,  "out": 25.0},
    "claude-opus-4-7":   {"in": 5.0,  "out": 25.0},
    "claude-opus-4-6":   {"in": 5.0,  "out": 25.0},
    "claude-sonnet-5":   {"in": 2.0,  "out": 10.0},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0},
    # OpenAI Standard processing. Codex ChatGPT obunasi orqali ishlasa pul
    # yechilmaydi; bular "API bilan ishlatilganda qancha bo'lardi" narxlari.
    "gpt-6-astra":       {"in": 10.0, "out": 50.0, "long": True},
    "gpt-5.6-sol":       {"in": 4.0,  "out": 20.0, "long": True},
    "gpt-5.6-terra":     {"in": 2.0,  "out": 12.0, "long": True},
    "gpt-5.6-luna":      {"in": 0.2,  "out": 1.2,  "long": True},
    "gpt-5.6":           {"in": 4.0,  "out": 20.0, "long": True},
    "gpt-5.5":           {"in": 5.0,  "out": 30.0, "long": True},
    "gpt-5.4-mini":      {"in": 0.75, "out": 4.5},
    "gpt-5":             {"in": 1.25, "out": 10.0},
    # Google Gemini rasmiy narxlari ($/1M token, API standard)
    "gemini-2.5-pro":    {"in": 1.25, "out": 5.0},
    "gemini-2.5-flash":  {"in": 0.075, "out": 0.3},
    "gemini-2.0-flash":  {"in": 0.1,  "out": 0.4},
    "gemini-1.5-pro":    {"in": 1.25, "out": 5.0},
    "gemini-1.5-flash":  {"in": 0.075, "out": 0.3},
}
DEFAULT_PRICE = {"in": 5.0, "out": 25.0}


def price_of(model):
    """Model nomi bo'yicha narx. '[1m]' kabi qo'shimchalar tozalanadi."""
    m = re.sub(r"\[.*?\]", "", str(model or "")).strip()
    p = PRICING.get(m)
    if not p:
        for k, v in PRICING.items():          # 'claude-opus-5-20260401' kabi variantlar
            if m.startswith(k):
                p = v
                break
    known = bool(p)
    # Noma'lum Codex/Gemini modeliga Claude default narxini qo'llab yolg'on raqam
    # bermaymiz. Rasmiy narxi jadvalga qo'shilmaguncha $0 qoladi.
    if not p and re.match(r"^(gpt-|o\d|codex|gemini)", m, re.I):
        p = {"in": 0.0, "out": 0.0, "cr_mult": 0.0}
    p = p or DEFAULT_PRICE
    base = p["in"]
    return {
        "in": base,
        "out": p["out"],
        "cw5": base * 1.25,
        "cw1h": base * 2.0,
        "cr": base * p.get("cr_mult", 0.1),
        "known": known,
        "long": bool(p.get("long")),
    }


def codex_usage_price(model, usage):
    """Bitta Codex API javobining token bo'linishi va taxminiy API narxi.

    Codex `input_tokens` ichiga cached va cache-write tokenlarini ham qo'shib
    beradi. Ularni yana alohida qo'shsak token va narx ikki marta sanaladi.
    """
    usage = usage or {}
    total_in = max(0, int(usage.get("input_tokens") or 0))
    cached = min(total_in, max(0, int(usage.get("cached_input_tokens") or 0)))
    cache_write = min(max(0, total_in - cached),
                      max(0, int(usage.get("cache_write_input_tokens") or 0)))
    uncached = max(0, total_in - cached - cache_write)
    output = max(0, int(usage.get("output_tokens") or 0))
    pr = price_of(model)
    long_input = bool(pr.get("long") and total_in > 272_000)
    in_mult = 2.0 if long_input else 1.0
    out_mult = 1.5 if long_input else 1.0
    cost = ((uncached * pr["in"] + cache_write * pr["cw5"]
             + cached * pr["cr"]) * in_mult
            + output * pr["out"] * out_mult) / 1_000_000.0
    return {"in": uncached, "out": output, "cw5": cache_write,
            "cw1h": 0, "cr": cached, "cost_usd": cost}


def reprice_old_codex_usage(usage):
    """Eski `$0` Codex taqsimotiga hozirgi rasmiy API narxini qo'llaydi."""
    total = 0.0
    for model, d in (usage or {}).items():
        if not price_of(model).get("known"):
            continue
        d = d or {}
        # Eski parser cache-write tokenini `in` ichida ham qoldirgan.
        write = int(d.get("cw5") or 0) + int(d.get("cw1h") or 0)
        d["in"] = max(0, int(d.get("in") or 0) - write)
        pr = price_of(model)
        cost = (int(d.get("in") or 0) * pr["in"]
                + int(d.get("out") or 0) * pr["out"]
                + int(d.get("cw5") or 0) * pr["cw5"]
                + int(d.get("cw1h") or 0) * pr["cw1h"]
                + int(d.get("cr") or 0) * pr["cr"]) / 1_000_000.0
        d["cost_usd"] = round(cost, 6)
        total += cost
    return round(total, 6)


def session_usage(transcript, start_line=0, end_line=None):
    """Sessiyaning O'Z narxi: transkriptdagi har bir so'rovning token hisobi.

    Bir so'rov transkriptga bir necha marta yozilishi mumkin (retry, sidechain),
    shuning uchun requestId/message.id bo'yicha dublikatlar tashlanadi -
    aks holda narx ikki barobar ko'p chiqadi.

    start_line/end_line - faqat shu oraliqdagi so'rovlar hisoblanadi. Bu muhim:
    26 kun davom etgan eski sessiya birinchi marta ishlanganda uning butun
    tarixi BUGUNGI sarf bo'lib yozilardi ($1800 kabi raqamlar shundan).
    Endi har ishlov faqat o'zi ko'rgan qismni hisoblaydi.
    """
    if transcript_kind(transcript) in CODEX_LIKE:
        return codex_session_usage(transcript, start_line, end_line)

    seen = set()
    per_model = {}
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")
    except Exception:
        return {}, 0.0
    with fh:
        line_no = 0
        for ln in fh:
            line_no += 1
            if line_no <= start_line:
                continue
            if end_line is not None and line_no > end_line:
                break
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            key = rec.get("requestId") or msg.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            model = msg.get("model") or "?"
            if model.startswith("<"):     # <synthetic> - haqiqiy so'rov emas
                continue
            d = per_model.setdefault(model, {"requests": 0, "in": 0, "out": 0,
                                             "cw5": 0, "cw1h": 0, "cr": 0})
            cc = usage.get("cache_creation") or {}
            cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
            cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
            if not (cw5 or cw1h):     # eski format: faqat yig'indi bor
                cw5 = int(usage.get("cache_creation_input_tokens") or 0)
            d["requests"] += 1
            d["in"] += int(usage.get("input_tokens") or 0)
            d["out"] += int(usage.get("output_tokens") or 0)
            d["cw5"] += cw5
            d["cw1h"] += cw1h
            d["cr"] += int(usage.get("cache_read_input_tokens") or 0)

    total = 0.0
    for model, d in per_model.items():
        pr = price_of(model)
        cost = sum(d[k] * pr[k] for k in ("in", "out", "cw5", "cw1h", "cr")) / 1_000_000.0
        d["cost_usd"] = round(cost, 6)
        total += cost
    return per_model, round(total, 6)


def codex_session_usage(transcript, start_line=0, end_line=None):
    """Codex tokenlarini API javobi bo'yicha bir martadan hisoblaydi."""
    responses, models = {}, {}
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")
    except Exception:
        return {}, 0.0
    with fh:
        for line_no, ln in enumerate(fh, 1):
            if end_line is not None and line_no > end_line:
                break
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            payload = rec.get("payload") or {}
            if rec.get("type") == "turn_context":
                models[payload.get("turn_id")] = payload.get("model") or "codex"
            if line_no <= start_line:
                continue
            if rec.get("type") != "token_usage_record":
                continue
            turn_id = payload.get("turn_id") or payload.get("response_id")
            # Real transkriptda `usage` aynan bitta API javobi, turn_token_usage
            # esa turn boshidan yig'indi. response_id bo'lsa individual yozuvni
            # olamiz; eski/test formatida turn yig'indisiga qaytamiz.
            response_id = payload.get("response_id")
            usage = (payload.get("usage") if response_id else None) or \
                    payload.get("turn_token_usage") or payload.get("usage") or {}
            if not turn_id or not usage:
                continue
            key = response_id or turn_id
            old = (responses.get(key) or (None, {}))[1]
            if int(usage.get("total_tokens") or 0) >= int(old.get("total_tokens") or 0):
                responses[key] = (turn_id, usage)

    per_model = {}
    for turn_id, usage in responses.values():
        model = models.get(turn_id) or "codex"
        d = per_model.setdefault(model, {"requests": 0, "in": 0, "out": 0,
                                         "cw5": 0, "cw1h": 0, "cr": 0})
        priced = codex_usage_price(model, usage)
        d["requests"] += 1
        for k in ("in", "out", "cw5", "cw1h", "cr"):
            d[k] += int(priced[k])
        d["cost_usd"] = float(d.get("cost_usd") or 0) + priced["cost_usd"]
    total = 0.0
    for model, d in per_model.items():
        # Codex har bir tool davomida alohida API javobi yaratadi. Claude bilan
        # bir xil ma'no va to'g'ri avg_context uchun shu javoblar soni olinadi.
        d["cost_usd"] = round(float(d.get("cost_usd") or 0), 6)
        total += d["cost_usd"]
    return per_model, round(total, 6)


# ---------------------------------------------------------- yordamchi agentlar

def subagent_transcripts(transcript):
    """Sessiyaning yordamchi agentlari (Agent/Task) transkriptlari.

    Claude Code ularni asosiy faylga emas, yonidagi papkaga yozadi:
    <sessiya>.jsonl  ->  <sessiya>/subagents/agent-<id>.jsonl
    Ilgari bu papka umuman o'qilmasdi va yordamchilar sarfi hisobdan
    tushib qolardi. Asosiy transkriptda ularning so'rovlari YO'Q
    (requestId bo'yicha tekshirilgan) - ikki marta sanash xavfi yo'q.
    """
    if not transcript or transcript_kind(transcript) != "claude":
        return []
    base = os.path.splitext(transcript)[0]
    return sorted(glob.glob(os.path.join(base, "subagents", "agent-*.jsonl")))


def subagent_label(path):
    """(nom, turi) - yonidagi agent-<id>.meta.json dan."""
    meta = {}
    try:
        with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            meta = json.load(fh) or {}
    except Exception:
        pass
    aid = os.path.basename(path)[len("agent-"):-len(".jsonl")]
    return (str(meta.get("description") or meta.get("agentType") or aid[:8]),
            str(meta.get("agentType") or ""))


def complete_lines(path):
    """Oxiri yangi qator bilan tugagan qatorlar soni.

    Yordamchi hali ishlayotgan bo'lsa oxirgi qator yarim yozilgan bo'lishi
    mumkin - uni "ishlandi" deb belgilasak, o'sha so'rov abadiy yo'qoladi.
    """
    n = 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                n += chunk.count(b"\n")
    except Exception:
        return 0
    return n


def peak_context(path, start_line=0, end_line=None):
    """Bitta so'rovda o'qilgan eng katta kontekst (kirish + kesh)."""
    peak = 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except Exception:
        return 0
    with fh:
        for i, ln in enumerate(fh, 1):
            if i <= start_line:
                continue
            if end_line is not None and i > end_line:
                break
            if '"usage"' not in ln:
                continue
            try:
                u = (json.loads(ln).get("message") or {}).get("usage") or {}
            except Exception:
                continue
            ctx = (int(u.get("input_tokens") or 0)
                   + int(u.get("cache_creation_input_tokens") or 0)
                   + int(u.get("cache_read_input_tokens") or 0))
            peak = max(peak, ctx)
    return peak


def subagent_usage(transcript, done=None):
    """Yordamchilarning hali hisoblanmagan qismi.

    done - {fayl nomi: ishlangan qatorlar} (processed.json da saqlanadi);
    asosiy transkriptdagi lines_done ning yordamchilar uchun aynan o'zi.

    Qaytaradi: (model kesimi, umumiy narx, agentlar ro'yxati, yangi done).
    """
    done = dict(done or {})
    per_model, agents, total = {}, [], 0.0
    for path in subagent_transcripts(transcript):
        key = os.path.basename(path)
        start = int(done.get(key) or 0)
        end = complete_lines(path)
        if end <= start:
            continue
        pm, cost = session_usage(path, start, end)
        done[key] = end
        if not pm:
            continue
        merge_usage(per_model, pm)
        total += cost
        name, kind = subagent_label(path)
        agents.append({
            "id": key[len("agent-"):-len(".jsonl")],
            "name": name,
            "type": kind,
            "requests": sum(int(d.get("requests") or 0) for d in pm.values()),
            "cost_usd": round(cost, 6),
            # 200k qoidasi: yordamchi kontekstni qanchalik to'ldirgani
            "peak_context": peak_context(path, 0, end),
        })
    return per_model, round(total, 6), agents, done


def merge_subagents(dst, src):
    """Agentlar ro'yxatini id bo'yicha qo'shadi (sessiya bo'laklarga bo'lingan)."""
    by_id = {a.get("id"): a for a in dst}
    for a in src or []:
        cur = by_id.get(a.get("id"))
        if cur is None:
            cur = dict(a)
            dst.append(cur)
            by_id[cur.get("id")] = cur
            continue
        cur["requests"] = int(cur.get("requests") or 0) + int(a.get("requests") or 0)
        cur["cost_usd"] = round(float(cur.get("cost_usd") or 0)
                                + float(a.get("cost_usd") or 0), 6)
        cur["peak_context"] = max(int(cur.get("peak_context") or 0),
                                  int(a.get("peak_context") or 0))
    return dst


def work_cost(r):
    """Sessiyaning to'liq ish narxi: bosh agent + uning yordamchilari."""
    return (float(r.get("session_cost_usd") or 0)
            + float(r.get("subagent_cost_usd") or 0))


# ---------------------------------------------------------------- xarajat hisobi

def ledger_path(cfg):
    return os.path.join(cfg["state"], "costs.jsonl")


def record_cost(cfg, session_id, project, date, meta, digest_chars, entries, added,
                transcript=None, title=None, start_line=0, end_line=None,
                note_name=None, tools=None, tool_cost=None,
                started_at=None, ended_at=None, sub=None):
    """Sessiya bo'yicha ikkita narxni daftarga yozadi:

      session_cost_usd  - ISHNING O'ZI qancha turgani (transkript token hisobidan)
      subagent_cost_usd - shu sessiya ochgan yordamchi agentlar (alohida)
      cost_usd          - shu ishni ESLATMAGA yozish qancha turgani (claude -p)

    sub - subagent_usage() natijasining (model kesimi, narx, agentlar) qismi.

    Obuna bilan ishlaganda haqiqiy pul yechilmaydi - bu 'API bilan ishlaganimizda
    qancha ketardi' degan savolga javob.
    """
    row = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "date": date,
        "session": session_id,
        "project": project,
        "digest_chars": digest_chars,
        "entries": entries,
        "bullets": added,
        "title": title,
        "session_note": (note_name or
                         session_note_name(date, project, session_id, title)),
    }
    row.update(meta or {})
    if transcript:
        per_model, sess_cost = session_usage(transcript, start_line, end_line)
        row["session_cost_usd"] = sess_cost
        row["session_requests"] = sum(d["requests"] for d in per_model.values())
        row["session_models"] = ", ".join(sorted(per_model))
        # Sessiya vaqti va tool statistikasi. process() ularni allaqachon
        # hisoblagan bo'lsa qayta o'qimaymiz: transkript 100 MB dan oshishi
        # mumkin. Vaqt hisobotda sana yoniga chiqadi - aks holda bir kundagi
        # bir nechta sessiya ajratib bo'lmaydigan bir xil qatorga aylanadi.
        if tools is None or tool_cost is None:
            tools, s_at, e_at, tool_cost = tool_histogram(
                transcript, start_line, end_line)
            started_at = started_at or s_at
            ended_at = ended_at or e_at
        row["started_at"] = started_at or ""
        row["ended_at"] = ended_at or ""
        row["tools"] = tools or {}
        row["tool_cost"] = tool_cost or {}
        # Model kesimidagi token hisobi. Narx keyin qayta hisoblanmasin:
        # daftar bir marta arxivlangan (costs-arxiv-*.jsonl) va transkriptni
        # noldan hisoblash hech qachon ishlanmagan tarixni ham qo'shib
        # yuboradi. Shuning uchun taqsimot ham SHU YERDA saqlanadi.
        row["usage"] = {m: dict(d) for m, d in per_model.items()}
    if sub and sub[1]:
        row["subagent_cost_usd"] = sub[1]
        row["subagent_usage"] = {m: dict(d) for m, d in (sub[0] or {}).items()}
        row["subagents"] = list(sub[2] or [])
    try:
        with open(ledger_path(cfg), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return row


def _local_dt(iso_utc):
    """UTC ISO -> mahalliy datetime. Transkriptdagi vaqt UTC da yoziladi."""
    t = str(iso_utc or "").strip().replace("Z", "")
    if not t or "T" not in t:
        return None
    try:
        dt = datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    try:
        off = datetime.datetime.now().astimezone().utcoffset()
    except Exception:
        off = None
    return dt + (off or datetime.timedelta())


def session_when(row):
    """Daftar qatoridan sana va vaqt.

    Faqat sana yetmaydi: bir kunda bitta loyihada bir nechta sessiya bo'lsa
    jadvalda bir xil qatorlar turgandek ko'rinadi.

    Sessiya bir necha kunga cho'zilgan bo'lsa tugash sanasi ham chiqadi -
    "21:31→16:12" degan g'alati oraliq shundan kelib chiqqan edi. Uzoq
    sessiya yashiriladigan narsa emas: kontekst har so'rovda qayta o'qiladi
    va narx aynan shundan oshadi.
    """
    a = _local_dt(row.get("started_at"))
    b = _local_dt(row.get("ended_at"))
    if a and b and b > a:
        if a.date() == b.date():
            return "%s %s→%s" % (a.date().isoformat(), a.strftime("%H:%M"),
                                 b.strftime("%H:%M"))
        return "%s %s → %s %s" % (a.date().isoformat(), a.strftime("%H:%M"),
                                  b.strftime("%m-%d"), b.strftime("%H:%M"))
    if a:
        return "%s %s" % (a.date().isoformat(), a.strftime("%H:%M"))
    date = row.get("date") or "-"
    ts = str(row.get("ts") or "")
    return "%s %s" % (date, ts.split("T")[1][:5]) if "T" in ts else date


def cell(text, limit=60):
    """Markdown jadval katagi uchun: quvur belgisi jadvalni buzmasin."""
    t = str(text or "").replace("|", "/").replace("\n", " ").strip()
    return t[:limit] if len(t) <= limit else t[:limit - 1] + "…"


USAGE_KEYS = ("requests", "in", "out", "cw5", "cw1h", "cr")

USAGE_LABEL = [
    ("requests", "Сўров"),
    ("in", "Кириш"),
    ("out", "Чиқиш"),
    ("cw5", "Кэш ёзиш 5м"),
    ("cw1h", "Кэш ёзиш 1с"),
    ("cr", "Кэш ўқиш"),
]


def thousands(n):
    """1234567 -> '1 234 567'. Token raqamlari o'qilishi uchun."""
    try:
        return "{:,}".format(int(n or 0)).replace(",", "\u00a0")
    except (TypeError, ValueError):
        return "0"


def usage_entry(d):
    """Serverga ketadigan bitta model yozuvi: tokenlar + narx.

    cost_usd ham yuboriladi - aks holda dashboard jadvalida narx ustuni
    bo'sh ($0.00) turib qoladi.
    """
    d = d or {}
    out = {k: int(d.get(k) or 0) for k in USAGE_KEYS}
    out["cost_usd"] = round(float(d.get("cost_usd") or 0), 6)
    return out


def merge_usage(dst, src):
    """Model kesimidagi token hisobini qo'shadi (bo'laklarga bo'lingan sessiya)."""
    for model, d in (src or {}).items():
        cur = dst.setdefault(model, {})
        for k in USAGE_KEYS:
            cur[k] = int(cur.get(k) or 0) + int(d.get(k) or 0)
        cur["cost_usd"] = round(float(cur.get("cost_usd") or 0)
                                + float(d.get("cost_usd") or 0), 6)
    return dst


def usage_rows(usage):
    """Markdown jadval qatorlari: model, so'rov, tokenlar, narx.

    Nima uchun kerak: "$968" degan raqam o'z-o'zidan hech narsani
    isbotlamaydi. Bu jadval uni qaysi model, qancha token va qanday narx
    bilan hosil bo'lganini ko'rsatadi - tekshirib bo'ladigan qilib.
    """
    out = []
    for model in sorted(usage or {}, key=lambda m: -float(
            (usage[m] or {}).get("cost_usd") or 0)):
        d = usage[model] or {}
        pr = price_of(model)
        out.append({
            "model": model,
            "requests": int(d.get("requests") or 0),
            "in": int(d.get("in") or 0),
            "out": int(d.get("out") or 0),
            "cw5": int(d.get("cw5") or 0),
            "cw1h": int(d.get("cw1h") or 0),
            "cr": int(d.get("cr") or 0),
            "cost_usd": float(d.get("cost_usd") or 0),
            "price": pr,
        })
    return out


def usage_table(usage, indent=""):
    """Model kesimidagi jadval - COSTS.md va sessiya sahifalari uchun."""
    rows = usage_rows(usage)
    if not rows:
        return []
    out = [
        indent + "| Модель | Сўров | Кириш | Чиқиш | Кэш ёзиш | Кэш ўқиш | Нарх |",
        indent + "|---|--:|--:|--:|--:|--:|--:|",
    ]
    tot = {k: 0 for k in USAGE_KEYS}
    tot_cost = 0.0
    for r in rows:
        cw = r["cw5"] + r["cw1h"]
        out.append(indent + "| `%s` | %s | %s | %s | %s | %s | $%.4f |"
                   % (r["model"], thousands(r["requests"]), thousands(r["in"]),
                      thousands(r["out"]), thousands(cw), thousands(r["cr"]),
                      r["cost_usd"]))
        for k in USAGE_KEYS:
            tot[k] += r[k]
        tot_cost += r["cost_usd"]
    if len(rows) > 1:
        out.append(indent + "| **ЖАМИ** | **%s** | **%s** | **%s** | **%s** | "
                            "**%s** | **$%.4f** |"
                   % (thousands(tot["requests"]), thousands(tot["in"]),
                      thousands(tot["out"]), thousands(tot["cw5"] + tot["cw1h"]),
                      thousands(tot["cr"]), tot_cost))
    return out


def price_note(usage):
    """Hisobda ishlatilgan narxlar - tekshirib ko'rish mumkin bo'lsin."""
    out = []
    for r in usage_rows(usage):
        p = r["price"]
        out.append("- `%s` — $%.2f кириш · $%.2f чиқиш · $%.2f кэш ёзиш (5м) · "
                   "$%.3f кэш ўқиш (1M токен учун)"
                   % (r["model"], p["in"], p["out"], p["cw5"], p["cr"]))
    return out


def read_ledger(cfg):
    rows = []
    try:
        with open(ledger_path(cfg), encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try:
                        row = json.loads(ln)
                        models = row.get("usage") or {}
                        is_codex = (row.get("agent") == "codex" or any(
                            re.match(r"^(gpt-|o\d|codex)", str(m), re.I)
                            for m in models))
                        if (is_codex and models
                                and not float(row.get("session_cost_usd") or 0)):
                            row["session_cost_usd"] = reprice_old_codex_usage(models)
                        rows.append(row)
                    except Exception:
                        pass
    except Exception:
        pass
    return rows


def merge_ledger_by_session(rows):
    """Bitta sessiya bir necha marta ishlanishi mumkin (PreCompact + SessionEnd,
    yoki --force). Har safar sintez uchun ALOHIDA pul to'lanadi, lekin ISH narxi
    o'sha bitta sessiyaniki - uni qo'shib yuborish narxni sun'iy oshirib yuboradi.

    Shuning uchun: eslatma narxi qo'shiladi, ish narxi esa sessiyaga bir marta
    (oxirgi hisob - u butun transkriptdan olingan).
    """
    merged = {}
    for r in rows:
        sid = r.get("session") or r.get("ts")
        cur = merged.get(sid)
        if cur is None:
            cur = dict(r)
            cur["runs"] = 1
            # nusxa: keyingi bo'laklar asl daftar qatorini o'zgartirmasin
            cur["usage"] = merge_usage({}, r.get("usage") or {})
            cur["tool_cost"] = dict(r.get("tool_cost") or {})
            cur["tools"] = dict(r.get("tools") or {})
            cur["subagent_usage"] = merge_usage({}, r.get("subagent_usage") or {})
            cur["subagents"] = merge_subagents([], r.get("subagents") or [])
            merged[sid] = cur
            continue
        cur["subagent_cost_usd"] = round(float(cur.get("subagent_cost_usd") or 0)
                                         + float(r.get("subagent_cost_usd") or 0), 6)
        merge_usage(cur.setdefault("subagent_usage", {}),
                    r.get("subagent_usage") or {})
        merge_subagents(cur.setdefault("subagents", []), r.get("subagents") or [])
        if r.get("kind") == "subagent_backfill":
            # Tarixdan tiklangan yordamchilar sarfi: sessiyaning sanasi,
            # nomi va ishlov soni o'zgarmasin - faqat pul qo'shiladi.
            continue
        cur["runs"] += 1
        cur["cost_usd"] = float(cur.get("cost_usd") or 0) + float(r.get("cost_usd") or 0)
        cur["bullets"] = int(cur.get("bullets") or 0) + int(r.get("bullets") or 0)
        cur["entries"] = int(cur.get("entries") or 0) + int(r.get("entries") or 0)
        cur["duration_ms"] = int(cur.get("duration_ms") or 0) + int(r.get("duration_ms") or 0)
        for k in ("input_tokens", "output_tokens", "cache_write", "cache_read",
                  "session_requests"):
            cur[k] = int(cur.get(k) or 0) + int(r.get(k) or 0)
        # Ish narxi QO'SHILADI: har ishlov transkriptning faqat o'z bo'lagini
        # hisoblaydi (start_line..end_line). Ilgari butun transkript qayta
        # hisoblanardi va "oxirgisini ol" to'g'ri edi - endi unday emas,
        # aks holda PreCompact bilan bo'lingan sessiya kam ko'rinardi.
        cur["session_cost_usd"] = (float(cur.get("session_cost_usd") or 0)
                                   + float(r.get("session_cost_usd") or 0))
        for k in ("ts", "date", "title", "session_note", "digest_chars"):
            if r.get(k):
                cur[k] = r[k]
        # Sessiya bo'laklarga bo'linib ishlangan bo'lsa - eng erta boshlanish
        # va eng kech tugash olinadi, ya'ni butun sessiyaning vaqt oralig'i.
        if r.get("started_at") and (not cur.get("started_at")
                                    or r["started_at"] < cur["started_at"]):
            cur["started_at"] = r["started_at"]
        if r.get("ended_at") and (not cur.get("ended_at")
                                  or r["ended_at"] > cur["ended_at"]):
            cur["ended_at"] = r["ended_at"]
        merge_usage(cur.setdefault("usage", {}), r.get("usage") or {})
        merge_tool_cost(cur.setdefault("tool_cost", {}), r.get("tool_cost") or {})
        for _t, _n in (r.get("tools") or {}).items():
            cur.setdefault("tools", {})[_t] = \
                int(cur["tools"].get(_t) or 0) + int(_n or 0)
    return list(merged.values())


def cost_summary(cfg):
    """(loyihalar bo'yicha jadval, umumiy yig'indi) qaytaradi."""
    rows = merge_ledger_by_session(read_ledger(cfg))
    by_project = {}
    for r in rows:
        p = r.get("project") or "umumiy"
        agg = by_project.setdefault(p, {
            "sessions": 0, "cost": 0.0, "work": 0.0, "sub": 0.0,
            "bullets": 0, "entries": 0,
            "in": 0, "out": 0, "cache_w": 0, "cache_r": 0, "ms": 0,
            "first": r.get("date"), "last": r.get("date"),
        })
        agg["sessions"] += 1
        agg["cost"] += float(r.get("cost_usd") or 0)
        # Ish narxi = bosh agent + yordamchilar; "sub" - shundan yordamchilar
        agg["work"] = agg.get("work", 0.0) + work_cost(r)
        agg["sub"] = agg.get("sub", 0.0) + float(r.get("subagent_cost_usd") or 0)
        agg["bullets"] += int(r.get("bullets") or 0)
        agg["entries"] += int(r.get("entries") or 0)
        agg["in"] += int(r.get("input_tokens") or 0)
        agg["out"] += int(r.get("output_tokens") or 0)
        agg["cache_w"] += int(r.get("cache_write") or 0)
        agg["cache_r"] += int(r.get("cache_read") or 0)
        agg["ms"] += int(r.get("duration_ms") or 0)
        d = r.get("date") or ""
        if d:
            agg["first"] = min(agg["first"] or d, d)
            agg["last"] = max(agg["last"] or d, d)

    total = {
        "sessions": sum(a["sessions"] for a in by_project.values()),
        "cost": sum(a["cost"] for a in by_project.values()),
        "work": sum(a.get("work", 0.0) for a in by_project.values()),
        "sub": sum(a.get("sub", 0.0) for a in by_project.values()),
        "bullets": sum(a["bullets"] for a in by_project.values()),
        "entries": sum(a["entries"] for a in by_project.values()),
        "in": sum(a["in"] for a in by_project.values()),
        "out": sum(a["out"] for a in by_project.values()),
        "cache_w": sum(a["cache_w"] for a in by_project.values()),
        "cache_r": sum(a["cache_r"] for a in by_project.values()),
        "ms": sum(a["ms"] for a in by_project.values()),
    }
    return by_project, total, rows


def backfill_times(cfg):
    """Eski daftar qatorlariga vaqt va token taqsimotini tiklaydi.

    started_at/ended_at, usage (model kesimidagi tokenlar) va tool_cost
    keyin qo'shilgan maydonlar - ulardan oldingi qatorlarda yo'q.

    MUHIM: taqsimot faqat transkriptdan hisoblangan UMUMIY narx daftardagi
    narx bilan mos kelsa yoziladi. Mos kelmasligi mumkin, chunki daftar bir
    marta arxivlangan (costs-arxiv-*.jsonl) va transkriptni noldan hisoblash
    hech qachon ishlanmagan tarixni ham qo'shib yuboradi. Mos kelmasa -
    yolg'on raqam ko'rsatgandan ko'ra bo'sh qoldirilgani yaxshi.

    Daftar joyida qayta yoziladi, avval .bak nusxasi olinadi.
    """
    rows = read_ledger(cfg)
    if not rows:
        return 0
    found = {}
    filled = skipped = 0
    for r in rows:
        sid = r.get("session")
        if not sid:
            continue
        need_time = not r.get("started_at")
        need_usage = not r.get("usage")
        if not (need_time or need_usage):
            continue
        if sid not in found:
            hits = glob.glob(os.path.join(os.path.expanduser("~"), ".claude",
                                          "projects", "*", sid + ".jsonl"))
            found[sid] = hits[0] if hits else None
        path = found[sid]
        if not path:
            continue

        tools, a, b, tcost = tool_histogram(path)
        if need_time and a:
            r["started_at"] = a
            r["ended_at"] = b or ""
            filled += 1

        if need_usage:
            per_model, whole = session_usage(path)
            led = float(r.get("session_cost_usd") or 0)
            if led and abs(whole - led) <= max(0.01, led * 0.01):
                r["usage"] = {m: dict(d) for m, d in per_model.items()}
                r["tools"] = tools
                r["tool_cost"] = tcost
                filled += 1
            else:
                skipped += 1
                log(cfg, "BACKFILL taqsimot tashlandi %s: daftar $%.2f, "
                         "transkript $%.2f (daftar arxivlangan bo'lishi mumkin)"
                    % (sid[:8], led, whole))

    if not filled:
        return 0
    p = ledger_path(cfg)
    try:
        shutil.copyfile(p, p + ".bak")
    except Exception:
        pass
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    log(cfg, "BACKFILL: %d maydon to'ldirildi, %d taqsimot tashlandi"
        % (filled, skipped))
    return filled


def backfill_subagents(cfg, dry=False):
    """Eski sessiyalarning yordamchi agentlari sarfini daftarga tiklaydi.

    Yordamchilar hisobi keyin qo'shilgan: ungacha ishlangan sessiyalarda
    sub_lines_done yo'q. Har biriga BITTA "subagent_backfill" qatori
    qo'shiladi (sana, nom, ishlov soni o'zgarmaydi - merge_ledger_by_session)
    va processed.json ga belgi qo'yiladi - qayta ishga tushirilsa ikki marta
    sanalmaydi, keyingi oddiy ishlov esa shu joydan davom etadi.

    Faqat joriy daftardagi sessiyalar: arxivlangan sessiyaning asosiy narxi
    daftarda yo'q - unga yolg'iz yordamchi narxini qo'shish hisobni buzadi.
    """
    st = load_state(cfg)
    ledger = {}
    for r in read_ledger(cfg):
        if r.get("session"):
            ledger.setdefault(r["session"], r)
    rows, total, n_agents = [], 0.0, 0
    for sid, first in ledger.items():
        prev = st.get(sid)
        if prev is None or "sub_lines_done" in prev:
            continue
        transcript = find_session_transcript(sid)
        if not transcript or transcript_kind(transcript) != "claude":
            continue
        pm, cost, agents, done = subagent_usage(transcript, {})
        prev["sub_lines_done"] = done
        if not cost:
            continue
        prev["subagent_cost_usd"] = round(
            float(prev.get("subagent_cost_usd") or 0) + cost, 6)
        rows.append({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "kind": "subagent_backfill",
            "session": sid,
            "project": first.get("project"),
            "subagent_cost_usd": cost,
            "subagent_usage": {m: dict(d) for m, d in pm.items()},
            "subagents": agents,
        })
        total += cost
        n_agents += len(agents)
    if dry:
        return len(rows), n_agents, round(total, 6)
    if rows:
        with open(ledger_path(cfg), "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        log(cfg, "BACKFILL yordamchilar: %d sessiya, %d agent, $%.2f"
            % (len(rows), n_agents, total))
    save_state(cfg, st)
    return len(rows), n_agents, round(total, 6)


def write_cost_report(cfg):
    by_project, total, rows = cost_summary(cfg)
    rows = merge_ledger_by_session(rows)
    if not rows:
        # Daftar bo'sh bo'lsa ham hisobotni QAYTA YOZAMIZ. Ilgari bu yerda
        # shunchaki chiqib ketilardi va hisob nolga tushirilganda ekranda
        # eski raqamlar turib qolardi.
        arx = sorted(glob.glob(os.path.join(cfg["state"], "costs-arxiv-*.jsonl")))
        body = [
            "# Харажат ҳисоби",
            "",
            "_Ҳисоб нолдан бошланди. Ҳали бирорта сессия ишланмаган._",
            "",
            "Биринчи сессия тугаши билан (ёки компакт бўлиши билан) шу ерда "
            "лойиҳалар бўйича жадвал пайдо бўлади.",
            "",
        ]
        if arx:
            body += ["Олдинги ҳисоб архиви:", ""]
            body += ["- `%s`" % os.path.basename(a) for a in arx]
            body += [""]
        with open(os.path.join(cfg["vault"], "COSTS.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(body) + "\n")
        return 0

    def money(x):
        return "$%.4f" % x if x < 0.01 else "$%.2f" % x

    def pct(part, whole):
        return "%.1f%%" % (100.0 * part / whole) if whole else "-"

    out = [
        "# Харажат ҳисоби",
        "",
        "Автомат яратилади. Иккита нарх ажратиб кўрсатилади:",
        "",
        "- **Иш нархи** — ўша сессияда Claude билан ишлаганингиз қанчага тушган",
        "  (транскриптдаги ҳар бир сўровнинг токен ҳисобидан, расмий нархлар бўйича).",
        "- **Эслатма нархи** — ўша ишни билим базасига ёзиш қанчага тушган (`claude -p`).",
        "",
        "Иш нархига сессия очган **ёрдамчи агентлар** (Agent/Task) ҳам киради —",
        "уларнинг улуши «Ёрдамчилар» устунида алоҳида кўрсатилади.",
        "",
        "> Обуна билан ишлаганда **ҳақиқий пул ечилмайди** — сарф лимитдан кетади.",
        "> Бу рақамлар «агар API билан ишлаганимизда қанча кетган бўларди» саволига жавоб.",
        "",
        "## Лойиҳалар бўйича",
        "",
        "| Лойиҳа | Сессия | Иш нархи | Шундан ёрдамчилар | Эслатма нархи | Устама "
        "| Ўртача иш/сессия | Банд |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for p, a in sorted(by_project.items(), key=lambda kv: kv[1].get("work", 0), reverse=True):
        work = a.get("work", 0.0)
        avg_w = work / a["sessions"] if a["sessions"] else 0
        out.append("| %s | %d | %s | %s | %s | %s | %s | %d |"
                   % (p, a["sessions"], money(work), money(a.get("sub", 0.0)),
                      money(a["cost"]), pct(a["cost"], work), money(avg_w),
                      a["bullets"]))
    tw = total.get("work", 0.0)
    avg_w_all = tw / total["sessions"] if total["sessions"] else 0
    out += [
        "| **ЖАМИ** | **%d** | **%s** | **%s** | **%s** | **%s** | **%s** | **%d** |"
        % (total["sessions"], money(tw), money(total.get("sub", 0.0)),
           money(total["cost"]), pct(total["cost"], tw), money(avg_w_all),
           total["bullets"]),
        "",
        "**Умумий сарф (иш + эслатма): %s**" % money(tw + total["cost"]),
        "",
        "Битта эслатма банди нархи: **%s** · Синтезга кетган вақт: **%.1f дақиқа**"
        % (money(total["cost"] / total["bullets"]) if total["bullets"] else "-",
           total["ms"] / 60000.0),
        "",
    ]

    # "$968 qayerdan chiqdi" - tekshirib bo'ladigan javob
    all_usage, all_tools, all_tcost = {}, {}, {}
    for r in rows:
        merge_usage(all_usage, r.get("usage") or {})
        merge_usage(all_usage, r.get("subagent_usage") or {})
        merge_tool_cost(all_tcost, r.get("tool_cost") or {})
        for t, n in (r.get("tools") or {}).items():
            all_tools[t] = int(all_tools.get(t) or 0) + int(n or 0)

    # Qamrov: eski qatorlarda taqsimot yo'q (daftar arxivlangan). Buni
    # aytib qo'ymasak, quyidagi jadval umumiy summadan kam chiqadi va
    # o'quvchi xato deb o'ylaydi.
    covered = sum(work_cost(r) for r in rows if r.get("usage"))
    n_cov = sum(1 for r in rows if r.get("usage"))

    utbl = usage_table(all_usage)
    if utbl:
        out += ["## Нарх қаердан чиқди", ""]
        if n_cov < len(rows):
            out += ["> Қуйидаги тақсимот **%d сессиядан %d таси** учун мавжуд "
                    "(%s / %s). Қолганлари дафтар архивланишидан олдин "
                    "ёзилган — уларда токен тафсилоти сақланмаган."
                    % (len(rows), n_cov, money(covered), money(total.get("work", 0.0))),
                    ""]
        out += ["Модель кесимида токенлар ва улардан ҳисобланган нарх:", ""]
        out += utbl
        out += ["", "Ҳисобда ишлатилган нархлар (1M токен учун):", ""]
        out += price_note(all_usage)
        out += ["",
                "> Кэш ўқиш одатда энг катта улуш бўлади: сессия узайган сари "
                "контекст ҳар сўровда қайта ўқилади. Нархни туширишнинг энг "
                "самарали йўли — сессияни қисқа тутиш (`/clear`).", ""]

    ttbl = tool_cost_table(all_tools, all_tcost, limit=25)
    if ttbl:
        out += ["## Пул қайси ишга кетди", ""]
        out += ttbl
        out += ["",
                "_Тахминий: битта сўровнинг нархи ўша сўровда чақирилган "
                "воситалар орасида тенг бўлинади. Аниқ бўлиши мумкин эмас — "
                "пулни контекст ейди, восита эмас. «(тоол ишлатилмаган)» — "
                "фақат матн қайтарилган навбатлар._", ""]

    # Yordamchi agentlar: kim qancha turdi va kontekstni qanchalik to'ldirdi
    # (bosh agent ularni 200k chegarada almashtiradi - shu jadvalda ko'rinadi).
    agents = []
    for r in rows:
        for a in r.get("subagents") or []:
            agents.append((r, a))
    if agents:
        agents.sort(key=lambda ra: -float(ra[1].get("cost_usd") or 0))
        out += ["## Ёрдамчи агентлар (энг қимматлари, 30 та)", "",
                "| Лойиҳа | Сессия | Ёрдамчи | Тури | Сўров | Энг катта контекст "
                "| Нарх |",
                "|---|---|---|---|--:|--:|--:|"]
        for r, a in agents[:30]:
            out.append("| %s | `%s` | %s | %s | %s | %s | %s |"
                       % (r.get("project") or "umumiy",
                          str(r.get("session", ""))[:8], cell(a.get("name"), 50),
                          cell(a.get("type"), 20), thousands(a.get("requests")),
                          thousands(a.get("peak_context")),
                          money(float(a.get("cost_usd") or 0))))
        out += ["", "_Жами %d ёрдамчи агент, %s._"
                % (len(agents), money(total.get("sub", 0.0))), ""]

    out += [
        "## Сессиялар (охирги 40 та)",
        "",
        "| Сана ва вақт | Лойиҳа | Сессия | Иш нархи | Шундан ёрдамчилар | Эслатма "
        "| Устама | Банд | ID |",
        "|---|---|---|--:|--:|--:|--:|--:|---|",
    ]
    for r in list(reversed(rows))[:40]:
        w = work_cost(r)
        sub = float(r.get("subagent_cost_usd") or 0)
        c = float(r.get("cost_usd") or 0)
        proj = r.get("project") or "umumiy"
        sn = r.get("session_note") or session_note_name(
            r.get("date", ""), proj, r.get("session", ""), r.get("title"))
        name = cell(r.get("title") or str(r.get("session", ""))[:8])
        out.append("| %s | %s | [[%s\\|%s]] | %s | %s | %s | %s | %d | `%s` |"
                   % (session_when(r), proj, cell(sn, 90), name,
                      money(w), money(sub) if sub else "-", money(c), pct(c, w),
                      int(r.get("bullets") or 0),
                      str(r.get("session", ""))[:8]))
    out.append("")

    with open(os.path.join(cfg["vault"], "COSTS.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return len(rows)


def print_costs(cfg):
    by_project, total, rows = cost_summary(cfg)
    if not rows:
        print("Харажат дафтари ҳали бўш.")
        return
    hdr = "%-24s %7s %11s %11s %8s %7s"
    print(hdr % ("ЛОЙИҲА", "СЕССИЯ", "ИШ НАРХИ", "ЭСЛАТМА", "УСТАМА", "БАНД"))
    print("-" * 74)
    for p, a in sorted(by_project.items(), key=lambda kv: kv[1].get("work", 0), reverse=True):
        work = a.get("work", 0.0)
        up = "%.1f%%" % (100.0 * a["cost"] / work) if work else "-"
        print(hdr % (p[:24], a["sessions"], "$%.4f" % work,
                     "$%.4f" % a["cost"], up, a["bullets"]))
    print("-" * 74)
    tw = total.get("work", 0.0)
    up_all = "%.1f%%" % (100.0 * total["cost"] / tw) if tw else "-"
    print(hdr % ("ЖАМИ", total["sessions"], "$%.4f" % tw,
                 "$%.4f" % total["cost"], up_all, total["bullets"]))
    print()
    print("Умумий сарф (иш + эслатма): $%.4f" % (tw + total["cost"]))
    if total.get("sub"):
        print("Шундан ёрдамчи агентлар: $%.4f" % total["sub"])
    print("Батафсил ҳисобот: " + os.path.join(cfg["vault"], "COSTS.md"))


# ---------------------------------------------------------------- rejimlar

def write_raw_log(cfg, digest, session_id, project, date, part=1):
    # bitta sessiya bir necha bo'lakda ishlansa, har biri alohida faylga
    suffix = "" if part <= 1 else "-%d" % part
    name = "%s-%s-%s%s.md" % (date, project, session_id[:8], suffix)
    path = os.path.join(cfg["vault"], "log", name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\nsession: %s\nproject: %s\ndate: %s\n---\n\n" % (session_id, project, date))
        fh.write("```\n" + digest + "\n```\n")
    return path


def write_inbox(cfg, data, project, date, session_id, dry=False):
    lines = ["", "## %s - %s _(%s)_" % (date, data.get("title") or "Сессия", project),
             "", data.get("summary") or "", ""]
    for e in data.get("entries") or []:
        lines.append("**%s** - %s" % (e.get("note_title") or e.get("note"), e.get("section")))
        for b in e.get("bullets") or []:
            lines.append("- " + str(b))
        lines.append("")
    lines.append("<sub>session: %s</sub>" % session_id)
    text = "\n".join(lines)
    if dry:
        print(text)
        return len(data.get("entries") or [])
    path = os.path.join(cfg["vault"], "inbox.md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Inbox\n\nСессиялардан келган конспектлар.\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")
    return len(data.get("entries") or [])


# ---------------------------------------------------------------- telemetriya
#
# Jamoa bitta Claude akkauntidan turli joylardan ishlaydi. Akkaunt bitta
# bo'lgani uchun Claude tomonidan "kim ishlayapti" degan savolga javob yo'q -
# shuning uchun kimlik LOKAL aniqlanadi: hostname + Windows useri + config
# dagi team_member.
#
# Serverga FAQAT meta va qisqa xulosa ketadi. Xom transkript, kod, tool
# natijalari va to'liq fayl yo'llari hech qachon chiqmaydi - erkin matnning
# hammasi scrub() dan o'tadi.

TELEMETRY_VERSION = 1

# Serverga chiqadigan matndan olib tashlanadigan narsalar
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?<![\w.])/(?:home|Users|var|etc|opt|srv|root|mnt)/)"
    r"[^\s'\"`,;)\]]{2,}")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{8,})", re.I)


def scrub(text, limit=1200):
    """Serverga chiqadigan har bir erkin matn shu yerdan o'tadi.

    To'liq fayl yo'llari, IP manzillar va kalitga o'xshash satrlar olib
    tashlanadi: hisobotda "nima qilingani" qolsin, "qayerda, qaysi server va
    qaysi kalit bilan" qolmasin. Yo'ldan faqat fayl nomi qoldiriladi -
    xulosa tushunarli bo'lishi uchun shuning o'zi yetadi.
    """
    s = str(text or "").strip()
    if not s:
        return ""
    s = _SECRET_RE.sub("«калит»", s)
    s = _PATH_RE.sub(lambda m: "…/" + (os.path.basename(m.group(0).rstrip("/\\"))
                                       or "файл"), s)
    s = _IP_RE.sub("«сервер»", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def outbox_dir(cfg):
    path = os.path.join(cfg["state"], "outbox")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def server_token(cfg):
    """Token config.json da TURMAYDI - config GitHub repoda.

    Tartib: CLAUDE_NOTE_TOKEN muhit o'zgaruvchisi, keyin state/server_token.txt
    (state/ .gitignore da).
    """
    tok = (os.environ.get("CLAUDE_NOTE_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(os.path.join(cfg["state"], "server_token.txt"),
                  encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def team_identity(cfg):
    """(member, host, os_user).

    Ism uch joydan qidiriladi:
      1. config.json -> team_member
      2. CLAUDE_NOTE_MEMBER muhit o'zgaruvchisi
      3. state/member.txt

    Nega uchtasi: config.json GitHub repoda turadi, ya'ni unga yozilgan ism
    hamma klonlarga tarqaladi va jamoadoshning hisoboti begona nom bilan
    ketishi mumkin. state/ esa .gitignore da - shu yerdagi ism faqat shu
    kompyuterniki. Hech biri yo'q bo'lsa - user@host, u ham kimligini
    ko'rsatadi.
    """
    host = (os.environ.get("COMPUTERNAME") or platform.node() or "?").strip()
    os_user = (os.environ.get("USERNAME") or os.environ.get("USER") or "?").strip()
    member = (cfg.get("team_member") or "").strip()
    if not member:
        member = (os.environ.get("CLAUDE_NOTE_MEMBER") or "").strip()
    if not member:
        try:
            with open(os.path.join(cfg["state"], "member.txt"),
                      encoding="utf-8") as fh:
                member = fh.read().strip()
        except Exception:
            member = ""
    member = member or ("%s@%s" % (os_user, host))
    return member[:60], host[:60], os_user[:60]


TEXT_ONLY = "(тоол ишлатилмаган)"

# Skill va Agent uchun NOMI ham olinadi: "Skill" deb ko'rsatish yetarli emas,
# qaysi skill qancha turgani ko'rinsin. Bu tool argumentlaridan olinadigan
# YAGONA narsa va u nom - fayl yo'li, buyruq yoki matn emas.
NAMED_TOOLS = {"Skill": ("skill", "command"),
               "Agent": ("subagent_type",),
               "Task": ("subagent_type",)}


def _tool_label(block):
    name = str(block.get("name") or "?")[:60]
    keys = NAMED_TOOLS.get(name)
    if not keys:
        return name
    inp = block.get("input")
    if not isinstance(inp, dict):
        return name
    for k in keys:
        val = inp.get(k)
        if isinstance(val, str) and val.strip():
            return ("%s: %s" % (name, val.strip()))[:60]
    return name


def tool_histogram(transcript, start_line=0, end_line=None):
    """Oraliqdagi tool chaqiruvlari, vaqt chegaralari va narx taqsimoti.

    -> ({tool: soni}, started_at, ended_at, {tool: usd})

    Narx TAXMINIY taqsimlanadi: bitta so'rovning narxi o'sha so'rovda
    chaqirilgan toollar orasida teng bo'linadi, hech biri bo'lmasa
    "(тоол ишлатилмаган)" ga yoziladi. Aniq bo'lishi mumkin emas - pulni
    kontekst yeydi, tool emas. Lekin "pul qaysi ishga ketdi" degan savolga
    javob shundan chiqadi.

    Tool argumentlari o'qilmaydi; istisno - Skill/Agent NOMI (NAMED_TOOLS).
    """
    if transcript_kind(transcript) in CODEX_LIKE:
        return codex_tool_histogram(transcript, start_line, end_line)

    tools = {}
    cost = {}
    started = ended = None
    seen = set()
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")
    except Exception:
        return tools, None, None, cost

    with fh:
        line_no = 0
        for ln in fh:
            line_no += 1
            if line_no <= start_line:
                continue
            if end_line is not None and line_no > end_line:
                break
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue

            ts = rec.get("timestamp")
            if isinstance(ts, str) and ts:
                if started is None or ts < started:
                    started = ts
                if ended is None or ts > ended:
                    ended = ts

            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            names = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    label = _tool_label(b)
                    names.append(label)
                    tools[label] = tools.get(label, 0) + 1

            # so'rov narxi - session_usage bilan bir xil qoida bo'yicha
            usage = msg.get("usage")
            key = rec.get("requestId") or msg.get("id")
            model = msg.get("model") or ""
            if not usage or not key or key in seen or model.startswith("<"):
                continue
            seen.add(key)
            pr = price_of(model)
            cc = usage.get("cache_creation") or {}
            cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
            cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
            if not (cw5 or cw1h):
                cw5 = int(usage.get("cache_creation_input_tokens") or 0)
            usd = (int(usage.get("input_tokens") or 0) * pr["in"]
                   + int(usage.get("output_tokens") or 0) * pr["out"]
                   + cw5 * pr["cw5"] + cw1h * pr["cw1h"]
                   + int(usage.get("cache_read_input_tokens") or 0) * pr["cr"]
                   ) / 1_000_000.0

            if not names:
                tools[TEXT_ONLY] = tools.get(TEXT_ONLY, 0) + 1
            targets = names or [TEXT_ONLY]
            share = usd / len(targets)
            for t in targets:
                cost[t] = cost.get(t, 0.0) + share

    if len(tools) > 60:      # cheksiz o'smasin
        tools = dict(sorted(tools.items(), key=lambda kv: -kv[1])[:60])
        cost = {k: v for k, v in cost.items() if k in tools or k == TEXT_ONLY}
    cost = {k: round(v, 6) for k, v in cost.items()}
    return tools, started, ended, cost


def codex_tool_histogram(transcript, start_line=0, end_line=None):
    """Codex response_item tool chaqiruvlarini sanaydi."""
    tools, started, ended = {}, None, None
    try:
        fh = open(transcript, encoding="utf-8", errors="replace")
    except Exception:
        return tools, None, None, {}
    with fh:
        for line_no, ln in enumerate(fh, 1):
            if line_no <= start_line:
                continue
            if end_line is not None and line_no > end_line:
                break
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            ts = rec.get("timestamp")
            if isinstance(ts, str) and ts:
                started = ts if started is None or ts < started else started
                ended = ts if ended is None or ts > ended else ended
            payload = rec.get("payload") or {}
            if (rec.get("type") == "response_item" and payload.get("type") in
                    ("custom_tool_call", "function_call")):
                name = str(payload.get("name") or "?")[:60]
                tools[name] = tools.get(name, 0) + 1
    return tools, started, ended, {k: 0.0 for k in tools}


def merge_tool_cost(dst, src):
    for k, v in (src or {}).items():
        dst[k] = round(float(dst.get(k) or 0) + float(v or 0), 6)
    return dst


def tool_cost_table(tools, cost, limit=25):
    """"Қайси ишга қанча кетди" jadvali - COSTS.md va sessiya sahifasi uchun."""
    keys = set(tools or {}) | set(cost or {})
    if not keys:
        return []
    rows = sorted(keys, key=lambda k: -float((cost or {}).get(k) or 0))[:limit]
    out = ["| Восита / скилл | Чақирув | Тахминий нарх |", "|---|--:|--:|"]
    for k in rows:
        out.append("| `%s` | %s | $%.2f "
                   % (k, thousands((tools or {}).get(k, 0)),
                      float((cost or {}).get(k) or 0)) + "|")
    return out


def build_report(cfg, session_id, project, date, event, transcript,
                 start_line, end_line, per_model, work_cost, note_cost,
                 title=None, summary=None, notes_added=0, sub=None):
    """Serverga ketadigan ixcham yozuv.

    Bu oq ro'yxat: bu yerda yo'q maydon serverga ham chiqmaydi.

    sub - yordamchi agentlar (model kesimi, narx, agentlar). Ular
    work_cost_usd va usage ga QO'SHILADI - dashboard summasi to'g'ri
    chiqsin; alohida ulushi subagent_cost_usd / subagents da keladi.
    """
    member, host, os_user = team_identity(cfg)
    tools, started_at, ended_at, tool_cost = tool_histogram(
        transcript, start_line, end_line)

    sub_pm, sub_cost, sub_agents = sub or ({}, 0.0, [])
    per_model = merge_usage(merge_usage({}, per_model or {}), sub_pm or {})
    work_cost = float(work_cost or 0) + float(sub_cost or 0)
    requests = sum(int(d.get("requests") or 0) for d in per_model.values())
    ctx = sum(int(d.get(k) or 0) for d in per_model.values()
              for k in ("in", "cw5", "cw1h", "cr"))
    try:
        tz_min = int(datetime.datetime.now().astimezone()
                     .utcoffset().total_seconds() // 60)
    except Exception:
        tz_min = 0

    return {
        "v": TELEMETRY_VERSION,
        "member": member,
        "host": host,
        "os_user": os_user,
        "agent": transcript_kind(transcript),
        "project": project,
        "session": session_id,
        "date": date,
        "event": event,
        "started_at": started_at,
        "ended_at": ended_at,
        "sent_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "tz_offset_min": tz_min,
        "title": scrub(title, 200),
        "summary": (scrub(summary, 1200)
                    if cfg.get("telemetry_send_summary", True) else ""),
        "requests": requests,
        "work_cost_usd": round(float(work_cost or 0), 6),
        "note_cost_usd": round(float(note_cost or 0), 6),
        "subagent_cost_usd": round(float(sub_cost or 0), 6),
        "subagents": [dict({k: a.get(k) for k in ("type", "requests", "cost_usd",
                                                  "peak_context")},
                           name=scrub(a.get("name"), 100))
                      for a in (sub_agents or [])],
        "models": sorted(per_model),
        "avg_context": int(ctx / requests) if requests else 0,
        "tools": tools,
        "tool_cost": tool_cost,
        "usage": {m: usage_entry(d) for m, d in (per_model or {}).items()},
        "notes_added": int(notes_added or 0),
        "lines": [int(start_line or 0), int(end_line or 0)],
    }


def queue_report(cfg, row):
    """Hisobotni navbatga (state/outbox/) yozadi. Fayl nomi vaqt bo'yicha
    saralanadi - server tiklanganda tartib buzilmaydi."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = "%s-%s.json" % (stamp, str(row.get("session") or "x")[:8])
    path = os.path.join(outbox_dir(cfg), name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(row, fh, ensure_ascii=False)
    except Exception:
        log(cfg, "TELEMETRY navbatga yozib bo'lmadi:\n" + traceback.format_exc())
        return None

    # navbat cheksiz o'smasin: server uzoq o'chiq tursa eng eskisi tashlanadi
    cap = int(cfg.get("telemetry_max_outbox") or 500)
    try:
        files = sorted(glob.glob(os.path.join(outbox_dir(cfg), "*.json")))
        if len(files) > cap:
            for old in files[:len(files) - cap]:
                os.remove(old)
    except Exception:
        pass
    return path


def post_report(cfg, row, timeout=None):
    """-> (ok, izoh). Tarmoq xatosi hech qachon istisno ko'tarmaydi."""
    url = (cfg.get("telemetry_url") or "").strip()
    if not url:
        return False, "telemetry_url ko'rsatilmagan"
    token = server_token(cfg)
    if not token:
        return False, "token yo'q (CLAUDE_NOTE_TOKEN yoki state/server_token.txt)"

    body = json.dumps(row, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "claude-note-win/%d" % TELEMETRY_VERSION)
    try:
        with urllib.request.urlopen(
                req,
                timeout=float(timeout or cfg.get("telemetry_timeout_sec") or 10)
        ) as resp:
            code = resp.getcode()
            resp.read(2000)
        return (200 <= int(code) < 300), "HTTP %s" % code
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code
    except Exception as e:
        return False, ("%s: %s" % (type(e).__name__, e))[:200]


def flush_outbox(cfg, limit=100, timeout=None):
    """Navbatdagi hisobotlarni yuboradi.

    Birinchi xatoda TO'XTAYDI: server o'chiq bo'lsa 500 marta urinib vaqt
    yo'qotmaymiz, va yuborish tartibi ham saqlanadi.

    timeout - SessionStart hook uchun qisqa qilib beriladi: hook sinxron
    ishlaydi va javob bermayotgan serverni kutib turish Claude Code ni
    ishga tushishini to'xtatib qo'yadi.
    """
    if not cfg.get("telemetry_enabled"):
        return 0
    files = sorted(glob.glob(os.path.join(outbox_dir(cfg), "*.json")))[:limit]
    sent = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                row = json.load(fh)
        except Exception:
            try:                      # buzilgan fayl navbatni to'sib qo'ymasin
                os.remove(path)
            except Exception:
                pass
            continue
        ok, info = post_report(cfg, row, timeout=timeout)
        if not ok:
            log(cfg, "TELEMETRY navbatda kutmoqda (%d ta): %s" % (len(files), info))
            break
        try:
            os.remove(path)
        except Exception:
            pass
        sent += 1
    if sent:
        log(cfg, "TELEMETRY yuborildi: %d ta hisobot" % sent)
    return sent


def send_report(cfg, row):
    """Avval navbatga yoziladi, keyin navbat bo'shatiladi.

    To'g'ridan-to'g'ri yubormaymiz: jarayon yuborish paytida uzilsa ham
    hisobot diskda qoladi va keyingi SessionStart da o'zi ketadi.
    """
    if not cfg.get("telemetry_enabled"):
        return False
    queue_report(cfg, row)
    return flush_outbox(cfg) > 0


def backfill_reports(cfg, limit=0, dry=False):
    """Daftardagi eski sessiyalarni serverga yuboradi.

    Telemetriya yoqilgunga qadar tugagan sessiyalar serverga tushmaydi -
    dashboard da faqat yangi ishlar ko'rinadi va summalar COSTS.md dagidan
    kam chiqadi. Bu buyruq shu bo'shliqni to'ldiradi.

    Narx DAFTARDAN olinadi, transkriptdan qayta hisoblanmaydi. Sabab:
    daftar note.py haqiqatan ishlagan bo'laklarni sanaydi, transkriptni
    noldan hisoblash esa hech qachon ishlanmagan eski tarixni ham qo'shib
    yuboradi - va dashboard COSTS.md bilan mos kelmay qoladi.

    Kontekst hajmi va tool statistikasi esa daftarda yo'q - ular transkript
    hali diskda bo'lsa o'sha yerdan olinadi, bo'lmasa bo'sh qoladi.
    """
    rows = merge_ledger_by_session(read_ledger(cfg))
    processed = load_state(cfg)
    rows.sort(key=lambda r: r.get("ts") or "")
    if limit:
        rows = rows[-int(limit):]

    sent = 0
    for r in rows:
        sid = r.get("session") or ""
        if not sid:
            continue
        transcript = find_session_transcript(sid) or ""

        # Daftarda bor bo'lsa - o'shani olamiz. Transkriptdan qayta hisoblash
        # hech qachon ishlanmagan tarixni ham qo'shib yuborardi.
        tools = dict(r.get("tools") or {})
        tool_cost = dict(r.get("tool_cost") or {})
        usage = {m: dict(d) for m, d in (r.get("usage") or {}).items()}
        s_at, e_at = r.get("started_at"), r.get("ended_at")
        avg_ctx = 0
        # Eski Codex parser `requests` sifatida user turnlarini sanagan. Shu
        # sabab avg_context millionlab ko'ringan. processed.json chegarasi
        # bor bo'lsa, faqat haqiqatan ishlangan qismni yangi parserda tiklaymiz.
        if transcript and transcript_kind(transcript) in CODEX_LIKE:
            done = int((processed.get(sid) or {}).get("lines_done") or 0)
            if done:
                fresh_usage, fresh_cost = session_usage(transcript, 0, done)
                if fresh_usage:
                    usage = {m: dict(d) for m, d in fresh_usage.items()}
                    r["session_cost_usd"] = fresh_cost
                    r["session_requests"] = sum(
                        int(d.get("requests") or 0) for d in usage.values())
        if usage:
            reqs = sum(int(d.get("requests") or 0) for d in usage.values())
            ctx = sum(int(d.get(k) or 0) for d in usage.values()
                      for k in ("in", "cw5", "cw1h", "cr"))
            avg_ctx = int(ctx / reqs) if reqs else 0
        elif transcript:
            # Eski qator: taqsimot yo'q. Transkriptdan olamiz, LEKIN faqat
            # umumiy narx daftar bilan mos kelsa - aks holda yolg'on raqam
            # ko'rsatgandan ko'ra bo'sh qoldirgan yaxshi.
            t_tools, t_a, t_b, t_cost = tool_histogram(transcript)
            per_model, whole = session_usage(transcript)
            led = float(r.get("session_cost_usd") or 0)
            s_at = s_at or t_a
            e_at = e_at or t_b
            if led and abs(whole - led) <= max(0.01, led * 0.01):
                tools, tool_cost = t_tools, t_cost
                usage = {m: dict(d) for m, d in per_model.items()}
                reqs = sum(int(d.get("requests") or 0) for d in per_model.values())
                ctx = sum(int(d.get(k) or 0) for d in per_model.values()
                          for k in ("in", "cw5", "cw1h", "cr"))
                avg_ctx = int(ctx / reqs) if reqs else 0
        # Yordamchilar work_cost_usd ga qo'shilgani kabi tokenlari ham
        # qo'shiladi - aks holda dashboard dagi model jadvali summaga mos kelmaydi
        if r.get("subagent_usage"):
            usage = merge_usage(merge_usage({}, usage), r["subagent_usage"])
            r["session_requests"] = int(r.get("session_requests") or 0) + sum(
                int(d.get("requests") or 0) for d in r["subagent_usage"].values())

        member, host, os_user = team_identity(cfg)
        models = sorted(usage) or [m.strip() for m in
                  str(r.get("session_models") or "").split(",") if m.strip()]
        agent = (r.get("agent") or
                 (transcript_kind(transcript) if transcript else
                  ("antigravity" if any("gemini" in m.lower() for m in models) else
                   ("codex" if any(re.match(r"^(gpt-|o\d|codex)", m, re.I)
                                   for m in models) else "claude"))))
        try:
            tz_min = int(datetime.datetime.now().astimezone()
                         .utcoffset().total_seconds() // 60)
        except Exception:
            tz_min = 0

        row = {
            "v": TELEMETRY_VERSION,
            "member": member, "host": host, "os_user": os_user,
            "agent": agent,
            "project": r.get("project") or "umumiy",
            "session": sid,
            "date": r.get("date") or datetime.date.today().isoformat(),
            "event": "backfill",
            "started_at": s_at or "", "ended_at": e_at or "",
            "sent_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "tz_offset_min": tz_min,
            "title": scrub(r.get("title"), 200),
            "summary": "",
            "requests": int(r.get("session_requests") or 0),
            "work_cost_usd": round(work_cost(r), 6),
            "note_cost_usd": round(float(r.get("cost_usd") or 0), 6),
            "subagent_cost_usd": round(float(r.get("subagent_cost_usd") or 0), 6),
            "subagents": [dict({k: a.get(k) for k in ("type", "requests",
                                                      "cost_usd", "peak_context")},
                               name=scrub(a.get("name"), 100))
                          for a in (r.get("subagents") or [])],
            "models": models,
            "avg_context": avg_ctx,
            "tools": tools,
            "tool_cost": tool_cost,
            "usage": {m: usage_entry(d) for m, d in usage.items()},
            "notes_added": int(r.get("bullets") or 0),
            "lines": [0, 0],
        }

        if dry:
            print("  [dry] %-9s %-20s $%8.2f  %s"
                  % (sid[:8], row["project"], row["work_cost_usd"],
                     (row["title"] or "")[:40]))
            sent += 1
            continue

        ok, info = post_report(cfg, row)
        print("  %-9s %-20s $%8.2f  %s"
              % (sid[:8], row["project"], row["work_cost_usd"],
                 "OK" if ok else "XATO: " + info))
        if ok:
            sent += 1
        else:
            queue_report(cfg, row)
    return sent, len(rows)


def cmd_telemetry(cfg, action="status", session=None, transcript=None):
    """`note telemetry [status|test|send|flush]` - lokal tekshirish uchun."""
    member, host, os_user = team_identity(cfg)
    if action == "status":
        files = sorted(glob.glob(os.path.join(outbox_dir(cfg), "*.json")))
        print("telemetriya : %s" % ("YOQILGAN" if cfg.get("telemetry_enabled")
                                    else "o'chiq (telemetry_enabled: false)"))
        print("kim         : %s  (host=%s, user=%s)" % (member, host, os_user))
        print("server      : %s" % (cfg.get("telemetry_url") or "-"))
        print("token       : %s" % ("bor" if server_token(cfg) else "YO'Q"))
        print("xulosa      : %s" % ("yuboriladi"
                                    if cfg.get("telemetry_send_summary", True)
                                    else "yuborilmaydi"))
        print("navbatda    : %d ta hisobot (%s)" % (len(files), outbox_dir(cfg)))
        lp = os.path.join(cfg["state"], LOCAL_CONFIG)
        print("lokal sozlama: %s" % (lp if os.path.exists(lp) else "yo'q"))
        return 0

    if action == "flush":
        print("Yuborildi: %d ta" % flush_outbox(cfg))
        return 0

    if action in ("backfill", "backfill-dry"):
        if not cfg.get("telemetry_enabled"):
            print("Telemetriya o'chiq - avval yoqing "
                  "(state/config.local.json -> telemetry_enabled: true)")
            return 1
        dry = action.endswith("-dry")
        print("Daftardagi sessiyalar serverga yuborilmoqda%s:"
              % (" [SINOV, yuborilmaydi]" if dry else ""))
        sent, total = backfill_reports(cfg, dry=dry)
        print("\n%d/%d ta %s" % (sent, total,
                                 "tayyor" if dry else "yuborildi"))
        return 0

    # test / send - oxirgi ishlangan sessiyadan namuna hisobot yig'iladi
    st = load_state(cfg)
    sid = session
    if not sid:
        rows = [(v.get("date") or "", k) for k, v in st.items()]
        if not rows:
            print("Ishlangan sessiya yo'q - avval bitta sessiya tugasin.")
            return 1
        sid = sorted(rows)[-1][1]
    prev = st.get(sid) or {}
    path = transcript
    if not path:
        path = find_session_transcript(sid)
    if not path or not os.path.exists(path):
        print("Transkript topilmadi: %s" % sid)
        return 1

    end_line = int(prev.get("lines_done") or 0) or None
    per_model, work_cost = session_usage(path, 0, end_line)
    row = build_report(
        cfg, sid, prev.get("project") or project_of(path),
        prev.get("date") or datetime.date.today().isoformat(),
        prev.get("last_event") or "test", path, 0, end_line or 0,
        per_model, work_cost, prev.get("cost_usd") or 0,
        title=prev.get("title"), summary=None,
        notes_added=prev.get("added") or 0)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    print("\n[hajmi: %d bayt]" % len(json.dumps(row, ensure_ascii=False)))

    if action == "send":
        ok, info = post_report(cfg, row)
        print("Yuborish: %s (%s)" % ("OK" if ok else "XATO", info))
        return 0 if ok else 1
    return 0


# ---------------------------------------------------------------- asosiy oqim

def process(cfg, transcript, session_id, cwd, dry=False, force=False, event="SessionEnd"):
    source = transcript_kind(transcript) if transcript else "claude"
    project = project_of(transcript, cwd)
    if project in (cfg.get("skip_projects") or []):
        log(cfg, "SKIP loyiha: %s" % project)
        return
    if not transcript or not os.path.exists(transcript):
        log(cfg, "SKIP transkript yo'q: %s" % transcript)
        return

    if is_own_synthesis(transcript):
        log(cfg, "SKIP o'z sintez chaqiruvimiz: %s" % session_id[:8])
        return

    st = load_state(cfg)
    prev = st.get(session_id) or {}
    start_line = 0 if force else int(prev.get("lines_done") or 0)

    date = datetime.date.today().isoformat()
    digest, n_user, _ask, end_line, more = parse_transcript(transcript, cfg, start_line)
    if end_line <= start_line and not force:
        log(cfg, "SKIP yangi qator yo'q: %s" % session_id[:8])
        return
    if n_user < int(cfg["min_user_messages"]) and not more:
        log(cfg, "SKIP juda qisqa (%d yangi user xabar): %s" % (n_user, session_id[:8]))
        return

    mode = cfg["mode"]
    max_chunks = int(cfg.get("max_chunks_per_run") or 4)

    # Suhbat bitta bo'lakka sig'masa - ketma-ket bo'laklarda ishlanadi.
    # Avval o'rtasi kesib tashlanardi va katta sessiyadan atigi 2% ko'rinardi.
    data = {}
    meta = {}
    entries_total = added = chunks = 0
    cost = 0.0
    touched = []          # (slug, section, qo'shilgan bulletlar) - sessiya sahifasi uchun
    brief_items, advice_items, titles, summaries = [], [], [], []
    cur = start_line

    while True:
        chunks += 1
        log(cfg, "START %s | %s | %s | %s | bo'lak %d | qator %d-%d | %d msg | %d chars"
            % (session_id[:8], project, mode, event, chunks, cur, end_line, n_user, len(digest)))

        if (cfg.get("keep_raw_log") or mode == "log") and not dry:
            write_raw_log(cfg, digest, session_id, project, date, part=chunks)

        if mode == "log":
            cur = end_line
        elif dry:
            log(cfg, "DRY-RUN sintez o'tkazildi: %s" % session_id[:8])
            print("   [dry-run] digest tayyorlandi (%d belgi, %d user xabar)"
                  % (len(digest), n_user), flush=True)
            cur = end_line
        else:
            data, meta = synthesize(cfg, digest, project, date, source=source)
            cost += float((meta or {}).get("cost_usd") or 0.0)
            entries = data.get("entries") or []
            entries_total += len(entries)
            if data.get("title"):
                titles.append(data["title"])
            if data.get("summary"):
                summaries.append(data["summary"])
            brief_items.extend(data.get("brief") or [])
            advice_items.extend(data.get("advice") or [])

            if mode == "inbox":
                added += write_inbox(cfg, data, project, date, session_id, dry=dry)
            else:
                for e in entries:
                    cnt, slug = append_entry(cfg, e, project, date, dry=dry,
                                             agent=source)
                    added += cnt
                    if cnt:
                        touched.append((slug, e.get("section") or "Learnings",
                                        (e.get("bullets") or [])[:cnt]))
                if not dry:
                    rebuild_index(cfg)
            cur = end_line

        if not more:
            break
        if chunks >= max_chunks:
            log(cfg, "CHEGARA: %d bo'lakdan keyin to'xtatildi, qolgani keyingi "
                     "ishlovga qoladi (qator %d dan)" % (max_chunks, cur))
            break
        digest, n_user, _ask, end_line, more = parse_transcript(transcript, cfg, cur)
        if end_line <= cur:
            break

    if mode == "log":
        st[session_id] = {"date": date, "project": project, "mode": "log",
                          "lines_done": cur,
                          "sub_lines_done": prev.get("sub_lines_done") or {}}
        if not dry:
            save_state(cfg, st)
        log(cfg, "DONE log | %d bo'lak" % chunks)
        return

    data = {"title": titles[0] if titles else None,
            "summary": " ".join(summaries),
            "brief": brief_items, "advice": advice_items}
    end_line = cur

    # Brifing/tavsiya ikkilamchi: ular yiqilsa ham sessiya hisobi va narx
    # yozilishi kerak - aks holda butun ishlov (va to'langan pul) yo'qoladi.
    n_brief = n_advice = 0
    if not dry:
        try:
            n_brief = write_brief(cfg, project, data.get("brief") or [], date, cwd)
        except Exception:
            log(cfg, "BRIEF xato:\n" + traceback.format_exc())
        # Brifing SessionStart kontekstiga sig'ishi kerak - o'sib ketsa ixchamlaymiz.
        # Narxi shu sessiyaning "eslatmaga yozish" xarajatiga qo'shiladi.
        if n_brief:
            try:
                _st, _o, _n, c_cost = compact_brief(cfg, project, date, source=source)
                cost += c_cost
            except Exception:
                log(cfg, "BRIEF_COMPACT xato:\n" + traceback.format_exc())
        try:
            n_advice = write_advice(cfg, project, data.get("advice") or [],
                                    date, session_id)
        except Exception:
            log(cfg, "ADVICE xato:\n" + traceback.format_exc())

    # cost - barcha bo'laklar yig'indisi; meta esa oxirgi chaqiruvniki,
    # shuning uchun umumiy narxni alohida uzatamiz.
    meta = dict(meta or {})
    meta["cost_usd"] = round(cost, 6)
    meta["chunks"] = chunks
    _pm, sess_cost = session_usage(transcript, start_line, end_line)
    # Yordamchi agentlar alohida fayllarda - o'zlarining "ishlangan qator"
    # belgilari bilan (sub_lines_done), asosiy transkriptdagi lines_done kabi.
    sub_done = {} if force else dict(prev.get("sub_lines_done") or {})
    sub_pm, sub_cost, sub_agents, sub_done = subagent_usage(transcript, sub_done)
    sub = (sub_pm, sub_cost, sub_agents)
    # Bir marta o'qiymiz: sessiya sahifasiga ham, daftarga ham, serverga ham
    # shu bitta hisob ketadi.
    sess_tools, sess_started, sess_ended, sess_tcost = tool_histogram(
        transcript, start_line, end_line)
    # Nom bir marta tanlanadi: keyingi bo'lakda sarlavha o'zgarsa ham
    # sessiya sahifasi ikkilanmasin.
    note_name = prev.get("session_note") or session_note_name(
        date, project, session_id, data.get("title"))

    if not dry:
        if mode != "inbox":
            write_session_note(cfg, data, project, date, session_id,
                               touched, sess_cost, cost, name=note_name,
                               usage=_pm, tools=sess_tools,
                               tool_cost=sess_tcost, agent=source, sub=sub)
        meta["agent"] = source
        record_cost(cfg, session_id, project, date, meta, end_line - start_line,
                    entries_total, added, transcript=transcript,
                    title=data.get("title"), start_line=start_line,
                    end_line=end_line, note_name=note_name,
                    tools=sess_tools, tool_cost=sess_tcost,
                    started_at=sess_started, ended_at=sess_ended, sub=sub)
        write_cost_report(cfg)
        rebuild_projects(cfg)

    st[session_id] = {"date": date, "project": project, "mode": mode,
                      "title": data.get("title"),
                      "added": int(prev.get("added") or 0) + added,
                      "cost_usd": round(float(prev.get("cost_usd") or 0) + cost, 6),
                      "session_cost_usd": round(
                          float(prev.get("session_cost_usd") or 0) + sess_cost, 6),
                      "subagent_cost_usd": round(
                          float(prev.get("subagent_cost_usd") or 0) + sub_cost, 6),
                      "lines_done": end_line,
                      "sub_lines_done": sub_done,
                      "session_note": note_name,
                      "runs": int(prev.get("runs") or 0) + 1,
                      "last_event": event,
                      "agent": source}
    if not dry:
        save_state(cfg, st)

    # Jamoa hisoboti. Telemetriya ikkilamchi: u yiqilsa ham bilim bazasi
    # ishlagan ish yo'qolmasin, shuning uchun butunlay try ichida.
    if not dry and cfg.get("telemetry_enabled"):
        try:
            send_report(cfg, build_report(
                cfg, session_id, project, date, event, transcript,
                start_line, end_line, _pm, sess_cost, cost,
                title=data.get("title"), summary=data.get("summary"),
                notes_added=added, sub=sub))
        except Exception:
            log(cfg, "TELEMETRY xato:\n" + traceback.format_exc())

    log(cfg, "DONE %s | %d bo'lak | %d entry | %d bullet | brief +%d | tavsiya +%d "
             "| ish $%.4f + eslatma $%.4f"
        % (mode, chunks, entries_total, added, n_brief, n_advice,
           sess_cost + sub_cost, cost))
    if dry:
        print("\n[dry-run] %s | %d bo'lak | %d entry | %d yangi bullet"
              % (mode, chunks, entries_total, added))
        print("[dry-run] ish narxi: $%.4f | eslatma narxi: $%.4f" % (sess_cost, cost))
        print("[dry-run] title: %s" % data.get("title"))


def spawn_detached(job_file):
    """Fon jarayonini uyg'otadi.

    Windows da DETACHED_PROCESS ishlatilmaydi: u konsolni butunlay olib
    tashlaydi va ichkarida claude.cmd ishga tushmay qoladi. CREATE_NO_WINDOW
    ko'rinmas konsol beradi, NEW_PROCESS_GROUP esa ota jarayon (Claude Code)
    yopilganda Ctrl+C ni meros qilib olishdan saqlaydi.

    macOS/Linux da buning o'rniga start_new_session: jarayon yangi sessiyaga
    o'tadi va terminal yopilganda SIGHUP olmaydi.
    """
    exe = sys.executable
    subprocess.Popen(
        [exe, os.path.abspath(__file__), "--job", job_file], close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **detach_kwargs()
    )


def cmd_hook(cfg):
    """SessionEnd hook: payload ni saqlab, fon jarayonni uyg'otadi va darhol chiqadi."""
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    session_id = payload.get("session_id") or hashlib.md5(raw.encode()).hexdigest()
    job = os.path.join(cfg["state"], "job-%s.json" % session_id[:12])
    with open(job, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    spawn_detached(job)
    # reason: nima uchun sessiya tugadi (clear / logout / prompt_input_exit / other)
    log(cfg, "HOOK qabul qilindi: %s | sabab=%s | %s"
        % (session_id[:8], payload.get("reason") or "?", payload.get("cwd")))


def _session_header(cfg, project):
    """SessionStart 1-qismi (yoki eski yagona rejim) sarlavhasi va yo'riqnomasi."""
    ppath = os.path.join(cfg["vault"], "projects", project + ".md")
    # Brifingning o'zi yetarli emas: foydalanuvchi "loyiha bilan tanishib chiq"
    # deganda model baribir fayllarni noldan kezib chiqadi va tejamning yarmi
    # yo'qoladi. Shuning uchun brifing bilan birga aniq yo'riqnoma beriladi.
    return (
        "## %s лойиҳаси — олдинги сессиялардан тайёр брифинг\n\n"
        "Қуйидагиси аввалги сессияларда тўпланган ва текширилган маълумот. "
        "Уни **аллақачон маълум** деб ҳисобла.\n\n"
        "- Фойдаланувчи «лойиҳа билан танишиб чиқ» деса — шу брифинг асосий жавоб. "
        "Лойиҳа тузилишини нолдан кезиб чиқма, `ls`/`find` билан умумий разведка қилма.\n"
        "- Фақат жорий вазифа учун **аниқ керак бўлган** файлларни оч.\n"
        "- Брифингда етишмаган нарса бўлса — ўшани, фақат ўшани текшир.\n"
        "- Брифинг код билан зид келса, **кодга ишон** ва зидликни айтиб ўт "
        "(брифинг эскирган бўлиши мумкин).\n"
        "- Мавзу эслатмаларини `%s` дан Grep (калит сўз) билан қидир; "
        "лойиҳа эслатмалари рўйхати — `%s`.\n\n"
    ) % (project, os.path.join(cfg["vault"], "notes"), ppath)


def _emit_session_context(cfg, project, context):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    log(cfg, "SESSION_START brifing yuklandi: %s (%d belgi)" % (project, len(context)))


def cmd_session_start(cfg, part=None):
    """SessionStart hook: yangi sessiyaga loyiha brifingini avtomat yuklaydi.

    /clear dan keyin kontekst nolga tushadi - lekin brifing darhol qaytadi,
    shuning uchun qaytadan uzoq tushuntirishga hojat qolmaydi.
    API chaqirilmaydi, faqat fayl o'qiladi - tez va bepul.

    part=None - eski rejim: hammasi bitta hookda (SESSION_CONTEXT_MAX ichida).
    part=N - brifingning N-qismi (brief_parts); 1-qism sarlavha, catch_up va
    flush_outbox ni ham bajaradi, qolganlari faqat o'z bo'lagini beradi va
    bo'lagi bo'lmasa hech narsa chiqarmaydi.
    """
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    cwd = payload.get("cwd") or ""
    project = resolve_project(cfg, payload.get("transcript_path"), cwd)

    if part is not None and part > 1:
        _session_start_extra_part(cfg, project, part)
        return

    # kompyuter o'chib qolgani uchun tugallanmagan ishlar bo'lsa - tiklaymiz
    try:
        catch_up(cfg)
    except Exception:
        pass

    # server o'chiq bo'lganda navbatda qolgan hisobotlar shu yerda ketadi.
    # Chegaralar qattiq: bu hook sinxron ishlaydi va uzoq kutish Claude Code
    # ishga tushishini kechiktiradi. Yetib ulgurmagani keyingi safar ketadi.
    try:
        flush_outbox(cfg, limit=25, timeout=3)
    except Exception:
        pass

    body = ""
    bpath = brief_path(cfg, project)
    if os.path.exists(bpath):
        with open(bpath, encoding="utf-8") as fh:
            body = FRONT_RE.sub("", fh.read(), count=1).strip()
    ppath = os.path.join(cfg["vault"], "projects", project + ".md")
    has_project = os.path.exists(ppath)
    if not body and not has_project:
        return                     # brifing yo'q - hech narsa qo'shmaymiz

    header = _session_header(cfg, project)

    # Claude Code ~10 000 belgidan uzun additionalContext ni faylga olib qo'yadi
    # va modelga faqat boshini ko'rsatadi - shuning uchun jami SESSION_CONTEXT_MAX.
    context = header
    if body and part is not None:
        texts, tail = _brief_split(cfg, project, body, bpath, header)
        k = sum(1 for t in texts if t)
        if k > 1:
            context += PART1_NOTE % k
        context += texts[0] + tail
    elif body:
        full = brief_full_path(cfg, project)
        tail = ("\n\n_Ихчамлашдан олдинги тўлиқ брифинг (тарих, тафсилот): `%s`._" % full
                if os.path.exists(full) else "")
        room = SESSION_CONTEXT_MAX - len(header) - len(tail)
        context += brief_for_context(body, room, bpath) + tail
    _emit_session_context(cfg, project, context)


PART1_NOTE = ("**Брифинг %d қисмга бўлинган:** бу 1-қисм, қолганлари алоҳида контекст "
              "блокларида (тартиби ихтиёрий) — ҳаммаси битта брифинг, барчасини ҳисобга ол.\n\n")
PART_HEADER = ("## %s лойиҳаси — брифинг, %d/%d-қисм\n\n"
               "Олдинги сессиялардан тайёр, текширилган маълумот (1-қисмдаги йўриқнома амал "
               "қилади): **аллақачон маълум** деб ҳисобла; код билан зид келса — кодга ишон.\n\n")


def _brief_split(cfg, project, body, bpath, header):
    """Brifing qismlari (brief_parts) va 1-qism oxiridagi arxiv izohi.

    Har bir hook buni mustaqil hisoblaydi - natija bir xil bo'lishi uchun
    sig'imlar faqat loyiha nomi va fayllardan kelib chiqadi.
    """
    full = brief_full_path(cfg, project)
    tail = ("\n\n_Ихчамлашдан олдинги тўлиқ брифинг (тарих, тафсилот): `%s`._" % full
            if os.path.exists(full) else "")
    room1 = SESSION_CONTEXT_MAX - len(header) - len(PART1_NOTE % 9) - len(tail)
    room_n = SESSION_CONTEXT_MAX - len(PART_HEADER % (project, 9, 9))
    rooms = [room1] + [room_n] * (BRIEF_PARTS - 1)
    return brief_parts(body, rooms, bpath), tail


def _session_start_extra_part(cfg, project, part):
    """--session-start --part N (N>1): faqat brifingning N-bo'lagi, bo'lmasa jim."""
    if part > BRIEF_PARTS:
        return
    bpath = brief_path(cfg, project)
    if not os.path.exists(bpath):
        return
    with open(bpath, encoding="utf-8") as fh:
        body = FRONT_RE.sub("", fh.read(), count=1).strip()
    if not body:
        return
    texts, _tail = _brief_split(cfg, project, body, bpath, _session_header(cfg, project))
    if not texts[part - 1]:
        return                     # kichik loyiha - bu qism kerak emas
    k = sum(1 for t in texts if t)
    _emit_session_context(cfg, project, PART_HEADER % (project, part, k) + texts[part - 1])


MAX_JOB_ATTEMPTS = 3


def cmd_job(cfg, job_file):
    """Job faylini FAQAT muvaffaqiyatli tugagach o'chiradi.

    Foydalanuvchi VS Code ni yopib darrov kompyuterni o'chirsa, fon jarayon
    o'ldiriladi va sintez tugamaydi. Job fayli joyida qolsa - keyingi safar
    SessionStart uni ko'radi va qayta ishlaydi (catch_up), ish yo'qolmaydi.
    """
    with open(job_file, encoding="utf-8") as fh:
        payload = json.load(fh)
    attempts = int(payload.get("_attempts") or 0) + 1
    payload["_attempts"] = attempts
    payload["_last_try"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with open(job_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    except Exception:
        pass

    try:
        process(cfg,
                payload.get("transcript_path") or "",
                payload.get("session_id") or "unknown",
                payload.get("cwd") or "",
                event=payload.get("hook_event_name") or "SessionEnd")
    except Exception:
        log(cfg, "JOB xato (urinish %d/%d)\n%s"
            % (attempts, MAX_JOB_ATTEMPTS, traceback.format_exc()))
        if attempts < MAX_JOB_ATTEMPTS:
            return                    # job qoladi - keyingi safar qayta uriniladi
        log(cfg, "JOB tashlab yuborildi (urinishlar tugadi): " + job_file)
    try:
        os.remove(job_file)
    except Exception:
        pass


def catch_up(cfg, min_age_sec=120):
    """Tugallanmagan ishlarni topib qayta ishga tushiradi.

    Kompyuter o'chirilgani, quvvat uzilgani yoki xato tufayli yarim qolgan
    ishlar shu yerda tiklanadi. Yosh (hozir ishlayotgan) joblarga tegilmaydi.
    """
    now = datetime.datetime.now().timestamp()
    revived = 0
    for job in glob.glob(os.path.join(cfg["state"], "job-*.json")):
        try:
            age = now - os.path.getmtime(job)
        except Exception:
            continue
        if age < min_age_sec:
            continue                  # hozir ishlayotgan bo'lishi mumkin
        log(cfg, "CATCH_UP tiklanmoqda: %s (%.0f daqiqa avval)"
            % (os.path.basename(job), age / 60))
        spawn_detached(job)
        revived += 1
    return revived


def all_transcripts(cfg=None):
    """Claude, Codex va Antigravity lokal transkriptlarini qaytaradi."""
    home = os.path.expanduser("~")
    files = glob.glob(os.path.join(home, ".claude", "projects", "*", "*.jsonl"))
    files += glob.glob(os.path.join(home, ".codex", "sessions", "**", "*.jsonl"),
                       recursive=True)
    state_dir = (cfg or {}).get("state") if isinstance(cfg, dict) else os.path.join(BASE, "state")
    files += glob.glob(os.path.join(state_dir, "antigravity", "*.jsonl"))
    return files


def find_session_transcript(session_id, cfg=None):
    for path in all_transcripts(cfg):
        if (session_id_of_transcript(path) == session_id or
                session_id in os.path.basename(path)):
            return path
    return None


def cmd_backfill(cfg, limit, dry, force=False, only_kind=None):
    files = all_transcripts(cfg)
    if only_kind:
        files = [f for f in files if transcript_kind(f) == only_kind]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    st = load_state(cfg)
    done = 0
    for path in files:
        sid = session_id_of_transcript(path)
        if sid in st and not force:
            continue
        if os.path.getsize(path) < 4000:
            continue
        print("[%d/%d] %s  (%s, %s, %.0f KB)"
              % (done + 1, limit, sid[:8], project_of(path),
                 transcript_kind(path), os.path.getsize(path) / 1024),
              flush=True)
        try:
            process(cfg, path, sid, cwd_of_transcript(path), dry=dry, force=force)
        except Exception as exc:
            log(cfg, "BACKFILL xato %s: %s" % (sid[:8], exc))
            print("   xato: %s" % exc, flush=True)
        done += 1
        if done >= limit:
            break
    print("Backfill tugadi: %d sessiya ko'rildi." % done, flush=True)


# Paketga kiradigan fayllar. Ikkala tizim uchun bitta paket: Windows
# foydalanuvchisi .command ni, macOS foydalanuvchisi .cmd ni shunchaki
# e'tiborsiz qoldiradi. Ikki xil paket yasash - ikki xil xato manbai.
PAKET_FILES = ("note.py", "install.py", "ornat.py", "README.md", "JAMOA.md",
               "OLTIN-QOIDALAR.md",
               "note.cmd", "ornat.cmd",          # Windows
               "note", "ornat.command")          # macOS / Linux

# macOS da bularsiz ikki marta bosib bo'lmaydi
MAC_FILES = ("ornat.command", "note")


def existing_token(archive):
    """Oldingi paketdagi tokenni qaytaradi (bo'lsa)."""
    if not os.path.exists(archive):
        return ""
    try:
        with zipfile.ZipFile(archive) as zf:
            name = [n for n in zf.namelist() if n.endswith("sozlama.json")][0]
            return json.loads(zf.read(name).decode("utf-8")).get("token") or ""
    except Exception:
        return ""


def make_packet_zip(folder):
    """Arxivni qo'lda yig'adi va mac fayllariga bajarilish ruxsatini beradi.

    shutil.make_archive ishlatib bo'lmaydi: Windows'da POSIX ruxsati
    yo'q, natijada macOS'da ornat.command bajarilmaydigan fayl bo'lib
    tushadi va jamoadosh uni ikki marta bosa olmaydi.
    """
    archive = folder + ".zip"
    root = os.path.dirname(folder)
    base = os.path.basename(folder)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            arc = "%s/%s" % (base, name)
            info = zipfile.ZipInfo(arc, date_time=time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if name in MAC_FILES else 0o644
            info.external_attr = (mode << 16) | 0o100000
            with open(path, "rb") as fh:
                zf.writestr(info, fh.read())
    return archive


def cmd_paket(cfg, member):
    """Jamoadosh uchun tayyor arxiv: ochib, ornat.cmd ni bosadi - tamom.

    Ichida shaxsiy token bo'ladi. Har kimga alohida token - odam ketganda
    faqat uniki o'chiriladi, qolganlar ishlayveradi.
    """
    member = (member or "").strip()
    if not member:
        print("Ism kerak:  note paket \"Алишер\"")
        return 1

    url = (cfg.get("telemetry_url") or "").strip()
    if not url:
        print("config.json da telemetry_url yo'q - avval uni to'ldiring.")
        return 1

    out_root = os.path.join(cfg["state"], "paket")
    name = "claude-note-" + (slugify(member, "jamoadosh") or "jamoadosh")
    folder = os.path.join(out_root, name)

    # Token SAQLANADI. Har qayta yasashda yangisini berish xavfli: paket
    # yangilanadi, serverdagi ro'yxatda esa eskisi qoladi va jamoadoshda
    # 401 chiqadi. Yangi token kerak bo'lsa - eski zip ni o'chiring.
    token = existing_token(folder + ".zip")
    reused = bool(token)
    token = token or secrets.token_hex(32)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)

    for f in PAKET_FILES:
        src = os.path.join(BASE, f)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(folder, f))

    # config.json ni to'g'ridan-to'g'ri nusxalash MUMKIN EMAS: unda vault va
    # state absolut yo'l bilan yozilgan (C:/Users/<ism>/...). Jamoadosh
    # kompyuterida bu yo'q joyga ishora qiladi. Ularni olib tashlaymiz -
    # DEFAULTS o'zi skript yonidagi papkani oladi. team_member ham chiqadi:
    # har kim o'z ismini ornat.cmd orqali oladi.
    try:
        with open(os.path.join(BASE, "config.json"), encoding="utf-8") as fh:
            base_cfg = json.load(fh)
    except Exception:
        base_cfg = {}
    for key in ("vault", "state", "team_member", "skip_projects"):
        base_cfg.pop(key, None)
    base_cfg["telemetry_enabled"] = False      # ornat.cmd yoqadi
    base_cfg["telemetry_url"] = url
    with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(base_cfg, fh, ensure_ascii=False, indent=2)

    with open(os.path.join(folder, "sozlama.json"), "w", encoding="utf-8") as fh:
        json.dump({"member": member, "token": token, "url": url},
                  fh, ensure_ascii=False, indent=2)

    # utf-8-sig: jamoadosh buni Bloknotda ochadi va BOM bo'lmasa
    # eski Bloknot kirillni buzib ko'rsatishi mumkin
    with open(os.path.join(folder, "OQING.txt"), "w",
              encoding="utf-8-sig") as fh:
        fh.write(
            "claude-note — %s учун\r\n"
            "%s\r\n\r\n"
            "0. JAMOA.md ни ўқинг — нима бу ва серверга нима кетади.\r\n"
            "1. Шу папкани компьютерингизга кўчиринг.\r\n"
            "2. Ўрнатишни ишга туширинг:\r\n"
            "     Windows — `ornat.cmd` ни ИККИ МАРТА БОСИНГ\r\n"
            "     macOS   — `ornat.command` ни ИККИ МАРТА БОСИНГ\r\n"
            "               (macOS «ишончсиз дастур» деса: файл устида\r\n"
            "                ўнг тугма → Open → Open. Фақат биринчи сафар.)\r\n"
            "3. Тамом. Бошқа ҳеч нарса қилиш шарт эмас.\r\n\r\n"
            "Кейин Claude Code билан одатдагидек ишлайверинг — ҳар сессия\r\n"
            "тугаганда ҳисобот ўзи юборилади.\r\n\r\n"
            "Серверга нима кетаётганини исталган пайт кўриш мумкин:\r\n"
            "    note telemetry test\r\n\r\n"
            "Юборилади: сессия вақти, лойиҳа номи, сарф, қайси восита неча\r\n"
            "марта ишлатилгани ва қисқа хулоса.\r\n"
            "Юборилмайди: суҳбат матни, код, файл мазмуни, файл йўллари.\r\n"
            % (member, "=" * 52))

    archive = make_packet_zip(folder)

    print("Пакет тайёр: %s" % archive)
    print()
    if reused:
        print("Токен ўзгармади — сервердаги рўйхатга тегиш шарт эмас.")
        print("(Янги токен керак бўлса, эски zip ни ўчириб қайта ясанг.)")
        print()
        print("Шу zip ни %s га юборинг." % member)
        return 0
    print("1) Шу zip ни %s га юборинг." % member)
    print("2) Серверда токенни рўйхатга қўшинг:")
    print()
    ssh_host = (os.environ.get("CLAUDE_NOTE_SSH") or cfg.get("server_ssh")
                or "<ssh-host>")
    srv_dir = (os.environ.get("CLAUDE_NOTE_SERVER_DIR") or cfg.get("server_dir")
               or "/opt/claude-note-server")
    print('   ssh %s "cd %s && '
          "sed -i 's/^INGEST_TOKENS=.*/&,%s/' .env && "
          'docker compose up -d"' % (ssh_host, srv_dir, token))
    print()
    print("   (server_ssh / server_dir - config.json yoki CLAUDE_NOTE_SSH /")
    print("    CLAUDE_NOTE_SERVER_DIR orqali; server lokal bo'lsa - .env ni")
    print("    qo'lda tahrirlab, serverni qayta ishga tushiring)")
    print()
    print("   (токен: %s)" % token)
    return 0


def main():
    ap = argparse.ArgumentParser(description="claude-note-win")
    ap.add_argument("--hook", action="store_true",
                    help="SessionEnd / PreCompact hook rejimi (stdin JSON)")
    ap.add_argument("--session-start", action="store_true",
                    help="SessionStart hook: brifingni kontekstga yuklaydi")
    ap.add_argument("--part", type=int, metavar="N",
                    help="--session-start bilan: brifingning N-qismi (1..%d); "
                         "berilmasa - hammasi bitta hookda" % BRIEF_PARTS)
    ap.add_argument("--job", help="ichki: fon jarayon uchun job fayli")
    ap.add_argument("--transcript", help="qo'lda: .jsonl yo'li")
    ap.add_argument("--session", help="qo'lda: sessiya id")
    ap.add_argument("--backfill", action="store_true", help="eski transkriptlarni qayta ishlash")
    ap.add_argument("--limit", type=int, default=5, help="backfill chegarasi")
    ap.add_argument("--mode", choices=["log", "inbox", "route"], help="config rejimini bekor qiladi")
    ap.add_argument("--reindex", action="store_true", help="faqat INDEX.md ni qayta yig'ish")
    ap.add_argument("--costs", action="store_true", help="loyihalar bo'yicha xarajat hisoboti")
    ap.add_argument("--analyze", action="store_true", help="sessiya nimaga qimmat tushganini tahlil qilish")
    ap.add_argument("--catchup", action="store_true",
                    help="tugallanmagan ishlarni qayta ishga tushirish")
    ap.add_argument("--scan", metavar="ROOT", nargs="?", const="auto",
                    help="diskdagi loyihalarni ro'yxatga olish "
                         "(standart: config scan_root yoki uy papkasi)")
    ap.add_argument("--telemetry", nargs="?", const="status",
                    choices=["status", "test", "send", "flush",
                             "backfill", "backfill-dry"],
                    help="jamoa hisoboti: holat / namuna / yuborish / navbat")
    ap.add_argument("--paket", metavar="ISM",
                    help="jamoadosh uchun tayyor arxiv (shaxsiy token bilan)")
    ap.add_argument("--antigravity", action="store_true",
                    help="Google Antigravity sessiyalarini eksport qilish")
    ap.add_argument("--compact-brief", metavar="LOYIHA", nargs="?", const="all",
                    help="brifingni LLM bilan ixchamlash (%d belgidan uzun yoki imlosi "
                         "buzilganlarini; --force - har qandayini)" % BRIEF_COMPACT_AT)
    ap.add_argument("--from-full", action="store_true",
                    help="--compact-brief manbasi: <loyiha>-brief.full.md arxivi")
    ap.add_argument("--fix-imlo", action="store_true",
                    help="bazadagi buzilgan kirill so'zlarni (фаҲат, ҲаР) lug'at bilan "
                         "tuzatish (--dry-run - faqat ko'rsatish)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.environ.get(GUARD_ENV):   # rekursiya himoyasi
        return 0

    cfg = load_config({"mode": args.mode})
    try:
        if args.antigravity and not args.backfill:
            try:
                from antigravity_export import export_antigravity_sessions
            except ImportError:
                print("antigravity_export moduli yo'q - bu buyruq shu "
                      "nusxada mavjud emas.")
                return 1
            res = export_antigravity_sessions(cfg)
            print("Google Antigravity eksport:")
            print("  Ko'rib chiqildi: %d suhbat" % res.get("scanned", 0))
            print("  Yangilandi: %d sessiya" % res.get("updated", 0))
            print("  Yozilgan qatorlar: %d" % res.get("lines_written", 0))
            if res.get("errors"):
                print("  Xatolar: %d ta" % len(res["errors"]))
                for err in res["errors"][:5]:
                    print("   - %s" % err)
            return 0
        elif args.scan:
            rows = cmd_scan(cfg, args.scan)
            print("Topildi: %d loyiha (%d tasida Claude ishlatilgan)"
                  % (len(rows), sum(1 for r in rows if r["used"])))
            for r in rows:
                print("  %s %-28s %s" % ("*" if r["used"] else " ", r["name"][:28], r["path"]))
            print("Ro'yxat: " + os.path.join(cfg["vault"], "PROJECTS.md"))
        elif args.paket:
            return cmd_paket(cfg, args.paket)
        elif args.compact_brief:
            if args.compact_brief == "all":
                pdir = os.path.join(cfg["vault"], "projects")
                names = sorted(f[:-len("-brief.md")] for f in os.listdir(pdir)
                               if f.endswith("-brief.md"))
            else:
                names = [args.compact_brief]
            for name in names:
                st, old, new, c = compact_brief(cfg, name, force=args.force,
                                                dry=args.dry_run, from_full=args.from_full)
                print("  %-22s %-10s %6d -> %6d belgi  $%.4f" % (name, st, old, new, c))
        elif args.fix_imlo:
            cmd_fix_imlo(cfg, dry=args.dry_run)
        elif args.telemetry:
            return cmd_telemetry(cfg, args.telemetry, args.session, args.transcript)
        elif args.catchup:
            n = catch_up(cfg, min_age_sec=0)
            print("Tiklangan ish: %d" % n)
        elif args.analyze:
            path = args.transcript
            if not path and args.session:
                path = find_session_transcript(args.session)
            if not path:
                print("--analyze uchun --session yoki --transcript kerak")
                return 1
            analyze_session(cfg, path)
        elif args.costs:
            write_cost_report(cfg)
            print_costs(cfg)
        elif args.reindex:
            if args.dry_run:
                n_s, n_a, sub_usd = backfill_subagents(cfg, dry=True)
                print("Yordamchi agentlar [SINOV, yozilmadi]: %d sessiya, "
                      "%d agent, $%.2f" % (n_s, n_a, sub_usd))
                return 0
            n_time = backfill_times(cfg)
            if n_time:
                print("Sessiya vaqti tiklandi: %d qator" % n_time)
            n_s, n_a, sub_usd = backfill_subagents(cfg)
            if n_s:
                print("Yordamchi agentlar tiklandi: %d sessiya, %d agent, $%.2f"
                      % (n_s, n_a, sub_usd))
            print("INDEX.md yangilandi: %d eslatma" % rebuild_index(cfg))
            print("Yetishmagan sessiya sahifalari: %d" % ensure_session_stubs(cfg))
            print("Loyiha sahifalari: %d" % rebuild_projects(cfg))
            write_cost_report(cfg)
        elif args.session_start:
            cmd_session_start(cfg, args.part)
        elif args.hook:
            cmd_hook(cfg)
        elif args.job:
            cmd_job(cfg, args.job)
        elif args.backfill:
            if args.antigravity:
                try:
                    from antigravity_export import export_antigravity_sessions
                except ImportError:
                    print("antigravity_export moduli yo'q - bu buyruq shu "
                          "nusxada mavjud emas.")
                    return 1
                export_antigravity_sessions(cfg)
            cmd_backfill(cfg, args.limit, args.dry_run, args.force,
                         only_kind="antigravity" if args.antigravity else None)
        elif args.transcript or args.session:
            path = args.transcript
            if not path:
                path = find_session_transcript(args.session)
                if not path:
                    print("Transkript topilmadi: " + args.session)
                    return 1
            sid = args.session or session_id_of_transcript(path)
            process(cfg, path, sid, cwd_of_transcript(path),
                    dry=args.dry_run, force=args.force)
        else:
            ap.print_help()
    except Exception:
        log(cfg, "FATAL\n" + traceback.format_exc())
        if not (args.hook or args.session_start):   # hook Claude Code ni to'xtatmasin
            traceback.print_exc()
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

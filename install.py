#!/usr/bin/env python3
"""Claude Code va ChatGPT Codex uchun claude-note o'rnatuvchisi.

Nima qiladi:
  1. ~/.claude/settings.json ga SessionEnd hook qo'shadi (user darajasida = BARCHA loyihalar).
  2. ~/.claude/CLAUDE.md ga vault/INDEX.md importini qo'shadi (baza har sessiyada ko'rinadi).
  3. ~/.codex/hooks.json ga ayni lifecycle hooklarni qo'shadi.
  4. O'zgartiriladigan fayllardan oldin zaxira nusxa oladi.

Ishlatish:
  python install.py            -- o'rnatish / yangilash
  python install.py --status   -- holatni ko'rish
  python install.py --uninstall -- olib tashlash
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
CLAUDE_MD = os.path.join(CLAUDE_DIR, "CLAUDE.md")
CODEX_DIR = os.path.join(os.path.expanduser("~"), ".codex")
CODEX_HOOKS = os.path.join(CODEX_DIR, "hooks.json")
NOTE_PY = BASE + "/note.py"
INDEX_MD = BASE + "/vault/INDEX.md"
MARK = "claude-note"


def python_exe():
    """Hook stdin dan JSON o'qiydi, shuning uchun konsolli python.exe kerak
    (pythonw.exe emas - unda stdin ishonchli emas). Fon jarayon esa note.py
    ichida pythonw bilan uzilib ketadi."""
    return sys.executable.replace("\\", "/")


HOOK_CMD = '"%s" "%s" --hook' % (python_exe(), NOTE_PY)
START_CMD = '"%s" "%s" --session-start' % (python_exe(), NOTE_PY)
HOOK_CMD_WINDOWS = '& "%s" "%s" --hook' % (python_exe(), NOTE_PY)
START_CMD_WINDOWS = '& "%s" "%s" --session-start' % (python_exe(), NOTE_PY)
# Claude Code ~10 000 belgidan uzun hook chiqishini kesadi, lekin chegara HAR BIR
# hookka alohida - shuning uchun katta brifing 3 ta hookka bo'linadi (note.py
# BRIEF_PARTS). Kichik loyihada 2- va 3-qism hech narsa chiqarmaydi.
START_PARTS = 3

# Uchta hook, uchta vazifa:
#   SessionEnd   - sessiya tugadi (/clear ham shu yerda) -> bilimni bazaga yoz
#   PreCompact   - kompakt tafsilotni yo'qotishidan OLDIN -> bilimni bazaga yoz
#   SessionStart - yangi sessiya -> loyiha brifingini kontekstga qaytar
HOOKS = {
    "SessionEnd": HOOK_CMD,
    "PreCompact": HOOK_CMD,
    "SessionStart": START_CMD,
}

IMPORT_BLOCK = (
    "\n<!-- %s:start -->\n"
    "## Билим базаси\n\n"
    "Олдинги сессиялар билими: `%s/vault/notes/` (бир мавзу — бир файл, ҳар бирида "
    "`name`/`description`/лойиҳа). Индекс контекстга юкланмайди (≈25k токен тежаш учун) — "
    "лойиҳа брифинги сессия бошида ҳук орқали келади. Вазифага оид тузоқ/қарор керак бўлса — "
    "шу папкада Grep (калит сўз, лойиҳа номи) билан қидириб, топилган эслатманинг ўзини ўқи; "
    "лойиҳа эслатмалари рўйхати: `vault/projects/<лойиҳа>.md`.\n"
    "<!-- %s:end -->\n"
) % (MARK, BASE, MARK)


def backup(path):
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = "%s.bak-%s" % (path, stamp)
        shutil.copy2(path, dst)
        return dst
    return None


def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def hook_entries(data, event="SessionEnd"):
    return (data.get("hooks") or {}).get(event) or []


def is_ours(entry):
    for h in entry.get("hooks") or []:
        if "note.py" in str(h.get("command", "")):
            return True
    return False


def install_hooks(path, label, timeout, windows_override=False, start_parts=None):
    data = load_json(path)
    bak = backup(path)
    if bak:
        print("  zaxira: " + bak)
    hooks = data.setdefault("hooks", {})
    for event, cmd in HOOKS.items():
        keep = [e for e in (hooks.get(event) or []) if not is_ours(e)]
        handler = {"type": "command", "command": cmd, "timeout": timeout}
        if windows_override:
            handler["commandWindows"] = (START_CMD_WINDOWS if event == "SessionStart"
                                          else HOOK_CMD_WINDOWS)
        handlers = [handler]
        if event == "SessionStart" and start_parts:
            handlers = []
            for n in range(1, start_parts + 1):
                h = dict(handler, command="%s --part %d" % (cmd, n))
                if "commandWindows" in h:
                    h["commandWindows"] = "%s --part %d" % (h["commandWindows"], n)
                handlers.append(h)
        keep.append({"hooks": handlers})
        hooks[event] = keep
        print("  [ok] %s %s hook qo'shildi" % (label, event))
    save_json(path, data)
    print("  -> " + path)


def uninstall_hooks(path, label):
    if not os.path.exists(path):
        return
    data = load_json(path)
    backup(path)
    hooks = data.get("hooks") or {}
    for event in HOOKS:
        left = [e for e in (hooks.get(event) or []) if not is_ours(e)]
        if left:
            hooks[event] = left
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    save_json(path, data)
    print("  [ok] %s hooklari olib tashlandi" % label)


def install():
    print("claude-note (Claude Code + Codex) o'rnatilmoqda...")
    print("  papka : " + BASE)
    print("  python: " + python_exe())

    install_hooks(SETTINGS, "Claude", 20, start_parts=START_PARTS)
    # Codex SessionEnd timeout'i rasmiy ravishda ko'pi bilan 3 soniya. Hook
    # og'ir ishni fon jarayoniga topshirib darhol chiqadi.
    # commandWindows - PowerShell varianti; macOS/Linux da uni yozishning
    # ma'nosi yo'q, faqat sozlamani ifloslantiradi
    install_hooks(CODEX_HOOKS, "Codex", 3,
                  windows_override=(sys.platform == "win32"))

    md = ""
    if os.path.exists(CLAUDE_MD):
        with open(CLAUDE_MD, encoding="utf-8") as fh:
            md = fh.read()
        backup(CLAUDE_MD)
    if MARK + ":start" in md:
        start = md.index("<!-- %s:start -->" % MARK)
        end = md.index("<!-- %s:end -->" % MARK) + len("<!-- %s:end -->" % MARK)
        md = md[:start] + IMPORT_BLOCK.strip() + md[end:]
    else:
        md = md.rstrip("\n") + "\n" + IMPORT_BLOCK
    with open(CLAUDE_MD, "w", encoding="utf-8") as fh:
        fh.write(md)
    print("  [ok] INDEX.md importi qo'shildi -> " + CLAUDE_MD)

    print("\nTayyor. Claude Code va Codex sessiyalari avtomat yoziladi.")
    print("Codex'da bir marta /hooks ni ochib yangi hooklarni Trust qiling.")
    print("Tekshirish : python install.py --status")
    print("Log        : " + BASE + "/state/note.log")


def uninstall():
    uninstall_hooks(SETTINGS, "Claude")
    uninstall_hooks(CODEX_HOOKS, "Codex")

    if os.path.exists(CLAUDE_MD):
        with open(CLAUDE_MD, encoding="utf-8") as fh:
            md = fh.read()
        if MARK + ":start" in md:
            backup(CLAUDE_MD)
            start = md.index("<!-- %s:start -->" % MARK)
            end = md.index("<!-- %s:end -->" % MARK) + len("<!-- %s:end -->" % MARK)
            with open(CLAUDE_MD, "w", encoding="utf-8") as fh:
                fh.write((md[:start] + md[end:]).strip() + "\n")
            print("  [ok] CLAUDE.md dan import olib tashlandi")
    print("\nVault va eslatmalar o'chirilmadi: " + BASE + "/vault")


def status():
    for label, path in (("Claude", SETTINGS), ("Codex", CODEX_HOOKS)):
        data = load_json(path)
        for event in HOOKS:
            ours = [e for e in hook_entries(data, event) if is_ours(e)]
            print("%-6s %-13s : %s" % (
                label, event, "O'RNATILGAN" if ours else "yo'q"))
    md_ok = os.path.exists(CLAUDE_MD) and MARK + ":start" in open(CLAUDE_MD, encoding="utf-8").read()
    print("CLAUDE.md     : " + ("ulangan" if md_ok else "ulanmagan"))

    try:
        with open(BASE + "/config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
        # state/config.local.json config.json ustidan yozadi - holat
        # haqiqiy sozlamani ko'rsatsin, aks holda "o'chiq" deb yolg'on aytadi
        try:
            state = cfg.get("state") or (BASE + "/state")
            with open(state + "/config.local.json", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception:
            pass
        print("Rejim         : %s   |   til: %s   |   model: %s"
              % (cfg.get("mode"), cfg.get("language"), cfg.get("model")))
        print("Sintez        : %s   |   Codex model: %s"
              % (cfg.get("synthesis_provider") or "auto",
                 cfg.get("codex_model") or "default"))
        if cfg.get("telemetry_enabled"):
            n_queued = len(glob.glob(BASE + "/state/outbox/*.json"))
            print("Telemetriya   : YOQILGAN -> %s%s"
                  % (cfg.get("telemetry_url") or "(url yo'q)",
                     "   |   navbatda: %d" % n_queued if n_queued else ""))
            print("Kim           : %s" % (cfg.get("team_member") or "(user@host)"))
        else:
            print("Telemetriya   : o'chiq   (batafsil: note telemetry)")
    except Exception:
        print("config.json   : o'qib bo'lmadi")

    brief_dir = BASE + "/vault/projects"
    briefs = [f for f in os.listdir(brief_dir) if f.endswith("-brief.md")]         if os.path.isdir(brief_dir) else []
    print("Брифинглар    : %d ta loyiha" % len(briefs))
    notes_dir = BASE + "/vault/notes"
    n_notes = len([f for f in os.listdir(notes_dir)]) if os.path.isdir(notes_dir) else 0
    try:
        with open(BASE + "/state/processed.json", encoding="utf-8") as fh:
            n_sessions = len(json.load(fh))
    except Exception:
        n_sessions = 0
    print("Baza          : %d eslatma, %d sessiya ishlangan" % (n_notes, n_sessions))
    print("Vault         : " + BASE + "/vault")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="claude-note-win o'rnatuvchisi")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.uninstall:
        uninstall()
    elif args.status:
        status()
    else:
        install()

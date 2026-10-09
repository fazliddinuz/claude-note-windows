# -*- coding: utf-8 -*-
"""claude-note - bir marta bosib o'rnatish.

Jamoadosh `ornat.cmd` ni ikki marta bosadi - qolgan hamma narsa shu yerda
bo'ladi: hooklar ro'yxatdan o'tadi, ism va token yoziladi, server bilan
aloqa tekshiriladi.

Yonida `sozlama.json` bo'lsa (paket bilan kelgan bo'lsa) hech narsa
so'ralmaydi. Bo'lmasa ism va token so'raladi.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def say(msg=""):
    print("   " + msg if msg else "")


def head(msg):
    print()
    print("   " + "-" * 56)
    print("   " + msg)
    print("   " + "-" * 56)


def fail(msg):
    print()
    say("[X] " + msg)
    return 1


def load_packet():
    """Paket bilan kelgan sozlama: ism, token, server."""
    for name in ("sozlama.json", "sozlama.txt"):
        path = os.path.join(BASE, name)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    return json.load(fh)
            except Exception:
                say("[!] %s o'qib bo'lmadi - qo'lda so'raladi" % name)
    return {}


def ask(label, current=""):
    if current:
        return current
    try:
        val = input("   %s: " % label).strip()
    except EOFError:
        val = ""
    return val


def run_installer():
    head("1/4  Hooklar ro'yxatdan o'tkazilmoqda")
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "install.py")],
                           cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception as exc:
        say("[X] install.py ishga tushmadi: %s" % exc)
        return False
    for ln in (r.stdout or "").splitlines():
        say(ln)
    if r.returncode != 0:
        for ln in (r.stderr or "").splitlines()[-8:]:
            say(ln)
        return False
    return True


def write_local(member, token, url):
    head("2/4  Sozlama yozilmoqda")
    state = os.path.join(BASE, "state")
    os.makedirs(state, exist_ok=True)

    local = {"telemetry_enabled": True, "team_member": member}
    if url:
        local["telemetry_url"] = url

    # Shu papkaning o'zi ham loyiha bo'lib ko'rinadi: note.py ning ichki
    # `claude -p` chaqiruvlari shu yerdan turadi. Uni ro'yxatdan chiqarib
    # qo'yamiz - nomi har kompyuterda boshqacha bo'lgani uchun shu yerda
    # hisoblanadi, config.json da qotirilmaydi.
    try:
        sys.path.insert(0, BASE)
        import note
        local["skip_projects"] = [note.project_of(cwd=BASE)]
    except Exception:
        pass
    with open(os.path.join(state, "config.local.json"), "w",
              encoding="utf-8") as fh:
        json.dump(local, fh, ensure_ascii=False, indent=2)
    say("ism va sozlama : state/config.local.json")

    tok_path = os.path.join(state, "server_token.txt")
    with open(tok_path, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    try:
        os.chmod(tok_path, 0o600)
    except Exception:
        pass
    say("token          : state/server_token.txt")
    say("")
    say("Ikkalasi ham git ga tushmaydi - faqat shu kompyuterda qoladi.")
    return True


def check_server(url, token):
    """Token haqiqatan ishlayaptimi - serverdan so'rab ko'ramiz."""
    head("3/4  Server bilan aloqa tekshirilmoqda")
    if not url:
        say("[!] server manzili yo'q - tekshirib bo'lmadi")
        return False
    ping = url.rsplit("/", 1)[0] + "/ping"
    req = urllib.request.Request(ping, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read(500)
        say("[OK] server javob berdi: " + ping)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            say("[X] token qabul qilinmadi (401). Tokenni tekshiring.")
        else:
            say("[X] server xatosi: HTTP %s" % exc.code)
    except Exception as exc:
        say("[X] serverga ulanib bo'lmadi: %s" % exc)
        say("    Internet yo'q bo'lsa ham ishlayveradi: hisobotlar")
        say("    navbatda kutadi va keyinroq o'zi ketadi.")
    return False


def show_status():
    head("4/4  Holat")
    try:
        r = subprocess.run([sys.executable, os.path.join(BASE, "note.py"),
                            "--telemetry"], cwd=BASE, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=60)
        for ln in (r.stdout or "").splitlines():
            say(ln)
    except Exception as exc:
        say("[!] holatni ko'rsatib bo'lmadi: %s" % exc)


def main():
    print()
    print("   ============================================================")
    print("    claude-note  -  o'rnatish")
    print("   ============================================================")

    cfg = load_packet()
    member = cfg.get("member") or cfg.get("team_member") or ""
    token = cfg.get("token") or ""
    url = cfg.get("url") or cfg.get("telemetry_url") or ""

    if not url:
        try:
            with open(os.path.join(BASE, "config.json"), encoding="utf-8") as fh:
                url = (json.load(fh) or {}).get("telemetry_url") or ""
        except Exception:
            url = ""

    if member and token:
        say("")
        say("Paket topildi: %s" % member)
    else:
        say("")
        say("Ikkita narsa kerak. Ikkalasini ham jamoa serveri egasi beradi.")
        member = ask("Ismingiz (hisobotda shunday ko'rinadi)", member)
        token = ask("Server tokeni", token)
        if not url:
            url = ask("Server manzili (masalan "
                      "https://your-server.example.com/ingest)", url)

    if not member:
        return fail("Ism kiritilmadi.")
    if not token:
        return fail("Token kiritilmadi.")

    if not run_installer():
        return fail("Hooklarni o'rnatib bo'lmadi. Yuqoridagi xatoga qarang.")
    if not write_local(member, token, url):
        return fail("Sozlamani yozib bo'lmadi.")

    ok = check_server(url, token)
    show_status()

    print()
    print("   ============================================================")
    if ok:
        print("    TAYYOR. Boshqa hech narsa qilish shart emas.")
    else:
        print("    O'RNATILDI. Server bilan aloqa hozir yo'q - hisobotlar")
        print("    navbatda kutadi va aloqa tiklanganda o'zi ketadi.")
    print("   ============================================================")
    say("")
    say("Endi Claude Code bilan odatdagidek ishlayvering.")
    say("Har sessiya tugaganda hisobot o'zi yuboriladi.")
    say("")
    say("Serverga nima ketayotganini istalgan payt ko'rish:")
    say("    note telemetry test")
    return 0


if __name__ == "__main__":
    sys.exit(main())

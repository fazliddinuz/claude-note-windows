# -*- coding: utf-8 -*-
"""Lokal sinov: serverni ishga tushiradi va HAQIQIY sessiyalarni yuboradi.

    python dev_run.py

Mijozning o'z kodi (note.build_report) bilan hisobot yig'iladi, ya'ni
serverga aynan prodda ketadigan narsa keladi. Baza vaqtinchalik -
data-dev/ papkasida, haqiqiy ma'lumot bilan aralashmaydi.

DIQQAT: bu yerda har sessiya BUTUN transkript bo'yicha hisoblanadi (0 dan
lines_done gacha), shu sababli summalar COSTS.md dagidan katta chiqadi -
COSTS.md faqat note.py haqiqatan ishlagan bo'laklarni sanaydi. Prodda esa
har ishlov o'z bo'lagini yuboradi, ya'ni ikki hisob mos keladi. Bu sinov
ma'lumoti, hisobot emas.
"""

import glob
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.dirname(HERE)
sys.stdout.reconfigure(encoding="utf-8")

PORT = int(os.environ.get("DEV_PORT") or 8787)
TOKEN = "dev-token"
PAROL = "dev"

os.environ.setdefault("DATA_DIR", os.path.join(HERE, "data-dev"))
os.environ.setdefault("INGEST_TOKENS", TOKEN)
os.environ.setdefault("DASHBOARD_PASSWORD", PAROL)
os.environ.setdefault("KNOWN_PROJECTS", "demoproject,WebShop,MobileApp")

sys.path.insert(0, HERE)
import app as srv                                    # noqa: E402
import uvicorn                                       # noqa: E402


def seed():
    """Lokal daftardagi sessiyalarni haqiqiy mijoz kodi bilan yuboradi."""
    sys.path.insert(0, CLIENT)
    import note

    cfg = note.load_config()
    st = note.load_state(cfg)
    sent = skipped = 0
    for sid, prev in st.items():
        hits = glob.glob(os.path.join(os.path.expanduser("~"), ".claude",
                                      "projects", "*", sid + ".jsonl"))
        if not hits:
            skipped += 1
            continue
        end_line = int(prev.get("lines_done") or 0) or None
        per_model, work = note.session_usage(hits[0], 0, end_line)
        row = note.build_report(
            cfg, sid, prev.get("project") or "?", prev.get("date") or "",
            prev.get("last_event") or "SessionEnd", hits[0], 0, end_line or 0,
            per_model, work, prev.get("cost_usd") or 0,
            title=prev.get("title"), summary=None,
            notes_added=prev.get("added") or 0)

        req = urllib.request.Request(
            "http://127.0.0.1:%d/ingest" % PORT,
            data=__import__("json").dumps(row, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            sent += 1
            print("   yuborildi: %-22s %-10s $%.2f  %s"
                  % (row["project"], row["date"], row["work_cost_usd"],
                     (row["title"] or "")[:40]))
        except Exception as exc:
            print("   XATO: %s -> %s" % (sid[:8], exc))
    print("\n   jami %d ta yuborildi, %d ta transkript topilmadi\n" % (sent, skipped))


def later():
    time.sleep(1.5)
    print("\n--- haqiqiy sessiyalar yuborilmoqda ---")
    try:
        seed()
    except Exception:
        import traceback
        traceback.print_exc()
    print("=" * 62)
    print("  Dashboard : http://127.0.0.1:%d/     парол: %s" % (PORT, PAROL))
    print("  Sessiyalar: http://127.0.0.1:%d/sessions" % PORT)
    print("=" * 62)


if __name__ == "__main__":
    threading.Thread(target=later, daemon=True).start()
    uvicorn.run(srv.app, host="127.0.0.1", port=PORT, log_level="warning")

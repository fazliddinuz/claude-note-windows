# -*- coding: utf-8 -*-
"""Server sinovi: ingest, autentifikatsiya, dashboard va ogohlantirishlar.

Ishga tushirish:  python test_server.py
Vaqtinchalik baza ishlatiladi, haqiqiy ma'lumotga tegilmaydi.
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

TMP = tempfile.mkdtemp(prefix="cn-test-")
os.environ.update(
    DATA_DIR=TMP,
    INGEST_TOKENS="tok-aaa,tok-bbb",
    DASHBOARD_PASSWORD="parol123",
    SECRET_KEY="sinov-kaliti",
    KNOWN_PROJECTS="demoproject,WebShop,MobileApp",
    ALERT_SESSION_USD="15",
    ALERT_DAY_USD="40",
    ALERT_CONTEXT_TOKENS="150000",
    HISTORY_BEFORE="2026-09-01",
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient    # noqa: E402
import app as srv                            # noqa: E402
import db                                    # noqa: E402

c = TestClient(srv.app)
OK = []


def check(name, cond, info=""):
    OK.append(bool(cond))
    print("  %-46s %s%s" % (name, "OK" if cond else "XATO",
                            ("  << " + str(info)) if (info and not cond) else ""))


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


def rep(**kw):
    """Sinov hisoboti - mijoz yuboradigan shakl."""
    base = {
        "v": 1, "member": "Алишер", "host": "AL-PC", "os_user": "alisher",
        "project": "demoproject", "session": "s-0001", "agent": "claude",
        "date": datetime.date.today().isoformat(),
        "event": "SessionEnd",
        "started_at": "2026-09-07T05:00:00.000Z",
        "ended_at": "2026-09-07T09:30:00.000Z",
        "sent_at": "2026-09-07T14:30:00", "tz_offset_min": 300,
        "title": "Каталог майдонлари", "summary": "Артикул ажратилди.",
        "requests": 40, "work_cost_usd": 3.5, "note_cost_usd": 0.2,
        "models": ["claude-opus-5"], "avg_context": 60000,
        "tools": {"Bash": 20, "Edit": 12, "Skill: dataviz": 1},
        "tool_cost": {"Bash": 2.1, "Edit": 1.0, "Skill: dataviz": 0.4},
        "usage": {"claude-opus-5": {"requests": 40, "in": 500, "out": 9000,
                                    "cw5": 12000, "cw1h": 0, "cr": 6000000,
                                    "cost_usd": 3.5}},
        "notes_added": 4,
        "lines": [0, 400],
    }
    base.update(kw)
    return base


def post(row, token="tok-aaa"):
    return c.post("/ingest", json=row,
                  headers={"Authorization": "Bearer " + token})


print("\n=== 1. Autentifikatsiya ===")
check("tokensiz -> 401", c.post("/ingest", json=rep()).status_code == 401)
check("noto'g'ri token -> 401", post(rep(), "yomon").status_code == 401)
check("to'g'ri token -> 200", post(rep()).status_code == 200)
check("ikkinchi token ham ishlaydi",
      post(rep(session="s-0002"), "tok-bbb").status_code == 200)

print("\n=== 2. Takror va noto'g'ri yozuv ===")
r = post(rep())
check("bir xil yozuv -> duplicate", r.json().get("duplicate") == 1, r.json())
check("majburiy maydonsiz -> 400",
      post({"member": "X"}).status_code == 400)
check("noto'g'ri sana -> 400",
      post(rep(session="s-x", date="07.09.2026")).status_code == 400)
check("JSON emas -> 400",
      c.post("/ingest", content=b"salom",
             headers={"Authorization": "Bearer tok-aaa"}).status_code == 400)
check("juda katta yozuv -> 413",
      c.post("/ingest", content=b"x" * 70000,
             headers={"Authorization": "Bearer tok-aaa"}).status_code == 413)

print("\n=== 3. Kutilmagan maydon bazaga tushmasin ===")
post(rep(session="s-0003", parol="MAXFIY", transcript="xom matn"))
raw = json.dumps(srv.clean(rep(session="s-x", parol="MAXFIY")), ensure_ascii=False)
check("oq ro'yxatdan tashqari maydon tashlanadi", "MAXFIY" not in raw)

print("\n=== 4. Ma'lumot to'plami ===")
rows = [
    rep(session="n-1", member="Алишер", started_at="2026-09-07T21:30:00.000Z",
        ended_at="2026-09-07T22:10:00.000Z", title="Тунги тузатиш"),
    rep(session="n-2", member="Бобур", project="ShaxsiyLoyiha",
        title="Бегона лойиҳа", work_cost_usd=2.0),
    rep(session="n-3", member="Бобур", work_cost_usd=22.0,
        title="Қиммат сессия", avg_context=90000),
    # tarix chegarasidan (2026-09-01) KEYIN boshlanadi, lekin bir necha kun
    # davom etadi - "uzun sessiya" belgisi va kontekst ogohlantirishi shunga
    rep(session="n-4", member="Алишер", avg_context=260000,
        title="Узун сессия", started_at="2026-09-02T06:00:00.000Z",
        ended_at="2026-09-06T15:00:00.000Z", work_cost_usd=25.0),
    rep(session="n-5", member="Нодир", project="WebShop",
        title="Оддий иш", work_cost_usd=1.2),
]
rows.append(rep(session="n-codex", agent="codex", member="CodexUser",
                title="Codex session", models=["gpt-5.6-sol"],
                usage={"gpt-5.6-sol": {"requests": 3, "in": 100, "out": 20,
                                        "cw5": 0, "cw1h": 0, "cr": 200,
                                        "cost_usd": 0}}))
for row in rows:
    check("  yozildi: %s" % row["title"], post(row).status_code == 200)

print("\n=== 5. Dashboard kirishsiz ochilmasin ===")
r = c.get("/", follow_redirects=False)
check("/ -> /login ga yo'naltiradi",
      r.status_code == 303 and "/login" in r.headers.get("location", ""))
r = c.post("/login", data={"login": "admin", "parol": "yomon", "next": "/"},
           follow_redirects=False)
check("noto'g'ri parol -> xato", "xato=1" in r.headers.get("location", ""))
r = c.post("/login", data={"login": "admin", "parol": "parol123", "next": "/"},
           follow_redirects=False)
check("to'g'ri parol -> cookie", srv.COOKIE in r.cookies or
      "set-cookie" in {k.lower() for k in r.headers})

print("\n=== 6. Dashboard ===")
r = c.get("/?days=0")
body = r.text
check("sahifa ochildi", r.status_code == 200, r.status_code)
check("odamlar ko'rinadi", "Алишер" in body and "Бобур" in body)
check("loyihalar ko'rinadi", "demoproject" in body)
check("ogohlantirish: begona loyiha", "Бегона лойиҳа" in body)
check("ogohlantirish: qimmat sessiya", "Қиммат сессия" in body)
check("ogohlantirish: katta kontekst", "Контекст жуда катта" in body)
check("ogohlantirish: tungi ish", "Тунги иш" in body)
check("maslahat matni bor", "clear" in body.lower())

print("\n=== 7. Sessiyalar ro'yxati ===")
r = c.get("/sessions?days=0")
body = r.text
check("sahifa ochildi", r.status_code == 200)
check("sarlavha ko'rinadi", "Узун сессия" in body)
check("vaqt oralig'i ko'rinadi", "→" in body)
check("uzun sessiya belgilangan", "узун" in body)
check("vositalar ko'rinadi", "Bash" in body)
r = c.get("/sessions?days=0&member=" + "Бобур")
# "Нодир" tanlash ro'yxatida baribir turadi - shuning uchun jadval
# qatoriga tegishli sarlavha bo'yicha tekshiramiz
check("odam bo'yicha filtr",
      "Қиммат сессия" in r.text and "Оддий иш" not in r.text)
r = c.get("/sessions?days=0&q=" + "Тунги")
check("qidiruv ishlaydi", "Тунги тузатиш" in r.text)

print("\n=== 7b. Agent filtri ===")
r = c.get("/sessions?days=0&agent=codex")
check("Codex sessiyasi ko'rinadi", "Codex session" in r.text)
check("Codex belgisi ko'rinadi", "agent-codex" in r.text)
check("Claude sessiyalari yashiriladi", "claude-opus-5" not in r.text)
r = c.get("/?days=0&agent=claude")
check("Claude dashboard Codex modelini yashiradi", "gpt-5.6-sol" not in r.text)
r = c.get("/?days=0&agent=codex")
check("Codex dashboard filtri ishlaydi", "CodexUser" in r.text)

print("\n=== 8. Vaqt mijoz mintaqasida ===")
row = {"started_at": "2026-09-07T21:30:00.000Z",
       "ended_at": "2026-09-07T22:10:00.000Z", "tz_offset_min": 300,
       "duration_min": 40, "date": "2026-09-07"}
check("UTC+5 ga o'girildi", srv.when(row) == "2026-09-08 02:30→03:10",
      srv.when(row))
row2 = dict(row, started_at="2026-08-20T06:00:00.000Z",
            ended_at="2026-09-07T15:00:00.000Z", duration_min=26000)
check("bir necha kunlik sessiya ko'rsatilади", "→ 09-07" in srv.when(row2),
      srv.when(row2))
check("uzun sessiya aniqlanadi", srv.multiday(row2) is True)

print("\n=== 8b. Narx qayerdan chiqdi ===")
r = c.get("/?days=0")
body = r.text
check("model taqsimoti ko'rinadi", "claude-opus-5" in body and "Кэш ўқиш" in body)
# jadval yig'indi ko'rsatadi (bir nechta yozuv), aniq raqam emas -
# formatlangan katta son borligini tekshiramiz
check("cache read raqami formatlangan", "000 000" in body)
check("vosita narxi ko'rinadi", "Пул қайси ишга кетди" in body)
check("skill nomi ko'rinadi", "Skill: dataviz" in body)
mrows, cov = db.model_totals(os.path.join(TMP, "telemetry.db"), 0)
check("model_totals yig'indisi", bool(mrows) and mrows[0]["cr"] > 0, mrows)
r = c.get("/sessions?days=0")
check("sessiya tafsilotida taqsimot", "Кэш ёзиш" in r.text)

print("\n=== 8c. Buzuq usage yiqitmasin ===")
check("usage ro'yxat bo'lsa -> tashlanadi",
      post(rep(session="bad-1", usage=["a", "b"])).status_code == 200)
check("tool_cost matn bo'lsa -> tashlanadi",
      post(rep(session="bad-2", tool_cost="salom")).status_code == 200)
check("usage ichida axlat bo'lsa -> tashlanadi",
      post(rep(session="bad-3",
               usage={"m": {"in": "yo'q", "cost_usd": None}})).status_code == 200)

print("\n=== 8d. Kun sessiya boshlangan kun bo'yicha ===")
DBP = os.path.join(TMP, "telemetry.db")
# 2026-08-10 da boshlanib bugun tugagan sessiya BUGUNGA tushmasligi kerak
post(rep(session="eski-1", started_at="2026-08-10T05:00:00.000Z",
         ended_at=datetime.date.today().isoformat() + "T10:00:00.000Z",
         work_cost_usd=500.0, title="Бир ойлик сессия"))
today = c.get("/?days=1").text
check("uzoq sessiya BUGUNGA tushmadi", "Бир ойлик сессия" not in today)
# matn qidirish ishonchsiz - summani bazadan o'qib solishtiramiz
bugun = db.totals(os.path.join(TMP, "telemetry.db"), 1,
                  since="2026-09-01")["work_cost"]
check("bugungi summaga $500 qo'shilmadi", bugun < 500, bugun)

rows = db.sessions(DBP, days=0, limit=50, since=None)
eski = [r for r in rows if r["session"] == "eski-1"]
check("day = boshlangan kun", bool(eski) and eski[0]["day"] == "2026-08-10",
      eski[0]["day"] if eski else None)

print("\n=== 8e. Tarix ajratilgan ===")
t_all = db.totals(DBP, 0)
t_new = db.totals(DBP, 0, since="2026-09-01")
t_old = db.totals(DBP, 0, since="2026-09-01", history=True)
check("tarix + yangi = hammasi",
      abs((t_new["work_cost"] + t_old["work_cost"]) - t_all["work_cost"]) < 0.01,
      (t_new["work_cost"], t_old["work_cost"], t_all["work_cost"]))
check("eski sessiya tarixda", t_old["sessions"] >= 1)
body = c.get("/?days=0").text
check("dashboardda tarix bloki bor", "Тарих" in body)
check("tarix asosiy hisobga qo'shilmagan", "Бир ойлик сессия" not in body)
r = c.get("/sessions?days=0&tarix=1")
check("tarix ro'yxati ochiladi", "Бир ойлик сессия" in r.text)

print("\n=== 8f. Eski bazadan migratsiya ===")
# Prodda jadval allaqachon bor edi va yangi ustunlar yo'q edi. Shu holatni
# ataylab yasab, init() uni ko'tara olishini tekshiramiz.
import sqlite3 as _sq
OLD = os.path.join(TMP, "eski.db")
_c = _sq.connect(OLD)
_c.executescript("""
CREATE TABLE reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
  v INTEGER, member TEXT NOT NULL, host TEXT, os_user TEXT,
  project TEXT NOT NULL, session TEXT NOT NULL, date TEXT NOT NULL,
  event TEXT, started_at TEXT, ended_at TEXT, sent_at TEXT,
  tz_offset_min INTEGER, local_hour INTEGER, duration_min INTEGER,
  title TEXT, summary TEXT, requests INTEGER, work_cost_usd REAL,
  note_cost_usd REAL, models TEXT, avg_context INTEGER, tools TEXT,
  notes_added INTEGER, line_start INTEGER, line_end INTEGER, raw TEXT);
INSERT INTO reports (received_at, member, project, session, date,
                     started_at, tz_offset_min, work_cost_usd)
VALUES ('2026-09-07', 'X', 'p', 'old-1', '2026-09-07',
        '2026-08-11T05:00:00.000Z', 300, 7.0);
""")
_c.commit()
_c.close()
try:
    db.init(OLD)
    ok_mig = True
except Exception as exc:
    ok_mig = False
    print("      ", exc)
check("eski baza ko'tarildi", ok_mig)
if ok_mig:
    rows = db.sessions(OLD, days=0, limit=5)
    check("eski qatorga day hisoblandi",
          bool(rows) and rows[0]["day"] == "2026-08-11",
          rows[0]["day"] if rows else None)
    check("yangi ustunlar qo'shildi", db.model_totals(OLD, 0)[1] == 0)
    check("eski qatorga loyiha nomi to'ldirildi",
          bool(rows) and rows[0]["project_canon"] == "p",
          rows[0].get("project_canon") if rows else None)

print("\n=== 9. Health ===")
r = c.get("/health")
check("health ochiq", r.status_code == 200 and r.json()["ok"])
check("yozuvlar soni to'g'ri", r.json()["reports"] >= 8, r.json())

print("\n=== 11. Foydalanuvchilar ===")
import users as U  # noqa: E402

check("boshlang'ich admin yaratildi", "admin" in U.load(TMP))
check("admin paroli .env dan", U.check(TMP, "admin", "parol123")[0] == "admin")
check("noto'g'ri parol rad etiladi", U.check(TMP, "admin", "x")[0] is None)
check("parol ochiq saqlanmaydi",
      "parol123" not in io.open(U.path_of(TMP), encoding="utf-8").read())

name, pwd = U.add(TMP, "sirojiddin", member="Сирожиддин")
check("yangi foydalanuvchi qo'shildi", name == "sirojiddin" and len(pwd) >= 10)
check("yangi parol ishlaydi", U.check(TMP, "sirojiddin", pwd)[0] == "sirojiddin")
check("takroriy login rad etiladi",
      _raises(lambda: U.add(TMP, "sirojiddin")))
check("yaroqsiz login rad etiladi", _raises(lambda: U.add(TMP, "Katta Harf")))

print("\n=== 11a. Ochiq redirekt yopilgan ===")
for bad in ("https://evil.example", "//evil.example", "http://evil.example/x"):
    r = c.post("/login", data={"login": "admin", "parol": "parol123",
                               "next": bad}, follow_redirects=False)
    check("tashqi manzilga yo'naltirmaydi: %s" % bad[:22],
          r.headers.get("location") == "/", r.headers.get("location"))
r = c.post("/login", data={"login": "admin", "parol": "parol123",
                           "next": "/sessions?days=7"}, follow_redirects=False)
check("ichki manzil saqlanadi", r.headers.get("location") == "/sessions?days=7")

print("\n=== 11b. Ruxsatlar ===")
c.cookies.clear()
c.post("/login", data={"login": "sirojiddin", "parol": pwd, "next": "/"})
r = c.get("/?days=0")
check("oddiy foydalanuvchi dashboardni ko'radi", r.status_code == 200)
check("hamma odam ko'rinadi (chegaralash yo'q)", "Алишер" in r.text)
check("shapkada kim ekani ko'rinadi", "sirojiddin" in r.text)
check("boshqaruv havolasi yo'q", "/users" not in r.text)
r = c.get("/users")
check("oddiy foydalanuvchi /users ga kira olmaydi", r.status_code == 403)
r = c.post("/users/add", data={"login": "yangi"}, follow_redirects=False)
check("oddiy foydalanuvchi odam qo'sha olmaydi", r.status_code == 403)

print("\n=== 11c. Parol almashsa eski cookie yaroqsiz ===")
eski = dict(c.cookies)
pwd2 = U.reset(TMP, "sirojiddin")
c.cookies.clear()
for k, v in eski.items():
    c.cookies.set(k, v)
r = c.get("/", follow_redirects=False)
check("parol almashgach eski sessiya yopiladi", r.status_code == 303, r.status_code)

print("\n=== 11d. Boshqaruv sahifasi ===")
c.cookies.clear()
c.post("/login", data={"login": "admin", "parol": "parol123", "next": "/"})
r = c.get("/users")
check("admin /users ni ko'radi", r.status_code == 200)
check("ro'yxatda sirojiddin bor", "sirojiddin" in r.text)
r = c.post("/users/add", data={"login": "jasur", "member": "Жасур"},
           follow_redirects=False)
loc = r.headers.get("location", "")
check("admin odam qo'sha oladi", "parol=" in loc, loc)
r = c.post("/users/delete", data={"login": "admin"}, follow_redirects=False)
check("o'zini o'chira olmaydi", "xato=" in r.headers.get("location", ""))
r = c.post("/users/delete", data={"login": "jasur"}, follow_redirects=False)
check("boshqasini o'chira oladi", r.status_code == 303
      and "xato=" not in r.headers.get("location", ""))
check("o'chirilgan odam yo'q", "jasur" not in U.load(TMP))

print("\n=== 12. Loyiha nomlarini birlashtirish ===")
# Bitta ish ikki kishida ikki xil nomlangan: papka nomidan keladi.
post(rep(session="m-1", member="Бобур", project="DemoProj",
         title="Шерик номи", work_cost_usd=4.0))
post(rep(session="m-2", member="Жасур", project="demo_project",
         title="Учинчи ном", work_cost_usd=1.0))

r = c.get("/projects")
check("admin /projects ni ko'radi", r.status_code == 200, r.status_code)
check("xom nomlar ro'yxatda", "DemoProj" in r.text and "demo_project" in r.text)
sug = db.suggest_merges(DBP)
sug_names = {x["raw"] for g in sug for x in g["rows"]}
check("o'xshash nomlar taklifga tushdi",
      {"DemoProj", "demoproject", "demo_project"} <= sug_names, sug_names)
check("boshqa loyiha taklifga tushmadi", "ShaxsiyLoyiha" not in sug_names)

before = {p["k"]: p for p in db.by(DBP, "project", days=0)}
check("birlashtirilmaguncha alohida turadi",
      "DemoProj" in before and "demoproject" in before, sorted(before))

r = c.post("/projects/merge",
           data={"raw": ["DemoProj", "demo_project", "demoproject"],
                 "canon_new": "demoproject"}, follow_redirects=False)
check("birlashtirish -> 303", r.status_code == 303, r.status_code)
after = {p["k"]: p for p in db.by(DBP, "project", days=0)}
check("uchta nom bitta bo'ldi",
      "DemoProj" not in after and "demo_project" not in after
      and "demoproject" in after, sorted(after))
check("summa qo'shildi",
      abs(after["demoproject"]["work_cost"]
          - (before["DemoProj"]["work_cost"] + before["demo_project"]["work_cost"]
             + before["demoproject"]["work_cost"])) < 0.01,
      after["demoproject"]["work_cost"])
check("nechta nom ekani ko'rinadi", after["demoproject"]["names"] == 3,
      after["demoproject"].get("names"))
check("filtr ro'yxatida xom nom qolmadi",
      "DemoProj" not in db.distinct(DBP, "project"))
r = c.get("/sessions?days=0&project=demoproject&tarix=1")
check("umumiy nom bo'yicha filtr hamma nomni topadi",
      "Шерик номи" in r.text and "Учинчи ном" in r.text)
check("sessiya qatorida xom nom ham ko'rinadi", "(DemoProj)" in r.text)

# Birlashtirilgandan KEYIN kelgan hisobot ham to'g'ri nom ostiga tushsin
post(rep(session="m-3", member="Бобур", project="DemoProj",
         title="Кейин келган", work_cost_usd=2.0))
rows = db.sessions(DBP, days=0, limit=200, since=None)
yangi = [x for x in rows if x["session"] == "m-3"]
check("yangi hisobot darhol umumiy nom ostida",
      bool(yangi) and yangi[0]["project_canon"] == "demoproject",
      yangi[0]["project_canon"] if yangi else None)

print("\n=== 12a. Begona loyiha ogohlantirishi ===")
body = c.get("/?days=0").text
check("ro'yxatda yo'q loyiha ogohlantiradi", "ShaxsiyLoyiha" in body)
r = c.post("/projects/register", data={"raw": ["ShaxsiyLoyiha"]},
           follow_redirects=False)
check("ro'yxatga olish -> 303", r.status_code == 303)
check("ro'yxatga olingan nom known ichida",
      "ShaxsiyLoyiha" in db.known_projects(DBP))
warns = srv.alerts.evaluate(DBP, srv.cfg, days=0)
begona = {w["project"] for w in warns if w["kind"] == "begona_loyiha"}
check("ro'yxatga olingandan keyin ogohlantirmaydi",
      "ShaxsiyLoyiha" not in begona, begona)
check("birlashtirilgan sherik loyihasi ham ogohlantirmaydi",
      "DemoProj" not in begona and "demoproject" not in begona, begona)

print("\n=== 12b. Ajratish orqaga qaytaradi ===")
r = c.post("/projects/split", data={"raw": ["DemoProj"]},
           follow_redirects=False)
check("ajratish -> 303", r.status_code == 303)
back = {p["k"]: p for p in db.by(DBP, "project", days=0)}
check("nom yana o'zicha turibdi", "DemoProj" in back, sorted(back))
check("demoproject ichida ikkita nom qoldi",
      back["demoproject"]["names"] == 2, back["demoproject"].get("names"))

print("\n=== 12c. Ruxsat ===")
c.cookies.clear()
c.post("/login", data={"login": "sirojiddin", "parol": pwd2, "next": "/"})
check("oddiy foydalanuvchi /projects ga kira olmaydi",
      c.get("/projects").status_code == 403)
check("oddiy foydalanuvchi birlashtira olmaydi",
      c.post("/projects/merge", data={"raw": ["DemoProj"],
                                      "canon_new": "x"}).status_code == 403)
check("shapkada havola ko'rinmaydi", "/projects" not in c.get("/?days=7").text)
c.cookies.clear()
c.post("/login", data={"login": "admin", "parol": "parol123", "next": "/"})

print("\n=== 10. Chiqish ===")
c.get("/logout")
c.cookies.clear()
r = c.get("/", follow_redirects=False)
check("chiqqandan keyin kirish talab qilinadi", r.status_code == 303)

shutil.rmtree(TMP, ignore_errors=True)
bad = OK.count(False)
print("\nNATIJA: %d/%d o'tdi%s" % (OK.count(True), len(OK),
                                  "" if not bad else "  —  %d TA XATO" % bad))
sys.exit(1 if bad else 0)

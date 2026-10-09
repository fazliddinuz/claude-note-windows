# -*- coding: utf-8 -*-
"""claude-note jamoa hisoboti - server.

  POST /ingest   Bearer token, mijozdan kelgan ixcham hisobot
  GET  /         dashboard (kishi / loyiha / kun kesimida + ogohlantirishlar)
  GET  /sessions sessiyalar ro'yxati, filtrlar bilan
  GET  /health   tekshirish uchun (autentifikatsiyasiz)

Ishga tushirish:
    DASHBOARD_PASSWORD=... INGEST_TOKENS=... uvicorn app:app --port 8000
"""

import datetime
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import alerts
import db
import users

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "cn_session"


def _env_list(name):
    return [x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()]


class Config:
    """Hamma sozlama muhit o'zgaruvchilaridan. Kodda hech qanday sir yo'q."""

    def __init__(self):
        self.data_dir = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")
        self.db_path = os.path.join(self.data_dir, "telemetry.db")
        self.ingest_tokens = _env_list("INGEST_TOKENS")
        self.password = os.environ.get("DASHBOARD_PASSWORD") or ""
        self.known_projects = _env_list("KNOWN_PROJECTS")
        self.night_start = int(os.environ.get("NIGHT_START") or 0)
        self.night_end = int(os.environ.get("NIGHT_END") or 6)
        self.alert_session_usd = float(os.environ.get("ALERT_SESSION_USD") or 15)
        self.alert_day_usd = float(os.environ.get("ALERT_DAY_USD") or 40)
        self.alert_context_tokens = int(
            os.environ.get("ALERT_CONTEXT_TOKENS") or 150000)
        self.max_body = int(os.environ.get("MAX_BODY_BYTES") or 65536)
        # Telemetriya ishga tushirilgan kun. Undan oldingi sessiyalar bir
        # necha hafta davomida yig'ilgan - ular kunlik ko'rsatkichlarni
        # buzadi, shuning uchun alohida "tarix" bo'lib turadi.
        self.history_before = (os.environ.get("HISTORY_BEFORE") or "").strip()
        self.secret = self._secret()

    def _secret(self):
        """Cookie imzosi uchun kalit.

        Berilmasa - bir marta yaratilib faylga saqlanadi: aks holda har
        restartda hamma dashboard sessiyasi uzilib qolardi.
        """
        env = os.environ.get("SECRET_KEY")
        if env:
            return env.encode("utf-8")
        os.makedirs(self.data_dir, exist_ok=True)
        path = os.path.join(self.data_dir, "secret.key")
        try:
            with open(path, "rb") as fh:
                val = fh.read().strip()
            if val:
                return val
        except Exception:
            pass
        val = secrets.token_hex(32).encode("ascii")
        with open(path, "wb") as fh:
            fh.write(val)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return val


cfg = Config()
db.init(cfg.db_path)
# Birinchi ishga tushirishda `admin` yaratiladi, paroli - DASHBOARD_PASSWORD.
# Shunda eski parol ishlayveradi va hech kim tizimdan tushib qolmaydi.
users.ensure_admin(cfg.data_dir, cfg.password)

app = FastAPI(title="claude-note jamoa hisoboti", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")),
          name="static")
tpl = Jinja2Templates(directory=os.path.join(HERE, "templates"))


# ---------------------------------------------------------------- yordamchi

def money(v):
    return "$%.2f" % float(v or 0)


def num(v):
    """1234567 -> '1 234 567' (uzun raqamlar o'qilishi uchun)."""
    return "{:,}".format(int(v or 0)).replace(",", " ")


def ctx(v):
    v = int(v or 0)
    return "%.0fK" % (v / 1000.0) if v >= 1000 else str(v)


def dur(minutes):
    m = int(minutes or 0)
    return "%dс %02dм" % (m // 60, m % 60) if m >= 60 else "%dм" % m


def when(row):
    """Sessiya vaqt oralig'i - MIJOZNING mahalliy vaqtida.

    Server boshqa mintaqada turishi mumkin, shuning uchun vaqt server
    vaqtiga o'girilmaydi: hisobot bilan birga kelgan tz_offset_min
    ishlatiladi. "Unda soat necha edi" degan savolga to'g'ri javob shu.

    Sessiya bir necha kunga cho'zilgan bo'lsa tugash sanasi ham chiqadi:
    aynan shunday sessiyalar eng qimmatga tushadi.
    """
    tz = datetime.timedelta(minutes=int(row.get("tz_offset_min") or 0))
    a = db._parse_iso(row.get("started_at"))
    b = db._parse_iso(row.get("ended_at"))
    if not a:
        return row.get("date") or "-"
    a += tz
    if b:
        b += tz
    if b and b > a:
        if a.date() == b.date():
            return "%s %s→%s" % (a.date().isoformat(), a.strftime("%H:%M"),
                                 b.strftime("%H:%M"))
        return "%s %s → %s %s" % (a.date().isoformat(), a.strftime("%H:%M"),
                                  b.strftime("%m-%d"), b.strftime("%H:%M"))
    return "%s %s" % (a.date().isoformat(), a.strftime("%H:%M"))


def multiday(row):
    """Sessiya bir kundan uzoq davom etganmi - jadvalda belgilash uchun."""
    return int(row.get("duration_min") or 0) >= 24 * 60


tpl.env.filters.update(money=money, num=num, ctx=ctx, dur=dur, when=when,
                       multiday=multiday)


# ---------------------------------------------------------------- kirish

def _sign(payload):
    return hmac.new(cfg.secret, payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def make_cookie(login, user, ttl_days=30):
    """Cookie ichida KIM ekani ham bor.

    Imzoga parol izi (fingerprint) qo'shiladi: parol almashsa yoki odam
    o'chirilsa, uning eski cookie'si darhol yaroqsiz bo'ladi.
    """
    exp = str(int(time.time()) + ttl_days * 86400)
    payload = "%s|%s|%s" % (login, exp, users.fingerprint(user))
    return "%s|%s.%s" % (login, exp, _sign(payload))


def cookie_user(value):
    """-> (login, user) yoki (None, None). Imzo/muddat/parol izi tekshiriladi."""
    if not value or "." not in value:
        return None, None
    body, sig = value.rsplit(".", 1)
    if "|" not in body:
        return None, None
    login, exp = body.rsplit("|", 1)
    try:
        if int(exp) <= time.time():
            return None, None
    except ValueError:
        return None, None
    user = users.load(cfg.data_dir).get(login)
    if not user:
        return None, None
    want = _sign("%s|%s|%s" % (login, exp, users.fingerprint(user)))
    if not hmac.compare_digest(sig, want):
        return None, None
    return login, user


def current_user(request):
    return cookie_user(request.cookies.get(COOKIE))


def logged_in(request):
    return current_user(request)[0] is not None


def need_login(request):
    """Kirilmagan bo'lsa -> /login ga yo'naltirish javobi, aks holda None."""
    if logged_in(request):
        return None
    nxt = request.url.path
    if request.url.query:
        nxt += "?" + request.url.query
    return RedirectResponse("/login?next=" + nxt, status_code=303)


def need_admin(request):
    """Faqat boshqaruvchi uchun. -> javob yoki None."""
    login, user = current_user(request)
    if not login:
        return need_login(request)
    if not user.get("admin"):
        return HTMLResponse("<h1>Рухсат йўқ</h1>"
                            "<p>Бу саҳифа фақат бошқарувчи учун.</p>"
                            "<p><a href=\"/\">Дашбордга қайтиш</a></p>",
                            status_code=403)
    return None


# ---------------------------------------------------------------- ingest

MAX_STR = {"member": 60, "host": 60, "os_user": 60, "project": 80,
           "session": 80, "date": 10, "event": 30, "started_at": 40,
           "ended_at": 40, "sent_at": 40, "title": 200, "summary": 1500,
           "agent": 20}
REQUIRED = ("member", "project", "session", "date")


def clean(payload):
    """Kelgan yozuvni oq ro'yxat bo'yicha tozalaydi.

    Bazaga faqat shu yerdan o'tgani yoziladi: mijoz nima yuborishidan qat'i
    nazar, kutilmagan maydon ham, cheksiz uzun satr ham saqlanib qolmaydi.
    """
    if not isinstance(payload, dict):
        raise ValueError("JSON obyekt kutilgan edi")

    row = {}
    for key, cap in MAX_STR.items():
        val = payload.get(key)
        row[key] = ("" if val is None else str(val))[:cap].strip()
    for key in REQUIRED:
        if not row[key]:
            raise ValueError("majburiy maydon bo'sh: %s" % key)
    if len(row["date"]) != 10 or row["date"][4] != "-" or row["date"][7] != "-":
        raise ValueError("date YYYY-MM-DD ko'rinishida bo'lishi kerak")

    for key in ("requests", "avg_context", "notes_added", "tz_offset_min", "v"):
        try:
            row[key] = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            row[key] = 0
    for key in ("work_cost_usd", "note_cost_usd"):
        try:
            row[key] = round(float(payload.get(key) or 0), 6)
        except (TypeError, ValueError):
            row[key] = 0.0

    models = payload.get("models")
    row["models"] = ([str(m)[:40] for m in models[:10]]
                     if isinstance(models, list) else [])
    agent = row.get("agent", "").lower()
    if agent not in ("claude", "codex"):
        agent = ("codex" if any(
            str(m).lower().startswith(("gpt-", "codex", "o1", "o3", "o4"))
            for m in row["models"]) else "claude")
    row["agent"] = agent

    tools = payload.get("tools")
    clean_tools = {}
    if isinstance(tools, dict):
        for k, v in list(tools.items())[:60]:
            try:
                clean_tools[str(k)[:60]] = int(v)
            except (TypeError, ValueError):
                continue
    row["tools"] = clean_tools

    usage = payload.get("usage")
    clean_usage = {}
    if isinstance(usage, dict):
        for model, d in list(usage.items())[:10]:
            if not isinstance(d, dict):
                continue
            keep = {}
            for k in ("requests", "in", "out", "cw5", "cw1h", "cr"):
                try:
                    keep[k] = int(d.get(k) or 0)
                except (TypeError, ValueError):
                    keep[k] = 0
            try:
                keep["cost_usd"] = round(float(d.get("cost_usd") or 0), 6)
            except (TypeError, ValueError):
                keep["cost_usd"] = 0.0
            clean_usage[str(model)[:60]] = keep
    row["usage"] = clean_usage

    tcost = payload.get("tool_cost")
    clean_tcost = {}
    if isinstance(tcost, dict):
        for k, v in list(tcost.items())[:80]:
            try:
                clean_tcost[str(k)[:60]] = round(float(v or 0), 6)
            except (TypeError, ValueError):
                continue
    row["tool_cost"] = clean_tcost

    lines = payload.get("lines")
    if isinstance(lines, list) and len(lines) >= 2:
        try:
            row["lines"] = [int(lines[0]), int(lines[1])]
        except (TypeError, ValueError):
            row["lines"] = [0, 0]
    else:
        row["lines"] = [0, 0]
    return row


def token_ok(header):
    if not cfg.ingest_tokens:
        return False
    if not header or not header.lower().startswith("bearer "):
        return False
    given = header[7:].strip()
    return any(hmac.compare_digest(given, t) for t in cfg.ingest_tokens)


@app.post("/ingest")
async def ingest(request: Request):
    if not token_ok(request.headers.get("authorization")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.body()
    if len(body) > cfg.max_body:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    # Ro'yxat ham qabul qilinadi: navbat to'planib qolganda bir zarbda yuborish
    items = payload if isinstance(payload, list) else [payload]
    if len(items) > 200:
        return JSONResponse({"error": "too many items"}, status_code=413)

    saved = dup = 0
    for item in items:
        try:
            row = clean(item)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            # "backfill" - mijoz daftaridagi yakuniy yozuv: shu sessiya
            # bo'yicha bazadagi eski qatorlar almashtiriladi.
            if db.insert(cfg.db_path, row,
                         replace=(row.get("event") == "backfill")):
                saved += 1
            else:
                dup += 1
        except Exception as exc:
            return JSONResponse({"error": "db: %s" % exc}, status_code=500)

    return {"ok": True, "saved": saved, "duplicate": dup}


@app.post("/ping")
async def ping(request: Request):
    """Token ishlayaptimi - o'rnatuvchi shuni so'raydi.

    /ingest ga soxta yozuv yuborib tekshirish mumkin edi, lekin unda
    bazaga axlat tushardi. Bu esa hech narsa yozmaydi.
    """
    if not token_ok(request.headers.get("authorization")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"ok": True, "server": "claude-note hisobot"}


@app.get("/health")
def health():
    st = db.stats(cfg.db_path)
    return {"ok": True, "reports": st["n"], "first": st["first"],
            "last": st["last"]}


# ---------------------------------------------------------------- kirish/chiqish

def safe_next(value):
    """Kirgandan keyin FAQAT shu saytning ichiga yo'naltiramiz.

    Aks holda ?next=https://boshqa-sayt orqali odamni begona joyga
    tashlab yuborish mumkin bo'lardi (ochiq redirekt).
    """
    value = (value or "/").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", xato: int = 0):
    nxt = safe_next(next)
    if logged_in(request):
        return RedirectResponse(nxt, status_code=303)
    return tpl.TemplateResponse(request, "login.html",
                                {"request": request, "next": nxt,
                                 "xato": bool(xato)})


@app.post("/login")
def login_post(request: Request, login: str = Form(""), parol: str = Form(""),
               next: str = Form("/")):
    nxt = safe_next(next)
    name, user = users.check(cfg.data_dir, login, parol)
    if not name:
        time.sleep(1)                     # oddiy brute-force sekinlatgichi
        return RedirectResponse("/login?xato=1&next=" + urllib.parse.quote(nxt),
                                status_code=303)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, make_cookie(name, user), httponly=True,
                    samesite="lax", secure=request.url.scheme == "https",
                    max_age=30 * 86400, path="/")
    return resp


# ---------------------------------------------------------------- foydalanuvchilar

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, yangi: str = "", parol: str = "",
               xato: str = ""):
    stop = need_admin(request)
    if stop:
        return stop
    login, me = current_user(request)
    rows = []
    for name, u in sorted(users.load(cfg.data_dir).items()):
        rows.append({"login": name, "member": u.get("member") or "",
                     "admin": bool(u.get("admin")),
                     "created": u.get("created") or ""})
    return tpl.TemplateResponse(request, "users.html", {
        "request": request, "rows": rows, "men": login,
        "men_admin": bool((me or {}).get("admin")),
        "yangi": yangi, "parol": parol, "xato": xato,
        "all_members": db.distinct(cfg.db_path, "member"),
    })


@app.post("/users/add")
def users_add(request: Request, login: str = Form(""), member: str = Form(""),
              admin: str = Form("")):
    stop = need_admin(request)
    if stop:
        return stop
    try:
        name, password = users.add(cfg.data_dir, login, member,
                                   admin=bool(admin))
    except ValueError as exc:
        return RedirectResponse("/users?xato=" + str(exc), status_code=303)
    return RedirectResponse("/users?yangi=%s&parol=%s" % (name, password),
                            status_code=303)


@app.post("/users/reset")
def users_reset(request: Request, login: str = Form("")):
    stop = need_admin(request)
    if stop:
        return stop
    try:
        password = users.reset(cfg.data_dir, login)
    except ValueError as exc:
        return RedirectResponse("/users?xato=" + str(exc), status_code=303)
    return RedirectResponse("/users?yangi=%s&parol=%s" % (login, password),
                            status_code=303)


@app.post("/users/delete")
def users_delete(request: Request, login: str = Form("")):
    stop = need_admin(request)
    if stop:
        return stop
    men, _u = current_user(request)
    if login == men:
        return RedirectResponse("/users?xato=" + "Ўзингизни ўчира олмайсиз",
                                status_code=303)
    try:
        users.delete(cfg.data_dir, login)
    except ValueError as exc:
        return RedirectResponse("/users?xato=" + str(exc), status_code=303)
    return RedirectResponse("/users", status_code=303)


# ---------------------------------------------------------------- loyihalar

def _project_groups():
    """Umumiy nom -> uning ostidagi xom nomlar va yig'indi.

    Har kimning kompyuterida loyiha papkasi boshqacha nomlanadi, shuning
    uchun bitta ish bir nechta nom bo'lib keladi. Shu sahifada ular
    QO'LDA birlashtiriladi: avtomat birlashtirish ikki boshqa loyihani
    jimgina qo'shib yuborish xavfini tug'diradi.
    """
    known = set(cfg.known_projects or []) | db.known_projects(cfg.db_path)
    groups = {}
    for row in db.project_catalog(cfg.db_path):
        g = groups.setdefault(row["canon"], {
            "canon": row["canon"], "rows": [], "sessions": 0,
            "work_cost": 0.0, "members": set()})
        g["rows"].append(row)
        g["sessions"] += row["sessions"]
        g["work_cost"] += row["work_cost"]
        g["members"].update(row["members"])
    out = []
    for g in groups.values():
        g["members"] = sorted(g["members"])
        g["names"] = len(g["rows"])
        g["known"] = g["canon"] in known
        out.append(g)
    out.sort(key=lambda g: -g["work_cost"])
    return out


@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, xabar: str = "", xato: str = ""):
    stop = need_admin(request)
    if stop:
        return stop
    men, me = current_user(request)
    return tpl.TemplateResponse(request, "projects.html", {
        "request": request, "men": men,
        "men_admin": bool((me or {}).get("admin")),
        "groups": _project_groups(),
        "suggestions": db.suggest_merges(
            cfg.db_path,
            known=set(cfg.known_projects or []) | db.known_projects(cfg.db_path)),
        "env_known": cfg.known_projects,
        "xabar": xabar, "xato": xato,
        "stats": db.stats(cfg.db_path),
    })


@app.post("/projects/merge")
def projects_merge(request: Request, raw: list[str] = Form([]),
                   canon: str = Form(""), canon_new: str = Form("")):
    """Tanlangan nomlarni bitta nom ostiga qo'yadi.

    canon_new bo'sh bo'lmasa - o'sha, aks holda ro'yxatdan tanlangani.
    Bitta nomni o'zining nomi bilan yuborish = "ro'yxatga olish".
    """
    stop = need_admin(request)
    if stop:
        return stop
    name = (canon_new or canon or "").strip()
    try:
        name = db.merge_projects(cfg.db_path, raw, name)
    except ValueError as exc:
        return RedirectResponse("/projects?xato=" + urllib.parse.quote(str(exc)),
                                status_code=303)
    msg = "«%s» — %d та ном шу ном остида бирлаштирилди" % (name, len(raw))
    return RedirectResponse("/projects?xabar=" + urllib.parse.quote(msg),
                            status_code=303)


@app.post("/projects/register")
def projects_register(request: Request, raw: list[str] = Form([])):
    """Nomini o'zgartirmay ro'yxatga oladi - "bu bizning loyiha"."""
    stop = need_admin(request)
    if stop:
        return stop
    try:
        n = db.register_projects(cfg.db_path, raw)
    except ValueError as exc:
        return RedirectResponse("/projects?xato=" + urllib.parse.quote(str(exc)),
                                status_code=303)
    msg = "%d та лойиҳа рўйхатга олинди" % n
    return RedirectResponse("/projects?xabar=" + urllib.parse.quote(msg),
                            status_code=303)


@app.post("/projects/split")
def projects_split(request: Request, raw: list[str] = Form([])):
    """Birlashtirishni bekor qiladi - har nom yana o'zicha turadi."""
    stop = need_admin(request)
    if stop:
        return stop
    db.split_projects(cfg.db_path, raw)
    msg = "%d та ном ажратилди" % len(raw)
    return RedirectResponse("/projects?xabar=" + urllib.parse.quote(msg),
                            status_code=303)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- dashboard

PERIODS = [(1, "Бугун"), (7, "7 кун"), (30, "30 кун"), (90, "90 кун"),
           (0, "Ҳаммаси")]


AGENTS = [("", "Барча агентлар"), ("codex", "Codex"),
          ("claude", "Claude Code")]


def _args(request):
    q = request.query_params
    try:
        days = int(q.get("days", 7))
    except ValueError:
        days = 7
    if days not in [p[0] for p in PERIODS]:
        days = 7
    agent = (q.get("agent") or "").strip().lower()
    if agent not in ("codex", "claude"):
        agent = ""
    return (days, (q.get("member") or "").strip(),
            (q.get("project") or "").strip(), agent)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    redir = need_login(request)
    if redir:
        return redir
    days, member, project, agent = _args(request)

    since = cfg.history_before
    daily = db.by(cfg.db_path, "day", days, member, project, limit=120,
                  since=since, agent=agent)
    peak = max([d["work_cost"] for d in daily] or [0]) or 1
    for d in daily:
        d["bar"] = int(100 * d["work_cost"] / peak)

    models, covered = db.model_totals(cfg.db_path, days, member, project, since,
                                      agent=agent)
    mt_total = db.totals(cfg.db_path, days, member, project,
                         since, agent=agent).get("sessions") or 0
    tarix = db.totals(cfg.db_path, 0, member, project, since,
                      history=True, agent=agent) if since else {}

    warns = alerts.evaluate(cfg.db_path, cfg, days=days, agent=agent)
    if member:
        warns = [w for w in warns if w["member"] == member]
    if project:
        warns = [w for w in warns if w["project"] in (project, "")]

    men, men_user = current_user(request)
    return tpl.TemplateResponse(request, "dashboard.html", {
        "request": request, "men": men,
        "men_admin": bool((men_user or {}).get("admin")),
        "days": days, "member": member, "project": project, "agent": agent,
        "periods": PERIODS, "agents": AGENTS,
        "totals": db.totals(cfg.db_path, days, member, project, since,
                             agent=agent),
        "members": db.by(cfg.db_path, "member", days, member, project,
                         since=since, agent=agent),
        "projects": db.by(cfg.db_path, "project", days, member, project,
                          since=since, agent=agent),
        "daily": daily,
        "tarix": tarix, "since": since,
        "tools": db.tool_totals(cfg.db_path, days, member, project,
                                since, agent=agent)[:18],
        "models": models, "model_covered": covered, "model_total": mt_total,
        "warns": warns[:60],
        "warn_summary": alerts.summary(warns),
        "advice": alerts.ADVICE,
        "labels": alerts.LABEL,
        "all_members": db.distinct(cfg.db_path, "member"),
        "all_projects": db.distinct(cfg.db_path, "project"),
        "stats": db.stats(cfg.db_path),
    })


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page(request: Request):
    redir = need_login(request)
    if redir:
        return redir
    days, member, project, agent = _args(request)
    q = (request.query_params.get("q") or "").strip()
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    per = 50
    tarix_ham = request.query_params.get("tarix") == "1"
    since = None if tarix_ham else cfg.history_before
    total = db.count_sessions(cfg.db_path, days, member, project, q, since,
                              agent=agent)
    rows = db.sessions(cfg.db_path, days, member, project, q,
                       limit=per, offset=(page - 1) * per, since=since,
                       agent=agent)
    men, men_user = current_user(request)
    return tpl.TemplateResponse(request, "sessions.html", {
        "request": request, "men": men,
        "men_admin": bool((men_user or {}).get("admin")),
        "days": days, "member": member, "project": project, "agent": agent,
        "q": q, "periods": PERIODS, "agents": AGENTS,
        "rows": rows, "page": page, "per": per,
        "tarix_ham": tarix_ham, "since": cfg.history_before,
        "total": total, "pages": max(1, (total + per - 1) // per),
        "all_members": db.distinct(cfg.db_path, "member"),
        "all_projects": db.distinct(cfg.db_path, "project"),
    })


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"

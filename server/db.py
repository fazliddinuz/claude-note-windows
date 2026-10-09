# -*- coding: utf-8 -*-
"""SQLite qatlami: hisobotlarni saqlash va kesimlar bo'yicha o'qish.

Baza bitta fayl (data/telemetry.db). WAL rejimi yoqilgan: dashboard o'qiyotgan
paytda ingest yozishi bloklanmaydi.
"""

import datetime
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at   TEXT NOT NULL,
  v             INTEGER DEFAULT 1,
  member        TEXT NOT NULL,
  host          TEXT,
  os_user       TEXT,
  agent         TEXT DEFAULT 'claude',
  project       TEXT NOT NULL,
  session       TEXT NOT NULL,
  date          TEXT NOT NULL,
  event         TEXT,
  started_at    TEXT,
  ended_at      TEXT,
  sent_at       TEXT,
  tz_offset_min INTEGER DEFAULT 0,
  local_hour    INTEGER,
  day           TEXT,
  duration_min  INTEGER DEFAULT 0,
  title         TEXT,
  summary       TEXT,
  requests      INTEGER DEFAULT 0,
  work_cost_usd REAL    DEFAULT 0,
  note_cost_usd REAL    DEFAULT 0,
  models        TEXT,
  avg_context   INTEGER DEFAULT 0,
  tools         TEXT,
  notes_added   INTEGER DEFAULT 0,
  line_start    INTEGER DEFAULT 0,
  line_end      INTEGER DEFAULT 0,
  usage         TEXT,
  tool_cost     TEXT,
  raw           TEXT
);

-- Bitta hisobot ikki marta kelishi mumkin: mijoz javobni kutmay uzilsa,
-- navbatdagi fayl o'chmaydi va keyingi safar qayta yuboriladi. Shu indeks
-- tufayli takror yozuv jimgina tashlanadi.
CREATE UNIQUE INDEX IF NOT EXISTS ix_reports_uniq
  ON reports(session, line_start, line_end);
CREATE INDEX IF NOT EXISTS ix_reports_date    ON reports(date);
CREATE INDEX IF NOT EXISTS ix_reports_member  ON reports(member, date);
CREATE INDEX IF NOT EXISTS ix_reports_project ON reports(project, date);

-- Bitta loyiha har kimning kompyuterida boshqacha nomlanadi (papka nomi
-- bo'yicha keladi): "demoproj", "demoproject", "Demo-Project". Shu jadval
-- xom nomni umumiy nom bilan bog'laydi. Yozuv YO'Q bo'lsa nom o'zicha,
-- alohida loyiha bo'lib turaveradi - hech narsa avtomat birlashmaydi.
CREATE TABLE IF NOT EXISTS project_alias (
  raw     TEXT PRIMARY KEY,
  canon   TEXT NOT NULL,
  updated TEXT
);
CREATE INDEX IF NOT EXISTS ix_alias_canon ON project_alias(canon);
"""


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


# Keyin qo'shilgan ustunlar. Baza allaqachon yaratilgan bo'lsa
# CREATE TABLE ularni qo'shmaydi - shuning uchun alohida tekshiramiz.
LATER_COLUMNS = [("usage", "TEXT"), ("tool_cost", "TEXT"), ("day", "TEXT"),
                 ("agent", "TEXT"), ("project_key", "TEXT")]

# Keyin qo'shilgan ustunlar ustidagi indekslar. Ular SCHEMA ichida tura
# OLMAYDI: jadval allaqachon mavjud bo'lsa, ustun ALTER bilan qo'shilgunga
# qadar indeks yaratib bo'lmaydi ("no such column: day").
LATER_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_reports_day ON reports(day)",
    "CREATE INDEX IF NOT EXISTS ix_reports_pkey ON reports(project_key)",
]


# Hisobotdagi tayanch nom. project_key - xom nomning birlashtirilgan
# ko'rinishi; u HAR DOIM project_alias jadvalidan kelib chiqadi, shuning
# uchun har o'zgarishdan keyin shu bitta so'rov bilan qayta hisoblanadi.
FILL_KEYS = ("UPDATE reports SET project_key = COALESCE("
             "(SELECT canon FROM project_alias a WHERE a.raw = reports.project), "
             "project)")


def init(path):
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(reports)")}
        for col, typ in LATER_COLUMNS:
            if col not in have:
                conn.execute("ALTER TABLE reports ADD COLUMN %s %s" % (col, typ))
        # Eski hisobotlarda agent maydoni yo'q. Model nomidan xavfsiz taxmin.
        conn.execute("""UPDATE reports SET agent = CASE
            WHEN lower(COALESCE(models, '')) LIKE '%gpt-%'
              OR lower(COALESCE(models, '')) LIKE '%codex%'
              OR lower(COALESCE(models, '')) LIKE '[\"o%' THEN 'codex'
            ELSE 'claude' END
            WHERE agent IS NULL OR agent = ''""")
        # day keyin qo'shildi - eski qatorlarga hisoblab qo'yamiz
        todo = conn.execute("SELECT id, started_at, tz_offset_min, date "
                            "FROM reports WHERE day IS NULL OR day = ''"
                            ).fetchall()
        for r in todo:
            conn.execute("UPDATE reports SET day = ? WHERE id = ?",
                         (day_of(dict(r)), r["id"]))
        for sql in LATER_INDEXES:
            conn.execute(sql)
        # project_key keyin qo'shildi. Eski qatorlarga hozirgi birlashtirish
        # jadvali bo'yicha to'ldiramiz; jadval bo'sh bo'lsa - nomning o'zi.
        conn.execute(FILL_KEYS + " WHERE project_key IS NULL OR project_key = ''")


# ------------------------------------------------------------------ yozish

def _parse_iso(s):
    """'2026-09-07T08:00:00.000Z' -> datetime (UTC, naive). Xato bo'lsa None."""
    if not isinstance(s, str) or not s:
        return None
    t = s.strip().replace("Z", "").replace("z", "")
    if "+" in t[10:]:
        t = t[:10] + t[10:].split("+")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def day_of(row):
    """Sessiya QAYSI KUN boshlangani (mijozning mahalliy vaqtida).

    Kunlik hisob shu bo'yicha yuritiladi - hisobot kelgan kun bo'yicha emas.
    Sessiya qachon qayta ishlanishidan qat'i nazar, ish qilingan kun
    hisoblanadi. Aks holda bir oy davom etgan sessiya butunlay bitta kunga
    tushib, o'sha kun "1669 soat ishlangan" bo'lib ko'rinadi.
    """
    started = _parse_iso(row.get("started_at"))
    if started:
        tz = int(row.get("tz_offset_min") or 0)
        return (started + datetime.timedelta(minutes=tz)).date().isoformat()
    return row.get("date") or ""


def derive(row):
    """Hisobotdan hosila maydonlar: mahalliy soat va davomiylik.

    local_hour tungi ish ogohlantirishi uchun kerak. Mijoz vaqtni UTC da
    yuboradi, mahalliy farqni esa alohida (tz_offset_min) - shu ikkisidan
    "u yerda soat necha edi" degan savolga javob chiqadi.
    """
    started = _parse_iso(row.get("started_at"))
    ended = _parse_iso(row.get("ended_at"))
    tz = int(row.get("tz_offset_min") or 0)
    local_hour = None
    if started:
        local_hour = (started + datetime.timedelta(minutes=tz)).hour
    duration = 0
    if started and ended and ended >= started:
        duration = int((ended - started).total_seconds() // 60)
    return local_hour, duration


# ------------------------------------------------- loyiha nomlarini birlashtirish

def norm_project(name):
    """Taqqoslash uchun soddalashtirilgan nom: faqat harf va raqam.

    "Demo-Project", "demo project", "demoproject" -> "demoproject". Bu FAQAT
    taklif berish uchun ishlatiladi: birlashtirish har doim odam tasdig'i
    bilan bo'ladi, aks holda bir-biriga o'xshash ikki boshqa loyiha
    jimgina qo'shilib ketardi.
    """
    return "".join(c for c in (name or "").lower() if c.isalnum())


def alias_map(path):
    """{xom nom: umumiy nom}. Faqat birlashtirilganlari."""
    with connect(path) as conn:
        return {r["raw"]: r["canon"]
                for r in conn.execute("SELECT raw, canon FROM project_alias")}


def canon_of(path, raw):
    """Xom nomning hisobotdagi nomi. Ro'yxatda bo'lmasa - o'zi."""
    raw = raw or ""
    with connect(path) as conn:
        r = conn.execute("SELECT canon FROM project_alias WHERE raw = ?",
                         (raw,)).fetchone()
    return r["canon"] if r else raw


def known_projects(path):
    """Ro'yxatga olingan (bizniki deb belgilangan) umumiy nomlar."""
    with connect(path) as conn:
        return {r["canon"] for r in
                conn.execute("SELECT DISTINCT canon FROM project_alias")}


def merge_projects(path, raws, canon):
    """Xom nomlarni bitta umumiy nom ostiga qo'yadi. -> umumiy nom.

    Bitta nomni o'zining yangi nomi bilan yozish ham shu yerda: bu
    "qayta nomlash" bo'lib chiqadi. Hech qanday yozuv o'chmaydi - xom
    nom qatorda qoladi, faqat hisob boshqa nom ostida yuritiladi.
    """
    canon = (canon or "").strip()[:80]
    if not canon:
        raise ValueError("Умумий ном бўш бўлмасин")
    raws = [str(r).strip()[:80] for r in (raws or []) if str(r).strip()]
    if not raws:
        raise ValueError("Бирорта ҳам лойиҳа танланмаган")
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    with connect(path) as conn:
        for raw in raws:
            conn.execute(
                "INSERT INTO project_alias (raw, canon, updated) VALUES (?,?,?) "
                "ON CONFLICT(raw) DO UPDATE SET canon = excluded.canon, "
                "updated = excluded.updated", (raw, canon, now))
        conn.execute(FILL_KEYS)
    return canon


def register_projects(path, raws):
    """Nomni o'zgartirmay ro'yxatga oladi: "bu bizning loyiha".

    Birlashtirish shart emas - loyiha faqat bir kishida bo'lishi mumkin.
    Ro'yxatga olingandan keyin u "бегона лойиҳа" bo'lib ogohlantirmaydi.
    """
    raws = [str(r).strip()[:80] for r in (raws or []) if str(r).strip()]
    if not raws:
        raise ValueError("Бирорта ҳам лойиҳа танланмаган")
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    with connect(path) as conn:
        for raw in raws:
            conn.execute(
                "INSERT INTO project_alias (raw, canon, updated) VALUES (?,?,?) "
                "ON CONFLICT(raw) DO UPDATE SET canon = excluded.canon, "
                "updated = excluded.updated", (raw, raw, now))
        conn.execute(FILL_KEYS)
    return len(raws)


def split_projects(path, raws):
    """Birlashtirishni bekor qiladi: har nom yana o'zicha turadi."""
    raws = [str(r).strip()[:80] for r in (raws or []) if str(r).strip()]
    if not raws:
        return 0
    with connect(path) as conn:
        cur = conn.executemany("DELETE FROM project_alias WHERE raw = ?",
                               [(r,) for r in raws])
        n = cur.rowcount
        conn.execute(FILL_KEYS)
    return n


def project_catalog(path):
    """Har bir XOM nom bo'yicha: kim ishlatgan, nechta sessiya, qancha pul.

    Birlashtirish sahifasi shu ro'yxatdan quriladi. Yozuvi qolmagan, lekin
    ro'yxatda turgan nomlar ham chiqadi - aks holda ularni olib tashlab
    bo'lmasdi.
    """
    sql = ("SELECT project AS raw, %s AS canon, COUNT(*) AS sessions, "
           "COALESCE(SUM(work_cost_usd),0) AS work_cost, "
           "MIN(%s) AS first_day, MAX(%s) AS last_day, "
           "GROUP_CONCAT(DISTINCT member) AS members "
           "FROM reports WHERE project <> '' GROUP BY project"
           % (PROJ_COL, DAY_COL, DAY_COL))
    rows = {}
    for r in _query(path, sql, []):
        r["members"] = [m for m in (r["members"] or "").split(",") if m]
        r["merged"] = r["canon"] != r["raw"]
        rows[r["raw"]] = r
    for raw, canon in alias_map(path).items():
        if raw not in rows:
            rows[raw] = {"raw": raw, "canon": canon, "sessions": 0,
                         "work_cost": 0.0, "first_day": "", "last_day": "",
                         "members": [], "merged": canon != raw}
    return sorted(rows.values(), key=lambda r: (-r["work_cost"], r["raw"]))


def suggest_merges(path, known=None):
    """O'xshash nomlar - "shu ikkisi bitta loyihami?" degan taklif.

    Faqat taklif: nomlar normallashtirilganda ustma-ust tushsa yoki biri
    ikkinchisining ichida bo'lsa (kamida 5 belgi - "erp" kabi qisqa so'z
    tasodifan mos kelmasligi uchun). Allaqachon bitta nom ostidagilar
    taklifga chiqmaydi.

    known - ro'yxatga olingan nomlar: taklif etiladigan umumiy nom sifatida
    avval o'sha tanlanadi, aks holda eng ko'p ishlangani.
    """
    known = set(known or [])
    cat = project_catalog(path)
    raws = [r["raw"] for r in cat]
    norms = {r["raw"]: norm_project(r["raw"]) for r in cat}
    parent = {raw: raw for raw in raws}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(raws):
        for b in raws[i + 1:]:
            na, nb = norms[a], norms[b]
            if not na or not nb:
                continue
            short, long_ = sorted((na, nb), key=len)
            if na == nb or (len(short) >= 5 and short in long_):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

    by_key = {}
    for r in cat:
        by_key.setdefault(find(r["raw"]), []).append(r)
    out = []
    for group in by_key.values():
        if len(group) < 2:
            continue
        if len({r["canon"] for r in group}) == 1:
            continue                      # allaqachon birlashtirilgan
        group.sort(key=lambda r: -r["work_cost"])
        best = max(group, key=lambda r: (r["raw"] in known, r["sessions"],
                                         r["work_cost"], r["raw"]))
        out.append({"rows": group, "suggest": best["raw"]})
    out.sort(key=lambda g: -sum(r["work_cost"] for r in g["rows"]))
    return out


def insert(path, row, replace=False):
    """-> True yangi yozuv, False takror. Xato bo'lsa istisno ko'tariladi.

    replace - backfill uchun: mijoz daftaridagi YAKUNIY yozuv keladi va u
    shu sessiya bo'yicha bazadagi hamma narsani almashtiradi. Aks holda
    jonli yuborilgan bo'laklar bilan backfill qo'shilib, bitta sessiya ikki
    marta sanalardi.
    """
    local_hour, duration = derive(row)
    lines = row.get("lines") or [0, 0]
    canon = canon_of(path, row["project"])
    rec = (
        datetime.datetime.utcnow().isoformat(timespec="seconds"),
        int(row.get("v") or 1),
        row["member"], row.get("host") or "", row.get("os_user") or "",
        row.get("agent") or "claude",
        row["project"], row["session"], row["date"], row.get("event") or "",
        row.get("started_at") or "", row.get("ended_at") or "",
        row.get("sent_at") or "", int(row.get("tz_offset_min") or 0),
        local_hour, day_of(row), duration,
        row.get("title") or "", row.get("summary") or "",
        int(row.get("requests") or 0),
        float(row.get("work_cost_usd") or 0), float(row.get("note_cost_usd") or 0),
        json.dumps(row.get("models") or [], ensure_ascii=False),
        int(row.get("avg_context") or 0),
        json.dumps(row.get("tools") or {}, ensure_ascii=False),
        int(row.get("notes_added") or 0),
        int(lines[0] if len(lines) > 0 else 0),
        int(lines[1] if len(lines) > 1 else 0),
        json.dumps(row.get("usage") or {}, ensure_ascii=False),
        json.dumps(row.get("tool_cost") or {}, ensure_ascii=False),
        json.dumps(row, ensure_ascii=False),
        canon,
    )
    sql = """INSERT OR IGNORE INTO reports
        (received_at, v, member, host, os_user, agent, project, session, date, event,
         started_at, ended_at, sent_at, tz_offset_min, local_hour, day,
         duration_min,
         title, summary, requests, work_cost_usd, note_cost_usd, models,
         avg_context, tools, notes_added, line_start, line_end,
         usage, tool_cost, raw, project_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    with connect(path) as conn:
        if replace:
            conn.execute("DELETE FROM reports WHERE session = ?", (row["session"],))
        cur = conn.execute(sql, rec)
        return cur.rowcount > 0


# ------------------------------------------------------------------ o'qish

DAY_COL = "COALESCE(NULLIF(day, ''), date)"
# Hisob-kitob HAR DOIM birlashtirilgan nom bo'yicha. Birlashtirilmagan
# loyiha uchun bu o'zining xom nomi bo'ladi.
PROJ_COL = "COALESCE(NULLIF(project_key, ''), project)"


def _window(days, since=None, history=False):
    """Davr chegarasi. Hisob DAY_COL - sessiya boshlangan kun bo'yicha.

    since - telemetriya ishga tushirilgan kun. Undan oldingi sessiyalar
    "tarix": ular bir necha hafta davomida yig'ilgan va kunlik
    ko'rsatkichlarni buzadi, shuning uchun asosiy kesimlarga qo'shilmaydi.
    history=True - aksincha, FAQAT o'sha eski qismni beradi.
    """
    where, args = [], []
    if days:
        first = (datetime.date.today()
                 - datetime.timedelta(days=int(days) - 1)).isoformat()
        where.append("%s >= ?" % DAY_COL)
        args.append(first)
    if since:
        where.append("%s %s ?" % (DAY_COL, "<" if history else ">="))
        args.append(since)
    elif history:
        where.append("1=0")          # tarix chegarasi belgilanmagan
    return (" AND ".join(where) or "1=1"), args


def _filters(member=None, project=None, agent=None):
    where, args = [], []
    if member:
        where.append("member = ?")
        args.append(member)
    if project:
        # Filtr umumiy nom bo'yicha: birlashtirilgan hamma xom nom tushadi.
        where.append("%s = ?" % PROJ_COL)
        args.append(project)
    if agent:
        where.append("agent = ?")
        args.append(agent)
    return where, args


def _query(path, sql, args):
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


AGG = ("COUNT(*) AS sessions, "
       "COALESCE(SUM(work_cost_usd),0) AS work_cost, "
       "COALESCE(SUM(note_cost_usd),0) AS note_cost, "
       "COALESCE(SUM(requests),0) AS requests, "
       "COALESCE(SUM(notes_added),0) AS notes_added, "
       "COALESCE(SUM(duration_min),0) AS minutes, "
       "COALESCE(MAX(avg_context),0) AS max_context")


def totals(path, days=7, member=None, project=None, since=None, history=False,
           agent=None):
    wnd, args = _window(days if not history else 0, since, history)
    extra, eargs = _filters(member, project, agent)
    sql = ("SELECT %s, COUNT(DISTINCT member) AS members, "
           "COUNT(DISTINCT %s) AS projects FROM reports WHERE %s"
           % (AGG, PROJ_COL, " AND ".join([wnd] + extra)))
    rows = _query(path, sql, args + eargs)
    return rows[0] if rows else {}


def by(path, field, days=7, member=None, project=None, limit=50, since=None,
       agent=None):
    """member / project / kun kesimida yig'indi."""
    assert field in ("member", "project", "day", "host")
    wnd, args = _window(days, since)
    extra, eargs = _filters(member, project, agent)
    col = {"day": DAY_COL, "project": PROJ_COL}.get(field, field)
    order = "k ASC" if field == "day" else "work_cost DESC"
    # Loyiha kesimida yana bitta ustun: shu nom ostida nechta xom nom bor
    # (ya'ni jamoada bu loyiha nechta turlicha nomlangan).
    agg = AGG + (", COUNT(DISTINCT project) AS names" if field == "project" else "")
    sql = ("SELECT %s AS k, %s FROM reports WHERE %s GROUP BY k ORDER BY %s LIMIT %d"
           % (col, agg, " AND ".join([wnd] + extra), order, int(limit)))
    return _query(path, sql, args + eargs)


USAGE_KEYS = ("requests", "in", "out", "cw5", "cw1h", "cr")


def usage_rows(usage):
    """{model: {tokenlar}} -> jadval qatorlari, qimmatidan boshlab."""
    out = []
    for model, d in (usage or {}).items():
        d = d or {}
        row = {k: int(d.get(k) or 0) for k in USAGE_KEYS}
        row["model"] = model
        row["cache_write"] = row["cw5"] + row["cw1h"]
        row["cost_usd"] = float(d.get("cost_usd") or 0)
        out.append(row)
    out.sort(key=lambda r: -(r["cost_usd"] or r["cr"]))
    return out


def tool_totals(path, days=7, member=None, project=None, since=None, agent=None):
    """Vositalar bo'yicha: nechta chaqiruv va taxminan qancha pul."""
    wnd, args = _window(days, since)
    extra, eargs = _filters(member, project, agent)
    sql = ("SELECT tools, tool_cost FROM reports WHERE %s"
           % " AND ".join([wnd] + extra))
    cnt, usd = {}, {}
    for r in _query(path, sql, args + eargs):
        try:
            for k, n in (json.loads(r["tools"] or "{}") or {}).items():
                cnt[k] = cnt.get(k, 0) + int(n)
        except Exception:
            pass
        try:
            for k, v in (json.loads(r.get("tool_cost") or "{}") or {}).items():
                usd[k] = round(usd.get(k, 0.0) + float(v or 0), 6)
        except Exception:
            pass
    keys = set(cnt) | set(usd)
    rows = [{"name": k, "count": cnt.get(k, 0), "cost": usd.get(k, 0.0)}
            for k in keys]
    rows.sort(key=lambda r: (-r["cost"], -r["count"]))
    return rows


def model_totals(path, days=7, member=None, project=None, since=None, agent=None):
    """Model kesimidagi token yig'indisi - "narx qayerdan chiqdi"."""
    wnd, args = _window(days, since)
    extra, eargs = _filters(member, project, agent)
    sql = "SELECT usage FROM reports WHERE %s" % " AND ".join([wnd] + extra)
    agg = {}
    covered = 0
    for r in _query(path, sql, args + eargs):
        try:
            u = json.loads(r["usage"] or "{}") or {}
        except Exception:
            continue
        if u:
            covered += 1
        for model, d in u.items():
            cur = agg.setdefault(model, {})
            for k in USAGE_KEYS:
                cur[k] = int(cur.get(k) or 0) + int((d or {}).get(k) or 0)
            cur["cost_usd"] = round(float(cur.get("cost_usd") or 0)
                                    + float((d or {}).get("cost_usd") or 0), 6)
    return usage_rows(agg), covered


def sessions(path, days=7, member=None, project=None, q=None,
             limit=100, offset=0, since=None, agent=None):
    wnd, args = _window(days, since)
    extra, eargs = _filters(member, project, agent)
    if q:
        extra.append("(title LIKE ? OR summary LIKE ? OR session LIKE ?)")
        eargs += ["%%%s%%" % q] * 3
    sql = ("SELECT * FROM reports WHERE %s "
           "ORDER BY started_at DESC, date DESC LIMIT ? OFFSET ?"
           % " AND ".join([wnd] + extra))
    rows = _query(path, sql, args + eargs + [int(limit), int(offset)])
    for r in rows:
        # Hisobotda ko'rinadigan nom - birlashtirilgani; xom nom qatorda
        # qoladi (kimning kompyuterida qanday nomlangani ko'rinsin).
        r["project_canon"] = r.get("project_key") or r["project"]
        try:
            r["models_list"] = json.loads(r["models"] or "[]")
        except Exception:
            r["models_list"] = []
        try:
            r["tools_dict"] = json.loads(r["tools"] or "{}")
        except Exception:
            r["tools_dict"] = {}
        for src, dst in (("usage", "usage_dict"), ("tool_cost", "tool_cost_dict")):
            try:
                r[dst] = json.loads(r.get(src) or "{}")
            except Exception:
                r[dst] = {}
        r["usage_rows"] = usage_rows(r["usage_dict"])
        r["tool_cost_rows"] = sorted(r["tool_cost_dict"].items(),
                                     key=lambda kv: -float(kv[1] or 0))[:20]
    return rows


def count_sessions(path, days=7, member=None, project=None, q=None, since=None,
                   agent=None):
    wnd, args = _window(days, since)
    extra, eargs = _filters(member, project, agent)
    if q:
        extra.append("(title LIKE ? OR summary LIKE ? OR session LIKE ?)")
        eargs += ["%%%s%%" % q] * 3
    sql = "SELECT COUNT(*) AS n FROM reports WHERE %s" % " AND ".join([wnd] + extra)
    return _query(path, sql, args + eargs)[0]["n"]


def distinct(path, field, days=0):
    assert field in ("member", "project")
    wnd, args = _window(days)
    col = PROJ_COL if field == "project" else field
    sql = ("SELECT DISTINCT %s AS k FROM reports WHERE %s AND %s <> '' ORDER BY 1"
           % (col, wnd, col))
    return [r["k"] for r in _query(path, sql, args)]


def daily_member(path, days=7, since=None, agent=None):
    """Kunlik ogohlantirish uchun: kishi x kun kesimida sarf."""
    wnd, args = _window(days, since)
    extra, eargs = _filters(agent=agent)
    sql = ("SELECT member, %s AS date, SUM(work_cost_usd) AS work_cost, "
           "COUNT(*) AS sessions FROM reports WHERE %s "
           "GROUP BY member, 2 ORDER BY 2 DESC" %
           (DAY_COL, " AND ".join([wnd] + extra)))
    return _query(path, sql, args + eargs)


def stats(path):
    with connect(path) as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last "
            "FROM reports").fetchone()
        return dict(r) if r else {"n": 0, "first": None, "last": None}

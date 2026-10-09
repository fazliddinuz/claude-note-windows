# -*- coding: utf-8 -*-
"""Ogohlantirishlar.

Maqsad - jazolash emas, nooptimal ishlashni ko'rish va to'g'ri yo'lga solish.
Shuning uchun har bir ogohlantirish yonida "nima qilish kerak" degan maslahat
ham boradi: raqamning o'zi hech kimga hech narsa o'rgatmaydi.
"""

import db

NIGHT = "tungi_ish"
FOREIGN = "begona_loyiha"
EXPENSIVE = "qimmat_sessiya"
BIG_DAY = "katta_kunlik"
BIG_CTX = "katta_kontekst"

LABEL = {
    NIGHT:     "Тунги иш",
    FOREIGN:   "Бегона лойиҳа",
    EXPENSIVE: "Қиммат сессия",
    BIG_DAY:   "Кунлик сарф юқори",
    BIG_CTX:   "Контекст жуда катта",
}

ADVICE = {
    NIGHT:     "Тунда ишлаш — чарчоқ ва хато белгиси. Иш вақтини текшириб кўринг.",
    FOREIGN:   "Бу лойиҳа рўйхатда йўқ. «Лойиҳалар» саҳифасида рўйхатга "
               "қўшинг (ёки шеригингизникига бирлаштиринг), ёки нима устида "
               "ишланганини сўранг.",
    EXPENSIVE: "Битта сессияга кўп кетган. Одатда сабаб — узун сессия ёки "
               "керагидан қиммат модель.",
    BIG_DAY:   "Кунлик сарф чегарадан ошди. Кун давомида нима қилинганини кўринг.",
    BIG_CTX:   "Сессия ҳафталаб чўзилган кўринади — контекст ҳар сўровда "
               "қайта ўқилади ва нарх бир неча баробар ошади. `/clear` ёки "
               "янги сессия керак.",
}


def _fmt_ctx(n):
    return "%.0fK" % (n / 1000.0) if n >= 1000 else str(n)


def evaluate(path, cfg, days=7, agent=None):
    """-> ogohlantirishlar ro'yxati, eng jiddiysi boshida.

    Tarix (telemetriya yoqilgunga qadar bo'lgan sessiyalar) tekshirilmaydi:
    ular haqida ogohlantirish berishning ma'nosi yo'q, ish allaqachon
    qilingan va o'zgartirib bo'lmaydi.
    """
    out = []
    since = getattr(cfg, "history_before", None)
    rows = db.sessions(path, days=days, limit=2000, since=since, agent=agent)
    # Ro'yxat ikki manbadan: .env dagi boshlang'ich ro'yxat va "Лойиҳалар"
    # sahifasida ro'yxatga olingan/birlashtirilgan nomlar. Sherikning
    # loyihasi bir marta birlashtirilsa - boshqa "бегона" bo'lib chiqmaydi.
    known = set(cfg.known_projects or []) | db.known_projects(path)

    for r in rows:
        name = r.get("project_canon") or r["project"]
        base = {"member": r["member"], "project": name,
                "session": r["session"], "date": r["date"],
                "title": r["title"] or "(сарлавҳасиз)"}

        if known and name not in known:
            out.append(dict(base, kind=FOREIGN, level=2,
                            text="«%s» рўйхатдаги лойиҳа эмас" % name))

        hour = r["local_hour"]
        if hour is not None and _is_night(hour, cfg):
            out.append(dict(base, kind=NIGHT, level=1,
                            text="маҳаллий вақт билан соат %02d:00 да бошланган" % hour))

        if r["work_cost_usd"] >= cfg.alert_session_usd:
            out.append(dict(base, kind=EXPENSIVE, level=2,
                            text="битта сессияга $%.2f" % r["work_cost_usd"]))

        if r["avg_context"] >= cfg.alert_context_tokens:
            out.append(dict(base, kind=BIG_CTX, level=3,
                            text="ўртача контекст %s токен"
                                 % _fmt_ctx(r["avg_context"])))

    for d in db.daily_member(path, days=days, since=since, agent=agent):
        if d["work_cost"] >= cfg.alert_day_usd:
            out.append({"kind": BIG_DAY, "level": 3, "member": d["member"],
                        "project": "", "session": "", "date": d["date"],
                        "title": "%d та сессия" % d["sessions"],
                        "text": "бир кунда $%.2f" % d["work_cost"]})

    # jiddiyroq bo'lgani tepada, teng bo'lsa yangisi tepada
    out.sort(key=lambda a: (a["level"], a["date"] or ""), reverse=True)
    return out


def _is_night(hour, cfg):
    """Tun yarim kechadan o'tib ketishi mumkin (masalan 22:00-06:00)."""
    a, b = cfg.night_start, cfg.night_end
    if a == b:
        return False
    if a < b:
        return a <= hour < b
    return hour >= a or hour < b


def summary(alerts):
    """Turlar bo'yicha sanoq - dashboard tepasidagi qatorlar uchun."""
    agg = {}
    for a in alerts:
        agg[a["kind"]] = agg.get(a["kind"], 0) + 1
    return [(k, LABEL[k], n) for k, n in
            sorted(agg.items(), key=lambda kv: -kv[1])]

"""
Datos complementarios: horario y calendario de NYSE, pre-market / after-hours,
calendario de datos económicos y probabilidades de la próxima reunión de la Fed.
"""

from __future__ import annotations

import io
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")
MVD = ZoneInfo("America/Montevideo")

PRE_START = (4, 0)
POST_END = (20, 0)

HOLIDAY_ES = {
    "New Year's Day": "Año Nuevo",
    "Dr. Martin Luther King Jr. Day": "Día de Martin Luther King",
    "President's Day": "Día de los Presidentes",
    "Presidents' Day": "Día de los Presidentes",
    "Good Friday": "Viernes Santo",
    "Memorial Day": "Memorial Day",
    "Juneteenth": "Juneteenth",
    "Independence Day": "Día de la Independencia",
    "Labor Day": "Día del Trabajo",
    "Thanksgiving": "Acción de Gracias",
    "Christmas": "Navidad",
}


def _f(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


# --------------------------------------------------------------------------- #
# Calendario y horario de NYSE
# --------------------------------------------------------------------------- #
_CAL = None


def _cal():
    global _CAL
    if _CAL is None:
        try:
            import exchange_calendars as xc
            _CAL = xc.get_calendar("XNYS")
        except Exception as e:  # noqa: BLE001
            print(f"Aviso: no pude cargar el calendario de NYSE ({e}); uso solo días hábiles.")
            _CAL = False
    return _CAL


def session_times(day) -> tuple[datetime, datetime] | None:
    """Apertura y cierre (hora de NY) de una rueda; None si ese día no hay rueda."""
    d = pd.Timestamp(day).normalize().tz_localize(None)
    cal = _cal()
    if cal:
        try:
            if not cal.is_session(d):
                return None
            o = cal.session_open(d).tz_convert(NY).to_pydatetime()
            c = cal.session_close(d).tz_convert(NY).to_pydatetime()
            return o, c
        except Exception:  # noqa: BLE001  (fuera del rango del calendario)
            pass
    if d.weekday() >= 5:
        return None
    base = datetime(d.year, d.month, d.day, tzinfo=NY)
    return base.replace(hour=9, minute=30), base.replace(hour=16)


def market_clock(now: datetime | None = None) -> dict:
    """Estado del mercado: pre / open / post / closed, con horarios del día."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    st = session_times(now.date())
    out = {"now": now.isoformat(), "session": st is not None, "phase": "closed", "early": False}
    if st:
        o, c = st
        out.update(open=o.isoformat(), close=c.isoformat(), early=c.hour < 16)
        if now < o:
            out["phase"] = "pre"
        elif now < c + timedelta(minutes=5):
            out["phase"] = "open"
        elif (now.hour, now.minute) < POST_END:
            out["phase"] = "post"
    return out


def holidays_ahead(start=None, days: int = 60) -> list[dict]:
    """Feriados y medias jornadas de NYSE en los próximos días."""
    start = pd.Timestamp(start or datetime.now(NY).date())
    end = start + pd.Timedelta(days=days)
    out = []
    cal = _cal()
    if not cal:
        return out
    try:
        names = cal.regular_holidays.holidays(start, end, return_name=True)
        for d, n in names.items():
            if d.weekday() < 5:
                out.append({"d": d.strftime("%Y-%m-%d"), "tipo": "cerrado", "n": HOLIDAY_ES.get(str(n), str(n))})
        for d in cal.adhoc_holidays:
            if start <= d <= end:
                out.append({"d": d.strftime("%Y-%m-%d"), "tipo": "cerrado", "n": "Cierre especial"})
        sched = cal.schedule.loc[start:end]
        for d, row in sched.iterrows():
            c = row["close"].tz_convert(NY)
            if c.hour < 16:
                out.append({"d": d.strftime("%Y-%m-%d"), "tipo": "media", "n": f"Cierra a las {c.strftime('%H:%M')}",
                            "close": c.strftime("%H:%M")})
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude calcular feriados ({e}).")
    return sorted(out, key=lambda x: x["d"])


def last_close_before(now: datetime) -> datetime | None:
    """Hora de cierre de la última rueda que ya cerró."""
    d = now.astimezone(NY).date()
    for k in range(0, 12):
        st = session_times(d - timedelta(days=k))
        if st and st[1] <= now:
            return st[1]
    return None


# --------------------------------------------------------------------------- #
# Pre-market y after-hours
# --------------------------------------------------------------------------- #
FUTURES = [("ES=F", "Futuro S&P 500"), ("NQ=F", "Futuro Nasdaq 100"), ("YM=F", "Futuro Dow Jones"),
           ("RTY=F", "Futuro Russell 2000")]
EXT_MACRO = [("CL=F", "Petróleo WTI"), ("GC=F", "Oro"), ("DX-Y.NYB", "Índice dólar (DXY)")]


def ext_summary(holdings: pd.DataFrame, kind: str, now: datetime | None = None) -> dict | None:
    """Movimientos fuera de la rueda contra el último cierre (16:00 NY o el de media jornada)."""
    import yfinance as yf

    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    ref_t = last_close_before(now)
    if ref_t is None:
        return None
    top = holdings.sort_values("weight", ascending=False).head(10)
    stocks = list(top["yf"])
    names = dict(zip(top["yf"], top["ticker"]))
    full = dict(zip(top["yf"], top["name"]))
    tickers = [t for t, _ in FUTURES] + stocks + [t for t, _ in EXT_MACRO]
    try:
        data = yf.download(tickers, period="5d", interval="5m", prepost=True, progress=False,
                           group_by="column", threads=True, auto_adjust=False)
        close = data["Close"]
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude bajar los precios fuera de rueda ({e}).")
        return None
    close.index = pd.to_datetime(close.index)
    close.index = close.index.tz_localize("UTC") if close.index.tz is None else close.index
    close.index = close.index.tz_convert(NY)

    def move(t):
        if t not in close.columns:
            return None, None, None
        s = close[t].dropna()
        if s.empty:
            return None, None, None
        before = s[s.index <= ref_t + timedelta(minutes=1)]
        if before.empty:
            return None, None, None
        ref, last, at = float(before.iloc[-1]), float(s.iloc[-1]), s.index[-1]
        if at <= ref_t:  # sin operaciones después del cierre
            return ref, 0.0, at
        return last, (last / ref - 1) * 100, at

    def rows(pairs):
        out = []
        for t, n in pairs:
            last, chg, at = move(t)
            if last is not None:
                out.append({"t": t, "n": n, "last": _f(last, 2), "chg": _f(chg, 2),
                            "at": at.isoformat() if at is not None else None})
        return out

    st_rows = []
    for t in stocks:
        last, chg, at = move(t)
        if last is not None:
            st_rows.append({"t": names[t], "n": full[t], "last": _f(last, 2), "chg": _f(chg, 2),
                            "at": at.isoformat() if at is not None else None})
    res = {
        "kind": kind,
        "at": now.isoformat(),
        "ref": ref_t.isoformat(),
        "futures": rows(FUTURES),
        "stocks": st_rows,
        "macro": rows(EXT_MACRO),
    }
    es = next((x for x in res["futures"] if x["t"] == "ES=F"), None)
    res["lead"] = _ext_lead(res, es)
    return res


def _ext_lead(r: dict, es: dict | None) -> str:
    if not es or es["chg"] is None:
        return ""
    when = "antes de la apertura" if r["kind"] == "pre" else "después del cierre"
    verb = "sube" if es["chg"] > 0.02 else "baja" if es["chg"] < -0.02 else "está plano"
    txt = f"El futuro del S&P 500 {verb}"
    if verb != "está plano":
        txt += f" {abs(es['chg']):.2f}%".replace(".", ",")
    txt += f" {when}."
    mv = sorted([s for s in r["stocks"] if s["chg"] is not None], key=lambda s: -abs(s["chg"]))[:3]
    mv = [s for s in mv if abs(s["chg"]) >= 0.5]
    if mv:
        txt += " Entre las de mayor peso se destacan " + ", ".join(
            f"{s['t']} ({s['chg']:+.2f}%)".replace(".", ",") for s in mv) + "."
    return txt


# --------------------------------------------------------------------------- #
# Calendario económico (fechas oficiales; revisar una vez por año)
# Fuentes: Reserva Federal (FOMC), BLS (CPI, PPI, empleo, JOLTS), BEA (PIB, PCE),
# Census (ventas minoristas). Hora de Nueva York.
# --------------------------------------------------------------------------- #
EVENT_TYPES = {
    "fomc": {"n": "Decisión de tasas de la Fed (FOMC)", "h": "14:00", "imp": 3},
    "fomc_sep": {"n": "Decisión de la Fed + proyecciones (FOMC)", "h": "14:00", "imp": 3},
    "cpi": {"n": "Inflación CPI", "h": "08:30", "imp": 3},
    "empleo": {"n": "Informe de empleo (nóminas y desempleo)", "h": "08:30", "imp": 3},
    "pce": {"n": "Inflación PCE e ingreso y gasto personal", "h": "08:30", "imp": 2},
    "pib": {"n": "PIB", "h": "08:30", "imp": 2},
    "ppi": {"n": "Inflación mayorista PPI", "h": "08:30", "imp": 2},
    "jolts": {"n": "Vacantes laborales JOLTS", "h": "10:00", "imp": 1},
    "ventas": {"n": "Ventas minoristas", "h": "08:30", "imp": 2},
    "claims": {"n": "Pedidos semanales de desempleo", "h": "08:30", "imp": 1},
}

# (fecha, tipo, período de referencia)
EVENTS = [
    # FOMC (federalreserve.gov) — día de la decisión
    ("2026-12-09", "fomc_sep", ""), ("2027-01-27", "fomc", ""), ("2027-03-17", "fomc", ""),
    ("2027-04-28", "fomc", ""), ("2027-06-09", "fomc", ""), ("2027-07-28", "fomc", ""),
    ("2027-09-15", "fomc", ""), ("2027-10-27", "fomc", ""), ("2027-11-17", "fomc", ""),
    # CPI (bls.gov)
    ("2026-12-10", "cpi", "nov."), ("2027-01-13", "cpi", "dic."), ("2027-02-11", "cpi", "ene."),
    ("2027-03-10", "cpi", "feb."), ("2027-04-13", "cpi", "mar."), ("2027-05-12", "cpi", "abr."),
    ("2027-06-10", "cpi", "may."), ("2027-07-14", "cpi", "jun."), ("2027-08-11", "cpi", "jul."),
    ("2027-09-14", "cpi", "ago."), ("2027-10-14", "cpi", "sep."), ("2027-11-10", "cpi", "oct."),
    ("2027-12-10", "cpi", "nov."),
    # Empleo (bls.gov)
    ("2026-12-04", "empleo", "nov."), ("2027-01-08", "empleo", "dic."), ("2027-02-05", "empleo", "ene."),
    ("2027-03-05", "empleo", "feb."), ("2027-04-02", "empleo", "mar."), ("2027-05-07", "empleo", "abr."),
    ("2027-06-04", "empleo", "may."), ("2027-07-02", "empleo", "jun."), ("2027-08-06", "empleo", "jul."),
    ("2027-09-03", "empleo", "ago."), ("2027-10-08", "empleo", "sep."), ("2027-11-05", "empleo", "oct."),
    ("2027-12-03", "empleo", "nov."),
    # PPI (bls.gov)
    ("2026-12-15", "ppi", "nov."), ("2027-01-14", "ppi", "dic."), ("2027-02-12", "ppi", "ene."),
    ("2027-03-12", "ppi", "feb."), ("2027-04-14", "ppi", "mar."), ("2027-05-13", "ppi", "abr."),
    ("2027-06-11", "ppi", "may."), ("2027-07-15", "ppi", "jun."), ("2027-08-10", "ppi", "jul."),
    ("2027-09-10", "ppi", "ago."), ("2027-10-15", "ppi", "sep."), ("2027-11-12", "ppi", "oct."),
    ("2027-12-15", "ppi", "nov."),
    # JOLTS (bls.gov)
    ("2026-12-01", "jolts", "oct."), ("2027-01-05", "jolts", "nov."), ("2027-02-02", "jolts", "dic."),
    ("2027-03-09", "jolts", "ene."), ("2027-03-30", "jolts", "feb."), ("2027-05-04", "jolts", "mar."),
    ("2027-06-02", "jolts", "abr."), ("2027-06-29", "jolts", "may."), ("2027-08-03", "jolts", "jun."),
    ("2027-08-31", "jolts", "jul."), ("2027-10-05", "jolts", "ago."), ("2027-11-02", "jolts", "sep."),
    ("2027-12-01", "jolts", "oct."),
    # PIB y PCE (bea.gov)
    ("2026-11-25", "pib", "3.er trim. 2026, 2.ª estimación"), ("2026-11-25", "pce", "oct."),
    ("2026-12-23", "pib", "3.er trim. 2026, 3.ª estimación"), ("2026-12-23", "pce", "nov."),
    ("2027-01-28", "pib", "4.º trim. 2026, preliminar"), ("2027-01-28", "pce", "dic."),
    ("2027-02-25", "pib", "4.º trim. 2026, 2.ª estimación"), ("2027-02-25", "pce", "ene."),
    ("2027-03-31", "pib", "4.º trim. 2026, 3.ª estimación"), ("2027-03-31", "pce", "feb."),
    ("2027-04-29", "pib", "1.er trim. 2027, preliminar"), ("2027-04-29", "pce", "mar."),
    ("2027-05-27", "pib", "1.er trim. 2027, 2.ª estimación"), ("2027-05-27", "pce", "abr."),
    ("2027-06-25", "pib", "1.er trim. 2027, 3.ª estimación"), ("2027-06-25", "pce", "may."),
    ("2027-07-29", "pib", "2.º trim. 2027, preliminar"), ("2027-07-29", "pce", "jun."),
    ("2027-08-25", "pib", "2.º trim. 2027, 2.ª estimación"), ("2027-08-25", "pce", "jul."),
    ("2027-09-30", "pib", "2.º trim. 2027, 3.ª estimación"), ("2027-09-30", "pce", "ago."),
    ("2027-10-28", "pib", "3.er trim. 2027, preliminar"), ("2027-10-28", "pce", "sep."),
    ("2027-11-24", "pib", "3.er trim. 2027, 2.ª estimación"), ("2027-11-24", "pce", "oct."),
    ("2027-12-22", "pib", "3.er trim. 2027, 3.ª estimación"), ("2027-12-22", "pce", "nov."),
    # Ventas minoristas (Census)
    ("2026-12-16", "ventas", "nov."),
]
EVENTS_REVIEWED = "2026-11-19"


def weekly_claims(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[str, str, str, bool]]:
    """Pedidos de desempleo: jueves 8:30; en semana de feriado se adelanta (fecha a confirmar)."""
    out = []
    d = start + pd.Timedelta(days=(3 - start.weekday()) % 7)
    while d <= end:
        st = session_times(d)
        conf = True
        day = d
        if st is None or (d.month == 12 and d.day in (24, 25)) or (d.month == 1 and d.day == 1):
            day, conf = d - pd.Timedelta(days=1), False
        if d.month == 11 and 22 <= d.day <= 28:  # Acción de Gracias: siempre el miércoles
            day, conf = d - pd.Timedelta(days=1), True
        out.append((day.strftime("%Y-%m-%d"), "claims", "semanal", conf))
        d += pd.Timedelta(days=7)
    return out


# Último dato publicado (FRED)
FRED_SERIES = {
    "cpi": ["CPIAUCSL", "CPILFESL"], "ppi": ["PPIFIS"], "empleo": ["PAYEMS", "UNRATE"], "jolts": ["JTSJOL"],
    "pib": ["A191RL1Q225SBEA"], "pce": ["PCEPI", "PCEPILFE"], "ventas": ["RSAFS"], "claims": ["ICSA"],
    "fomc": ["DFEDTARL", "DFEDTARU"], "fomc_sep": ["DFEDTARL", "DFEDTARU"],
}
MONTHS = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.", "jul.", "ago.", "sep.", "oct.", "nov.", "dic."]


def load_fred(series: list[str]) -> dict[str, pd.Series]:
    """Series de FRED en paralelo, con tiempo máximo corto: si FRED no responde, el calendario sale sin 'último dato'."""
    import requests
    from concurrent.futures import ThreadPoolExecutor

    start = (datetime.now() - timedelta(days=900)).strftime("%Y-%m-%d")

    def one(sid):
        try:
            r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}",
                             headers={"User-Agent": "Mozilla/5.0 (compatible; spy-movers/1.0)"}, timeout=(5, 12))
            r.raise_for_status()
            f = pd.read_csv(io.StringIO(r.text))
            f.columns = ["d", "v"]
            s = pd.to_numeric(f["v"], errors="coerce")
            s.index = pd.to_datetime(f["d"])
            return sid, s.dropna()
        except Exception as e:  # noqa: BLE001
            print(f"Aviso: no pude leer {sid} de FRED ({type(e).__name__}).")
            return sid, None

    with ThreadPoolExecutor(8) as pool:
        res = list(pool.map(one, sorted(set(series))))
    return {k: v for k, v in res if v is not None and len(v)}


def _num(x, nd=1):
    return f"{x:.{nd}f}".replace(".", ",")


def last_value_text(kind: str, fred: dict[str, pd.Series]) -> str | None:
    def per(s, q=False):
        d = s.index[-1]
        return f"{(d.month - 1) // 3 + 1}.º trim. {d.year}" if q else f"{MONTHS[d.month - 1]} {d.year}"

    def yoy(s):
        return (s.iloc[-1] / s.iloc[-13] - 1) * 100 if len(s) > 13 else None

    try:
        if kind == "cpi" and "CPIAUCSL" in fred:
            s, c = fred["CPIAUCSL"], fred.get("CPILFESL")
            t = f"{_num(yoy(s))}% interanual"
            if c is not None and yoy(c) is not None:
                t += f", núcleo {_num(yoy(c))}%"
            return f"{t} ({per(s)})"
        if kind == "ppi" and "PPIFIS" in fred:
            s = fred["PPIFIS"]
            return f"{_num(yoy(s))}% interanual ({per(s)})"
        if kind == "empleo" and "PAYEMS" in fred:
            s, u = fred["PAYEMS"], fred.get("UNRATE")
            t = f"{s.iloc[-1] - s.iloc[-2]:+,.0f} mil empleos".replace(",", ".")
            if u is not None and len(u):
                t += f", desempleo {_num(u.iloc[-1])}%"
            return f"{t} ({per(s)})"
        if kind == "jolts" and "JTSJOL" in fred:
            s = fred["JTSJOL"]
            return f"{_num(s.iloc[-1] / 1000, 2)} millones de vacantes ({per(s)})"
        if kind == "pib" and "A191RL1Q225SBEA" in fred:
            s = fred["A191RL1Q225SBEA"]
            return f"{_num(s.iloc[-1])}% trimestral anualizado ({per(s, True)})"
        if kind == "pce" and "PCEPI" in fred:
            s, c = fred["PCEPI"], fred.get("PCEPILFE")
            t = f"{_num(yoy(s))}% interanual"
            if c is not None and yoy(c) is not None:
                t += f", núcleo {_num(yoy(c))}%"
            return f"{t} ({per(s)})"
        if kind == "ventas" and "RSAFS" in fred:
            s = fred["RSAFS"]
            return f"{_num((s.iloc[-1] / s.iloc[-2] - 1) * 100)}% mensual ({per(s)})"
        if kind == "claims" and "ICSA" in fred:
            s = fred["ICSA"]
            return f"{s.iloc[-1] / 1000:.0f} mil (semana al {s.index[-1].day} {MONTHS[s.index[-1].month - 1]})"
        if kind in ("fomc", "fomc_sep") and "DFEDTARU" in fred:
            lo, hi = fred["DFEDTARL"].iloc[-1], fred["DFEDTARU"].iloc[-1]
            return f"Rango actual {_num(lo, 2)}%–{_num(hi, 2)}%"
    except Exception:  # noqa: BLE001
        return None
    return None


def econ_calendar(fred: dict[str, pd.Series], today=None, days: int = 10) -> dict:
    today = pd.Timestamp(today or datetime.now(NY).date())
    end = today + pd.Timedelta(days=days)
    items = [(d, k, p, True) for d, k, p in EVENTS] + weekly_claims(today, end)
    out = []
    for d, k, p, conf in items:
        dt = pd.Timestamp(d)
        if not (today <= dt <= end):
            continue
        meta = EVENT_TYPES[k]
        hh, mm = map(int, meta["h"].split(":"))
        ny = datetime(dt.year, dt.month, dt.day, hh, mm, tzinfo=NY)
        out.append({
            "d": d, "k": k, "n": meta["n"], "p": p, "imp": meta["imp"], "conf": conf,
            "ny": meta["h"], "mvd": ny.astimezone(MVD).strftime("%H:%M"),
            "ts": ny.isoformat(),
            "last": last_value_text(k, fred),
        })
    for h in holidays_ahead(today, days):
        out.append({"d": h["d"], "k": "mercado", "n": ("Mercado cerrado: " if h["tipo"] == "cerrado" else "Media jornada: ")
                     + h["n"], "p": "", "imp": 2, "conf": True, "ny": "", "mvd": "", "ts": h["d"] + "T00:00:00",
                     "last": None})
    out.sort(key=lambda x: (x["d"], x["ny"] or "00:00"))
    last_ev = max(d for d, _, _ in EVENTS)
    return {"items": out, "revisado": EVENTS_REVIEWED, "hasta": last_ev,
            "aviso": pd.Timestamp(last_ev) - today < pd.Timedelta(days=45)}


# --------------------------------------------------------------------------- #
# Probabilidades de la próxima reunión (metodología de CME FedWatch)
# --------------------------------------------------------------------------- #
FOMC_DECISIONS = sorted(d for d, k, _ in EVENTS if k.startswith("fomc"))
MONTH_CODES = "FGHJKMNQUVXZ"


def _zq_ticker(year: int, month: int) -> str:
    return f"ZQ{MONTH_CODES[month - 1]}{year % 100:02d}.CBT"


def _month_add(y, m, k):
    m2 = m - 1 + k
    return y + m2 // 12, m2 % 12 + 1


def fedwatch(fred: dict[str, pd.Series], today=None) -> dict | None:
    """Probabilidades implícitas en los futuros de fed funds para la próxima reunión."""
    import yfinance as yf

    today = pd.Timestamp(today or datetime.now(NY).date())
    nxt = next((pd.Timestamp(d) for d in FOMC_DECISIONS if pd.Timestamp(d) >= today), None)
    if nxt is None:
        return None
    y, m = nxt.year, nxt.month
    meet_months = {(pd.Timestamp(d).year, pd.Timestamp(d).month) for d in FOMC_DECISIONS}
    prev_ym, next_ym = _month_add(y, m, -1), _month_add(y, m, 1)
    tick = {k: _zq_ticker(*ym) for k, ym in (("prev", prev_ym), ("cur", (y, m)), ("next", next_ym))}
    try:
        data = yf.download(list(tick.values()), period="10d", interval="1d", progress=False,
                           group_by="column", auto_adjust=False)["Close"]
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude bajar los futuros de fed funds ({e}).")
        return None
    px = {}
    for k, t in tick.items():
        if t in data.columns and data[t].notna().any():
            px[k] = float(data[t].dropna().iloc[-1])
    if "cur" not in px:
        return None
    rate = {k: 100 - v for k, v in px.items()}
    days = pd.Timestamp(y, m, 1).days_in_month
    n = nxt.day           # días con la tasa vieja (el cambio rige desde el día siguiente a la decisión)
    mm = days - n         # días con la tasa nueva
    if mm <= 0:
        return None
    if next_ym not in meet_months and "next" in rate:
        end = rate["next"]
        start = (rate["cur"] - mm / days * end) / (n / days)
        metodo = "mes siguiente"
    elif prev_ym not in meet_months and "prev" in rate:
        start = rate["prev"]
        end = (rate["cur"] - n / days * start) / (mm / days)
        metodo = "mes anterior"
    else:
        return None
    change = end - start
    k = abs(change) / 0.25
    lo = int(math.floor(k + 1e-9))
    r = k - lo
    sign = -1 if change < 0 else 1
    lower = fred["DFEDTARL"].iloc[-1] if "DFEDTARL" in fred and len(fred["DFEDTARL"]) else None

    def label(moves):
        if moves == 0:
            txt = "Mantener"
        else:
            txt = f"{'Baja' if sign < 0 else 'Suba'} de {moves * 25} pb"
        if lower is not None:
            lo_new = lower + sign * moves * 0.25
            txt += f" ({_num(lo_new, 2)}%–{_num(lo_new + 0.25, 2)}%)"
        return txt

    probs = [{"label": label(lo), "p": _f((1 - r) * 100, 1), "moves": sign * lo}]
    if r > 0.005:
        probs.append({"label": label(lo + 1), "p": _f(r * 100, 1), "moves": sign * (lo + 1)})
    probs.sort(key=lambda x: x["moves"])
    return {
        "meeting": nxt.strftime("%Y-%m-%d"),
        "probs": probs,
        "start": _f(start, 3), "end": _f(end, 3),
        "range": f"{_num(lower, 2)}%–{_num(lower + 0.25, 2)}%" if lower is not None else None,
        "contracts": {k: tick[k] for k in px},
        "metodo": metodo,
    }

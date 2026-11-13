"""
Calcula qué movió al SPY en el día y guarda el resultado en site/data/.

Fuentes:
  - Ponderaciones y sectores: archivo diario de holdings de SSGA (emisor del SPY).
  - Precios: yfinance (un solo batch con ~1 año de cierres diarios).

Salida:
  site/data/latest.json            último cálculo
  site/data/history/YYYY-MM-DD.json  un archivo por día (se sobrescribe hasta el cierre)
  site/data/dates.json             lista de días disponibles
"""

from __future__ import annotations

import io
import json
import math
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np
import pandas as pd

SSGA_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
OUT_DIR = Path(__file__).resolve().parent.parent / "site" / "data"
SECTORS_CACHE = OUT_DIR / "sectors.json"
THEMES_FILE = Path(__file__).resolve().parent.parent / "themes.json"
MIN_THEME_MEMBERS = 3

SECTOR_ES = {
    "Information Technology": "Tecnología",
    "Financials": "Financiero",
    "Health Care": "Salud",
    "Consumer Discretionary": "Consumo discrecional",
    "Communication Services": "Comunicaciones",
    "Industrials": "Industriales",
    "Consumer Staples": "Consumo básico",
    "Energy": "Energía",
    "Utilities": "Utilities",
    "Real Estate": "Inmobiliario",
    "Materials": "Materiales",
}

MAG7 = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA"}

# Activos de contexto: (ticker yfinance, nombre, tipo de variación)
#   "pct"  -> variación porcentual
#   "abs"  -> variación absoluta (VIX en puntos, tasa en puntos básicos)
CONTEXT = [
    ("^GSPC", "S&P 500", "pct"),
    ("SPY", "SPY", "pct"),
    ("RSP", "S&P 500 equal weight (RSP)", "pct"),
    ("QQQ", "Nasdaq 100 (QQQ)", "pct"),
    ("IWM", "Russell 2000 (IWM)", "pct"),
    ("^VIX", "VIX", "abs"),
    ("^TNX", "Tasa del Tesoro a 10 años", "bps"),
    ("DX-Y.NYB", "Índice dólar (DXY)", "pct"),
    ("CL=F", "Petróleo WTI", "pct"),
    ("GC=F", "Oro", "pct"),
]


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #
def load_holdings() -> tuple[pd.DataFrame, str | None]:
    """Devuelve (DataFrame con ticker/name/weight/sector, fecha de los holdings)."""
    import requests

    resp = requests.get(SSGA_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    resp.raise_for_status()
    raw = pd.read_excel(io.BytesIO(resp.content), header=None)
    return parse_holdings(raw)


def parse_holdings(raw: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    as_of = None
    header_row = None
    for i in range(min(len(raw), 30)):
        cells = [str(c).strip() for c in raw.iloc[i].tolist()]
        for cell in cells:
            if cell.lower().startswith("as of"):
                as_of = cell[5:].strip()
        if cells and cells[0].lower() == "name":
            header_row = i
            break
    if header_row is None:
        raise RuntimeError("No encontré la fila de encabezados en el archivo de SSGA.")

    df = raw.iloc[header_row + 1 :].copy()
    df.columns = [str(c).strip().lower() for c in raw.iloc[header_row].tolist()]
    df = df.rename(columns={"name": "name", "ticker": "ticker", "weight": "weight", "sector": "sector"})
    df = df[["name", "ticker", "weight", "sector"]]
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df = df[df["weight"].notna() & (df["weight"] > 0)]
    # Solo acciones: deja afuera caja, futuros y filas de pie de página.
    df = df[df["ticker"].str.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?")]
    df["yf"] = df["ticker"].str.replace(".", "-", regex=False)
    df["sector"] = df["sector"].fillna("").astype(str).str.strip()
    df["name"] = df["name"].astype(str).str.strip()
    # Por si un ticker aparece repetido: se suman los pesos.
    df = df.groupby("yf", as_index=False, sort=False).agg(
        name=("name", "first"), ticker=("ticker", "first"),
        weight=("weight", "sum"), sector=("sector", "first"),
    )
    return df[["name", "ticker", "weight", "sector", "yf"]], as_of


def load_sectors() -> dict:
    """Sector y sub-industria GICS por ticker, desde la lista del S&P 500 de Wikipedia.

    El Excel de SSGA no trae el sector completo, así que lo tomamos de acá.
    Si Wikipedia falla, se usa la última copia guardada en site/data/sectors.json.
    """
    try:
        import requests

        r = requests.get(WIKI_URL, headers={"User-Agent": "spy-movers/1.0 (GitHub Actions)"}, timeout=60)
        r.raise_for_status()
        table = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
        m = {
            str(row["Symbol"]).strip().upper(): {
                "s": str(row["GICS Sector"]).strip(),
                "sub": str(row["GICS Sub-Industry"]).strip(),
            }
            for _, row in table.iterrows()
        }
        if len(m) > 400:
            SECTORS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SECTORS_CACHE.write_text(json.dumps(m, ensure_ascii=False, indent=0), encoding="utf-8")
            return m
        print(f"Aviso: Wikipedia devolvió solo {len(m)} filas; uso el caché.")
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude leer los sectores de Wikipedia ({e}); uso el caché.")
    if SECTORS_CACHE.exists():
        return json.loads(SECTORS_CACHE.read_text(encoding="utf-8"))
    return {}


def apply_sectors(h: pd.DataFrame, m: dict) -> pd.DataFrame:
    h = h.copy()

    def pick(row):
        info = m.get(row["ticker"])
        if info and info.get("s"):
            return info["s"]
        s = str(row["sector"]).strip()
        return s if s and s not in {"-", "nan", "None"} else "Sin sector"

    h["sector"] = h.apply(pick, axis=1).map(lambda x: SECTOR_ES.get(x, x))
    h["sub"] = h["ticker"].map(lambda t: (m.get(t) or {}).get("sub", ""))
    return h


def load_prices(tickers: list[str], period: str = "13mo") -> pd.DataFrame:
    import yfinance as yf

    data = yf.download(
        tickers,
        period=period,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    close = data["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame()
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close


# --------------------------------------------------------------------------- #
# Cálculo
# --------------------------------------------------------------------------- #
def _f(x, nd=4):
    """Float limpio para JSON (NaN -> None)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def load_themes() -> dict:
    try:
        return json.loads(THEMES_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude leer themes.json ({e}); sigo sin temáticas.")
        return {"tematicas": []}


def compute_themes(df: pd.DataFrame, cfg: dict) -> tuple[list[dict], dict, dict]:
    """Métricas por temática. Devuelve (temáticas, info de cobertura, mapa ticker -> temáticas)."""
    sub = df["sub"].fillna("") if "sub" in df.columns else pd.Series("", index=df.index)
    tick = df["ticker"]
    in_index = set(tick)
    out, covered, by_ticker, missing = [], set(), {}, set()

    for th in cfg.get("tematicas", []):
        gics = set(th.get("gics", []))
        add = set(th.get("agregar", []))
        exc = set(th.get("excluir", []))
        missing |= add - in_index
        m = df[(sub.isin(gics) | tick.isin(add)) & ~tick.isin(exc)]
        if len(m) < MIN_THEME_MEMBERS:
            continue
        name = th["nombre"]
        covered |= set(m.index)
        for t in m["ticker"]:
            by_ticker.setdefault(t, []).append(name)
        w = float(m["weight"].sum())
        c = float(m["contrib"].sum(skipna=True))
        p = float(m["pts"].sum(skipna=True)) if m["pts"].notna().any() else None
        valid = m["ret"].notna()
        val = m["pts"] if p is not None else m["contrib"]
        lead = m.assign(v=val).dropna(subset=["v"])
        lead = lead.reindex(lead["v"].abs().sort_values(ascending=False).index).head(3)
        out.append({
            "n": name,
            "g": th.get("grupo", ""),
            "agg": bool(th.get("agregada", False)),
            "k": int(len(m)),
            "w": _f(w, 2),
            "c": _f(c, 4),
            "p": _f(p, 2),
            "r": _f(c / w * 100 if w else None, 3),
            "up": int((m["ret"] > 0).sum()),
            "valid": int(valid.sum()),
            "lead": [{"t": r["ticker"], "v": _f(r["v"], 2)} for _, r in lead.iterrows()],
            "mem": m.sort_values("weight", ascending=False)["ticker"].tolist(),
        })

    out.sort(key=lambda x: -(x["p"] if x["p"] is not None else x["c"] or 0))
    total_w = float(df["weight"].sum())
    unc = df[~df.index.isin(covered)].sort_values("weight", ascending=False)
    info = {
        "revisado": cfg.get("revisado"),
        "cobertura": _f(df.loc[list(covered), "weight"].sum() / total_w * 100 if total_w else None, 1),
        "sin_tematica": [{"t": r["ticker"], "w": _f(r["weight"], 2)} for _, r in unc.head(15).iterrows()],
        "no_en_indice": sorted(missing),
    }
    return out, info, by_ticker


def compute(holdings: pd.DataFrame, holdings_as_of: str | None, close: pd.DataFrame) -> dict:
    if "SPY" not in close.columns:
        raise RuntimeError("Falta SPY en los precios descargados.")

    # Calendario de trading = días con cierre de SPY.
    days = close["SPY"].dropna().index
    px = close.loc[days]
    today, prev = days[-1], days[-2]

    tick = [t for t in holdings["yf"] if t in px.columns]
    stocks = px[tick]
    ret = stocks.iloc[-1] / stocks.iloc[-2] - 1  # retorno simple del día

    df = holdings.set_index("yf").loc[tick].copy()
    df["ret"] = ret
    df["contrib"] = df["weight"] * df["ret"]  # puntos porcentuales del retorno del SPY

    spx = close["^GSPC"].dropna() if "^GSPC" in close.columns else None
    spx_prev = float(spx.loc[:prev].iloc[-1]) if spx is not None and len(spx) else None
    spx_last = float(spx.iloc[-1]) if spx is not None and len(spx) else None
    to_pts = (spx_prev / 100.0) if spx_prev else None
    df["pts"] = df["contrib"] * to_pts if to_pts else np.nan

    spy_ret = float(px["SPY"].iloc[-1] / px["SPY"].iloc[-2] - 1) * 100
    model_ret = float(df["contrib"].sum(skipna=True))
    missing = df[df["ret"].isna()].index.tolist()

    # ---- Temáticas ------------------------------------------------------ #
    themes, themes_info, th_by_ticker = compute_themes(df, load_themes())

    # ---- Componentes ---------------------------------------------------- #
    comps = []
    for yf_t, r in df.sort_values("contrib", ascending=False).iterrows():
        comps.append(
            {
                "t": r["ticker"],
                "n": r["name"],
                "s": r["sector"],
                "sub": r.get("sub", "") if "sub" in r else "",
                "th": th_by_ticker.get(r["ticker"], []),
                "w": _f(r["weight"], 4),
                "r": _f(r["ret"] * 100, 3),
                "c": _f(r["contrib"], 5),
                "p": _f(r["pts"], 3),
            }
        )

    # ---- Sectores ------------------------------------------------------- #
    sec = (
        df.assign(up=(df["ret"] > 0).astype(int), valid=df["ret"].notna().astype(int))
        .groupby("sector")
        .agg(weight=("weight", "sum"), contrib=("contrib", "sum"), pts=("pts", "sum"),
             up=("up", "sum"), n=("valid", "sum"))
        .sort_values("contrib", ascending=False)
    )
    sectors = [
        {
            "s": s,
            "w": _f(r["weight"], 2),
            "c": _f(r["contrib"], 4),
            "p": _f(r["pts"], 2),
            "r": _f(r["contrib"] / r["weight"] * 100 if r["weight"] else None, 3),
            "up": int(r["up"]),
            "n": int(r["n"]),
        }
        for s, r in sec.iterrows()
    ]

    # ---- Amplitud ------------------------------------------------------- #
    last = stocks.iloc[-1]
    sma50 = stocks.rolling(50, min_periods=45).mean().iloc[-1]
    sma200 = stocks.rolling(200, min_periods=180).mean().iloc[-1]
    win = stocks.iloc[-252:]
    hi52, lo52 = win.max(), win.min()

    valid = ret.notna()
    adv = int((ret > 0).sum())
    dec = int((ret < 0).sum())
    unch = int(valid.sum()) - adv - dec

    # Avances y declives diarios del último año, con el cierre del S&P 500 de cada día
    daily = stocks.pct_change(fill_method=None).iloc[-252:]
    ad_adv = (daily > 0).sum(axis=1)
    ad_dec = (daily < 0).sum(axis=1)
    spx_d = close["^GSPC"].reindex(daily.index).ffill() if "^GSPC" in close.columns else None
    ad_daily = [
        {"d": d.strftime("%Y-%m-%d"), "a": int(ad_adv[d]), "b": int(ad_dec[d]),
         "x": _f(spx_d[d], 2) if spx_d is not None else None}
        for d in daily.index
    ]

    above50 = (last > sma50)[sma50.notna()]
    above200 = (last > sma200)[sma200.notna()]

    rsp_ret = None
    if "RSP" in px.columns and px["RSP"].iloc[-2:].notna().all():
        rsp_ret = float(px["RSP"].iloc[-1] / px["RSP"].iloc[-2] - 1) * 100

    breadth = {
        "adv": adv,
        "dec": dec,
        "unch": unch,
        "total": int(valid.sum()),
        "pct_above_50": _f(above50.mean() * 100, 1),
        "pct_above_200": _f(above200.mean() * 100, 1),
        "new_highs": int(((last >= hi52 * 0.9999) & last.notna()).sum()),
        "new_lows": int(((last <= lo52 * 1.0001) & last.notna()).sum()),
        "equal_weight_ret": _f(ret.mean() * 100, 3),
        "median_ret": _f(ret.median() * 100, 3),
        "rsp_ret": _f(rsp_ret, 3),
        "ad_daily": ad_daily,
    }

    # ---- Concentración -------------------------------------------------- #
    mag = df[df["ticker"].isin(MAG7)]
    top10w = df.sort_values("weight", ascending=False).head(10)
    concentration = {
        "mag7_weight": _f(mag["weight"].sum(), 2),
        "mag7_contrib": _f(mag["contrib"].sum(), 4),
        "mag7_pts": _f(mag["pts"].sum(), 2),
        "rest_contrib": _f(model_ret - mag["contrib"].sum(), 4),
        "rest_pts": _f(df["pts"].sum() - mag["pts"].sum(), 2),
        "top10_weight": _f(top10w["weight"].sum(), 2),
        "top10_contrib": _f(top10w["contrib"].sum(), 4),
        "top10_pts": _f(top10w["pts"].sum(), 2),
        "top10_tickers": top10w["ticker"].tolist(),
    }

    # ---- Contexto ------------------------------------------------------- #
    context = []
    for t, name, kind in CONTEXT:
        if t not in close.columns:
            continue
        s = close[t].dropna()
        s = s.loc[:today]
        if len(s) < 2:
            continue
        a, b = float(s.iloc[-2]), float(s.iloc[-1])
        if kind == "pct":
            chg = (b / a - 1) * 100
        elif kind == "bps":
            chg = (b - a) * 100  # ^TNX cotiza la tasa en %, x100 = pb
        else:
            chg = b - a
        context.append({"t": t, "n": name, "kind": kind, "last": _f(b, 3), "chg": _f(chg, 3)})

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    closed = today.date() < now_ny.date() or (now_ny.hour, now_ny.minute) >= (16, 0)

    result = {
        "date": today.strftime("%Y-%m-%d"),
        "session": "closed" if closed else "open",
        "prev_date": prev.strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "holdings_as_of": holdings_as_of,
        "spy_ret": _f(spy_ret, 3),
        "model_ret": _f(model_ret, 3),
        "residual": _f(spy_ret - model_ret, 3),
        "spx_last": _f(spx_last, 2),
        "spx_prev": _f(spx_prev, 2),
        "spx_pts": _f(spx_last - spx_prev, 2) if spx_last and spx_prev else None,
        "model_pts": _f(df["pts"].sum(), 2),
        "n_components": int(len(df)),
        "missing": missing,
        "components": comps,
        "sectors": sectors,
        "themes": themes,
        "themes_info": themes_info,
        "breadth": breadth,
        "concentration": concentration,
        "context": context,
    }
    result["summary"] = build_summary(result)
    return result


# --------------------------------------------------------------------------- #
# Resumen en texto
# --------------------------------------------------------------------------- #
def _num(x, nd=2, sign=True):
    if x is None:
        return "s/d"
    s = f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"
    return s.replace(".", ",")


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " y " + items[-1]


def _who(cs: list[dict]) -> str:
    return _join([f"{c['t']} ({_num(c['p'], 1)} pts)" for c in cs])


def build_summary(d: dict) -> list[str]:
    """Dos párrafos: qué movió al índice, y cómo se movió el resto del mercado."""
    live = d.get("session") == "open"
    spy = d["spy_ret"] or 0.0
    up = spy >= 0
    sign = 1 if up else -1
    T = lambda pres, past: pres if live else past  # noqa: E731

    # ---- Párrafo 1: qué empujó ------------------------------------------ #
    mov = T("sube" if up else "baja", "subió" if up else "bajó")
    pts = d["spx_pts"] if d["spx_pts"] is not None else d["model_pts"]
    p1 = [f"El SPY {mov} {_num(abs(spy), 2, False)}% ({_num(pts, 1)} puntos del S&P 500)."]

    comps = [c for c in d["components"] if c["p"] is not None]
    secs = [s for s in d["sectors"] if s["p"] is not None and s["s"] != "Sin sector"]
    with_move = sorted([s for s in secs if s["p"] * sign > 0], key=lambda s: -abs(s["p"]))
    if with_move:
        drv = with_move[0]
        leaders = sorted([c for c in comps if c["s"] == drv["s"] and c["p"] * sign > 0],
                         key=lambda c: -abs(c["p"]))[:3]
        verbo_sec = T("suma", "sumó") if up else T("resta", "restó")
        frase = (
            f"Lo que más empujó {'al alza' if up else 'a la baja'} {T('es', 'fue')} el sector "
            f"{drv['s']}, que {verbo_sec} {_num(abs(drv['p']), 1, False)} pts"
        )
        if leaders:
            frase += f", liderado por {_who(leaders)}"
        p1.append(frase + ".")
        if len(with_move) > 1 and abs(with_move[1]["p"]) >= 0.3 * abs(drv["p"]):
            s2 = with_move[1]
            p1.append(f"También {verbo_sec} {s2['s']} ({_num(s2['p'], 1)} pts).")
    contra = sorted([c for c in comps if c["p"] * sign < 0], key=lambda c: -abs(c["p"]))[:2]
    if contra:
        plural = len(contra) > 1
        ser = T("son" if plural else "es", "fueron" if plural else "fue")
        p1.append(f"Lo que más empujó {'a la baja' if up else 'al alza'} {ser} {_who(contra)}.")

    # ---- Párrafo 2: concentración, amplitud, RSP, VIX ------------------- #
    p2 = []
    c = d["concentration"]
    if c["mag7_pts"] is not None:
        p2.append(f"Las 7 magníficas {T('explican', 'explicaron')} {_num(c['mag7_pts'], 1)} pts del movimiento "
                  f"y el resto del índice {_num(c['rest_pts'], 1)} pts.")

    ths = [t for t in d.get("themes", []) if not t.get("agg") and t.get("p") is not None]
    if ths:
        a_favor = sorted([t for t in ths if t["p"] * sign > 0], key=lambda t: -abs(t["p"]))
        en_contra = sorted([t for t in ths if t["p"] * sign < 0], key=lambda t: -abs(t["p"]))
        partes = []
        if a_favor:
            t = a_favor[0]
            partes.append(f"la que más empujó {'al alza' if up else 'a la baja'} {T('es', 'fue')} {t['n']} "
                          f"({_num(t['p'], 1)} pts)")
        if en_contra:
            t = en_contra[0]
            partes.append(f"la que más empujó {'a la baja' if up else 'al alza'}, {t['n']} ({_num(t['p'], 1)} pts)")
        if partes:
            p2.append("Por temática, " + " y ".join(partes) + ".")

    b = d["breadth"]
    if b["total"]:
        pct_up = b["adv"] / b["total"] * 100
        acomp = pct_up if up else 100 - pct_up  # % de acciones que van en la dirección del índice
        frase = (f"{b['adv']} acciones {T('suben', 'subieron')} y {b['dec']} {T('bajan', 'bajaron')} "
                 f"({_num(pct_up, 0, False)}% en alza)")
        if acomp >= 65:
            p2.append(f"La amplitud {T('es', 'fue')} amplia: {frase}, así que el movimiento {T('es', 'fue')} generalizado.")
        elif acomp >= 45:
            p2.append(f"La amplitud {T('es', 'fue')} mixta: {frase}.")
        else:
            p2.append(f"La amplitud {T('es', 'fue')} angosta: {frase}. El índice {mov} aunque la mayoría de las "
                      f"acciones {T('va', 'fue')} para el otro lado.")

    if b["rsp_ret"] is not None:
        spread = spy - b["rsp_ret"]
        a, r = f"SPY ({_num(spy)}%)", f"equal weight RSP ({_num(b['rsp_ret'])}%)"
        if abs(spread) < 0.15:
            p2.append(f"El {a} y el {r} {T('se mueven', 'se movieron')} parecido, sin un sesgo claro hacia las grandes.")
        elif spread > 0:
            p2.append(f"El {a} le {T('gana', 'ganó')} al {r} por {_num(spread, 2, False)} pp, señal de que las "
                      f"empresas grandes {T('empujan', 'empujaron')} más que el promedio de las acciones.")
        else:
            p2.append(f"El {r} le {T('gana', 'ganó')} al {a} por {_num(-spread, 2, False)} pp, señal de que el "
                      f"promedio de las acciones {T('rinde', 'rindió')} mejor que las grandes.")

    vix = next((x for x in d["context"] if x["t"] == "^VIX"), None)
    if vix and vix["chg"] is not None:
        verbo = T("sube", "subió") if vix["chg"] > 0 else T("baja", "bajó")
        p2.append(f"El VIX {verbo} {_num(abs(vix['chg']), 2, False)} puntos, a {_num(vix['last'], 2, False)}.")

    return [" ".join(p1)] + p2


# --------------------------------------------------------------------------- #
# Guardado
# --------------------------------------------------------------------------- #
def save(result: dict, out_dir: Path = OUT_DIR) -> None:
    hist = out_dir / "history"
    hist.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "latest.json").write_text(payload, encoding="utf-8")
    (hist / f"{result['date']}.json").write_text(payload, encoding="utf-8")
    dates = sorted((p.stem for p in hist.glob("*.json")), reverse=True)
    (out_dir / "dates.json").write_text(json.dumps(dates), encoding="utf-8")


def main() -> int:
    holdings, as_of = load_holdings()
    print(f"Holdings SSGA: {len(holdings)} acciones (as of {as_of})")
    holdings = apply_sectors(holdings, load_sectors())
    sin = int((holdings["sector"] == "Sin sector").sum())
    print(f"Sectores asignados; sin sector: {sin}")
    tickers = sorted(set(holdings["yf"]) | {t for t, _, _ in CONTEXT})
    close = load_prices(tickers)
    result = compute(holdings, as_of, close)
    save(result)
    print(
        f"{result['date']}: SPY {result['spy_ret']}% | modelo {result['model_ret']}% | "
        f"residuo {result['residual']} pp | sin precio: {len(result['missing'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

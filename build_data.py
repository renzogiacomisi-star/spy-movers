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
from pathlib import Path

import numpy as np
import pandas as pd

SSGA_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
OUT_DIR = Path(__file__).resolve().parent.parent / "site" / "data"

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
    df["sector"] = df["sector"].fillna("Sin sector").astype(str).str.strip()
    df["sector"] = df["sector"].map(lambda x: SECTOR_ES.get(x, x))
    df["name"] = df["name"].astype(str).str.strip()
    # Por si un ticker aparece repetido: se suman los pesos.
    df = df.groupby("yf", as_index=False, sort=False).agg(
        name=("name", "first"), ticker=("ticker", "first"),
        weight=("weight", "sum"), sector=("sector", "first"),
    )
    return df[["name", "ticker", "weight", "sector", "yf"]], as_of


def load_prices(tickers: list[str]) -> pd.DataFrame:
    import yfinance as yf

    data = yf.download(
        tickers,
        period="13mo",
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

    # ---- Componentes ---------------------------------------------------- #
    comps = []
    for yf_t, r in df.sort_values("contrib", ascending=False).iterrows():
        comps.append(
            {
                "t": r["ticker"],
                "n": r["name"],
                "s": r["sector"],
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

    daily = stocks.pct_change(fill_method=None).iloc[-60:]
    ad_net = ((daily > 0).sum(axis=1) - (daily < 0).sum(axis=1))
    ad_line = ad_net.cumsum()

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
        "ad_line": [{"d": d.strftime("%Y-%m-%d"), "v": int(v)} for d, v in ad_line.items()],
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

    result = {
        "date": today.strftime("%Y-%m-%d"),
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


def build_summary(d: dict) -> list[str]:
    out = []
    spy, pts = d["spy_ret"], d["model_pts"]
    verbo = "subió" if (spy or 0) >= 0 else "bajó"
    head = f"El SPY {verbo} {_num(abs(spy or 0), 2, False)}%"
    if d["spx_pts"] is not None:
        head += f" y el S&P 500 se movió {_num(d['spx_pts'], 1)} puntos"
    out.append(head + ".")

    comps = d["components"]
    pos = [c for c in comps if (c["p"] or 0) > 0][:3]
    neg = sorted([c for c in comps if (c["p"] or 0) < 0], key=lambda c: c["p"])[:3]
    if pos:
        out.append(
            "Lo que más sumó: "
            + ", ".join(f"{c['t']} ({_num(c['p'], 1)} pts)" for c in pos)
            + "."
        )
    if neg:
        out.append(
            "Lo que más restó: "
            + ", ".join(f"{c['t']} ({_num(c['p'], 1)} pts)" for c in neg)
            + "."
        )

    secs = d["sectors"]
    if secs:
        best, worst = secs[0], secs[-1]
        if (worst["p"] or 0) < 0:
            out.append(
                f"Por sector, {best['s']} fue el que más aportó ({_num(best['p'], 1)} pts) "
                f"y {worst['s']} el que más restó ({_num(worst['p'], 1)} pts)."
            )
        else:
            out.append(
                f"Todos los sectores sumaron: {best['s']} el que más ({_num(best['p'], 1)} pts) "
                f"y {worst['s']} el que menos ({_num(worst['p'], 1)} pts)."
            )

    c = d["concentration"]
    if c["mag7_pts"] is not None and pts:
        out.append(
            f"Las Mag 7 explicaron {_num(c['mag7_pts'], 1)} pts; "
            f"el resto del índice, {_num(c['rest_pts'], 1)} pts."
        )

    b = d["breadth"]
    pct_up = b["adv"] / b["total"] * 100 if b["total"] else 0
    line = f"Amplitud: {b['adv']} acciones subieron y {b['dec']} bajaron ({_num(pct_up, 0, False)}% en alza)."
    if spy is not None:
        if spy > 0 and pct_up < 45:
            line += " Suba angosta: el índice sube con la mayoría de las acciones en baja."
        elif spy < 0 and pct_up > 55:
            line += " Baja concentrada: el índice cae aunque la mayoría de las acciones sube."
    out.append(line)

    if b["rsp_ret"] is not None and spy is not None:
        spread = spy - b["rsp_ret"]
        quien = "el índice ponderado por capitalización" if spread > 0 else "el equal weight"
        out.append(
            f"SPY {_num(spy)}% contra RSP {_num(b['rsp_ret'])}%: ganó {quien} por "
            f"{_num(abs(spread), 2, False)} pp."
        )

    vix = next((x for x in d["context"] if x["t"] == "^VIX"), None)
    if vix and vix["chg"] is not None:
        out.append(f"VIX en {_num(vix['last'], 2, False)} ({_num(vix['chg'], 2)} pts).")
    return out


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

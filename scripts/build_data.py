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
CHANGES_CACHE = OUT_DIR / "changes.json"
HIST_YEARS = 2.2          # cuántos años hacia atrás se reconstruye la composición
_CHANGES: list[dict] | None = None
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

# Índices para la sección de mercado: (ticker, nombre, ETF de respaldo)
INDICES = [
    ("^GSPC", "S&P 500", "SPY"),
    ("^SP400", "S&P MidCap 400", "IJH"),
    ("^SP600", "S&P SmallCap 600", "IJR"),
    ("^NDX", "Nasdaq 100", "QQQ"),
    ("^DJI", "Dow Jones Industrial", "DIA"),
    ("URTH", "MSCI World", "URTH"),
]
# Tasas en vivo: (ticker Yahoo, nombre, clave de la serie oficial)
LIVE_RATES = [
    ("2YY=F", "2 años", "2"),
    ("^FVX", "5 años", "5"),
    ("^TNX", "10 años", "10"),
    ("^TYX", "30 años", "30"),
]
TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                "daily-treasury-rates.csv")
RATES_CACHE = OUT_DIR / "rates_official.csv"
PERIODS = {"1S": 5, "1M": 21, "3M": 63, "6M": 126, "1A": 252}


def all_tickers(holdings: pd.DataFrame, extra_stocks: list[str] | None = None) -> list[str]:
    extra = {t for t, _, _ in CONTEXT} | {t for t, _, _ in INDICES} | {p for _, _, p in INDICES} | {t for t, _, _ in LIVE_RATES}
    return sorted(set(holdings["yf"]) | extra | set(extra_stocks or []))


# Clima del día: cíclico vs defensivo y escenarios. Umbrales editables.
REGIME = {
    "defensivos": ["Consumo básico", "Salud", "Utilities"],
    "spread_claro": 0.75,   # pp entre promedio cíclico y defensivo
    "spread_leve": 0.30,
    "ftq_tasa_bps": -5.0,   # fly to quality: la tasa a 10 años baja al menos 5 pb
    "ftq_vix": 1.5,         # el VIX sube al menos 1,5 pts
    "ftq_spy": -0.5,        # el SPY baja al menos 0,5%
    "ftq_oro": 0.5,         # el oro sube al menos 0,5%
    "ron_tasa_bps": 3.0,    # risk on: la tasa sube al menos 3 pb
    "ron_vix": -1.0,        # el VIX baja al menos 1 pt
    "ron_spy": 0.5,         # el SPY sube al menos 0,5%
    "ron_iwm": 0.5,         # el Russell 2000 le gana al SPY por al menos 0,5 pp
    "tasas_bps": 7.0,       # presión de tasas: la tasa sube al menos 7 pb con el SPY en baja
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

        r = requests.get(WIKI_URL, headers={"User-Agent": "spy-movers/1.0 (GitHub Actions)"}, timeout=(5, 30))
        r.raise_for_status()
        table = pd.read_html(io.StringIO(r.text), attrs={"id": "constituents"})[0]
        added_col = next((c for c in table.columns if "added" in str(c).lower()), None)
        m = {
            str(row["Symbol"]).strip().upper(): {
                "s": str(row["GICS Sector"]).strip(),
                "sub": str(row["GICS Sub-Industry"]).strip(),
                "added": _iso(row[added_col]) if added_col is not None else None,
            }
            for _, row in table.iterrows()
        }
        global _CHANGES
        try:
            _CHANGES = parse_changes(r.text)
            if len(_CHANGES) > 50:
                CHANGES_CACHE.parent.mkdir(parents=True, exist_ok=True)
                CHANGES_CACHE.write_text(json.dumps(_CHANGES), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"Aviso: no pude leer la tabla de cambios de Wikipedia ({e}); uso el caché.")
            _CHANGES = None
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


def _iso(x) -> str | None:
    d = pd.to_datetime(str(x).split("(")[0].strip(), errors="coerce")
    return None if pd.isna(d) else d.strftime("%Y-%m-%d")


def _clean_ticker(x) -> str | None:
    t = re.sub(r"\[.*?\]", "", str(x)).strip().upper()
    if pd.isna(x) if not isinstance(x, str) else False:
        return None
    return t if t not in {"NAN", "NONE", "NA"} and re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", t) else None


def parse_changes(html: str) -> list[dict]:
    """Altas y bajas del S&P 500 con fecha efectiva, desde la tabla de cambios de Wikipedia."""
    try:
        tables = pd.read_html(io.StringIO(html), attrs={"id": "changes"})
    except ValueError:
        tables = [t for t in pd.read_html(io.StringIO(html))
                  if {"added", "removed"} <= {str(x).lower() for c in t.columns for x in (c if isinstance(c, tuple) else (c,))}]
    if not tables:
        raise RuntimeError("no encontré la tabla de cambios")
    t = tables[0]
    names = [" ".join(str(x) for x in (c if isinstance(c, tuple) else (c,))).lower() for c in t.columns]
    find = lambda *keys: next(i for i, n in enumerate(names) if all(k in n for k in keys))  # noqa: E731
    i_d, i_a, i_r = find("date"), find("added", "ticker"), find("removed", "ticker")
    out = []
    for _, row in t.iterrows():
        d = _iso(row.iloc[i_d])
        a, r = _clean_ticker(row.iloc[i_a]), _clean_ticker(row.iloc[i_r])
        if d and (a or r):
            out.append({"d": d, "add": a, "rem": r})
    return out


def load_changes() -> list[dict]:
    if _CHANGES:
        return _CHANGES
    if CHANGES_CACHE.exists():
        try:
            return json.loads(CHANGES_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return []


def history_tickers(holdings: pd.DataFrame, changes: list[dict]) -> list[str]:
    """Empresas que salieron del índice en el período reconstruido (para bajar sus precios)."""
    cut = (pd.Timestamp.now() - pd.Timedelta(days=int(HIST_YEARS * 365))).strftime("%Y-%m-%d")
    cur = set(holdings["yf"])
    out = {c["rem"].replace(".", "-") for c in changes if c["rem"] and c["d"] >= cut}
    return sorted(out - cur)


def membership(index: pd.DatetimeIndex, cols: list[str], current: set[str], changes: list[dict],
               sectors: dict | None = None) -> tuple[pd.DataFrame, pd.Series]:
    """Matriz días × tickers (True si la empresa estaba en el índice ese día) y cantidad real de miembros por día,
    incluidas las empresas sin precios."""
    ch = sorted(({"d": pd.Timestamp(c["d"]), "add": (c["add"] or "").replace(".", "-") or None,
                  "rem": (c["rem"] or "").replace(".", "-") or None} for c in changes), key=lambda c: c["d"], reverse=True)
    in_changes = {c["add"] for c in ch if c["add"]} | {c["rem"] for c in ch if c["rem"]}
    added = {}
    for t, info in (sectors or {}).items():
        yt = t.replace(".", "-")
        if info.get("added") and yt in current and yt not in in_changes:
            added[yt] = pd.Timestamp(info["added"])
    pos = {c: i for i, c in enumerate(cols)}
    mask = np.zeros((len(index), len(cols)), dtype=bool)
    count = np.zeros(len(index), dtype=int)
    members, k = set(current), 0
    for row in range(len(index) - 1, -1, -1):
        d = index[row]
        while k < len(ch) and ch[k]["d"] > d:  # el cambio fue después de este día: se deshace
            if ch[k]["add"]:
                members.discard(ch[k]["add"])
            if ch[k]["rem"]:
                members.add(ch[k]["rem"])
            k += 1
        for t in members:
            if t in added and added[t] > d:
                continue
            count[row] += 1
            if t in pos:
                mask[row, pos[t]] = True
    return pd.DataFrame(mask, index=index, columns=cols), pd.Series(count, index=index)


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


def load_official_rates() -> pd.DataFrame | None:
    """Tasas oficiales de cierre (Tesoro de EE.UU.; respaldo FRED; si no, la última copia guardada)."""
    import requests

    from concurrent.futures import ThreadPoolExecutor

    hdr = {"User-Agent": "Mozilla/5.0 (compatible; spy-movers/1.0)"}
    cols = {"1 Yr": "1", "2 Yr": "2", "5 Yr": "5", "10 Yr": "10", "30 Yr": "30"}
    y = datetime.now(timezone.utc).year
    to = (5, 20)  # conexión, lectura: si no responde rápido, se pasa a la alternativa

    def treasury(year):
        url = f"{TREASURY_URL}/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
        r = requests.get(url, headers=hdr, timeout=to)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text)) if r.text.strip() else None

    try:
        with ThreadPoolExecutor(3) as pool:
            frames = [f for f in pool.map(treasury, (y - 2, y - 1, y)) if f is not None]
        df = pd.concat(frames)
        df["Date"] = pd.to_datetime(df["Date"])
        out = df.set_index("Date")[list(cols)].rename(columns=cols).apply(pd.to_numeric, errors="coerce")
        out = out[~out.index.duplicated()].sort_index().dropna(how="all")
        if len(out) > 200:
            RATES_CACHE.parent.mkdir(parents=True, exist_ok=True)
            out.to_csv(RATES_CACHE)
            out.attrs["fuente"] = "Tesoro de EE.UU."
            return out
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude leer las tasas del Tesoro ({e}); pruebo FRED.")
    try:
        def fred(k):
            r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS{k}", headers=hdr, timeout=to)
            r.raise_for_status()
            f = pd.read_csv(io.StringIO(r.text))
            f.columns = ["Date", k]
            return k, pd.to_numeric(f.set_index(pd.to_datetime(f["Date"]))[k], errors="coerce")

        with ThreadPoolExecutor(5) as pool:
            series = dict(pool.map(fred, ("1", "2", "5", "10", "30")))
        out = pd.DataFrame(series).dropna(how="all")
        out = out[out.index >= out.index.max() - pd.Timedelta(days=800)]
        out.attrs["fuente"] = "FRED (Reserva Federal de St. Louis)"
        out.to_csv(RATES_CACHE)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude leer FRED ({e}); uso la copia guardada.")
    if RATES_CACHE.exists():
        out = pd.read_csv(RATES_CACHE, index_col=0, parse_dates=True)
        out.columns = [str(c) for c in out.columns]
        out.attrs["fuente"] = "Tesoro de EE.UU. (copia guardada)"
        return out
    return None


def load_last_trade_times(tickers: list[str]) -> dict:
    """Hora (UTC) del último dato intradía de cada ticker, para mostrar qué tan fresco es."""
    import yfinance as yf

    try:
        data = yf.download(tickers, period="1d", interval="5m", progress=False, group_by="column", threads=True)
        close = data["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(tickers[0])
        out = {}
        for t in tickers:
            if t in close.columns and close[t].notna().any():
                ts = close[t].dropna().index[-1]
                ts = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
                out[t] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no pude leer la hora de los últimos datos ({e}).")
        return {}


def load_prices(tickers: list[str], period: str = "2y") -> pd.DataFrame:
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


def compute(holdings: pd.DataFrame, holdings_as_of: str | None, close: pd.DataFrame,
            official_rates: pd.DataFrame | None = None, last_trade: dict | None = None,
            changes: list[dict] | None = None, wiki: dict | None = None) -> dict:
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

    spx_d = close["^GSPC"].reindex(stocks.index).ffill() if "^GSPC" in close.columns else None
    gone = [t for t in history_tickers(holdings, changes or []) if t in px.columns]
    univ = px[tick + gone]
    mask, nmem = membership(univ.index, list(univ.columns), set(tick), changes or [], wiki)
    series, series_info = breadth_series(univ, mask, spx_d, members=nmem)
    series_info["salidas"] = len(history_tickers(holdings, changes or []))
    series_info["salidas_con_precio"] = len(gone)
    series_info["historica"] = bool(changes)

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
        "series": series,
        "series_info": series_info,
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
    try:
        from extras import session_times
        st_today = session_times(today.date())
        close_t = st_today[1] if st_today else None
    except Exception:  # noqa: BLE001
        close_t = None
    if close_t is None:
        close_t = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    closed = today.date() < now_ny.date() or now_ny >= close_t

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
        "spx_ret": _f((spx_last / spx_prev - 1) * 100, 3) if spx_last and spx_prev else None,
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
    result["regime"] = compute_regime(sectors, context, result["spy_ret"])
    result["breadth_read"] = breadth_read(series, series_info.get("bands"))
    result["markets"] = compute_markets(close, days)
    result["rates"] = compute_rates(close, official_rates, last_trade)
    result["summary"] = build_summary(result)
    return result


def breadth_series(px: pd.DataFrame, mask: pd.DataFrame, spx: pd.Series | None, n: int = 252,
                   members: pd.Series | None = None) -> tuple[list[dict], dict]:
    """Serie diaria del último año con la composición que tenía el índice cada día.

    px: precios de todas las empresas (actuales y las que salieron). mask: True si estaba en el índice ese día.
    """
    daily = px.pct_change(fill_method=None)
    sma50 = px.rolling(50, min_periods=45).mean()
    sma200 = px.rolling(200, min_periods=180).mean()
    hi = px.rolling(252, min_periods=200).max()
    lo = px.rolling(252, min_periods=200).min()
    have = px.notna() & mask
    adv = ((daily > 0) & mask).sum(axis=1)
    dec = ((daily < 0) & mask).sum(axis=1)
    ok50, ok200 = sma50.notna() & have, sma200.notna() & have
    p50 = ((px > sma50) & ok50).sum(axis=1) / ok50.sum(axis=1).replace(0, np.nan) * 100
    p200 = ((px > sma200) & ok200).sum(axis=1) / ok200.sum(axis=1).replace(0, np.nan) * 100
    okh = hi.notna() & have
    nh = ((px >= hi * 0.9999) & okh).sum(axis=1)
    nl = ((px <= lo * 1.0001) & okh).sum(axis=1)
    nn = (nh - nl) / okh.sum(axis=1).replace(0, np.nan) * 100   # máximos netos, % de las empresas
    nnm = nn.rolling(10, min_periods=5).mean()
    nmem = members if members is not None else mask.sum(axis=1)
    cov = have.sum(axis=1) / nmem.replace(0, np.nan) * 100

    idx = px.index[-n:]
    out = [{
        "d": d.strftime("%Y-%m-%d"),
        "a": int(adv[d]), "b": int(dec[d]),
        "x": _f(spx.get(d), 2) if spx is not None else None,
        "p50": _f(p50[d], 1), "p200": _f(p200[d], 1),
        "nh": int(nh[d]), "nl": int(nl[d]),
        "nn": _f(nn[d], 2), "nhm": _f(nnm[d], 2),
        "m": int(nmem[d]), "cov": _f(cov[d], 1),
    } for d in idx]

    hist = nnm.loc[idx].dropna()
    bands = {"hi": _f(hist.quantile(0.8), 2), "lo": _f(hist.quantile(0.2), 2), "dias": int(len(hist))} if len(hist) > 40 else None
    info = {"bands": bands, "cov_min": _f(cov.loc[idx].min(), 1), "cov_avg": _f(cov.loc[idx].mean(), 1)}
    return out, info


def breadth_read(series: list[dict], bands: dict | None = None) -> dict | None:
    """Lectura automática de la amplitud con los tres indicadores (horizonte de 20 ruedas)."""
    if len(series) < 25:
        return None
    now, ago = series[-1], series[-21]

    def chg(k):
        return (now[k] - ago[k]) if now.get(k) is not None and ago.get(k) is not None else None

    spx20 = (now["x"] / ago["x"] - 1) * 100 if now.get("x") and ago.get("x") else None
    ad20 = sum(x["a"] - x["b"] for x in series[-20:])
    d50, d200, dnh = chg("p50"), chg("p200"), chg("nhm")

    def zone(v, strong, weak=20):
        if v is None:
            return None, 0
        if v >= strong:
            return "fuerte", 2
        if v >= 50:
            return "por encima de neutral", 1
        if v > weak:
            return "por debajo de neutral", -1
        return "débil", -2

    z50, s50 = zone(now.get("p50"), 70)
    z200, s200 = zone(now.get("p200"), 70)
    def zone_nn(v):
        if v is None or not bands:
            return None, 0
        if v >= bands["hi"]:
            return "fuerte", 2
        if v > 0:
            return "por encima de neutral", 1
        if v > bands["lo"]:
            return "por debajo de neutral", -1
        return "débil", -2

    znh, snh = zone_nn(now.get("nhm"))
    nn_step = max(1.0, (bands["hi"] - bands["lo"]) * 0.3) if bands else 3.0

    def trend(v, step):
        if v is None:
            return "sin datos"
        return "mejorando" if v >= step else "empeorando" if v <= -step else "estable"

    items = [
        {"k": "p50", "titulo": "Acciones sobre su media de 50 ruedas (corto plazo)", "v": now.get("p50"), "u": "%",
         "zona": z50, "tend": trend(d50, 5), "d": _f(d50, 1)},
        {"k": "p200", "titulo": "Acciones sobre su media de 200 ruedas (largo plazo)", "v": now.get("p200"), "u": "%",
         "zona": z200, "tend": trend(d200, 3), "d": _f(d200, 1)},
        {"k": "nhm", "titulo": "Máximos netos de 52 semanas, % de las empresas (media de 10 ruedas)", "v": now.get("nhm"), "u": "%",
         "zona": znh, "tend": trend(dnh, nn_step), "d": _f(dnh, 1)},
        {"k": "ad", "titulo": "Línea de avance y declive, últimas 20 ruedas", "v": ad20, "u": "",
         "zona": "en suba" if ad20 > 0 else "en baja", "tend": "mejorando" if ad20 > 0 else "empeorando", "d": None},
    ]

    worse = sum([d50 is not None and d50 <= -5, ad20 < 0, dnh is not None and dnh <= -nn_step])
    better = sum([d50 is not None and d50 >= 5, ad20 > 0, dnh is not None and dnh >= nn_step])
    score = (s50 + s200 + snh) / 3
    # La tendencia de las últimas 20 ruedas también pesa
    if worse >= 2:
        score -= 0.8
    elif better >= 2:
        score += 0.8

    if spx20 is not None and spx20 > 1 and worse >= 2:
        tipo, titulo = "neg", "Divergencia negativa (mala amplitud)"
        texto = (f"El S&P 500 subió {spx20:.1f}% en las últimas 20 ruedas, pero cada vez menos acciones acompañan. "
                 "La suba se apoya en pocas empresas y puede estar perdiendo fuerza.")
    elif spx20 is not None and spx20 < -1 and better >= 2:
        tipo, titulo = "pos", "Divergencia positiva (buena amplitud)"
        texto = (f"El S&P 500 bajó {abs(spx20):.1f}% en las últimas 20 ruedas, pero cada vez más acciones mejoran. "
                 "Puede ser una señal temprana de recuperación.")
    elif score >= 1.3:
        tipo, titulo = "pos", "Amplitud fuerte"
        texto = "La mayoría de las acciones está en tendencia alcista de corto y largo plazo: el mercado sube con participación amplia."
    elif score >= 0.3:
        tipo, titulo = "pos", "Amplitud sana"
        texto = "Más de la mitad de las acciones acompaña la tendencia, sin llegar a niveles de fuerza extrema."
    elif score <= -1.3:
        tipo, titulo = "neg", "Amplitud débil"
        texto = "La mayoría de las acciones está por debajo de sus medias y predominan los mínimos: el mercado está débil por dentro."
    elif score <= -0.3:
        tipo, titulo = "neg", "Amplitud floja"
        texto = "Menos de la mitad de las acciones acompaña: la participación es baja."
    else:
        tipo, titulo = "neu", "Amplitud neutral"
        texto = "Los indicadores están cerca de la zona neutral, sin una señal clara."

    if tipo not in ("neg",) and worse >= 2 and not titulo.startswith("Divergencia"):
        texto += " Ojo: en las últimas 20 ruedas viene empeorando, con menos acciones sobre su media de 50 y más declives que avances."
    elif tipo != "pos" and better >= 2 and not titulo.startswith("Divergencia"):
        texto += " Aun así, en las últimas 20 ruedas viene mejorando."
    elif titulo in ("Amplitud débil", "Amplitud floja") and worse >= 2:
        texto += " Y en las últimas 20 ruedas sigue empeorando."
    if tipo != "neg" and now.get("p50") is not None and now["p50"] <= 25 and (d50 or 0) >= 5:
        texto += " Además, el porcentaje sobre la media de 50 viene de niveles muy bajos y está subiendo, algo que suele aparecer cerca de pisos."
    return {"tipo": tipo, "titulo": titulo, "texto": texto, "items": items, "spx20": _f(spx20, 2)}


def rsi14(s: pd.Series) -> float | None:
    s = s.dropna()
    if len(s) < 30:
        return None
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = up.iloc[-1] / dn.iloc[-1] if dn.iloc[-1] else None
    return 100 - 100 / (1 + rs) if rs is not None else 100.0


def period_changes(s: pd.Series, mode: str) -> dict:
    """Variaciones contra cierres anteriores. mode 'pct' (precios) o 'bps' (tasas)."""
    s = s.dropna()
    if len(s) < 2:
        return {}
    last = s.iloc[-1]
    f = (lambda a: (last / a - 1) * 100) if mode == "pct" else (lambda a: (last - a) * 100)
    out = {"1D": _f(f(s.iloc[-2]), 2)}
    for k, n in PERIODS.items():
        out[k] = _f(f(s.iloc[-1 - n]), 2) if len(s) > n else None
    prev_year = s[s.index.year < s.index[-1].year]
    out["YTD"] = _f(f(prev_year.iloc[-1]), 2) if len(prev_year) else None
    return out


def compute_markets(close: pd.DataFrame, days: pd.DatetimeIndex) -> dict:
    rows, hist, proxies = [], {}, []
    for t, name, proxy in INDICES:
        s = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
        used = t
        if len(s) < 260 and proxy in close.columns:
            s, used = close[proxy].dropna(), proxy
            proxies.append(f"{name} ({proxy})")
        if len(s) < 2:
            continue
        rows.append({"t": used, "n": name, "proxy": used != t or not used.startswith("^"), "last": _f(s.iloc[-1], 2),
                     "chg": period_changes(s, "pct"), "rsi": _f(rsi14(s), 1)})
        al = s.reindex(days).ffill()
        hist[name] = [_f(v, 2) for v in al.iloc[-260:]]
    return {"rows": rows, "dates": [d.strftime("%Y-%m-%d") for d in days[-260:]], "hist": hist, "proxies": proxies}


def compute_rates(close: pd.DataFrame, official: pd.DataFrame | None, last_trade: dict | None) -> dict:
    live = []
    last_day = close.index[-1] if len(close.index) else None

    def official_row(name, k):
        if official is None or k not in official.columns:
            return None
        so = official[k].dropna()
        if len(so) < 2:
            return None
        return {"t": None, "n": name, "k": k, "v": _f(so.iloc[-1], 3), "d1": _f((so.iloc[-1] - so.iloc[-2]) * 100, 1),
                "fecha": so.index[-1].strftime("%Y-%m-%d"), "hora": None, "en_vivo": False}

    # 1 año: no hay dato en vivo gratuito; se usa el cierre oficial
    r1 = official_row("1 año", "1")
    if r1:
        live.append(r1)
    for t, name, k in LIVE_RATES:
        s = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
        # Sin datos, o datos viejos (más de 4 días detrás del resto): se usa el cierre oficial
        if len(s) < 2 or (last_day is not None and (last_day - s.index[-1]).days > 4):
            row = official_row(name, k)
            if row:
                live.append(row)
            continue
        live.append({"t": t, "n": name, "k": k, "v": _f(s.iloc[-1], 3), "d1": _f((s.iloc[-1] - s.iloc[-2]) * 100, 1),
                     "fecha": s.index[-1].strftime("%Y-%m-%d"), "hora": (last_trade or {}).get(t), "en_vivo": True})
    off = None
    if official is not None and len(official):
        rows = []
        for t, name, k in [(None, "1 año", "1")] + LIVE_RATES:
            if k in official.columns:
                s = official[k].dropna()
                if len(s) >= 2:
                    rows.append({"n": name, "k": k, "v": _f(s.iloc[-1], 2), "chg": period_changes(s, "bps")})
        tail = official.iloc[-520:]
        off = {"fecha": official.index[-1].strftime("%Y-%m-%d"), "fuente": official.attrs.get("fuente", "Tesoro de EE.UU."),
               "rows": rows, "dates": [d.strftime("%Y-%m-%d") for d in tail.index],
               "hist": {k: [_f(v, 2) for v in tail[k]] for k in tail.columns}}
    return {"live": live, "official": off}


def compute_regime(sectors: list[dict], context: list[dict], spy: float | None) -> dict | None:
    secs = [x for x in sectors if x["r"] is not None and x["s"] != "Sin sector"]
    if not secs or spy is None:
        return None
    R = REGIME
    deff = [x for x in secs if x["s"] in R["defensivos"]]
    cyc = [x for x in secs if x["s"] not in R["defensivos"]]
    if not deff or not cyc:
        return None
    avg = lambda xs: sum(x["r"] for x in xs) / len(xs)  # noqa: E731
    c_avg, d_avg = avg(cyc), avg(deff)
    spread = c_avg - d_avg
    ctx = {x["t"]: x for x in context}
    g = lambda t: ctx[t]["chg"] if t in ctx else None  # noqa: E731
    tasa, vix, oro, iwm = g("^TNX"), g("^VIX"), g("GC=F"), g("IWM")
    iwm_rel = (iwm - spy) if iwm is not None else None

    def sig(label, val, ok, umbral, unidad):
        return {"label": label, "v": _f(val, 2), "ok": bool(ok) if val is not None else False,
                "umbral": umbral, "u": unidad}

    ftq = [
        sig("Defensivos le ganan a cíclicos", -spread, -spread >= R["spread_claro"], f"≥ {R['spread_claro']:.2f} pp".replace(".", ","), "pp"),
        sig("Baja la tasa a 10 años", tasa, tasa is not None and tasa <= R["ftq_tasa_bps"], f"≤ {R['ftq_tasa_bps']:.0f} pb", "pb"),
        sig("Sube el VIX", vix, vix is not None and vix >= R["ftq_vix"], f"≥ +{R['ftq_vix']:.1f} pts".replace(".", ","), "pts"),
        sig("Baja el SPY", spy, spy <= R["ftq_spy"], f"≤ {R['ftq_spy']:.1f}%".replace(".", ","), "%"),
        sig("Sube el oro", oro, oro is not None and oro >= R["ftq_oro"], f"≥ +{R['ftq_oro']:.1f}%".replace(".", ","), "%"),
    ]
    ron = [
        sig("Cíclicos le ganan a defensivos", spread, spread >= R["spread_claro"], f"≥ {R['spread_claro']:.2f} pp".replace(".", ","), "pp"),
        sig("Sube la tasa a 10 años", tasa, tasa is not None and tasa >= R["ron_tasa_bps"], f"≥ +{R['ron_tasa_bps']:.0f} pb", "pb"),
        sig("Baja el VIX", vix, vix is not None and vix <= R["ron_vix"], f"≤ {R['ron_vix']:.1f} pts".replace(".", ","), "pts"),
        sig("Sube el SPY", spy, spy >= R["ron_spy"], f"≥ +{R['ron_spy']:.1f}%".replace(".", ","), "%"),
        sig("Russell 2000 le gana al SPY", iwm_rel, iwm_rel is not None and iwm_rel >= R["ron_iwm"], f"≥ +{R['ron_iwm']:.1f} pp".replace(".", ","), "pp"),
    ]

    def grade(signals):
        if not signals[0]["ok"]:
            return None, sum(x["ok"] for x in signals)
        k = sum(x["ok"] for x in signals)
        return ("claro" if k >= 4 else "posible" if k >= 3 else None), k

    g_ftq, k_ftq = grade(ftq)
    g_ron, k_ron = grade(ron)
    tasas = tasa is not None and spy <= R["ftq_spy"] and tasa >= R["tasas_bps"]

    if g_ftq:
        esc = {"id": "ftq", "nivel": g_ftq, "titulo": f"Fly to quality {'claro' if g_ftq == 'claro' else 'posible'}",
               "texto": "El dinero sale de lo cíclico hacia lo defensivo y hacia los bonos (baja la tasa). "
                        "Es el patrón típico de búsqueda de refugio."}
    elif g_ron:
        esc = {"id": "ron", "nivel": g_ron, "titulo": f"Risk on {'claro' if g_ron == 'claro' else 'posible'}",
               "texto": "Lideran los sectores cíclicos, con menos demanda de cobertura. "
                        "Es el patrón típico de apetito por riesgo."}
    elif tasas:
        esc = {"id": "tasas", "nivel": "claro", "titulo": "Presión de tasas",
               "texto": "El índice cae mientras la tasa a 10 años sube con fuerza: bajan acciones y bonos a la vez. "
                        "No es un fly to quality, porque los bonos no funcionan como refugio."}
    else:
        esc = {"id": "none", "nivel": None, "titulo": "Sin un escenario claro",
               "texto": "Las señales no alcanzan para hablar de fly to quality ni de risk on."}

    if spread >= R["spread_claro"]:
        sesgo = "Predominó lo cíclico"
    elif spread <= -R["spread_claro"]:
        sesgo = "Predominó lo defensivo"
    elif spread >= R["spread_leve"]:
        sesgo = "Leve sesgo cíclico"
    elif spread <= -R["spread_leve"]:
        sesgo = "Leve sesgo defensivo"
    else:
        sesgo = "Sin sesgo entre cíclicos y defensivos"

    return {
        "cyc": _f(c_avg, 3), "def": _f(d_avg, 3), "spread": _f(spread, 3), "sesgo": sesgo,
        "sec_cyc": [{"s": x["s"], "r": x["r"]} for x in sorted(cyc, key=lambda x: -x["r"])],
        "sec_def": [{"s": x["s"], "r": x["r"]} for x in sorted(deff, key=lambda x: -x["r"])],
        "ftq": ftq, "ron": ron, "k_ftq": k_ftq, "k_ron": k_ron,
        "tasa": _f(tasa, 1), "esc": esc,
    }


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
    idx_ret = d.get("spx_ret") if d.get("spx_ret") is not None else spy
    p1 = [f"El S&P 500 {mov} {_num(abs(idx_ret), 2, False)}% ({_num(pts, 1)} puntos)."]

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
            p2.append(f"{T('Hay', 'Hubo')} amplitud: {frase}. El movimiento {'al alza' if up else 'a la baja'} {T('es', 'fue')} generalizado.")
        elif acomp >= 45:
            p2.append(f"No {T('hay', 'hubo')} una amplitud clara: {frase}. El movimiento {T('está', 'estuvo')} repartido, "
                      f"sin una mayoría definida.")
        else:
            p2.append(f"No {T('hay', 'hubo')} amplitud: {frase}. El movimiento {T('está', 'estuvo')} concentrado en pocas "
                      f"empresas: el índice {mov} aunque la mayoría de las acciones {T('va', 'fue')} para el otro lado.")

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
    sectors = load_sectors()
    holdings = apply_sectors(holdings, sectors)
    sin = int((holdings["sector"] == "Sin sector").sum())
    print(f"Sectores asignados; sin sector: {sin}")
    changes = load_changes()
    gone = history_tickers(holdings, changes)
    print(f"Cambios en el índice: {len(changes)}; empresas que salieron en el período: {len(gone)}")
    close = load_prices(all_tickers(holdings, gone))
    result = compute(holdings, as_of, close, load_official_rates(), changes=changes, wiki=sectors)
    si = result["breadth"]["series_info"]
    print(f"Composición histórica: {si['salidas_con_precio']} de {si['salidas']} salidas con precio; "
          f"cobertura mínima {si['cov_min']}%")
    save(result)
    print(
        f"{result['date']}: SPY {result['spy_ret']}% | modelo {result['model_ret']}% | "
        f"residuo {result['residual']} pp | sin precio: {len(result['missing'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

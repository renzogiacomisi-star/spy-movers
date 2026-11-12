"""
Qué movió al SPY, versión Streamlit.

- Holdings, sectores e historia de precios: se descargan como máximo cada 6 horas.
- Últimos precios: se descargan como máximo una vez cada REFRESH_SEC segundos,
  y el resultado se comparte entre todos los usuarios conectados (caché).
- Los días anteriores se leen de site/data/history/, que guarda el workflow de GitHub al cierre.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_data as bd  # noqa: E402

REFRESH_SEC = 120
NY = ZoneInfo("America/New_York")
HIST_DIR = ROOT / "site" / "data" / "history"
PAGE = (ROOT / "site" / "index.html").read_text(encoding="utf-8")

st.set_page_config(page_title="Qué movió al SPY", page_icon="⚖️", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown(
    """<style>
    header[data-testid="stHeader"], footer, #MainMenu { display: none !important; }
    .block-container, [data-testid="stMainBlockContainer"] { padding: 0 !important; max-width: 100% !important; }
    [data-testid="stVerticalBlock"] { gap: 0 !important; }
    iframe { display: block; border: 0; }
    </style>""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# Datos
# --------------------------------------------------------------------------- #
def phase(now: datetime) -> str:
    """pre / open / post / closed, en hora de Nueva York (no contempla feriados)."""
    if now.weekday() >= 5:
        return "closed"
    hm = (now.hour, now.minute)
    if hm < (9, 30):
        return "pre"
    if hm < (16, 5):
        return "open"
    return "post"


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def base_data(day: str):
    holdings, as_of = bd.load_holdings()
    holdings = bd.apply_sectors(holdings, bd.load_sectors())
    tickers = sorted(set(holdings["yf"]) | {t for t, _, _ in bd.CONTEXT})
    hist = bd.load_prices(tickers, period="13mo")
    return holdings, as_of, hist, tickers


@st.cache_data(ttl=None, max_entries=4, show_spinner=False)
def snapshot(day: str, bucket: str) -> dict:
    """Un cálculo completo. `bucket` cambia cada REFRESH_SEC con el mercado abierto."""
    holdings, as_of, hist, tickers = base_data(day)
    if bucket.startswith("open") or bucket == "post":
        live = bd.load_prices(tickers, period="5d")
        close = live.combine_first(hist).sort_index()
    else:
        close = hist
    return bd.compute(holdings, as_of, close)


def history_dates() -> list[str]:
    if not HIST_DIR.exists():
        return []
    return sorted((p.stem for p in HIST_DIR.glob("*.json")), reverse=True)


def read_history(day: str) -> dict | None:
    p = HIST_DIR / f"{day}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def page_html(data: dict, dates: list[str], view: str, live_date: str | None) -> str:
    dump = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")  # noqa: E731
    inject = (
        f"<script>window.__SPY__={dump(data)};window.__SPY_DATES__={dump(dates)};"
        f"window.__SPY_VIEW__={dump(view)};window.__SPY_LIVE_DATE__={dump(live_date)};"
        f"window.__SPY_REFRESH__={REFRESH_SEC};</script>\n"
    )
    return PAGE.replace("<script>\nconst LOC", inject + "<script>\nconst LOC", 1)


# --------------------------------------------------------------------------- #
# Vista
# --------------------------------------------------------------------------- #
def current_view():
    now = datetime.now(NY)
    ph = phase(now)
    day = now.strftime("%Y-%m-%d")
    bucket = f"open-{int(time.time() // REFRESH_SEC)}" if ph == "open" else ph

    dates = history_dates()
    data, warn = None, None
    try:
        with st.spinner("Calculando… la primera carga del día puede tardar hasta un minuto."):
            data = snapshot(day, bucket)
    except Exception as e:  # noqa: BLE001
        warn = f"No se pudieron actualizar los precios ({type(e).__name__}). Se muestra el último cierre guardado."
        data = read_history(dates[0]) if dates else None

    if data is None:
        st.error("No hay datos disponibles todavía. Probá de nuevo en unos minutos.")
        return
    if warn:
        st.warning(warn)
    components.html(page_html(data, dates, "live", data["date"]), height=4200, scrolling=False)


wanted = st.query_params.get("d")
past = read_history(wanted) if wanted else None

if past is not None and past["date"] != datetime.now(NY).strftime("%Y-%m-%d"):
    # Día anterior: estático, sin actualización automática
    components.html(page_html(past, history_dates(), past["date"], None), height=4200, scrolling=False)
else:
    live_now = phase(datetime.now(NY)) == "open"

    @st.fragment(run_every=REFRESH_SEC if live_now else None)
    def live_fragment():
        current_view()

    live_fragment()

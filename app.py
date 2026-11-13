"""
Qué movió al SPY, versión Streamlit.

- Holdings, sectores e historia de precios: se descargan como máximo cada 6 horas.
- Últimos precios: con el mercado abierto se recalcula como máximo cada REFRESH_SEC segundos.
  El resultado se comparte entre todos los usuarios conectados.
- El botón "Actualizar ahora" fuerza un cálculo nuevo solo si el último tiene más de MANUAL_MIN_SEC.
- Los días anteriores se leen de site/data/history/, que guarda el workflow de GitHub al cierre.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_data as bd  # noqa: E402

REFRESH_SEC = int(os.environ.get("SPY_REFRESH_SEC", 15 * 60))  # cada cuánto se recalcula
CHECK_SEC = int(os.environ.get("SPY_CHECK_SEC", 60))            # cada cuánto cada pestaña mira si hay datos nuevos
MANUAL_MIN_SEC = 120
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
    /* Sin pantalla gris mientras Streamlit actualiza */
    [data-stale="true"], .stale-element, [data-testid="stElementContainer"][data-stale="true"] {
      opacity: 1 !important; filter: none !important; transition: none !important; }
    [data-testid="stStatusWidget"] { display: none !important; }
    .upd-banner { position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 1000;
      background: #18222D; color: #fff; padding: 8px 16px; border-radius: 999px;
      font: 600 14px/1.3 Arial, sans-serif; box-shadow: 0 4px 14px rgba(0,0,0,.18); }
    .upd-banner::before { content: ""; display: inline-block; width: 10px; height: 10px; margin-right: 8px;
      border: 2px solid #fff; border-right-color: transparent; border-radius: 50%; vertical-align: -1px;
      animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    </style>""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# Datos
# --------------------------------------------------------------------------- #
def phase(now: datetime) -> str:
    """pre / open / post / closed, en hora de Nueva York (no contempla feriados)."""
    if os.environ.get("SPY_TEST_OPEN"):
        return "open"
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


@st.cache_resource
def shared_state() -> dict:
    """Último cálculo, compartido entre todas las sesiones."""
    return {"lock": threading.Lock(), "data": None, "ts": 0.0, "key": None}


def calc(day: str, ph: str) -> dict:
    holdings, as_of, hist, tickers = base_data(day)
    if ph in ("open", "post"):
        live = bd.load_prices(tickers, period="5d")
        close = live.combine_first(hist).sort_index()
    else:
        close = hist
    return bd.compute(holdings, as_of, close)


def needs_update(st_: dict, day: str, ph: str, force: bool) -> bool:
    if st_["data"] is None or st_["key"] != (day, ph):
        return True
    age = time.time() - st_["ts"]
    if force:
        return age > MANUAL_MIN_SEC
    return ph == "open" and age > REFRESH_SEC


def next_update_epoch(st_: dict) -> float:
    return st_["ts"] + REFRESH_SEC


def history_dates() -> list[str]:
    if not HIST_DIR.exists():
        return []
    return sorted((p.stem for p in HIST_DIR.glob("*.json")), reverse=True)


def read_history(day: str) -> dict | None:
    p = HIST_DIR / f"{day}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def page_html(data: dict, dates: list[str], view: str, live_date: str | None, next_at: float | None = None) -> str:
    dump = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")  # noqa: E731
    inject = (
        f"<script>window.__SPY__={dump(data)};window.__SPY_DATES__={dump(dates)};"
        f"window.__SPY_VIEW__={dump(view)};window.__SPY_LIVE_DATE__={dump(live_date)};"
        f"window.__SPY_REFRESH__={REFRESH_SEC};window.__SPY_NEXT__={dump(next_at)};</script>\n"
    )
    return PAGE.replace("<script>\nconst LOC", inject + "<script>\nconst LOC", 1)


# --------------------------------------------------------------------------- #
# Vista
# --------------------------------------------------------------------------- #
def current_view(force: bool = False):
    now = datetime.now(NY)
    ph = phase(now)
    day = now.strftime("%Y-%m-%d")
    st_ = shared_state()
    dates = history_dates()
    warn = None

    if needs_update(st_, day, ph, force):
        banner = st.empty()
        banner.markdown('<div class="upd-banner">Actualizando datos…</div>', unsafe_allow_html=True)
        with st_["lock"]:
            # Otra sesión pudo haberlo calculado mientras esperábamos
            if needs_update(st_, day, ph, force):
                try:
                    st_["data"] = calc(day, ph)
                    st_["ts"] = time.time()
                    st_["key"] = (day, ph)
                except Exception as e:  # noqa: BLE001
                    warn = (f"No se pudieron actualizar los precios ({type(e).__name__}). "
                            "Se muestran los últimos datos disponibles.")
        banner.empty()

    data = st_["data"] or (read_history(dates[0]) if dates else None)
    if data is None:
        st.error("No hay datos disponibles todavía. Probá de nuevo en unos minutos.")
        return
    if warn:
        st.warning(warn)
    nxt = next_update_epoch(st_) if ph == "open" and st_["data"] else None
    components.html(page_html(data, dates, "live", data["date"], nxt), height=4200, scrolling=False)


wanted = st.query_params.get("d")
past = read_history(wanted) if wanted else None
force = "r" in st.query_params
if force:
    del st.query_params["r"]  # el pedido manual vale una sola vez

if past is not None and past["date"] != datetime.now(NY).strftime("%Y-%m-%d"):
    # Día anterior: estático, sin actualización automática
    components.html(page_html(past, history_dates(), past["date"], None), height=4200, scrolling=False)
else:
    live_now = phase(datetime.now(NY)) == "open"
    first = {"force": force}

    @st.fragment(run_every=CHECK_SEC if live_now else None)
    def live_fragment():
        current_view(force=first.pop("force", False))

    live_fragment()

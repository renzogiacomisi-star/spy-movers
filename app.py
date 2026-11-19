"""
Qué movió al S&P 500, versión Streamlit.

- Holdings, sectores e historia de precios: se descargan como máximo cada 6 horas.
- Últimos precios: con el mercado abierto se recalcula como máximo cada REFRESH_SEC segundos.
  El resultado se comparte entre todos los usuarios conectados.
- El botón "Actualizar ahora" fuerza un cálculo nuevo solo si el último tiene más de MANUAL_MIN_SEC.
- Los días anteriores se leen de site/data/history/, que guarda el workflow de GitHub al cierre.
"""

from __future__ import annotations

import base64
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
import extras as ex  # noqa: E402
import pdf_report as pdfr  # noqa: E402

REFRESH_SEC = int(os.environ.get("SPY_REFRESH_SEC", 15 * 60))  # cada cuánto se recalcula
EXT_SEC = int(os.environ.get("SPY_EXT_SEC", 10 * 60))           # pre-market / after-hours
CHECK_SEC = int(os.environ.get("SPY_CHECK_SEC", 60))            # cada cuánto cada pestaña mira si hay datos nuevos
MANUAL_MIN_SEC = 120
NY = ZoneInfo("America/New_York")
HIST_DIR = ROOT / "site" / "data" / "history"
PAGE = (ROOT / "site" / "index.html").read_text(encoding="utf-8")

st.set_page_config(page_title="Qué movió al S&P 500", page_icon="⚖️", layout="wide",
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
    """pre / open / post / closed según el calendario de NYSE (feriados y medias jornadas incluidos)."""
    if os.environ.get("SPY_TEST_PHASE"):
        return os.environ["SPY_TEST_PHASE"]
    return ex.market_clock(now)["phase"]


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fred_data(day: str) -> dict:
    return ex.load_fred([s for v in ex.FRED_SERIES.values() for s in v])


@st.cache_data(ttl=3 * 3600, show_spinner=False)
def calendar_data(day: str) -> dict | None:
    try:
        return ex.econ_calendar(fred_data(day), day, 10)
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: calendario no disponible ({e}).")
        return None


@st.cache_data(ttl=20 * 60, show_spinner=False)
def fed_data(day: str, bucket: int) -> dict | None:
    try:
        return ex.fedwatch(fred_data(day), day)
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: probabilidades de la Fed no disponibles ({e}).")
        return None


def clock_data() -> dict:
    c = ex.market_clock()
    c["holidays"] = ex.holidays_ahead(None, 45)
    return c


@st.cache_data(ttl=None, max_entries=24, show_spinner=False)
def pdf_bytes(data_key: str, ext_key: str, _data: dict, _ext: dict | None, _cal: dict | None, _fed: dict | None) -> str:
    raw = pdfr.build_pdf(_data, _ext, _cal, _fed, datetime.now(NY))
    return base64.b64encode(raw).decode()


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def base_data(day: str):
    holdings, as_of = bd.load_holdings()
    sectors = bd.load_sectors()
    holdings = bd.apply_sectors(holdings, sectors)
    changes = bd.load_changes()
    gone = bd.history_tickers(holdings, changes)
    hist = bd.load_prices(bd.all_tickers(holdings, gone), period="2y")
    tickers = bd.all_tickers(holdings)  # para la actualización en vivo alcanza con las actuales
    official = bd.load_official_rates()
    return holdings, as_of, hist, tickers, official, changes, sectors


@st.cache_resource
def shared_state() -> dict:
    """Último cálculo, compartido entre todas las sesiones."""
    return {"lock": threading.Lock(), "data": None, "ts": 0.0, "key": None,
            "ext": None, "ext_ts": 0.0, "ext_key": None}


def ext_view(st_: dict, day: str, ph: str) -> dict | None:
    """Pre-market / after-hours compartido, como máximo cada EXT_SEC."""
    if ph not in ("pre", "post"):
        return None
    key = (day, ph)
    if st_["ext_key"] != key or time.time() - st_["ext_ts"] > EXT_SEC:
        with st_["lock"]:
            if st_["ext_key"] != key or time.time() - st_["ext_ts"] > EXT_SEC:
                try:
                    holdings = base_data(day)[0]
                    st_["ext"] = ex.ext_summary(holdings, ph)
                except Exception as e:  # noqa: BLE001
                    print(f"Aviso: resumen fuera de rueda no disponible ({e}).")
                    st_["ext"] = None
                st_["ext_ts"], st_["ext_key"] = time.time(), key
    return st_["ext"]


def calc(day: str, ph: str) -> dict:
    holdings, as_of, hist, tickers, official, changes, sectors = base_data(day)
    last_trade = None
    if ph in ("open", "post"):
        live = bd.load_prices(tickers, period="5d")
        close = live.combine_first(hist).sort_index()
        last_trade = bd.load_last_trade_times([t for t, _, _ in bd.LIVE_RATES])
    else:
        close = hist
    return bd.compute(holdings, as_of, close, official, last_trade, changes=changes, wiki=sectors)


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


def page_html(data: dict, dates: list[str], view: str, live_date: str | None, next_at: float | None = None,
              extra: dict | None = None) -> str:
    dump = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")  # noqa: E731
    extra = extra or {}
    inject = (
        f"<script>window.__SPY__={dump(data)};window.__SPY_DATES__={dump(dates)};"
        f"window.__SPY_VIEW__={dump(view)};window.__SPY_LIVE_DATE__={dump(live_date)};"
        f"window.__SPY_REFRESH__={REFRESH_SEC};window.__SPY_NEXT__={dump(next_at)};"
        + "".join(f"window.__SPY_{k}__={dump(v)};" for k, v in extra.items())
        + "</script>\n"
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
    ext = ext_view(st_, day, ph)
    cal = calendar_data(day)
    fed = fed_data(day, int(time.time() // (20 * 60)))
    extra = {"CLOCK": clock_data(), "EXT": ext, "CAL": cal, "FED": fed}
    extra.update(pdf_extra(data, ext, cal, fed))
    components.html(page_html(data, dates, "live", data["date"], nxt, extra), height=4200, scrolling=False)


def pdf_extra(data: dict, ext: dict | None, cal: dict | None, fed: dict | None) -> dict:
    try:
        b64 = pdf_bytes(f"{data['date']}|{data['generated_at']}", ext["at"] if ext else "", data, ext, cal, fed)
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no se pudo generar el PDF ({e}).")
        return {}
    gen = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00")).astimezone(NY)
    if ext:
        suf = f"_{ext['kind']}_" + datetime.fromisoformat(ext["at"]).astimezone(NY).strftime("%H%M")
    elif data.get("session") == "open":
        suf = gen.strftime("_%H%M")
    else:
        suf = "_cierre"
    return {"PDF": b64, "PDF_NAME": f"resumen_SP500_{data['date']}{suf}.pdf"}


wanted = st.query_params.get("d")
past = read_history(wanted) if wanted else None
force = "r" in st.query_params
if force:
    del st.query_params["r"]  # el pedido manual vale una sola vez

if past is not None and past["date"] != datetime.now(NY).strftime("%Y-%m-%d"):
    # Día anterior: estático, sin actualización automática
    extra = {"CLOCK": clock_data()}
    extra.update(pdf_extra(past, None, None, None))
    components.html(page_html(past, history_dates(), past["date"], None, None, extra), height=4200, scrolling=False)
else:
    live_now = phase(datetime.now(NY)) in ("pre", "open", "post")
    first = {"force": force}

    @st.fragment(run_every=CHECK_SEC if live_now else None)
    def live_fragment():
        current_view(force=first.pop("force", False))

    live_fragment()

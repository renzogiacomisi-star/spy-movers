"""
Qué movió al S&P 500, versión Streamlit.

Todas las descargas corren en un proceso de fondo (el "motor"), separado de la página:
- Datos base (pesos, sectores, 2 años de precios, tasas oficiales): cada 6 horas.
- Precios del día: cada REFRESH_SEC con el mercado abierto.
- Pre-market / after-hours: cada EXT_SEC fuera de la rueda.
- Calendario económico: cada 3 horas. Probabilidades de la Fed: cada 20 minutos.
- PDF: se arma cada vez que cambian los datos.

La página nunca espera una descarga: muestra siempre lo último que tiene el motor.
Solo después de un reinicio, mientras no hay ningún dato, muestra un mensaje de carga.
Los días anteriores se leen de site/data/history/, que guarda el workflow de GitHub al cierre.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import traceback
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

REFRESH_SEC = int(os.environ.get("SPY_REFRESH_SEC", 15 * 60))  # precios del día
EXT_SEC = int(os.environ.get("SPY_EXT_SEC", 10 * 60))           # pre-market / after-hours
BASE_SEC = 6 * 3600                                             # datos base
CAL_SEC = 3 * 3600                                              # calendario
FED_SEC = 20 * 60                                               # Fed
CHECK_SEC = int(os.environ.get("SPY_CHECK_SEC", 60))            # cada cuánto la página mira si hay datos nuevos
MANUAL_MIN_SEC = 120
STALL_SEC = 10 * 60                                             # motor sin señales de vida: se relanza
NY = ZoneInfo("America/New_York")
MVD = ZoneInfo("America/Montevideo")
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
    [data-stale="true"], .stale-element, [data-testid="stElementContainer"][data-stale="true"] {
      opacity: 1 !important; filter: none !important; transition: none !important; }
    [data-testid="stStatusWidget"] { display: none !important; }
    .loading { max-width: 560px; margin: 18vh auto 0; padding: 28px 32px; background: #F7F9FB; border-radius: 10px;
      font: 15px/1.5 Arial, sans-serif; color: #18222D; }
    .loading h2 { font-size: 22px; margin: 0 0 8px; }
    .loading .step { color: #5B6878; margin-top: 10px; }
    .loading .spin { display: inline-block; width: 12px; height: 12px; margin-right: 8px; border: 2px solid #2457A6;
      border-right-color: transparent; border-radius: 50%; vertical-align: -1px; animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    </style>""",
    unsafe_allow_html=True,
)


def log(msg: str) -> None:
    print(f"[motor {datetime.now(MVD):%H:%M:%S}] {msg}", flush=True)


def phase(now: datetime | None = None) -> str:
    """pre / open / post / closed según el calendario de NYSE (feriados y medias jornadas incluidos)."""
    if os.environ.get("SPY_TEST_PHASE"):
        return os.environ["SPY_TEST_PHASE"]
    return ex.market_clock(now)["phase"]


# --------------------------------------------------------------------------- #
# Motor de datos (corre en segundo plano, compartido por todas las sesiones)
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.s: dict = {
            "base": None, "base_ts": 0.0, "base_day": None,
            "data": None, "data_ts": 0.0, "data_key": None,
            "ext": None, "ext_ts": 0.0, "ext_key": None,
            "fred": {}, "fred_ts": 0.0,
            "cal": None, "cal_ts": 0.0, "cal_day": None,
            "fed": None, "fed_ts": 0.0,
            "pdf": None, "pdf_name": None, "pdf_key": None,
            "step": "Iniciando", "error": None, "beat": time.time(),
        }
        self.force = False
        self.gen = 0
        self.thread: threading.Thread | None = None

    # -- control ------------------------------------------------------------ #
    def ensure_running(self) -> None:
        with self.lock:
            alive = self.thread is not None and self.thread.is_alive()
            stalled = alive and time.time() - self.s["beat"] > STALL_SEC
            if alive and not stalled:
                return
            if stalled:
                log(f"sin señales desde hace {int(time.time() - self.s['beat'])} s en '{self.s['step']}'; se relanza")
            self.gen += 1
            self.s["beat"] = time.time()
            self.thread = threading.Thread(target=self._loop, args=(self.gen,), daemon=True, name=f"motor-{self.gen}")
            self.thread.start()

    def request_refresh(self) -> None:
        if time.time() - self.s["data_ts"] > MANUAL_MIN_SEC:
            self.force = True

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.s)

    def _set(self, **kw) -> None:
        with self.lock:
            self.s.update(kw)

    def _step(self, name: str, fn):
        self._set(step=name, beat=time.time())
        t0 = time.time()
        try:
            out = fn()
            log(f"{name}: {time.time() - t0:.1f} s")
            return out, None
        except Exception as e:  # noqa: BLE001
            log(f"{name}: error tras {time.time() - t0:.1f} s: {type(e).__name__}: {e}")
            traceback.print_exc()
            return None, f"{type(e).__name__}"

    # -- ciclo ---------------------------------------------------------------- #
    def _loop(self, gen: int) -> None:
        log(f"motor {gen} iniciado")
        while gen == self.gen:
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                log(f"error inesperado: {e}")
                traceback.print_exc()
            self._set(step="En espera", beat=time.time())
            for _ in range(10):
                if gen != self.gen or self.force:
                    break
                time.sleep(1)
        log(f"motor {gen} detenido")

    def _tick(self) -> None:
        now = datetime.now(NY)
        ph, day, s, t = phase(now), now.strftime("%Y-%m-%d"), self.s, time.time()

        # 1. Datos base
        if s["base"] is None or s["base_day"] != day or t - s["base_ts"] > BASE_SEC:
            base, err = self._step("Descargando datos base (pesos, sectores y 2 años de precios)", self._load_base)
            if base is not None:
                self._set(base=base, base_ts=time.time(), base_day=day, error=None)
            elif s["base"] is None:
                self._set(error=err)
                return

        # 2. Precios del día y cálculo
        force, self.force = self.force, False
        age = time.time() - s["data_ts"]
        if (s["data"] is None or s["data_key"] != (day, ph) or force
                or (ph == "open" and age > REFRESH_SEC)):
            data, err = self._step("Calculando con los precios del día", lambda: self._calc(ph))
            if data is not None:
                self._set(data=data, data_ts=time.time(), data_key=(day, ph), error=None)
            else:
                self._set(error=err)

        # 3. Pre-market / after-hours
        if ph in ("pre", "post"):
            if s["ext_key"] != (day, ph) or time.time() - s["ext_ts"] > EXT_SEC:
                ext, _ = self._step("Pre-market / after-hours",
                                    lambda: ex.ext_summary(self.s["base"][0], ph))
                self._set(ext=ext, ext_ts=time.time(), ext_key=(day, ph))
        elif s["ext"] is not None:
            self._set(ext=None, ext_key=None)

        # 4. Calendario (con el último dato de FRED) y Fed
        if not s["fred"] or time.time() - s["fred_ts"] > BASE_SEC:
            fred, _ = self._step("Datos de FRED", lambda: ex.load_fred([x for v in ex.FRED_SERIES.values() for x in v]))
            self._set(fred=fred or s["fred"], fred_ts=time.time())
        if s["cal"] is None or s["cal_day"] != day or time.time() - s["cal_ts"] > CAL_SEC:
            cal, _ = self._step("Calendario económico", lambda: ex.econ_calendar(self.s["fred"], day, 10))
            self._set(cal=cal or s["cal"], cal_ts=time.time(), cal_day=day)
        if time.time() - s["fed_ts"] > FED_SEC:
            fed, _ = self._step("Probabilidades de la Fed", lambda: ex.fedwatch(self.s["fred"], day))
            self._set(fed=fed or s["fed"], fed_ts=time.time())

        # 5. PDF
        d = self.s["data"]
        if d is not None:
            key = (d["generated_at"], (self.s["ext"] or {}).get("at"), self.s["cal_ts"], self.s["fed_ts"])
            if key != self.s["pdf_key"]:
                b64, _ = self._step("PDF", lambda: base64.b64encode(
                    pdfr.build_pdf(d, self.s["ext"], self.s["cal"], self.s["fed"], datetime.now(NY))).decode())
                if b64:
                    self._set(pdf=b64, pdf_name=pdf_name(d, self.s["ext"]), pdf_key=key)

    def _load_base(self):
        holdings, as_of = bd.load_holdings()
        sectors = bd.load_sectors()
        holdings = bd.apply_sectors(holdings, sectors)
        changes = bd.load_changes()
        gone = bd.history_tickers(holdings, changes)
        hist = bd.load_prices(bd.all_tickers(holdings, gone), period="2y")
        tickers = bd.all_tickers(holdings)
        official = bd.load_official_rates()
        log(f"datos base: {len(holdings)} acciones, {hist.shape[1]} series de precios, cambios en el índice: {len(changes)}")
        return holdings, as_of, hist, tickers, official, changes, sectors

    def _calc(self, ph: str) -> dict:
        holdings, as_of, hist, tickers, official, changes, sectors = self.s["base"]
        last_trade = None
        if ph in ("open", "post"):
            live = bd.load_prices(tickers, period="5d")
            close = live.combine_first(hist).sort_index()
            last_trade = bd.load_last_trade_times([t for t, _, _ in bd.LIVE_RATES])
        else:
            close = hist
        return bd.compute(holdings, as_of, close, official, last_trade, changes=changes, wiki=sectors)


@st.cache_resource
def engine() -> Engine:
    return Engine()


# --------------------------------------------------------------------------- #
# Página
# --------------------------------------------------------------------------- #
def pdf_name(data: dict, ext: dict | None) -> str:
    gen = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00")).astimezone(NY)
    if ext:
        suf = f"_{ext['kind']}_" + datetime.fromisoformat(ext["at"]).astimezone(NY).strftime("%H%M")
    elif data.get("session") == "open":
        suf = gen.strftime("_%H%M")
    else:
        suf = "_cierre"
    return f"resumen_SP500_{data['date']}{suf}.pdf"


@st.cache_data(ttl=3600, max_entries=16, show_spinner=False)
def past_pdf(day: str, _data: dict) -> str | None:
    try:
        return base64.b64encode(pdfr.build_pdf(_data, None, None, None, datetime.now(NY))).decode()
    except Exception as e:  # noqa: BLE001
        log(f"PDF del {day}: error {e}")
        return None


def clock_data() -> dict:
    c = ex.market_clock()
    c["holidays"] = ex.holidays_ahead(None, 45)
    return c


def history_dates() -> list[str]:
    if not HIST_DIR.exists():
        return []
    return sorted((p.stem for p in HIST_DIR.glob("*.json")), reverse=True)


def read_history(day: str) -> dict | None:
    p = HIST_DIR / f"{day}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def page_html(data: dict, dates: list[str], view: str, live_date: str | None, extra: dict | None = None) -> str:
    dump = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")  # noqa: E731
    extra = extra or {}
    inject = (
        f"<script>window.__SPY__={dump(data)};window.__SPY_DATES__={dump(dates)};"
        f"window.__SPY_VIEW__={dump(view)};window.__SPY_LIVE_DATE__={dump(live_date)};"
        f"window.__SPY_REFRESH__={REFRESH_SEC};"
        + "".join(f"window.__SPY_{k}__={dump(v)};" for k, v in extra.items())
        + "</script>\n"
    )
    return PAGE.replace("<script>\nconst LOC", inject + "<script>\nconst LOC", 1)


def loading_view(snap: dict) -> None:
    err = snap.get("error")
    msg = (f"<div class='step'>El último intento falló ({err}); se reintenta solo en unos segundos.</div>" if err else "")
    st.markdown(
        f"""<div class="loading"><h2>Cargando los datos del día</h2>
        <div>Después de un reinicio, la primera carga tarda entre 1 y 3 minutos. La página se completa sola.</div>
        <div class="step"><span class="spin"></span>{snap.get('step', '')}…</div>{msg}</div>""",
        unsafe_allow_html=True,
    )


def live_view(eng: Engine) -> bool:
    """Dibuja la vista en vivo. Devuelve False mientras todavía no hay datos."""
    snap = eng.snapshot()
    dates = history_dates()
    data = snap["data"]
    if data is None:
        if snap["base"] is None and dates and snap.get("error"):
            data = read_history(dates[0])  # sin datos nuevos: el último cierre guardado
        if data is None:
            loading_view(snap)
            return False
    ph = phase()
    nxt = snap["data_ts"] + REFRESH_SEC if ph == "open" and snap["data"] else None
    stale = bool(snap["data"]) and ph == "open" and time.time() - snap["data_ts"] > REFRESH_SEC + 10 * 60
    extra = {
        "CLOCK": clock_data(), "EXT": snap["ext"], "CAL": snap["cal"], "FED": snap["fed"],
        "NEXT": nxt, "STALE": stale,
    }
    if snap["pdf"] and snap["data"] is data:
        extra.update({"PDF": snap["pdf"], "PDF_NAME": snap["pdf_name"]})
    components.html(page_html(data, dates, "live", data["date"], extra), height=4200, scrolling=False)
    return True


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #
eng = engine()
eng.ensure_running()

wanted = st.query_params.get("d")
past = read_history(wanted) if wanted else None
if "r" in st.query_params:
    eng.request_refresh()
    del st.query_params["r"]  # el pedido manual vale una sola vez

if past is not None and past["date"] != datetime.now(NY).strftime("%Y-%m-%d"):
    # Día anterior: estático, sin actualización automática
    extra = {"CLOCK": clock_data()}
    b64 = past_pdf(past["date"], past)
    if b64:
        extra.update({"PDF": b64, "PDF_NAME": f"resumen_SP500_{past['date']}_cierre.pdf"})
    components.html(page_html(past, history_dates(), past["date"], None, extra), height=4200, scrolling=False)
else:
    loaded = eng.snapshot()["data"] is not None
    live_now = phase() in ("pre", "open", "post")
    every = CHECK_SEC if (loaded and live_now) else (5 if not loaded else None)

    @st.fragment(run_every=every)
    def live_fragment():
        eng.ensure_running()
        ok = live_view(eng)
        if not ok:
            st.session_state["cargando"] = True
        elif st.session_state.pop("cargando", False):
            # Llegaron los primeros datos: se recarga la página para pasar al refresco cada CHECK_SEC
            st.rerun()

    live_fragment()

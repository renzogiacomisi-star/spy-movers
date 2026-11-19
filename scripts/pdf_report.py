"""Resumen diario en PDF, armado con los mismos datos que muestra la página."""

from __future__ import annotations

import io
from datetime import datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

NY = ZoneInfo("America/New_York")
MVD = ZoneInfo("America/Montevideo")
INK = colors.HexColor("#18222D")
MUTED = colors.HexColor("#5B6878")
LINE = colors.HexColor("#CBD3DC")
BG = colors.HexColor("#EEF1F4")
UP = colors.HexColor("#1D7A5B")
DOWN = colors.HexColor("#B3374A")
ACCENT = colors.HexColor("#2F5577")
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre"]
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

S = {
    "brand": ParagraphStyle("brand", fontName="Helvetica-Bold", fontSize=9, textColor=MUTED),
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, leading=23, textColor=INK, spaceAfter=2),
    "stamp": ParagraphStyle("stamp", fontName="Helvetica", fontSize=8.5, leading=11, textColor=MUTED),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=INK, spaceBefore=8, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK, spaceAfter=3),
    "lead": ParagraphStyle("lead", fontName="Helvetica", fontSize=10, leading=14, textColor=INK, spaceAfter=4),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=7.5, leading=10, textColor=MUTED),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8, leading=10, textColor=INK),
    "cellr": ParagraphStyle("cellr", fontName="Helvetica", fontSize=8, leading=10, textColor=INK, alignment=TA_RIGHT),
    "big": ParagraphStyle("big", fontName="Helvetica-Bold", fontSize=17, leading=20, textColor=INK),
}

_REPL = {"−": "-", "≥": ">=", "≤": "<=", "≈": "~", "→": "->", " ": " ",
         " ": " ", " ": " "}


def _t(x) -> str:
    s = "" if x is None else str(x)
    for a, b in _REPL.items():
        s = s.replace(a, b)
    return escape(s)


def _n(x, nd=2, sign=True, suf=""):
    if x is None:
        return "s/d"
    s = f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s + suf


def _c(x, s):
    if x is None or x == 0:
        return s
    col = "#1D7A5B" if x > 0 else "#B3374A"
    return f'<font color="{col}">{s}</font>'


def _date_long(iso: str) -> str:
    d = datetime.fromisoformat(iso[:10])
    return f"{DAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def _table(rows, widths, head=True, zebra=False, align_right_from=1):
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    st = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"),
    ]
    if head:
        st += [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
               ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK)]
    t.setStyle(TableStyle(st))
    return t


def _P(txt, style="cell"):
    return Paragraph(txt, S[style])


def _sector_bars(sectors, width):
    sec = [s for s in sectors if s.get("p") is not None or s.get("c") is not None]
    if not sec:
        return None
    val = lambda s: s["p"] if s.get("p") is not None else s["c"]  # noqa: E731
    mx = max(abs(val(s)) for s in sec) or 1
    rowh, labw, valw = 12, 120, 48
    h = rowh * len(sec) + 4
    d = Drawing(width, h)
    mid = labw + (width - labw - valw) / 2
    half = (width - labw - valw) / 2 - 4
    d.add(Line(mid, 0, mid, h, strokeColor=MUTED, strokeWidth=0.5))
    for i, s in enumerate(sec):
        y = h - (i + 1) * rowh + 2
        v = val(s)
        w = abs(v) / mx * half
        d.add(String(0, y + 2, s["s"], fontName="Helvetica", fontSize=7.5, fillColor=INK))
        d.add(Rect(mid if v >= 0 else mid - w, y + 1, max(w, 0.5), rowh - 4,
                   fillColor=UP if v >= 0 else DOWN, strokeColor=None))
        d.add(String(width, y + 2, _n(v, 2), fontName="Helvetica-Bold", fontSize=7.5,
                     fillColor=UP if v > 0 else DOWN if v < 0 else INK, textAnchor="end"))
    return d


def stamp_text(data: dict, generated: datetime, ext: dict | None) -> str:
    gen_ny = generated.astimezone(NY)
    data_t = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00")).astimezone(NY)
    mvd = data_t.astimezone(MVD)
    if data.get("session") == "open":
        txt = (f"Movimientos del {_date_long(data['date'])} hasta las {data_t:%H:%M} de Nueva York "
               f"({mvd:%H:%M} de Montevideo), con el mercado abierto y precios con hasta 15 minutos de demora.")
    else:
        txt = f"Resumen de la sesión del {_date_long(data['date'])}, al cierre."
    if ext:
        at = datetime.fromisoformat(ext["at"]).astimezone(NY)
        lbl = "pre-market" if ext["kind"] == "pre" else "after-hours"
        txt += f" Incluye el {lbl} hasta las {at:%H:%M} de Nueva York ({at.astimezone(MVD):%H:%M} de Montevideo)."
    txt += f" Generado el {gen_ny.day}/{gen_ny.month}/{gen_ny.year} a las {gen_ny.astimezone(MVD):%H:%M} de Montevideo."
    return txt


def build_pdf(data: dict, ext: dict | None = None, cal: dict | None = None, fed: dict | None = None,
              generated: datetime | None = None) -> bytes:
    generated = generated or datetime.now(NY)
    buf = io.BytesIO()
    W, H = A4
    margin = 15 * mm
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=13 * mm,
                            bottomMargin=13 * mm, title="Qué movió al S&P 500", author="Qué movió al S&P 500")
    cw = W - 2 * margin
    story = []
    live = data.get("session") == "open"
    idx_ret = data.get("spx_ret") if data.get("spx_ret") is not None else data.get("spy_ret")
    up = (idx_ret or 0) >= 0
    verb = ("sube" if up else "baja") if live else ("subió" if up else "bajó")
    title = f"¿Por qué {verb} el mercado {'hoy' if live else 'el ' + _date_long(data['date']).rsplit(' de ', 1)[0]}?"

    story += [_P("QUÉ MOVIÓ AL S&amp;P 500", "brand"), Spacer(1, 2), _P(_t(title), "title"),
              _P(_t(stamp_text(data, generated, ext)), "stamp"), Spacer(1, 6)]

    # Cifras principales
    ctx = {x["t"]: x for x in data.get("context", [])}
    vix, tnx = ctx.get("^VIX"), ctx.get("^TNX")
    b = data["breadth"]
    figs = [
        (_c(idx_ret, _n(idx_ret, 2, suf="%")), "S&amp;P 500"),
        (_c(data.get("spx_pts"), _n(data.get("spx_pts"), 1)), f"puntos, en {_n(data.get('spx_last'), 2, False)}"),
        (f"{b['adv']} / {b['dec']}", "acciones que suben / bajan"),
        (_c(-(vix or {}).get("chg", 0) if vix else None, _n(vix["last"], 2, False)) if vix else "s/d",
         f"VIX ({_n(vix['chg'], 2)})" if vix else "VIX"),
        (_n(tnx["last"], 2, False, "%") if tnx else "s/d", f"tasa a 10 años ({_n(tnx['chg'], 1)} pb)" if tnx else ""),
    ]
    ft = Table([[_P(f, "big") for f, _ in figs], [_P(lab, "small") for _, lab in figs]], colWidths=[cw / 5] * 5)
    ft.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BG), ("TOPPADDING", (0, 0), (-1, 0), 7),
                            ("BOTTOMPADDING", (0, -1), (-1, -1), 7), ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
    story += [ft, Spacer(1, 8)]

    # Pre-market / after-hours
    if ext:
        lbl = "Antes de la apertura" if ext["kind"] == "pre" else "Después del cierre (after-hours)"
        blk = [_P(_t(lbl), "h2")]
        if ext.get("lead"):
            blk.append(_P(_t(ext["lead"]), "body"))
        rows = [["Futuros", "Último", "Var."]] + [
            [x["n"], _n(x["last"], 2, False), _P(_c(x["chg"], _n(x["chg"], 2, suf="%")), "cellr")] for x in ext["futures"]]
        rows2 = [["Mayor peso", "Último", "Var."]] + [
            [x["t"], _n(x["last"], 2, False), _P(_c(x["chg"], _n(x["chg"], 2, suf="%")), "cellr")] for x in ext["stocks"]]
        t = Table([[_table(rows, [cw * .26, cw * .1, cw * .1]), _table(rows2, [cw * .2, cw * .1, cw * .1])]],
                  colWidths=[cw * .5, cw * .5])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        blk += [t, _P("Variación contra el último cierre de la rueda (16:00 de Nueva York, o 13:00 en media jornada).", "small")]
        story.append(KeepTogether(blk))

    # Resumen
    story.append(_P("Qué pasó", "h2"))
    for i, p in enumerate(data.get("summary", [])):
        story.append(_P(_t(p), "lead" if i == 0 else "body"))

    # Acciones que más movieron el índice
    val = lambda c: c["p"] if c.get("p") is not None else c.get("c")  # noqa: E731
    comps = [c for c in data["components"] if val(c) is not None]
    pos = sorted([c for c in comps if val(c) > 0], key=lambda c: -val(c))[:6]
    neg = sorted([c for c in comps if val(c) < 0], key=lambda c: val(c))[:6]

    def mov_rows(arr, head):
        rows = [[head, "Var.", "Pts"]]
        for c in arr:
            rows.append([_P(f"<b>{_t(c['t'])}</b> <font color='#5B6878' size=7>{_t(c['n'][:22])}</font>"),
                         _P(_c(c["r"], _n(c["r"], 2, suf="%")), "cellr"), _P(_c(val(c), _n(val(c), 2)), "cellr")])
        return _table(rows, [cw * .3, cw * .08, cw * .08])

    t = Table([[mov_rows(pos, "Sumaron"), mov_rows(neg, "Restaron")]], colWidths=[cw * .5, cw * .5])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(KeepTogether([_P("Acciones que más movieron el índice (puntos del S&amp;P 500)", "h2"), t]))

    # Sectores
    bars = _sector_bars(data.get("sectors", []), cw)
    if bars:
        story.append(KeepTogether([_P("Aporte por sector (puntos)", "h2"), bars]))

    # Clima del día
    rg = data.get("regime")
    if rg:
        story.append(KeepTogether([
            _P("Clima del día", "h2"),
            _P(f"<b>{_t(rg['sesgo'])}</b>. {_t(rg['esc']['titulo'])}: {_t(rg['esc']['texto'])} Promedio de los sectores "
               f"cíclicos {_n(rg['cyc'], 2, suf='%')} contra defensivos {_n(rg['def'], 2, suf='%')}.", "body")]))

    # Amplitud
    br = data.get("breadth_read")
    if br:
        items = " ".join(
            f"{_t(it['titulo'])}: <b>{_n(it['v'], 0 if it['k'] == 'ad' else 1, it['k'] in ('ad', 'nhm'))}{_t(it.get('u') or '')}</b> "
            f"({_t(it.get('zona') or '')}{', ' + _t(it['tend']) if it['k'] != 'ad' else ''})." for it in br["items"])
        story.append(KeepTogether([_P("Amplitud", "h2"), _P(f"<b>{_t(br['titulo'])}.</b> {_t(br['texto'])}", "body"),
                                   _P(items, "small")]))

    # Temáticas
    ths = [x for x in data.get("themes", []) if not x.get("agg") and x.get("p") is not None]
    if ths:
        tp = sorted([x for x in ths if x["p"] > 0], key=lambda x: -x["p"])[:4]
        tn = sorted([x for x in ths if x["p"] < 0], key=lambda x: x["p"])[:4]

        def th_rows(arr, head):
            if not arr:
                return _P("Ninguna.", "small")
            return _table([[head, "Var.", "Pts"]] + [[_P(_t(x["n"])), _P(_c(x["r"], _n(x["r"], 2, suf="%")), "cellr"),
                                                    _P(_c(x["p"], _n(x["p"], 2)), "cellr")] for x in arr],
                          [cw * .3, cw * .08, cw * .08])
        t = Table([[th_rows(tp, "Temáticas que sumaron"), th_rows(tn, "Temáticas que restaron")]], colWidths=[cw * .5, cw * .5])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(KeepTogether([_P("Temáticas", "h2"), t]))

    # Índices
    mk = data.get("markets")
    if mk and mk.get("rows"):
        per = [("1D", "1 día"), ("1S", "1 sem."), ("1M", "1 mes"), ("3M", "3 meses"), ("YTD", "YTD"), ("1A", "1 año")]
        rows = [["Índice", "Último"] + [p for _, p in per]]
        for r in mk["rows"]:
            rows.append([_P(_t(r["n"]) + (f" <font size=6.5 color='#5B6878'>({_t(r['t'])})</font>" if r.get("proxy") else "")),
                         _n(r["last"], 2, False)] + [_P(_c(r["chg"].get(k), _n(r["chg"].get(k), 2, suf="%")), "cellr")
                                                     for k, _ in per])
        story.append(KeepTogether([_P("Índices", "h2"), _table(rows, [cw * .25, cw * .13] + [cw * .62 / 6] * 6)]))

    # Tasas
    ra = data.get("rates")
    if ra and ra.get("live"):
        off = {x["k"]: x for x in (ra.get("official") or {}).get("rows", [])}
        per = [("1S", "1 sem."), ("1M", "1 mes"), ("3M", "3 meses"), ("YTD", "YTD"), ("1A", "1 año")]
        rows = [["Tesoro EE.UU.", "Tasa", "1 día"] + [p for _, p in per]]

        def rc(v, nd=0):  # tasa que sube en rojo
            return _P(_c(-v if v is not None else None, _n(v, nd, suf=" pb")), "cellr")
        for x in ra["live"]:
            o = off.get(x["k"], {}).get("chg", {})
            rows.append([x["n"] + ("" if x.get("en_vivo", True) else " (cierre)"), _n(x["v"], 2, False, "%"), rc(x["d1"], 1)]
                        + [rc(o.get(k)) for k, _ in per])
        story.append(KeepTogether([_P("Tasas del Tesoro", "h2"), _table(rows, [cw * .2, cw * .1, cw * .11] + [cw * .59 / 5] * 5)]))

    # Fed y calendario
    if fed and fed.get("probs"):
        d = fed["meeting"]
        txt = "; ".join(f"{_t(p['label'])}: <b>{_n(p['p'], 1, False, '%')}</b>" for p in fed["probs"])
        story.append(KeepTogether([
            _P(f"Próxima reunión de la Fed: {_date_long(d)}", "h2"), _P(txt, "body"),
            _P("Estimación propia con futuros de fed funds y la metodología de CME FedWatch; puede diferir unos puntos "
               "de la publicada por CME.", "small")]))
    if cal and cal.get("items"):
        rows = [["Fecha", "Hora NY / MVD", "Dato", "Último publicado"]]
        for it in cal["items"][:14]:
            dd = datetime.fromisoformat(it["d"])
            fecha = f"{DAYS[dd.weekday()][:3]} {dd.day}/{dd.month}"
            hora = f"{it['ny']} / {it['mvd']}" if it["ny"] else ""
            name = _t(it["n"]) + (f" ({_t(it['p'])})" if it.get("p") and it["p"] != "semanal" else "")
            if not it.get("conf", True):
                name += " <font color='#5B6878'>(fecha a confirmar)</font>"
            rows.append([fecha, hora, _P(name), _P(_t(it.get("last") or ""), "cell")])
        story.append(KeepTogether([_P("Calendario de los próximos días", "h2"),
                                   _table(rows, [cw * .1, cw * .14, cw * .4, cw * .36], align_right_from=9)]))

    story += [Spacer(1, 8), _P(
        "Fuentes: pesos del SPY (SSGA), precios de Yahoo Finance con hasta 15 minutos de demora, sectores GICS (Wikipedia), "
        "tasas del Tesoro de EE.UU. y Cboe, datos económicos de FRED y calendarios oficiales de la Fed, BLS, BEA y Census. "
        "Información con fines informativos; no constituye una recomendación de inversión.", "small")]

    def on_page(canv, _doc):
        canv.saveState()
        canv.setFont("Helvetica", 7)
        canv.setFillColor(MUTED)
        canv.drawRightString(W - margin, 8 * mm, f"Página {_doc.page}")
        canv.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()

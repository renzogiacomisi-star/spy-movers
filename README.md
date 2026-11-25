# Qué movió al S&P 500

Página que descompone el movimiento del SPY por acción y por sector, en puntos del S&P 500, con amplitud de mercado y contexto macro. Con el mercado abierto se actualiza sola cada 15 minutos, y tiene un botón "Actualizar ahora".

## Cómo funciona

- **Streamlit Community Cloud** corre `app.py`. Todas las descargas las hace un proceso de fondo (el "motor"), separado de la página: datos base cada 6 horas, precios del día cada 15 minutos con el mercado abierto, pre-market y after-hours cada 10 minutos, calendario cada 3 horas y Fed cada 20 minutos. La página nunca espera una descarga: muestra siempre lo último que tiene, con la hora de los datos, y avisa si la actualización está demorada. Solo después de un reinicio muestra un mensaje de carga hasta tener los primeros datos.
- El log de Streamlit registra cuánto tarda cada paso (líneas que empiezan con `[motor ...]`).
- El botón "Actualizar ahora" pide un cálculo nuevo si el último tiene más de 2 minutos.
- **GitHub Actions** corre una vez por día después del cierre y guarda la foto del día en `site/data/history/`, para poder ver días anteriores con el selector.

## Archivos

- `app.py`: la app de Streamlit.
- `scripts/build_data.py`: descarga y cálculo.
- `site/index.html`: el diseño de la página.
- `.streamlit/config.toml`: colores de Streamlit.
- `.github/workflows/update.yml`: foto diaria al cierre.

## Notas

- Las apps de Streamlit Community Cloud se duermen tras 12 horas sin visitas. El primero que entra hace clic en "Yes, get this app back up!" y espera alrededor de un minuto.
- Precios de Yahoo Finance con hasta 15 minutos de demora. Si Yahoo falla, la página muestra el último cierre guardado con un aviso.
- No contempla feriados de EE.UU.: un feriado se ve como el último día hábil.

## Temáticas

Están en `themes.json`, en la raíz del repo. Cada temática junta:
- las empresas cuya sub-industria GICS está en `gics` (se actualiza solo cuando cambia el índice),
- más los tickers de `agregar` (listas revisadas a mano, para temáticas que cruzan industrias),
- menos los de `excluir`.

Para editarlas: abrí `themes.json` en GitHub, tocá el lápiz, cambiá y hacé commit. La app toma el cambio sola.

Conviene revisarlas una vez por trimestre, después del rebalanceo del S&P 500 (tercer viernes de marzo, junio, septiembre y diciembre). La página muestra qué porcentaje del índice cubren las temáticas, y si algún ticker de las listas ya no está en el índice.

## Clima del día

Compara el promedio de los sectores cíclicos contra el de los defensivos (Consumo básico, Salud y Utilities) y, con la tasa a 10 años, el VIX, el SPY, el oro y el Russell 2000, indica si el día se parece a un fly to quality, a un risk on o a presión de tasas. Los umbrales están en `REGIME`, al principio de `scripts/build_data.py`, y se pueden ajustar ahí.

## Amplitud

Tres gráficos del último año: línea de avance y declive, porcentaje de acciones sobre su media de 50 y de 200 ruedas, y máximos netos de 52 semanas como % de las empresas del índice ((máximos − mínimos) ÷ empresas × 100, media de 10 ruedas, precios de cierre; bandas Fuerte y Débil en los percentiles 80 y 20 del último año). Debajo, una lectura automática que combina el nivel de cada indicador, su tendencia de 20 ruedas y las divergencias con el S&P 500. Cada día se calcula con las empresas que estaban en el índice ese día, reconstruidas con la tabla de altas y bajas del S&P 500 de Wikipedia (copia en `site/data/changes.json`). Las empresas que salieron y ya no cotizan (en general, adquiridas) quedan afuera; la página muestra la cobertura.

## Índices y tasas

- Índices (S&P 500, MidCap 400, SmallCap 600, Nasdaq 100, Dow Jones y MSCI World, este último medido con el ETF URTH): Yahoo Finance, en vivo con hasta ~15 minutos de demora. Si un índice no está disponible se usa su ETF (IJH, IJR, QQQ, DIA) y se aclara.
- Tasas en vivo: índices de rendimiento de Cboe (5, 10 y 30 años) y futuro de rendimiento a 2 años de CBOT, vía Yahoo.
- Tasa a 1 año: solo cierre oficial (no hay dato en vivo gratuito).
- Historia de tasas y variaciones de 1 semana a 1 año: cierre oficial del Tesoro de EE.UU. (respaldo: FRED).

## Horario, pre-market y after-hours

- Horario, feriados y medias jornadas: calendario de NYSE de la librería `exchange_calendars`.
- Antes de la apertura y después del cierre (hasta las 20:00 de Nueva York) aparece un resumen con futuros, las 10 de mayor peso, petróleo, oro y dólar, contra el último cierre de la rueda. El de pre-market desaparece al abrir.

## Calendario económico y Fed

- Fechas oficiales en `scripts/extras.py` (`EVENTS`): Fed (FOMC), BLS (CPI, PPI, empleo, JOLTS), BEA (PIB, PCE) y Census (ventas minoristas). **Hay que cargar las fechas del año siguiente una vez por año**; la página avisa cuando faltan menos de 45 días de fechas cargadas. Los pedidos de desempleo se generan solos (jueves 8:30).
- Último dato publicado de cada indicador: FRED.
- Probabilidades de la próxima reunión de la Fed: cálculo propio con futuros de fed funds a 30 días (CBOT, vía Yahoo) y la metodología de CME FedWatch.

## PDF

El botón "Descargar PDF" baja un resumen con los mismos datos de la página: al cierre si es un día anterior o el mercado ya cerró, hasta la última actualización si está abierto, y con el pre-market o el after-hours si corresponde. El encabezado del PDF aclara a qué hora corresponden los datos.

## Mapa del S&P 500

Mapa de calor tipo treemap: cada acción del índice agrupada por sector, con tamaño según su peso en el SPY y color según su variación del día (escala de -3% a +3%).

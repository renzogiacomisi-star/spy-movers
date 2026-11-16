# Qué movió al SPY

Página que descompone el movimiento del SPY por acción y por sector, en puntos del S&P 500, con amplitud de mercado y contexto macro. Con el mercado abierto se actualiza sola cada 15 minutos, y tiene un botón "Actualizar ahora".

## Cómo funciona

- **Streamlit Community Cloud** corre `app.py`. Con el mercado abierto recalcula cada 15 minutos (`REFRESH_SEC`) y el resultado se comparte entre todos los usuarios conectados: da igual si la miran 2 o 30 personas. Cada pestaña abierta revisa cada minuto si hay un cálculo nuevo y, si no lo hay, no cambia nada. Mientras se calcula aparece el aviso "Actualizando datos…" y se sigue viendo la versión anterior.
- El botón "Actualizar ahora" fuerza un cálculo nuevo solo si el último tiene más de 2 minutos, para no saturar a Yahoo. F5 recarga la página con el último cálculo compartido.
- Pesos (SSGA), sectores (Wikipedia) e historia de precios (Yahoo) se descargan como máximo cada 6 horas.
- **GitHub Actions** corre una vez por día después del cierre y guarda la foto del día en `site/data/history/`, para poder ver días anteriores con el selector.

Contribución de cada acción = peso en el SPY × variación del día. En puntos de índice: contribución / 100 × cierre anterior del S&P 500.

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

Tres gráficos del último año, calculados con los precios de las acciones del índice: línea de avance y declive, porcentaje de acciones sobre su media de 50 y de 200 ruedas, e índice de máximos netos de 52 semanas (máximos ÷ (máximos + mínimos) × 100, con media de 10 ruedas, sobre precios de cierre). Debajo, una lectura automática que combina el nivel de cada indicador, su tendencia de 20 ruedas y las divergencias con el S&P 500. Se usa la composición actual del índice para toda la historia.

## Índices y tasas

- Índices (S&P 500, MidCap 400, SmallCap 600, Nasdaq 100, Dow Jones): Yahoo Finance, en vivo con hasta ~15 minutos de demora. Si un índice no está disponible se usa su ETF (IJH, IJR, QQQ, DIA) y se aclara.
- Tasas en vivo: índices de rendimiento de Cboe (5, 10 y 30 años) y futuro de rendimiento a 2 años de CBOT, vía Yahoo.
- Historia de tasas y variaciones de 1 semana a 1 año: cierre oficial del Tesoro de EE.UU. (respaldo: FRED).

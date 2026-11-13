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

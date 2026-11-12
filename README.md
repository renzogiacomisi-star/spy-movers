# Qué movió al SPY

Página que descompone el movimiento del SPY por acción y por sector, en puntos del S&P 500, con amplitud de mercado y contexto macro. Se actualiza cada 2 minutos con el mercado abierto.

## Cómo funciona

- **Streamlit Community Cloud** corre `app.py`. Cada 2 minutos, como máximo, descarga los últimos precios, recalcula todo y muestra la página. El cálculo se comparte entre todos los usuarios conectados: da igual si la miran 2 o 30 personas.
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

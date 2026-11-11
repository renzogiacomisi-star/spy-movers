# Qué movió al SPY

Página diaria que descompone el movimiento del SPY por acción y por sector, en puntos del S&P 500, junto con la amplitud del mercado y el contexto macro.

## Cómo funciona

1. **GitHub Actions** corre `scripts/build_data.py` cuatro veces por día hábil (10:00, 12:30, 14:30 y 16:20 hora de Nueva York en horario de invierno).
2. El script baja los holdings del día desde SSGA y los precios desde Yahoo Finance, calcula todo y guarda JSON en `site/data/`.
3. El workflow hace commit de esos JSON. **Netlify** detecta el commit y publica `site/` (página estática, sin build).

Contribución de cada acción = peso en el SPY × variación del día. En puntos de índice: contribución / 100 × cierre anterior del S&P 500.

## Puesta en marcha

1. Creá un repositorio nuevo en GitHub (por ejemplo `spy-movers`) y subí todo el contenido de esta carpeta, incluida `.github/`.
2. En el repo: **Settings → Actions → General → Workflow permissions** → elegí **Read and write permissions** y guardá.
3. En la pestaña **Actions**, abrí "Actualizar datos SPY" y tocá **Run workflow** para generar el primer JSON.
4. En Netlify: **Add new site → Import an existing project → GitHub** → elegí el repo. Netlify toma la configuración de `netlify.toml` (publica la carpeta `site`, sin comando de build).
5. Listo. Cada vez que el workflow corre, Netlify se actualiza solo.

## Probar en tu computadora

```bash
pip install -r requirements.txt
python scripts/build_data.py
cd site && python -m http.server 8000
# abrir http://localhost:8000
```

## Notas

- Los cron de GitHub están en UTC y están armados para el horario de invierno de EE.UU. (UTC-5). Cuando en marzo pasa a horario de verano (UTC-4), cada corrida cae una hora más tarde en Nueva York (la de las 10:00 pasa a las 11:00). Para mantener los horarios, restá 1 a la hora de cada línea `cron` en `.github/workflows/update.yml`, y volvé a sumarla en noviembre.
- GitHub puede demorar los cron programados varios minutos en horarios de mucha carga.
- yfinance no es una fuente oficial: si algún día falla, el workflow queda en rojo y la página sigue mostrando el último dato bueno.
- Si una acción no tiene precio del día aparece al pie de la página, y el control de residuo te muestra si eso afecta el total.

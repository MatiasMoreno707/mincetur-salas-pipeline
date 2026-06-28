# MINCETUR — Pipeline ETL y visualización de salas de juego

Breve descripción
- Script Python para extraer, normalizar, geocodificar, enriquecer y visualizar el inventario de salas de juego obtenido del portal MINCETUR.
- Objetivo: producir un mapa interactivo (`output/mincetur_salas_map.html`) y reportes (`output/pipeline_summary.csv`, `output/geocode_detail.xlsx`).

Requisitos
- Python 3.10+ (se usa en un virtualenv local)
- Dependencias: ver `requirements.txt` (instalar con `pip install -r requirements.txt`).

Instalación rápida
```powershell
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Ejecución del pipeline
- Comando único:
```powershell
$env:PYTHONUTF8="1"; python main.py --departamento Lima --provincia Lima --pois csv
```
- El pipeline siempre: (1) scrapeará MINCETUR, (2) detectará registros nuevos via delta y los geocodificará, (3) enriquecerá con POIs y (4) generará el mapa.
- Flags disponibles:
  - `--departamento`, `--provincia`: filtros por ubicación (por defecto `Lima`/`Lima`)
  - `--pois`: `csv` (usar `data/raw/pois_ley27153.csv`, recomendado) o `new` (regenerar desde Overpass API)

Arquitectura y mapeo a código
- `src/extract.py`: extracción (Playwright) → `data/raw/mincetur_salas.csv`
- `src/transform.py`: limpieza, normalización y deduplicación → `data/raw/mincetur_salas_transformed.csv`
- `src/geocode.py`: geocodificación (Geopy / Nominatim) → `data/raw/mincetur_salas_geocoded.csv`
- `src/enrich.py`: enriquecimiento con POIs (Overpass / OSMnx) → `data/raw/mincetur_salas_enriched.csv`
- `src/visualize.py`: genera `output/mincetur_salas_map.html` (Folium) y controla capas (Lima: marcadores/POI/heat; otras: polígonos departamentales)
- `src/report.py`: guarda `output/pipeline_summary.csv` y `output/geocode_detail.xlsx`

Estructura de datos relevante
- `data/raw/mincetur_salas.csv` — CSV crudo de origen (portal MINCETUR).
- `data/raw/mincetur_salas_transformed.csv` — CSV limpio y deduplicado (producción de `src/transform.py`).
- `data/raw/mincetur_salas_geocoded.csv` — CSV con `latitude`/`longitude` tras geocodificación.
- `data/raw/mincetur_salas_enriched.csv` — CSV con campos añadidos por `src/enrich.py` (POIs, cumplimiento Ley 27153).
- `data/raw/mincetur_salas_summary.csv` — Resumen agregado generado durante el pipeline (conteos por etapa).
- `data/raw/geo/peru_departamental_simple.geojson` — Polígonos departamentales usados por `src/visualize.py` para el mapa de departamentos.
- `data/raw/geo/peru_distrital_simple.geojson` — Polígonos distritales usados por `src/visualize.py` para vistas a nivel distrital.
- `output/` — Salidas del pipeline: mapa HTML y reportes.

Cómo se calculan los conteos mostrados en reportes
- `pipeline_summary.csv` incluye los siguientes campos:
  - Metadatos de ejecución: `start_datetime`, `end_datetime`, `total_duration_seconds`, `departamento_filter`, `provincia_filter`
  - Conteos: `scrape_records` (crudo), `clean_records` (tras deduplicar), `filtered_records` (aplicando filtros), `geocoded_records`, `not_geocoded_records`, `enriched_records`
  - Porcentajes: `geocoded_pct`, `not_geocoded_pct`
  - Duraciones por etapa: `scrape_duration_seconds`, `transform_duration_seconds`, `geocode_duration_seconds`, `enrich_duration_seconds`
- `geocode_detail.xlsx` contiene dos hojas: `Geocoding Records` (una fila por registro filtrado, con lat/lng o NULL) y `Geocoding Summary` (agregado por departamento/provincia/distrito con porcentajes).

Notas operativas y buenas prácticas
- El pipeline siempre scrapeará MINCETUR y aplicará geocodificación en **modo delta**: identifica registros nuevos mediante la clave compuesta `ruc + resolucion + vigencia` y llama a Nominatim solo para ellos. Esto es robusto frente a duplicados por empresa o establecimiento, ya que la resolución y la vigencia son únicas por autorización.
- Respeta el delay configurado en `config.py` al geocodificar para no exceder las políticas de uso de Nominatim.
- Para regenerar el mapa tras cambiar datos o parámetros, ejecuta `main.py` y revisa `output/mincetur_salas_map.html` en un navegador.
- Los conteos de departamentos en el mapa se calculan desde `data/raw/mincetur_salas_transformed.csv` (datos limpios), no desde el subconjunto geocodificado — esto evita perder establecimientos no geocodificados.

---
Actualizado: documentación básica para entender y ejecutar el proyecto. (Se ha omitido la carpeta `diagramas` en este README.)

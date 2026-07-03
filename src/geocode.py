from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from time import sleep

import pandas as pd
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import config

OUTPUT_FILE_NAME = "mincetur_salas_geocoded.csv"
DEPARTAMENTAL_GEOJSON = "peru_departamental_simple.geojson"
VALIDATION_DEPARTAMENTO = "LIMA"

# Columnas que forman la clave compuesta para identificar registros únicos.
# Se usa ruc + resolucion + vigencia: los duplicados por empresa/establecimiento
# comparten ruc pero difieren en resolución o vigencia.
_KEY_COLS = ["ruc", "resolucion", "vigencia"]


def get_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / OUTPUT_FILE_NAME


def load_geocoded_df() -> pd.DataFrame | None:
    path = get_output_path()
    if not path.exists():
        return None

    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:
        print(f"⚠ No se pudo cargar el CSV geocodificado existente: {e}")
        return None


def _build_composite_key(df: pd.DataFrame) -> pd.Series:
    """Devuelve una Serie con la clave compuesta ruc||resolucion||vigencia."""
    parts = []
    for col in _KEY_COLS:
        if col in df.columns:
            parts.append(df[col].astype(str).str.strip().str.upper())
        else:
            parts.append(pd.Series([""] * len(df), index=df.index))
    return parts[0] + "||" + parts[1] + "||" + parts[2]


@lru_cache(maxsize=1)
def _load_validation_polygon():
    """Carga el polígono del departamento de Lima para la validación espacial (.within)."""
    try:
        import geopandas as gpd
    except Exception as e:  # pragma: no cover
        print(f"⚠ GeoPandas no disponible; se omite la validación espacial: {e}")
        return None

    workspace_root = Path(__file__).resolve().parents[1]
    geo_path = workspace_root / "data" / "raw" / "geo" / DEPARTAMENTAL_GEOJSON
    if not geo_path.exists():
        print("⚠ GeoJSON departamental no encontrado; se omite la validación espacial.")
        return None

    try:
        gdf = gpd.read_file(geo_path)
        lima = gdf[gdf["NOMBDEP"].astype(str).str.upper() == VALIDATION_DEPARTAMENTO]
        if lima.empty:
            print("⚠ No se encontró el polígono de Lima; se omite la validación espacial.")
            return None
        # Unión de geometrías por si Lima está compuesta de varias partes.
        return lima.geometry.union_all() if hasattr(lima.geometry, "union_all") else lima.geometry.unary_union
    except Exception as e:  # pragma: no cover
        print(f"⚠ Error al cargar el polígono de validación: {e}")
        return None


def _within_lima(lat: float, lon: float, polygon) -> bool:
    """Verifica que la coordenada esté contenida en el polígono de Lima (.within)."""
    if polygon is None:
        return True  # sin polígono no se puede validar; se acepta por defecto
    try:
        from shapely.geometry import Point
        return Point(float(lon), float(lat)).within(polygon)
    except Exception:
        return True


def _candidate_addresses(row: pd.Series) -> list[str]:
    """Construye la lista escalonada de direcciones a intentar (completa → simplificada).

    Usa las variantes normalizadas generadas en la transformación; a cada variante
    le añade distrito, provincia, departamento y "Peru".
    """
    sufijo_parts = []
    if pd.notna(row.get("distrito")):
        sufijo_parts.append(str(row["distrito"]))
    if pd.notna(row.get("provincia")):
        sufijo_parts.append(str(row["provincia"]))
    if pd.notna(row.get("departamento")):
        sufijo_parts.append(str(row["departamento"]))
    sufijo = ", ".join(sufijo_parts)

    variantes: list[str] = []
    raw = row.get("direccion_variantes")
    if isinstance(raw, str) and raw.strip():
        try:
            variantes = json.loads(raw)
        except Exception:
            variantes = []
    if not variantes and pd.notna(row.get("direccion_normalizada")):
        variantes = [str(row["direccion_normalizada"])]

    direcciones = []
    for v in variantes:
        partes = [p for p in [v, sufijo, "Peru"] if p]
        direccion = ", ".join(partes)
        if direccion not in direcciones:
            direcciones.append(direccion)
    return direcciones


def _geocode_once(geolocator, address: str):
    """Geocodifica una dirección con reintentos ante timeouts y errores 429."""
    for intento in range(1, config.GEOCODING_MAX_RETRIES + 1):
        try:
            return geolocator.geocode(address, timeout=config.GEOCODING_TIMEOUT)
        except GeocoderTimedOut:
            if intento < config.GEOCODING_MAX_RETRIES:
                sleep(config.GEOCODING_DELAY)
        except GeocoderServiceError as e:
            if "429" in str(e):
                print(f"  ⚠ Rate limit 429 (intento {intento}/{config.GEOCODING_MAX_RETRIES}) — esperando {config.GEOCODING_RETRY_WAIT:.0f}s...")
                sleep(config.GEOCODING_RETRY_WAIT)
            else:
                print(f"  ⚠ Error: {e}")
                return None
    return None


def geocode_salas(
    df: pd.DataFrame,
    departamento: str | None = None,
    provincia: str | None = None,
    existing_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Geocodifica registros de salas con estrategia escalonada y validación espacial.

    - Escalonada: intenta la dirección completa y luego versiones simplificadas
      (variantes generadas en la transformación) hasta obtener una coordenada.
    - Validación espacial: solo se aceptan coordenadas contenidas dentro del
      polígono del departamento de Lima (operación .within de GeoPandas/Shapely).
    - Modo delta: si se pasa `existing_df`, reutiliza coordenadas ya calculadas
      (clave ruc+resolucion+vigencia) y geocodifica solo los registros nuevos.
    """
    df = df.copy()

    original_len = len(df)
    if departamento:
        df = df[df["departamento"].str.upper() == departamento.upper()]
        print(f"✓ Filtro aplicado: Departamento = {departamento} ({len(df)} registros de {original_len})")
    if provincia:
        df = df[df["provincia"].str.upper() == provincia.upper()]
        print(f"✓ Filtro aplicado: Provincia = {provincia} ({len(df)} registros de {original_len})")

    if len(df) == 0:
        print("⚠ Ningún registro coincide con los filtros proporcionados")
        return df

    # --- Modo delta: reutilizar coordenadas existentes ---
    if existing_df is not None and not existing_df.empty:
        df_keys = _build_composite_key(df)
        ex_keys = _build_composite_key(existing_df)
        ex_with_key = existing_df.assign(_key=ex_keys)
        coords_lat = ex_with_key.dropna(subset=["latitude"]).groupby("_key")["latitude"].last()
        coords_lon = ex_with_key.dropna(subset=["longitude"]).groupby("_key")["longitude"].last()
        df["_key"] = df_keys
        df["latitude"] = df["_key"].map(coords_lat)
        df["longitude"] = df["_key"].map(coords_lon)
        df = df.drop(columns=["_key"])

        nuevos = int(df["latitude"].isna().sum())
        existentes = len(df) - nuevos
        print(f"Modo delta: {existentes} registros con coordenadas reutilizadas, {nuevos} nuevos a geocodificar.")
    else:
        df["latitude"] = None
        df["longitude"] = None

    polygon = _load_validation_polygon()

    # Validación espacial de las coordenadas reutilizadas: descartar las que caen
    # fuera del polígono de Lima para mantener el dataset coherente con la Fase 3.
    revalidados = 0
    for idx, row in df.iterrows():
        if pd.notna(row["latitude"]) and pd.notna(row["longitude"]):
            if not _within_lima(row["latitude"], row["longitude"], polygon):
                df.at[idx, "latitude"] = None
                df.at[idx, "longitude"] = None
                revalidados += 1
    if revalidados:
        print(f"ℹ {revalidados} coordenadas reutilizadas descartadas por caer fuera de Lima.")

    pendientes = int(df["latitude"].isna().sum())
    if pendientes == 0:
        print("✓ Todos los registros ya tienen coordenadas. No se llama a Nominatim.")
        _save(df)
        return df

    geolocator = Nominatim(user_agent="expansion_tool_v1")
    print(f"Iniciando geocodificación escalonada de {pendientes} ubicaciones...")

    contador = 0
    fuera_de_lima = 0
    for idx, row in df.iterrows():
        if not (pd.isna(row["latitude"]) or pd.isna(row["longitude"])):
            continue
        contador += 1
        empresa = row.get("empresa", "N/A")

        aceptado = False
        for address in _candidate_addresses(row):
            location = _geocode_once(geolocator, address)
            sleep(config.GEOCODING_DELAY)  # respetar la política de uso de Nominatim
            if not location:
                continue
            if _within_lima(location.latitude, location.longitude, polygon):
                df.at[idx, "latitude"] = location.latitude
                df.at[idx, "longitude"] = location.longitude
                print(f"✓ [{contador}/{pendientes}] {empresa} -> ({location.latitude:.4f}, {location.longitude:.4f})")
                aceptado = True
                break
            else:
                fuera_de_lima += 1  # coordenada descartada por caer fuera de Lima

        if not aceptado:
            print(f"✗ [{contador}/{pendientes}] Sin coordenadas válidas: {empresa}")

    if fuera_de_lima:
        print(f"ℹ {fuera_de_lima} resultados descartados por caer fuera del polígono de Lima.")

    _save(df)
    return df


def _save(df: pd.DataFrame) -> None:
    output_path = get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"Geocoding completado. Archivo guardado en: {output_path}")

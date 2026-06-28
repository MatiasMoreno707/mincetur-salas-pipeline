from __future__ import annotations

from pathlib import Path
from time import sleep

import pandas as pd
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import config

OUTPUT_FILE_NAME = "mincetur_salas_geocoded.csv"

# Columnas que forman la clave compuesta para identificar registros únicos.
# Se usa ruc + resolucion + vigencia: los duplicados por empresa/establecimiento
# comparten ruc pero difieren en resolución o vigencia.
_KEY_COLS = ["ruc", "resolucion", "vigencia"]


def _build_composite_key(df: pd.DataFrame) -> pd.Series:
    """Devuelve una Serie con la clave compuesta ruc||resolucion||vigencia."""
    parts = []
    for col in _KEY_COLS:
        if col in df.columns:
            parts.append(df[col].astype(str).str.strip().str.upper())
        else:
            parts.append(pd.Series([""] * len(df), index=df.index))
    return parts[0] + "||" + parts[1] + "||" + parts[2]


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


def geocode_salas(
    df: pd.DataFrame,
    departamento: str | None = None,
    provincia: str | None = None,
    existing_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Geocodifica registros de salas.

    Si se pasa `existing_df`, opera en modo delta: copia las coordenadas de los
    registros ya geocodificados (identificados por ruc+resolucion+vigencia) y
    llama a Nominatim solo para los registros sin coordenadas previas.
    Si `existing_df` es None, geocodifica todo desde cero.
    """
    df = df.copy()

    # Aplicar filtros si se proporcionan
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

    pendientes = int(df["latitude"].isna().sum())
    if pendientes == 0:
        print("✓ Todos los registros ya tienen coordenadas. No se llama a Nominatim.")
        output_path = get_output_path()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"Geocoding completado. Archivo guardado en: {output_path}")
        return df

    # Initialize geocoder
    geolocator = Nominatim(user_agent="expansion_tool_v1")

    print(f"Iniciando geocodificación de {pendientes} ubicaciones...")

    contador = 0
    for idx, row in df.iterrows():
        if pd.isna(row["latitude"]) or pd.isna(row["longitude"]):
            contador += 1
            address_parts = []
            if pd.notna(row.get("direccion_normalizada")):
                address_parts.append(row["direccion_normalizada"])
            if pd.notna(row.get("distrito")):
                address_parts.append(row["distrito"])
            if pd.notna(row.get("provincia")):
                address_parts.append(row["provincia"])
            if pd.notna(row.get("departamento")):
                address_parts.append(row["departamento"])

            address = ", ".join(address_parts) + ", Peru"

            for intento in range(1, config.GEOCODING_MAX_RETRIES + 1):
                try:
                    location = geolocator.geocode(address, timeout=config.GEOCODING_TIMEOUT)
                    if location:
                        df.at[idx, "latitude"] = location.latitude
                        df.at[idx, "longitude"] = location.longitude
                        print(f"✓ [{contador}/{pendientes}] {row.get('empresa', 'N/A')} -> ({location.latitude:.4f}, {location.longitude:.4f})")
                    else:
                        print(f"✗ [{contador}/{pendientes}] Sin resultado: {address}")
                    break
                except GeocoderTimedOut:
                    print(f"⚠ [{contador}/{pendientes}] Timeout (intento {intento}/{config.GEOCODING_MAX_RETRIES}): {row.get('empresa', 'N/A')}")
                    if intento < config.GEOCODING_MAX_RETRIES:
                        sleep(config.GEOCODING_DELAY)
                except GeocoderServiceError as e:
                    if "429" in str(e):
                        print(f"⚠ [{contador}/{pendientes}] Rate limit 429 (intento {intento}/{config.GEOCODING_MAX_RETRIES}) — esperando {config.GEOCODING_RETRY_WAIT:.0f}s...")
                        sleep(config.GEOCODING_RETRY_WAIT)
                    else:
                        print(f"⚠ [{contador}/{pendientes}] Error: {e}")
                        break
            
            # Be respectful to Nominatim API - add delay
            sleep(config.GEOCODING_DELAY)
    
    output_path = get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\nGeocoding completado. Archivo guardado en: {output_path}")
    
    return df


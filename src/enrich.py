from __future__ import annotations

from pathlib import Path
import json

import pandas as pd

OUTPUT_FILE_NAME = "mincetur_salas_enriched.csv"
POIS_FILE_NAME = "pois_ley27153.csv"

# CRS métrico para Lima (UTM zona 18S) usado en los cálculos de distancia en metros.
METRIC_CRS = 32718
WGS84 = 4326
RADIO_LEY_27153_M = 150.0

# Categorías de lugares sensibles del Artículo 5 de la Ley N.° 27153 y sus etiquetas OSM.
POI_TAGS = {
    "amenity": [
        "place_of_worship", "school", "kindergarten", "university", "college",
        "hospital", "clinic", "police", "fire_station",
    ],
    "building": ["church"],
    "military": ["barracks"],
}


def get_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / OUTPUT_FILE_NAME


def get_pois_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / POIS_FILE_NAME


def get_geo_dir() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / "geo"


def _categorize(tags: dict) -> str:
    amenity = tags.get("amenity")
    if amenity == "place_of_worship" or tags.get("building") == "church":
        return "Iglesia"
    if amenity in {"school", "kindergarten", "university", "college"}:
        return "Centro educativo"
    if amenity in {"hospital", "clinic"}:
        return "Centro hospitalario"
    if amenity == "police":
        return "Comisaría"
    if amenity == "fire_station" or tags.get("military") == "barracks":
        return "Cuartel"
    return "Otro"


def _fetch_sensitive_pois_osmnx(df: pd.DataFrame) -> pd.DataFrame:
    """Descarga los POIs sensibles desde OpenStreetMap (Overpass) mediante OSMnx."""
    import osmnx as ox
    from shapely.geometry import box

    df = df.copy()
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    if df["latitude"].dropna().empty or df["longitude"].dropna().empty:
        return pd.DataFrame(columns=["name", "category", "latitude", "longitude", "tags"])

    south = float(df["latitude"].min()) - 0.05
    north = float(df["latitude"].max()) + 0.05
    west = float(df["longitude"].min()) - 0.05
    east = float(df["longitude"].max()) + 0.05

    area = box(west, south, east, north)
    print("Descargando POIs sensibles con OSMnx (Overpass)...")
    gdf = ox.features_from_polygon(area, tags=POI_TAGS)

    if gdf.empty:
        return pd.DataFrame(columns=["name", "category", "latitude", "longitude", "tags"])

    # Usar el centroide (proyectado) para geometrías que no sean puntos.
    geom = gdf.geometry
    centroids = geom.to_crs(METRIC_CRS).centroid.to_crs(WGS84)

    records = []
    for (idx, row), pt in zip(gdf.iterrows(), centroids):
        if pt is None or pt.is_empty:
            continue
        tags = {k: row[k] for k in ("amenity", "building", "military") if k in gdf.columns and pd.notna(row.get(k))}
        name = row.get("name") if "name" in gdf.columns and pd.notna(row.get("name")) else "Lugar sensible"
        records.append({
            "name": name,
            "category": _categorize(tags),
            "latitude": pt.y,
            "longitude": pt.x,
            "tags": json.dumps(tags, ensure_ascii=False),
        })

    pois_df = pd.DataFrame(records)
    _save_pois_layers(pois_df)
    return pois_df


def _save_pois_layers(pois_df: pd.DataFrame) -> None:
    """Guarda el CSV combinado y una capa GeoJSON independiente por categoría."""
    pois_path = get_pois_output_path()
    pois_path.parent.mkdir(parents=True, exist_ok=True)
    pois_df.to_csv(pois_path, index=False, encoding="utf-8-sig")

    if pois_df.empty:
        return

    try:
        import geopandas as gpd
        gdf = gpd.GeoDataFrame(
            pois_df.copy(),
            geometry=gpd.points_from_xy(pois_df["longitude"], pois_df["latitude"]),
            crs=WGS84,
        )
        geo_dir = get_geo_dir()
        geo_dir.mkdir(parents=True, exist_ok=True)
        for category, group in gdf.groupby("category"):
            slug = (
                str(category).lower()
                .replace(" ", "_").replace("í", "i").replace("á", "a")
                .replace("é", "e").replace("ó", "o").replace("ú", "u")
            )
            group.to_file(geo_dir / f"pois_{slug}.geojson", driver="GeoJSON")
        print(f"✓ POIs guardados: {len(pois_df)} registros en {gdf['category'].nunique()} capas GeoJSON por categoría.")
    except Exception as e:
        print(f"⚠ No se pudieron escribir las capas GeoJSON por categoría: {e}")


def _load_sensitive_pois() -> pd.DataFrame:
    pois_path = get_pois_output_path()
    if pois_path.exists():
        try:
            return pd.read_csv(pois_path, encoding="utf-8-sig")
        except Exception:
            pass
    return pd.DataFrame(columns=["name", "category", "latitude", "longitude", "tags"])


def _ensure_pois(df: pd.DataFrame, mode: str = "csv") -> pd.DataFrame:
    mode = (mode or "csv").lower()
    if mode in ("new", "overpass", "osmnx"):
        return _fetch_sensitive_pois_osmnx(df)
    # modo csv: usar el CSV existente; si no hay, descargar como respaldo.
    pois_df = _load_sensitive_pois()
    if not pois_df.empty:
        return pois_df
    return _fetch_sensitive_pois_osmnx(df)


def _compute_compliance(df: pd.DataFrame, pois_df: pd.DataFrame) -> pd.DataFrame:
    """Calcula el cumplimiento de la Ley 27153 usando GeoPandas en un CRS métrico.

    Para cada establecimiento determina la distancia (en metros) al lugar sensible
    más cercano, cuántos hay dentro de 150 m y si cumple (distancia > 150 m).
    """
    import geopandas as gpd

    df = df.copy()
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    cols_default = {
        "closest_sensitive_distance_m": pd.NA,
        "closest_sensitive_name": pd.NA,
        "closest_sensitive_category": pd.NA,
        "sensitive_sites_within_150m": 0,
        "complies_ley_27153": pd.NA,
    }

    valid_pois = pois_df.dropna(subset=["latitude", "longitude"]) if not pois_df.empty else pois_df
    if valid_pois.empty or df.dropna(subset=["latitude", "longitude"]).empty:
        for col, val in cols_default.items():
            df[col] = val
        return df

    pois_gdf = gpd.GeoDataFrame(
        valid_pois.copy(),
        geometry=gpd.points_from_xy(valid_pois["longitude"], valid_pois["latitude"]),
        crs=WGS84,
    ).to_crs(METRIC_CRS)

    distances, names, categories, counts, complies = [], [], [], [], []
    for _, row in df.iterrows():
        if pd.isna(row["latitude"]) or pd.isna(row["longitude"]):
            distances.append(pd.NA); names.append(pd.NA); categories.append(pd.NA)
            counts.append(0); complies.append(pd.NA)
            continue

        punto = gpd.GeoSeries(
            gpd.points_from_xy([row["longitude"]], [row["latitude"]]), crs=WGS84
        ).to_crs(METRIC_CRS).iloc[0]

        dist = pois_gdf.geometry.distance(punto)
        idx_min = dist.idxmin()
        best = float(dist.loc[idx_min])
        distances.append(round(best, 2))
        names.append(pois_gdf.loc[idx_min, "name"])
        categories.append(pois_gdf.loc[idx_min, "category"])
        counts.append(int((dist <= RADIO_LEY_27153_M).sum()))
        complies.append(best > RADIO_LEY_27153_M)

    df["closest_sensitive_distance_m"] = distances
    df["closest_sensitive_name"] = names
    df["closest_sensitive_category"] = categories
    df["sensitive_sites_within_150m"] = counts
    df["complies_ley_27153"] = complies
    return df


def enrich_salas(df: pd.DataFrame, pois_mode: str = "csv") -> pd.DataFrame:
    df = df.copy()
    df["source"] = "MINCETUR"
    df["extraction_date"] = pd.Timestamp.now().floor("s")
    pois_df = _ensure_pois(df, mode=pois_mode)
    df = _compute_compliance(df, pois_df)

    output_path = get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
    except PermissionError:
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = output_path.with_name(output_path.stem + f"_{ts}" + output_path.suffix)
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        print(f"Aviso: no se pudo sobrescribir {output_path}; se guardó en {fallback}.")
    return df

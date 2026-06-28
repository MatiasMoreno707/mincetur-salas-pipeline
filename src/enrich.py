from __future__ import annotations

from pathlib import Path
import json
import math
import urllib.parse
import urllib.request

import pandas as pd

OUTPUT_FILE_NAME = "mincetur_salas_enriched.csv"
POIS_FILE_NAME = "pois_ley27153.csv"


def get_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / OUTPUT_FILE_NAME


def get_pois_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / POIS_FILE_NAME


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def _ensure_numeric_lat_lon(df: pd.DataFrame) -> pd.DataFrame:
    if "latitude" in df.columns and "longitude" in df.columns:
        df = df.copy()
        df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
        df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    return df


def _load_sensitive_pois() -> pd.DataFrame:
    pois_path = get_pois_output_path()
    if pois_path.exists():
        try:
            return pd.read_csv(pois_path, encoding="utf-8-sig")
        except Exception:
            pass
    return pd.DataFrame(columns=["name", "category", "latitude", "longitude", "tags"])


def _save_sensitive_pois(pois: list[dict]) -> pd.DataFrame:
    pois_df = pd.DataFrame(pois)
    pois_path = get_pois_output_path()
    pois_path.parent.mkdir(parents=True, exist_ok=True)
    pois_df.to_csv(pois_path, index=False, encoding="utf-8-sig")
    return pois_df


def _element_to_poi(el: dict) -> dict | None:
    tags = el.get("tags", {}) or {}
    if el.get("type") == "node":
        lat = el.get("lat")
        lon = el.get("lon")
    else:
        center = el.get("center")
        if not center:
            return None
        lat = center.get("lat")
        lon = center.get("lon")
    if lat is None or lon is None:
        return None

    name = tags.get("name") or tags.get("official_name") or tags.get("alt_name") or "Lugar sensible"
    category = "Otro"
    if tags.get("amenity") == "place_of_worship" or tags.get("building") == "church":
        category = "Iglesia"
    elif tags.get("amenity") in {"school", "kindergarten", "university", "college"}:
        category = "Centro educativo"
    elif tags.get("amenity") in {"hospital", "clinic"}:
        category = "Centro hospitalario"
    elif tags.get("amenity") == "police":
        category = "Comisaría"
    elif tags.get("amenity") == "fire_station" or tags.get("military") == "barracks":
        category = "Cuartel"

    return {
        "name": name,
        "category": category,
        "latitude": float(lat),
        "longitude": float(lon),
        "tags": json.dumps(tags, ensure_ascii=False),
    }


def _build_overpass_query(south: float, west: float, north: float, east: float) -> str:
    filters = [
        '["amenity"="place_of_worship"]',
        '["building"="church"]',
        '["amenity"="school"]',
        '["amenity"="kindergarten"]',
        '["amenity"="university"]',
        '["amenity"="college"]',
        '["amenity"="hospital"]',
        '["amenity"="clinic"]',
        '["amenity"="police"]',
        '["amenity"="fire_station"]',
        '["military"="barracks"]',
    ]
    blocks = []
    for f in filters:
        blocks.extend([
            f"node{f}({south},{west},{north},{east});",
            f"way{f}({south},{west},{north},{east});",
            f"relation{f}({south},{west},{north},{east});",
        ])
    query = "\n".join(blocks)
    return f"[out:json][timeout:120];\n(\n{query}\n);\nout center qt;"


def _query_overpass(south: float, west: float, north: float, east: float) -> list[dict]:
    query = _build_overpass_query(south, west, north, east)
    url = "https://overpass-api.de/api/interpreter"
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "expansion-tool/1.0"})
    with urllib.request.urlopen(req, timeout=180) as response:
        response_data = response.read().decode("utf-8")
    result = json.loads(response_data)
    pois = []
    for element in result.get("elements", []):
        poi = _element_to_poi(element)
        if poi is not None:
            pois.append(poi)
    return pois


def _fetch_sensitive_pois_from_overpass(df: pd.DataFrame) -> pd.DataFrame:
    df = _ensure_numeric_lat_lon(df)
    if df["latitude"].dropna().empty or df["longitude"].dropna().empty:
        return pd.DataFrame(columns=["name", "category", "latitude", "longitude", "tags"])
    south = float(df["latitude"].min()) - 0.05
    north = float(df["latitude"].max()) + 0.05
    west = float(df["longitude"].min()) - 0.05
    east = float(df["longitude"].max()) + 0.05
    pois = _query_overpass(south, west, north, east)
    return _save_sensitive_pois(pois)


def _ensure_pois(df: pd.DataFrame) -> pd.DataFrame:
    pois_df = _load_sensitive_pois()
    if not pois_df.empty:
        return pois_df
    return _fetch_sensitive_pois_from_overpass(df)


def _compute_compliance(df: pd.DataFrame, pois_df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = _ensure_numeric_lat_lon(df)
    if pois_df.empty or df.empty:
        df["closest_sensitive_distance_m"] = pd.NA
        df["closest_sensitive_name"] = pd.NA
        df["closest_sensitive_category"] = pd.NA
        df["sensitive_sites_within_150m"] = 0
        df["complies_ley_27153"] = pd.NA
        return df

    pois = [
        {
            "name": row["name"],
            "category": row["category"],
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
        }
        for _, row in pois_df.iterrows()
        if pd.notna(row["latitude"]) and pd.notna(row["longitude"])
    ]

    distances = []
    nearest_names = []
    nearest_categories = []
    counts = []
    complies = []

    for _, row in df.iterrows():
        lat = row.get("latitude")
        lon = row.get("longitude")
        if pd.isna(lat) or pd.isna(lon):
            distances.append(pd.NA)
            nearest_names.append(pd.NA)
            nearest_categories.append(pd.NA)
            counts.append(0)
            complies.append(pd.NA)
            continue

        row_lat = float(lat)
        row_lon = float(lon)
        best_distance = None
        best_site = None
        count = 0
        for site in pois:
            distance = _haversine_distance(row_lat, row_lon, site["latitude"], site["longitude"])
            if distance <= 150:
                count += 1
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_site = site

        if best_site is None:
            distances.append(pd.NA)
            nearest_names.append(pd.NA)
            nearest_categories.append(pd.NA)
            counts.append(0)
            complies.append(pd.NA)
        else:
            distances.append(round(best_distance, 2))
            nearest_names.append(best_site["name"])
            nearest_categories.append(best_site["category"])
            counts.append(count)
            complies.append(best_distance > 150)

    df["closest_sensitive_distance_m"] = distances
    df["closest_sensitive_name"] = nearest_names
    df["closest_sensitive_category"] = nearest_categories
    df["sensitive_sites_within_150m"] = counts
    df["complies_ley_27153"] = complies
    return df


def _ensure_pois(df: pd.DataFrame, mode: str = "csv") -> pd.DataFrame:
    mode = (mode or "csv").lower()
    if mode == "csv":
        pois_df = _load_sensitive_pois()
        if not pois_df.empty:
            return pois_df
        return _fetch_sensitive_pois_from_overpass(df)
    if mode in ("new", "overpass"):
        return _fetch_sensitive_pois_from_overpass(df)
    # fallback
    return _load_sensitive_pois()


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
    except PermissionError as exc:
        # Fallback: write to a timestamped file to avoid overwrite when file is locked
        import datetime

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = output_path.with_name(output_path.stem + f"_{ts}" + output_path.suffix)
        try:
            df.to_csv(fallback, index=False, encoding="utf-8-sig")
            print(f"Aviso: no se pudo sobrescribir {output_path}; se guardó en {fallback} en su lugar.")
        except Exception as exc2:
            print(f"Error al guardar CSV enriquecido: {exc2}")
            raise
    return df

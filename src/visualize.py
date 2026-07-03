from __future__ import annotations

from pathlib import Path
import unicodedata
from time import sleep
import math

import pandas as pd
import folium
from folium.plugins import MarkerCluster, HeatMap
import json
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import sys
from pathlib import Path as _Path
try:
    import config
except ModuleNotFoundError:
    # allow running this module directly by adding project root to sys.path
    proj_root = _Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(proj_root))
    import config
from branca.colormap import linear

OUTPUT_FILE_NAME = "mincetur_salas_summary.csv"
MAP_FILE_NAME = "mincetur_salas_map.html"


def get_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / OUTPUT_FILE_NAME


def get_map_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "output" / MAP_FILE_NAME


def _normalize_name(value: str) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value.strip().upper())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _load_local_district_geojson() -> dict[str, dict]:
    workspace_root = Path(__file__).resolve().parents[1]
    candidate_paths = [
        workspace_root / "data" / "geo" / "peru_distrital_hd_inei2.geojson",
        workspace_root / "data" / "geo" / "peru_distrital_hd.geojson",
        workspace_root / "data" / "raw" / "geo" / "peru_distrital_simple.geojson",
    ]
    geojson = None
    used_path = None
    for local_path in candidate_paths:
        if local_path.exists():
            try:
                with local_path.open("r", encoding="utf-8") as f:
                    geojson = json.load(f)
                used_path = local_path
                break
            except Exception:
                continue

    if geojson is None:
        return {}

    print(f"Cargando GeoJSON distrital local desde: {used_path}")
    index: dict[str, dict] = {}
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        departamento = props.get("DEPARTAMEN") or props.get("NOMBDEP") or props.get("DEPARTAMENTO") or ""
        provincia = props.get("PROVINCIA") or props.get("NOMBPROV") or props.get("PROVINCIA") or ""
        distrito = props.get("DISTRITO") or props.get("NOMBDIST") or props.get("NOMBDIST") or ""
        key = "|".join(
            [_normalize_name(departamento), _normalize_name(provincia), _normalize_name(distrito)]
        )
        if key:
            index[key] = feature
    return index


def _load_local_district_geojson_full():
    workspace_root = Path(__file__).resolve().parents[1]
    candidate_paths = [
        workspace_root / "data" / "geo" / "peru_distrital_hd_inei2.geojson",
        workspace_root / "data" / "geo" / "peru_distrital_hd.geojson",
        workspace_root / "data" / "raw" / "geo" / "peru_distrital_simple.geojson",
    ]
    for local_path in candidate_paths:
        if local_path.exists():
            try:
                with local_path.open("r", encoding="utf-8") as f:
                    geojson = json.load(f)
                return geojson, local_path
            except Exception:
                continue
    return None, None


def _load_department_geojson():
    workspace_root = Path(__file__).resolve().parents[1]
    dept_path = workspace_root / "data" / "raw" / "geo" / "peru_departamental_simple.geojson"
    if not dept_path.exists():
        return None, None
    try:
        with dept_path.open("r", encoding="utf-8") as f:
            geojson = json.load(f)
        return geojson, dept_path
    except Exception:
        return None, None


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def _load_sensitive_sites() -> list[dict]:
    workspace_root = Path(__file__).resolve().parents[1]
    pois_path = workspace_root / "data" / "raw" / "pois_ley27153.csv"
    if not pois_path.exists():
        return []
    try:
        pois_df = pd.read_csv(pois_path, encoding="utf-8-sig")
    except Exception:
        return []
    sites = []
    for _, row in pois_df.iterrows():
        if pd.isna(row.get("latitude")) or pd.isna(row.get("longitude")):
            continue
        sites.append(
            {
                "name": row.get("name", "Lugar sensible"),
                "category": row.get("category", "Lugar sensible"),
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
            }
        )
    return sites


def _build_exclusion_buffers(sites: list[dict], radius_m: float = 150.0):
    """Calcula los radios de exclusión de la Ley 27153 con GeoPandas (buffer métrico).

    Proyecta los puntos a un CRS métrico (UTM 18S), genera un buffer de `radius_m`
    metros y los devuelve reproyectados a WGS84 como GeoDataFrame.
    """
    try:
        import geopandas as gpd
    except Exception:
        return None
    if not sites:
        return None
    df = pd.DataFrame(sites)
    gdf = gpd.GeoDataFrame(
        df.copy(),
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs=4326,
    ).to_crs(32718)
    gdf["geometry"] = gdf.geometry.buffer(radius_m)
    return gdf.to_crs(4326)


def _add_sensitive_sites_layer(map_obj: folium.Map, sites: list[dict]) -> None:
    layer = folium.FeatureGroup(name="Lugares sensibles", show=False)

    # Radios de exclusión de 150 m calculados con GeoPandas.
    buffers = _build_exclusion_buffers(sites, radius_m=150.0)
    if buffers is not None and not buffers.empty:
        folium.GeoJson(
            buffers.to_json(),
            name="Radios de exclusión (150 m)",
            style_function=lambda _f: {
                "fillColor": "#1f77b4",
                "color": "#1f77b4",
                "weight": 2,
                "fillOpacity": 0.15,
            },
        ).add_to(layer)

    for site in sites:
        popup_html = f"<b>{site.get('name')}</b><br>Tipo: {site.get('category')}"
        folium.Marker(
            location=[site["latitude"], site["longitude"]],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=site.get("name"),
            icon=folium.Icon(color="blue", icon="info-sign"),
        ).add_to(layer)
    layer.add_to(map_obj)


def _within_sensitive_radius(lat: float, lon: float, sites: list[dict], radius_m: float = 150.0) -> bool:
    return any(_haversine_distance(lat, lon, site["latitude"], site["longitude"]) <= radius_m for site in sites)


def _find_district_feature(local_geojson_index: dict[str, dict], departamento: str, provincia: str, distrito: str):
    search_key = "|".join([_normalize_name(departamento), _normalize_name(provincia), _normalize_name(distrito)])
    if search_key in local_geojson_index:
        return local_geojson_index[search_key]

    def matches(source: str, target: str) -> bool:
        if not source or not target:
            return False
        return source == target or source in target or target in source

    for key, feature in local_geojson_index.items():
        dep, prov, dist = key.split("|", 2)
        if matches(_normalize_name(departamento), dep) and matches(_normalize_name(provincia), prov) and matches(_normalize_name(distrito), dist):
            return feature

    return None


def visualize_salas(df: pd.DataFrame) -> pd.DataFrame:
    summary = pd.DataFrame(
        [
            {"step": "rows", "value": len(df)},
            {"step": "unique_empresas", "value": df["empresa"].nunique() if "empresa" in df.columns else None},
            {"step": "departamentos", "value": df["departamento"].nunique() if "departamento" in df.columns else None},
        ]
    )
    output_path = get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"Resumen guardado en: {output_path}")
    return summary


def create_map(df: pd.DataFrame, full_df: pd.DataFrame | None = None, departamento: str | None = None, provincia: str | None = None) -> folium.Map:
    """
    Create an interactive map with all geolocated salas.
    
    Args:
        df: DataFrame with latitude and longitude columns for markers
        full_df: DataFrame with cleaned MINCETUR records used for department counts
        departamento: optional departamento filter for marker display
        provincia: optional provincia filter for marker display
        
    Returns:
        Folium Map object
    """
    # Filter data with valid coordinates
    df_geo = df.dropna(subset=["latitude", "longitude"])
    if full_df is None:
        full_df = df
    
    if len(df_geo) == 0:
        print("⚠ No se encontraron coordenadas válidas en los datos.")
        print("Por favor, ejecuta primero la geocodificación.")
        return None
    
    # Select the subset to show as markers (e.g., Lima)
    df_selected = df_geo
    if departamento and provincia:
        df_selected = df_geo[
            (df_geo["departamento"].astype(str).str.upper() == departamento.upper()) &
            (df_geo["provincia"].astype(str).str.upper() == provincia.upper())
        ]
    elif departamento:
        df_selected = df_geo[df_geo["departamento"].astype(str).str.upper() == departamento.upper()]

    # Calculate map center from selection if available, otherwise from all geocoded points
    if len(df_selected) > 0:
        center_lat = df_selected["latitude"].mean()
        center_lon = df_selected["longitude"].mean()
    else:
        center_lat = df_geo["latitude"].mean()
        center_lon = df_geo["longitude"].mean()
    
    # Create base map
    map_obj = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=6,
        tiles="OpenStreetMap"
    )
    
    # Add marker cluster group
    marker_cluster = MarkerCluster(name="Clusters").add_to(map_obj)

    # Add district-level detail for the selected area (Lima)
    if len(df_selected) > 0:
        _add_district_heat_layer(map_obj, df_selected)

    # Load and filter sensitive sites to the selection bbox (so the POI layer shows only Lima when selected)
    sensitive_sites = _load_sensitive_sites()
    lima_sites = []
    if sensitive_sites and len(df_selected) > 0:
        min_lat = float(df_selected["latitude"].min()) - 0.05
        max_lat = float(df_selected["latitude"].max()) + 0.05
        min_lon = float(df_selected["longitude"].min()) - 0.05
        max_lon = float(df_selected["longitude"].max()) + 0.05
        for s in sensitive_sites:
            if min_lat <= s["latitude"] <= max_lat and min_lon <= s["longitude"] <= max_lon:
                lima_sites.append(s)

    if lima_sites:
        print(f"Cargando {len(lima_sites)} lugares sensibles para la capa seleccionada.")
        _add_sensitive_sites_layer(map_obj, lima_sites)
    else:
        print("No se añadieron lugares sensibles filtrados para la selección.")

    # Add individual markers with popups (only for the selected area)
    for idx, row in df_selected.iterrows():
        popup_text = f"""
        <b>{row.get('empresa', 'N/A')}</b><br>
        Establecimiento: {row.get('establecimiento', 'N/A')}<br>
        Dirección: {row.get('direccion', 'N/A')}<br>
        Distrito: {row.get('distrito', 'N/A')}<br>
        Provincia: {row.get('provincia', 'N/A')}<br>
        Departamento: {row.get('departamento', 'N/A')}<br>
        Giro: {row.get('giro', 'N/A')}<br>
        Máquinas: {row.get('maq', 'N/A')}<br>
        Mesas: {row.get('mesas', 'N/A')}<br>
        Cumple ley 27153: {row.get('complies_ley_27153', 'desconocido')}<br>
        Distancia más cercana (m): {row.get('closest_sensitive_distance_m', 'N/A')}<br>
        Sitio sensible: {row.get('closest_sensitive_name', 'N/A')} ({row.get('closest_sensitive_category', 'N/A')})
        """
        latitude = float(row["latitude"])
        longitude = float(row["longitude"])
        # Marker color: green = cumple, red = no cumple, gray = desconocido
        icon_color = "gray"
        if pd.notna(row.get("complies_ley_27153")):
            icon_color = "green" if bool(row.get("complies_ley_27153")) else "red"
        else:
            # if unknown but within sensitive radius, mark red as potentially non-compliant
            if sensitive_sites and _within_sensitive_radius(latitude, longitude, sensitive_sites):
                icon_color = "red"

        folium.Marker(
            location=[latitude, longitude],
            popup=folium.Popup(popup_text, max_width=300),
            tooltip=row.get("empresa", "N/A"),
            icon=folium.Icon(color=icon_color, icon="info-sign")
        ).add_to(marker_cluster)
    
    # Mapa de calor (HeatMap) de densidad competitiva sobre el área seleccionada.
    heat_data = df_selected[["latitude", "longitude"]].dropna().values.tolist()
    if heat_data:
        HeatMap(heat_data, name="Densidad competitiva", radius=18, blur=15).add_to(map_obj)

    # Add department-level polygons aggregated by departamento for the rest of Peru (skip selected dept like LIMA)
    # For department-level aggregation (non-Lima), load departmental GeoJSON
    dept_geojson, dept_path = _load_department_geojson()
    if dept_geojson is not None:
        dept_counts = full_df.groupby("departamento").size().to_dict()
        if dept_counts:
            dept_counts_upper = {k.upper(): v for k, v in dept_counts.items()}
            max_count = max(dept_counts_upper.values())
            colormap = linear.YlOrRd_09.scale(0, max(max_count, 1))

            def _feature_dept(props: dict) -> str:
                return (props.get("DEPARTAMEN") or props.get("NOMBDEP") or props.get("DEPARTAMENTO") or "").strip()

            def style_function(feature):
                props = feature.get("properties", {})
                dept = _feature_dept(props).upper()
                if dept == "LIMA":
                    return {"weight": 0.5, "color": "#888", "fillOpacity": 0.0}
                count = dept_counts_upper.get(dept, 0)
                color = colormap(count) if count else "#ffffff"
                return {"fillColor": color, "color": "#444", "weight": 1, "fillOpacity": 0.6}

            # Inject department-level counts into geojson properties for tooltips
            for feature in dept_geojson.get("features", []):
                props = feature.setdefault("properties", {})
                dept = _feature_dept(props)
                props["_dept_name"] = dept
                props["_dept_count"] = int(dept_counts.get(dept, 0))

            dept_layer = folium.FeatureGroup(name="Departamentos", show=True)
            gj = folium.GeoJson(
                dept_geojson,
                name="Departamentos",
                style_function=style_function,
                tooltip=folium.GeoJsonTooltip(fields=["_dept_name", "_dept_count"], aliases=["Departamento", "Establecimientos"]),
            )
            gj.add_to(dept_layer)
            dept_layer.add_to(map_obj)

    # Add layer control
    folium.LayerControl().add_to(map_obj)
    
    # Add title
    title_html = '''
    <div style="position: fixed; 
                top: 10px; left: 50px; width: 300px; height: 60px; 
                background-color: white; border:2px solid grey; z-index:9999; 
                font-size:16px; font-weight: bold; padding: 10px">
        Mapa de Salas MINCETUR<br>
        <small>Total de ubicaciones: {}</small>
    </div>
    '''.format(len(df_geo))
    map_obj.get_root().html.add_child(folium.Element(title_html))
    
    return map_obj


def _add_district_heat_layer(map_obj: folium.Map, df_geo: pd.DataFrame) -> None:
    if not {"departamento", "provincia", "distrito"}.issubset(df_geo.columns):
        return

    district_data = (
        df_geo.groupby(["departamento", "provincia", "distrito"], dropna=False)
        .agg(
            establecimientos=("establecimiento", "size"),
            lat_mean=("latitude", "mean"),
            lon_mean=("longitude", "mean"),
        )
        .reset_index()
    )
    if district_data.empty:
        return

    district_data["establecimientos"] = district_data["establecimientos"].astype(int)
    max_count = int(district_data["establecimientos"].max())
    local_geojson_index = _load_local_district_geojson()
    district_layer = folium.FeatureGroup(name="Distritos Lima", show=True)
    # Create a colormap scaled to the range of establishment counts
    _max = max(max_count, 1)
    colormap = linear.YlOrRd_09.scale(0, _max)
    colormap.caption = "Cantidad de establecimientos por distrito"

    if local_geojson_index:
        print("Usando GeoJSON local de distritos para delimitaciones exactas.")
    else:
        print("GeoJSON local de distritos no encontrado; se usarán círculos de fallback.")

    for _, row in district_data.iterrows():
        dept = str(row["departamento"]) if pd.notna(row["departamento"]) else ""
        prov = str(row["provincia"]) if pd.notna(row["provincia"]) else ""
        dist = str(row["distrito"]) if pd.notna(row["distrito"]) else ""
        value = int(row["establecimientos"])

        feature = _find_district_feature(local_geojson_index, dept, prov, dist)

        def style_function(feature_obj, v=value):
            c = colormap(v)
            return {
                "fillColor": c,
                "color": "#444444",
                "weight": 1,
                "fillOpacity": 0.35,
            }

        if feature is not None:
            gj = folium.GeoJson(
                data=feature,
                style_function=style_function,
                name=f"Distrito: {dist}",
            )
            popup_html = f"<b>{dist}</b><br>Departamento: {dept}<br>Provincia: {prov}<br>Establecimientos: {value}"
            gj.add_child(folium.Popup(popup_html, max_width=300))
            gj.add_to(district_layer)
            continue

        # fallback: draw semi-transparent circle at mean coords
        if pd.isna(row["lat_mean"]) or pd.isna(row["lon_mean"]):
            continue
        color = colormap(value)
        folium.Circle(
            location=[row["lat_mean"], row["lon_mean"]],
            radius=2500 + value * 200,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.25,
            weight=1,
            popup=folium.Popup(
                f"<b>{dist}</b><br>Departamento: {dept}<br>Provincia: {prov}<br>Establecimientos: {value}",
                max_width=300,
            ),
        ).add_to(district_layer)

    district_layer.add_to(map_obj)
    colormap.add_to(map_obj)


def save_map(map_obj: folium.Map) -> None:
    """Save map to HTML file."""
    if map_obj is None:
        return
    
    output_path = get_map_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    map_obj.save(str(output_path))
    print(f"✓ Mapa guardado en: {output_path}")
    print(f"✓ Abre el archivo en tu navegador para ver el mapa interactivo")


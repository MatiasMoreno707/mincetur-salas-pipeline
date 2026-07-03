from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import pandas as pd

INPUT_FILE_NAME = "mincetur_salas.csv"
OUTPUT_FILE_NAME = "mincetur_salas_transformed.csv"
EXCEPTIONS_FILE_NAME = "mincetur_salas_exceptions.csv"
DISTRITAL_GEOJSON = "peru_distrital_simple.geojson"

# Campos obligatorios para que un registro sea apto para geocodificación.
OBLIGATORY_FIELDS = ["establecimiento", "direccion", "distrito"]


def get_input_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / INPUT_FILE_NAME


def get_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / OUTPUT_FILE_NAME


def get_exceptions_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / EXCEPTIONS_FILE_NAME


def _load_raw_csv(input_path: Path | None = None) -> pd.DataFrame:
    if input_path is None:
        input_path = get_input_path()
    return pd.read_csv(input_path, encoding="utf-8-sig")


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


# --------------------------------------------------------------------------
# Estandarización de distritos con UBIGEO (INEI, desde GeoJSON distrital local)
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _load_ubigeo_index() -> dict[str, tuple[str, str]]:
    """Construye un índice {departamento|distrito -> (DISTRITO canónico, UBIGEO)}
    a partir del shapefile distrital del INEI (peru_distrital_simple.geojson)."""
    workspace_root = Path(__file__).resolve().parents[1]
    geo_path = workspace_root / "data" / "raw" / "geo" / DISTRITAL_GEOJSON
    if not geo_path.exists():
        return {}

    with geo_path.open("r", encoding="utf-8") as f:
        geojson = json.load(f)

    index: dict[str, tuple[str, str]] = {}
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        departamento = props.get("DEPARTAMEN", "")
        distrito = props.get("DISTRITO", "")
        ubigeo = str(props.get("UBIGEO", "") or "")
        key = f"{_strip_accents(departamento).strip().upper()}|{_strip_accents(distrito).strip().upper()}"
        if key and key not in index:
            index[key] = (str(distrito).strip().upper(), ubigeo)
    return index


def _standardize_distritos(df: pd.DataFrame) -> pd.DataFrame:
    """Estandariza el nombre del distrito al estándar INEI y añade la columna ubigeo."""
    index = _load_ubigeo_index()
    if not index or "distrito" not in df.columns or "departamento" not in df.columns:
        df["ubigeo"] = pd.NA
        return df

    canonical: list[str] = []
    ubigeos: list[object] = []
    for _, row in df.iterrows():
        dep = _strip_accents(row.get("departamento", "")).strip().upper()
        dist = _strip_accents(row.get("distrito", "")).strip().upper()
        match = index.get(f"{dep}|{dist}")
        if match:
            canonical.append(match[0])
            ubigeos.append(match[1])
        else:
            canonical.append(str(row.get("distrito", "")).strip().upper())
            ubigeos.append(pd.NA)

    df["distrito"] = canonical
    df["ubigeo"] = ubigeos
    return df


def _normalize_address(value: str, district: str | None = None) -> str:
    text = str(value or "").strip().upper()
    text = re.sub(r"[‘’“”]", "'", text)
    text = re.sub(r"[^A-Z0-9ÁÉÍÓÚÑÜ\s\-\/,\.()&]", " ", text)

    expansions = {
        r"\bAVDA?\.?\b": "AVENIDA",
        r"\bAV\.?\b": "AVENIDA",
        r"\bJR\.?\b": "JIRON",
        r"\bJIRON?\.?\b": "JIRON",
        r"\bCDRA?\.?\b": "CDRA",
        r"\bPSJE?\.?\b": "PSJE",
        r"\bMZ\.?\b": "MZ",
        r"\bLT\.?\b": "LT",
        r"\bNRO\.?\b": "NO",
        r"\bNUM\.?\b": "NO",
        r"\bNO\.?\b": "NO",
        r"\bSIN\s+NUM(?:ERO)?\b": "S/N",
        r"\bSN\b": "S/N",
        r"\bS\s*\/\s*N\b": "S/N",
        r"\bOTRO\b": "",
    }
    for pattern, replacement in expansions.items():
        text = re.sub(pattern, replacement, text)

    if district:
        district_text = str(district).strip().upper()
        if district_text:
            pattern = rf"[,\s]*{re.escape(district_text)}$"
            text = re.sub(pattern, "", text)

    text = re.sub(r"\s*[,;:\-]\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\bNO\s+\.?\s*(\d+)\b", r"NO. \1", text)
    text = re.sub(r"\bS/N\b", "S/N", text)

    return text.strip()


def _generate_variants(normalized: str, district: str | None = None) -> list[str]:
    variants: list[str] = []
    normalized = normalized or ""
    if not normalized:
        return variants

    variants.append(normalized)
    if district:
        district_text = str(district).strip().upper()
        if district_text and district_text not in normalized:
            variants.append(f"{normalized}, {district_text}")

    abbreviation_map = {
        " AVENIDA ": " AV. ",
        " JIRON ": " JR. ",
        " CDRA ": " CDRA. ",
        " PSJE ": " PSJ. ",
        " MZ ": " MZ. ",
        " LT ": " LT. ",
    }
    for full, abbr in abbreviation_map.items():
        if full in normalized:
            variants.append(normalized.replace(full, abbr))

    unique_variants = []
    for variant in variants:
        cleaned = re.sub(r"\s+", " ", variant).strip()
        if cleaned and cleaned not in unique_variants:
            unique_variants.append(cleaned)

    return unique_variants


def _apply_filters(df: pd.DataFrame, departamento: str | None = None, provincia: str | None = None) -> pd.DataFrame:
    """Aplicar filtros opcionales a los datos."""
    if departamento:
        departamento = departamento.upper()
        df = df[df["departamento"].astype(str).str.upper() == departamento]
        print(f"✓ Filtro aplicado: Departamento = {departamento} ({len(df)} registros)")

    if provincia:
        provincia = provincia.upper()
        df = df[df["provincia"].astype(str).str.upper() == provincia]
        print(f"✓ Filtro aplicado: Provincia = {provincia} ({len(df)} registros)")

    return df


def _clean_text(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _is_missing(value: object) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return True
    text = str(value).strip().lower()
    return text in {"", "nan", "none", "--", "sin dato", "s/d"}


def _validate_and_impute(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Valida campos obligatorios (nombre, dirección, distrito).

    Aplica reglas de imputación simples; los registros que aún quedan incompletos
    se descartan y se documentan en un log de excepciones con código validation_error.
    Devuelve (df_aptos, df_excepciones).
    """
    df = df.copy()

    # Regla de imputación: si falta el nombre del establecimiento, usar la empresa.
    if "establecimiento" in df.columns and "empresa" in df.columns:
        mask = df["establecimiento"].apply(_is_missing) & ~df["empresa"].apply(_is_missing)
        df.loc[mask, "establecimiento"] = df.loc[mask, "empresa"]

    def faltantes(row) -> list[str]:
        return [c for c in OBLIGATORY_FIELDS if c not in df.columns or _is_missing(row.get(c))]

    faltantes_por_fila = df.apply(faltantes, axis=1)
    invalidos = faltantes_por_fila.apply(len) > 0

    exceptions = df[invalidos].copy()
    if not exceptions.empty:
        exceptions["validation_error"] = faltantes_por_fila[invalidos].apply(
            lambda cols: "campos_obligatorios_faltantes: " + ", ".join(cols)
        )

    aptos = df[~invalidos].copy()
    print(f"✓ Validación: {len(aptos)} registros aptos, {len(exceptions)} descartados (validation_error)")
    return aptos, exceptions


def _deduplicate_salas(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicación heurística con clave compuesta nombre + dirección + distrito.

    Conserva el registro más reciente por clave (vigencia descendente).
    """
    required = {"establecimiento", "direccion", "distrito"}
    if not required.issubset(df.columns):
        return df

    df = df.copy()
    df["_nombre_key"] = df["establecimiento"].astype(str).str.strip().str.upper()
    df["_dir_key"] = df["direccion"].astype(str).str.strip().str.upper()
    df["_dist_key"] = df["distrito"].astype(str).str.strip().str.upper()

    df = df.sort_values(by=["vigencia"], ascending=[False], na_position="last")
    df = df.drop_duplicates(subset=["_nombre_key", "_dir_key", "_dist_key"], keep="first")
    df = df.drop(columns=["_nombre_key", "_dir_key", "_dist_key"])
    return df


def transform_salas(df: pd.DataFrame | None = None, input_path: Path | None = None, departamento: str | None = None, provincia: str | None = None) -> pd.DataFrame:
    if df is None:
        df = _load_raw_csv(input_path)
    df = df.copy()

    rename_map = {
        "Ruc": "ruc",
        "Empresa": "empresa",
        "Establecimiento": "establecimiento",
        "Giro": "giro",
        "Resoluci�n": "resolucion",
        "Resolución": "resolucion",
        "C�digo Sala": "codigo_sala",
        "Código Sala": "codigo_sala",
        "Vigencia": "vigencia",
        "M�q.": "maq",
        "Máq.": "maq",
        "Memoria": "memoria",
        "Mesas": "mesas",
        "Direcci�n": "direccion",
        "Dirección": "direccion",
        "Distrito": "distrito",
        "Provincia": "provincia",
        "Departamento": "departamento",
    }

    df = df.rename(columns=rename_map)
    df["empresa"] = df["empresa"].astype(str).apply(_clean_text)
    df["establecimiento"] = df["establecimiento"].astype(str).apply(_clean_text)
    df["vigencia"] = pd.to_datetime(df["vigencia"], dayfirst=True, errors="coerce")

    for numeric_col in ["maq", "memoria", "mesas"]:
        if numeric_col in df.columns:
            df[numeric_col] = pd.to_numeric(df[numeric_col].replace({"--": None}), errors="coerce")

    # Estandarización de distritos con el estándar UBIGEO del INEI.
    df = _standardize_distritos(df)

    # Validación de campos obligatorios, imputación y log de excepciones.
    df, exceptions = _validate_and_impute(df)
    exc_path = get_exceptions_path()
    exc_path.parent.mkdir(parents=True, exist_ok=True)
    exceptions.to_csv(exc_path, index=False, encoding="utf-8-sig")

    # Deduplicación heurística (nombre + dirección + distrito).
    df = _deduplicate_salas(df)
    df = _apply_filters(df, departamento, provincia)

    df["direccion_original"] = df["direccion"].astype(str).str.strip()
    df["direccion_normalizada"] = df.apply(
        lambda row: _normalize_address(row["direccion_original"], row.get("distrito")),
        axis=1,
    )
    df["direccion_variantes"] = df.apply(
        lambda row: json.dumps(
            _generate_variants(row["direccion_normalizada"], row.get("distrito")),
            ensure_ascii=False,
        ),
        axis=1,
    )

    output_path = get_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    return df


def filter_salas(df: pd.DataFrame, departamento: str | None = None, provincia: str | None = None) -> pd.DataFrame:
    """Aplicar filtros a los datos limpios sin depender de la geocodificación."""
    return _apply_filters(df.copy(), departamento, provincia)

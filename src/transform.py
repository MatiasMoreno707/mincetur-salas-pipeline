from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

INPUT_FILE_NAME = "mincetur_salas.csv"
OUTPUT_FILE_NAME = "mincetur_salas_transformed.csv"


def get_input_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / INPUT_FILE_NAME


def get_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / OUTPUT_FILE_NAME


def _load_raw_csv(input_path: Path | None = None) -> pd.DataFrame:
    if input_path is None:
        input_path = get_input_path()
    return pd.read_csv(input_path, encoding="utf-8-sig")


def _normalize_address(value: str, district: str | None = None) -> str:
    text = str(value or "").strip().upper()
    text = re.sub(r"[\u2018\u2019\u201c\u201d]", "'", text)
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


def _normalize_business_text(value: str) -> str:
    return _clean_text(value).upper()


def _deduplicate_salas(df: pd.DataFrame) -> pd.DataFrame:
    if "empresa" not in df.columns or "establecimiento" not in df.columns:
        return df

    df = df.copy()
    df["empresa_clean"] = df["empresa"].astype(str).apply(_normalize_business_text)
    df["establecimiento_clean"] = df["establecimiento"].astype(str).apply(_normalize_business_text)

    df = df.sort_values(
        by=["empresa_clean", "establecimiento_clean", "vigencia"],
        ascending=[True, True, False],
        na_position="last",
    )
    df = df.drop_duplicates(subset=["empresa_clean", "establecimiento_clean"], keep="first")
    df = df.drop(columns=["empresa_clean", "establecimiento_clean"])
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

    df = _deduplicate_salas(df)
    df = _apply_filters(df, departamento, provincia)

    for numeric_col in ["maq", "memoria", "mesas"]:
        if numeric_col in df.columns:
            df[numeric_col] = pd.to_numeric(df[numeric_col].replace({"--": None}), errors="coerce")

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

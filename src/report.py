from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

SUMMARY_FILE_NAME = "pipeline_summary.csv"
GEOCODE_DETAIL_FILE_NAME = "geocode_detail.xlsx"


def get_summary_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "output" / SUMMARY_FILE_NAME


def get_detail_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "output" / GEOCODE_DETAIL_FILE_NAME


def save_run_summary(summary: dict[str, object]) -> None:
    output_path = get_summary_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary_df = pd.DataFrame([summary])
    if output_path.exists():
        existing_df = pd.read_csv(output_path, encoding="utf-8-sig")
        summary_df = pd.concat([existing_df, summary_df], ignore_index=True)

    summary_df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"✓ Resumen de ejecución guardado en: {output_path}")


def save_geocode_detail(df: pd.DataFrame) -> None:
    detail = df.copy()
    detail["departamento"] = detail["departamento"].fillna("SIN_DEPARTAMENTO").astype(str)
    detail["provincia"] = detail["provincia"].fillna("SIN_PROVINCIA").astype(str)
    detail["distrito"] = detail["distrito"].fillna("SIN_DISTRITO").astype(str)

    detail["geocoded"] = detail["latitude"].notna() & detail["longitude"].notna()
    detail["not_geocoded"] = ~detail["geocoded"]

    grouped = (
        detail
        .groupby(["departamento", "provincia", "distrito"], dropna=False)
        .agg(
            registros_total=("geocoded", "size"),
            geocoded_count=("geocoded", "sum"),
            not_geocoded_count=("not_geocoded", "sum"),
        )
        .reset_index()
    )
    grouped["geocoded_pct"] = (grouped["geocoded_count"] / grouped["registros_total"] * 100).round(2)
    grouped = grouped.sort_values(["departamento", "provincia", "distrito"]).reset_index(drop=True)

    output_path = get_detail_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        detail.to_excel(writer, index=False, sheet_name="Geocoding Records")
        grouped.to_excel(writer, index=False, sheet_name="Geocoding Summary")

    print(f"✓ Detalle de geocodificación guardado en: {output_path}")

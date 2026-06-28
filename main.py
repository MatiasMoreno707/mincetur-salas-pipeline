from __future__ import annotations

from datetime import datetime
from time import perf_counter
import argparse
from src.enrich import enrich_salas
from src.extract import scrape_mincetur_salas
from src.geocode import geocode_salas, load_geocoded_df
from src.report import save_run_summary, save_geocode_detail
from src.transform import transform_salas, filter_salas
from src.visualize import visualize_salas, create_map, save_map


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline de extracción y procesamiento de salas MINCETUR"
    )
    parser.add_argument(
        "--departamento",
        type=str,
        default="Lima",
        help='Filtrar por departamento (ej: "Lima")'
    )
    parser.add_argument(
        "--provincia",
        type=str,
        default="Lima",
        help='Filtrar por provincia (ej: "Lima")'
    )
    parser.add_argument(
        "--pois",
        type=str,
        choices=["csv", "new"],
        default="csv",
        help='Fuente de POIs sensibles: "csv" para usar pois_ley27153.csv o "new" para regenerarlos desde Overpass'
    )

    args = parser.parse_args()

    start_datetime = datetime.now()
    start_timestamp = perf_counter()

    print("Iniciando pipeline de extracción de MINCETUR...")
    if args.departamento or args.provincia:
        print(f"Filtros: Departamento={args.departamento}, Provincia={args.provincia}")

    # 1. EXTRACT — siempre vía web scraping
    scrape_start = perf_counter()
    raw_df = scrape_mincetur_salas()
    scrape_duration = perf_counter() - scrape_start
    scrape_records = len(raw_df)
    print(f"Extracción completada: {scrape_records} filas")

    # 2. TRANSFORM — limpieza, normalización y filtros
    transform_start = perf_counter()
    transformed_df = transform_salas(raw_df)
    selected_df = filter_salas(transformed_df, args.departamento, args.provincia)
    transform_duration = perf_counter() - transform_start
    clean_records = len(transformed_df)
    filtered_records = len(selected_df)
    print(f"Transformación completada: {filtered_records} registros seleccionados")

    # 3. GEOCODE — modo delta: reutiliza coordenadas existentes por ruc+resolucion+vigencia
    geocode_start = perf_counter()
    existing_geocoded = load_geocoded_df()
    if existing_geocoded is not None:
        print("Modo delta: geocodificando solo registros nuevos...")
    else:
        print("Sin geocodificación previa: geocodificando el dataset completo...")
    geocoded_df = geocode_salas(
        selected_df,
        existing_df=existing_geocoded,
    )
    geocode_duration = perf_counter() - geocode_start
    print("Geocodificación completada")

    geocoded_mask = geocoded_df["latitude"].notna() & geocoded_df["longitude"].notna()
    geocoded_records = int(geocoded_mask.sum())
    not_geocoded_records = len(geocoded_df) - geocoded_records
    geocoded_pct = round(
        (geocoded_records / len(geocoded_df) * 100) if len(geocoded_df) > 0 else 0, 2
    )
    not_geocoded_pct = round(
        (not_geocoded_records / len(geocoded_df) * 100) if len(geocoded_df) > 0 else 0, 2
    )

    # 4. ENRICH — POIs y cumplimiento Ley 27153
    enrich_start = perf_counter()
    enriched_df = enrich_salas(geocoded_df, pois_mode=args.pois)
    enrich_duration = perf_counter() - enrich_start
    enriched_records = len(enriched_df)
    print(f"Enriquecimiento completado: {enriched_records} registros")

    # 5. VISUALIZE — resumen y mapa interactivo
    summary_df = visualize_salas(enriched_df)
    print("Resumen completado")
    print(summary_df.to_string(index=False))

    print("\nGenerando mapa interactivo...")
    map_obj = create_map(enriched_df, full_df=transformed_df, departamento=args.departamento, provincia=args.provincia)
    if map_obj:
        save_map(map_obj)

    end_datetime = datetime.now()
    total_duration = perf_counter() - start_timestamp

    # 6. REPORT — métricas de ejecución
    run_summary = {
        "start_datetime": start_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        "end_datetime": end_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        "total_duration_seconds": round(total_duration, 2),
        "departamento_filter": args.departamento or "",
        "provincia_filter": args.provincia or "",
        "scrape_records": scrape_records,
        "clean_records": clean_records,
        "filtered_records": filtered_records,
        "enriched_records": enriched_records,
        "scrape_duration_seconds": round(scrape_duration, 2),
        "transform_duration_seconds": round(transform_duration, 2),
        "geocode_duration_seconds": round(geocode_duration, 2),
        "enrich_duration_seconds": round(enrich_duration, 2),
        "geocoded_records": geocoded_records,
        "not_geocoded_records": not_geocoded_records,
        "geocoded_pct": geocoded_pct,
        "not_geocoded_pct": not_geocoded_pct,
    }

    save_run_summary(run_summary)
    save_geocode_detail(geocoded_df)

    print("\n✓ Pipeline finalizado con éxito")


if __name__ == "__main__":
    main()

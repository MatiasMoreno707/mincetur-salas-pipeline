from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

URL = (
    "https://consultasenlinea.mincetur.gob.pe/webCasinos/Index.aspx?po=frmSalas.aspx"
)
OUTPUT_FILE_NAME = "mincetur_salas.csv"


def get_output_path() -> Path:
    workspace_root = Path(__file__).resolve().parents[1]
    return workspace_root / "data" / "raw" / OUTPUT_FILE_NAME


def _get_total_pages(page) -> int:
    texts = page.eval_on_selector_all(
        "td",
        "els => els.map(el => el.innerText.trim())",
    )
    for text in texts:
        if "Página" in text or "Pagina" in text:
            match = re.search(r"[Pp][áa]gina\s*\d+\s*de\s*(\d+)", text)
            if match:
                return int(match.group(1))
    raise RuntimeError("No se pudo determinar el número total de páginas de resultados.")


def _extract_rows(page) -> list[list[str]]:
    rows = page.query_selector_all("#divResultadoSala table:nth-of-type(1) tbody tr")
    data = []
    for row in rows[1:]:
        cells = row.query_selector_all("td")
        if not cells:
            continue
        data.append([cell.inner_text().strip() for cell in cells])
    return data


def _get_total_records(page) -> int:
    texts = page.eval_on_selector_all(
        "td",
        "els => els.map(el => el.innerText.trim())",
    )
    for text in texts:
        if "Registros" in text or "registros" in text:
            match = re.search(r"[Rr]egistros\s*[:\-]?\s*(\d+)", text)
            if match:
                return int(match.group(1))
    raise RuntimeError("No se pudo determinar el total de registros desde la página.")


def scrape_mincetur_salas(output_path: Path | None = None) -> pd.DataFrame:
    if output_path is None:
        output_path = get_output_path()

    start_time = time.perf_counter()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, timeout=60000)
        page.wait_for_selector("#divResultadoSala table:nth-of-type(1)", timeout=60000)

        logger.info("Abriendo URL: %s", URL)
        page.click("#btnBuscar")
        logger.info("Pulsado botón buscar. Esperando resultados...")
        page.wait_for_selector("#divResultadoSala input.paginaActual.numerica", timeout=60000)
        page.wait_for_function(
            "() => document.querySelectorAll('#divResultadoSala table tr').length > 1",
            timeout=60000,
        )

        total_pages = _get_total_pages(page)
        total_records = _get_total_records(page)
        logger.info("Total de páginas detectadas: %d", total_pages)
        logger.info("Total de registros indicado en la web: %d", total_records)

        first_page_rows = page.query_selector_all("#divResultadoSala table:nth-of-type(1) tbody tr")
        if len(first_page_rows) < 2:
            raise RuntimeError("No se encontraron filas de datos después de la búsqueda.")

        header_cells = first_page_rows[0].query_selector_all("th, td")
        columns = [cell.inner_text().strip() for cell in header_cells]
        if len(columns) != 14:
            raise RuntimeError(
                f"Encabezado inesperado: se detectaron {len(columns)} columnas en lugar de 14. "
                f"Columnas: {columns}"
            )

        data = []

        for current_page in range(1, total_pages + 1):
            logger.info("Extrayendo página %d de %d", current_page, total_pages)
            if current_page > 1:
                page.fill("input.paginaActual.numerica", str(current_page))
                page.click(".irPagina.imagenBoton")
                page.wait_for_function(
                    f"() => document.querySelector('input.paginaActual.numerica')?.value === '{current_page}'",
                    timeout=60000,
                )
                page.wait_for_function(
                    "() => document.querySelectorAll('#divResultadoSala table tr').length > 1",
                    timeout=60000,
                )

            page_data = _extract_rows(page)
            for row_idx, row_values in enumerate(page_data, start=1):
                if len(row_values) != 14:
                    raise RuntimeError(
                        f"Fila con columnas inválidas en página {current_page}, fila {row_idx}: "
                        f"{len(row_values)} columnas encontradas"
                    )
            logger.info("Página %d cargada, filas extraídas: %d", current_page, len(page_data))
            data.extend(page_data)

        df = pd.DataFrame(data, columns=columns)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    elapsed = time.perf_counter() - start_time
    logger.info("CSV guardado en: %s", output_path)
    logger.info("Total de filas extraídas: %d", len(df))
    if len(df) != total_records:
        logger.error(
            "El número de filas extraídas (%d) no coincide con el total esperado (%d)",
            len(df),
            total_records,
        )
        raise RuntimeError(
            f"Validación fallida: filas extraídas {len(df)} != registros esperados {total_records}"
        )
    logger.info("Validación OK: filas extraídas coinciden con los registros indicados.")
    logger.info("Tiempo total de scraping: %.2f segundos", elapsed)
    return df


if __name__ == "__main__":
    df = scrape_mincetur_salas()
    print(f"CSV guardado en: {get_output_path()}")
    print(f"Filas extraídas: {len(df)}")

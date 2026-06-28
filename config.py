"""
Configuración general de la aplicación
"""
from __future__ import annotations

# Filtros opcionales para procesar solo un subset de datos
FILTER_DEPARTAMENTO: str | None = None  # Ej: "LIMA"
FILTER_PROVINCIA: str | None = None      # Ej: "LIMA"

# Configuración de geocodificación
GEOCODING_DELAY: float = 2.0   # Segundos entre solicitudes (Nominatim exige >= 1 s)
GEOCODING_TIMEOUT: int = 10    # Timeout en segundos para cada solicitud
GEOCODING_RETRY_WAIT: float = 60.0  # Segundos de espera al recibir un error 429
GEOCODING_MAX_RETRIES: int = 3      # Intentos máximos por registro ante errores 429

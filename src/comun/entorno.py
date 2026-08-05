# -*- coding: utf-8 -*-
"""
comun.entorno
=============
Detección del intérprete en uso y recolección de versiones de librerías.

Doctrina (CLAUDE.md, sección 3): el estudio se ejecuta bajo dos intérpretes. Los
módulos SIG corren sobre el Python de QGIS y no importan librerías del venv. Un
módulo que se ejecute en el entorno equivocado debe detenerse antes de producir
resultados, no fallar a mitad del proceso.

Doctrina (CLAUDE.md, sección 2): el log de cada módulo registra las versiones de
las librerías empleadas. Este archivo provee esa información.

Solo usa la librería estándar: es importable desde el entorno de QGIS.
"""

from __future__ import annotations

import os
import platform
import sys
from importlib import metadata

from .errores import ErrorEntorno

__all__ = [
    "ENTORNO_QGIS",
    "ENTORNO_VENV",
    "ENTORNO_SISTEMA",
    "PAQUETES_VIGILADOS",
    "nombre_entorno",
    "es_entorno_qgis",
    "exigir_entorno",
    "versiones_librerias",
    "resumen_entorno",
]

ENTORNO_QGIS = "qgis"
ENTORNO_VENV = "venv"
ENTORNO_SISTEMA = "sistema"

# Paquetes cuya versión se registra en la cabecera del log. Los ausentes se
# omiten sin error: cada módulo instala únicamente lo que necesita.
PAQUETES_VIGILADOS: tuple[str, ...] = (
    "PyYAML",
    "numpy",
    "pandas",
    "scipy",
    "statsmodels",
    "lmoments3",
    "matplotlib",
    "requests",
    "geopandas",
    "shapely",
    "fiona",
    "pyproj",
    "rasterio",
    "gdal",
    "openpyxl",
    "python-docx",
    "jinja2",
)


def _hay_qgis() -> bool:
    """Indica si el intérprete actual expone la API de QGIS."""
    if "qgis.core" in sys.modules:
        return True
    if os.environ.get("QGIS_PREFIX_PATH"):
        return True
    try:
        import qgis.core  # noqa: F401
    except Exception:
        return False
    return True


def _hay_venv() -> bool:
    """Indica si el intérprete corre dentro de un entorno virtual."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def nombre_entorno() -> str:
    """
    Devuelve 'qgis', 'venv' o 'sistema'.

    La detección de QGIS tiene precedencia: un Python de QGIS puede estar además
    dentro de un entorno virtual, y lo relevante para la doctrina es la
    disponibilidad de la API SIG.
    """
    if _hay_qgis():
        return ENTORNO_QGIS
    if _hay_venv():
        return ENTORNO_VENV
    return ENTORNO_SISTEMA


def es_entorno_qgis() -> bool:
    """Atajo booleano de nombre_entorno()."""
    return nombre_entorno() == ENTORNO_QGIS


def exigir_entorno(esperado: str, modulo: str = "") -> None:
    """
    Detiene la ejecución si el intérprete no corresponde al entorno esperado.

    Se invoca al inicio de cada módulo. Un módulo SIG ejecutado desde el venv
    fallaría más adelante al importar qgis; detenerlo aquí produce un mensaje
    accionable en lugar de un ImportError.

    Excepciones
    -----------
    ErrorEntorno
        Si el entorno detectado no coincide con el esperado.
    """
    if esperado not in (ENTORNO_QGIS, ENTORNO_VENV, ENTORNO_SISTEMA):
        raise ValueError(f"Entorno esperado no reconocido: '{esperado}'")

    actual = nombre_entorno()
    if actual == esperado:
        return

    # El entorno 'sistema' se acepta donde se esperaba 'venv': el consultor
    # puede haber instalado las dependencias de forma global. Se deja
    # constancia, pero no se detiene el proceso por ello.
    if esperado == ENTORNO_VENV and actual == ENTORNO_SISTEMA:
        return

    referencia = f"El módulo {modulo} " if modulo else "Este módulo "
    raise ErrorEntorno(
        f"{referencia}requiere el entorno '{esperado}' y se está ejecutando en "
        f"'{actual}' ({sys.executable}). Ver CLAUDE.md, sección 3."
    )


def versiones_librerias(paquetes: tuple[str, ...] | None = None) -> dict[str, str]:
    """
    Devuelve las versiones instaladas de los paquetes indicados.

    Los paquetes no instalados se omiten. La consulta usa los metadatos de la
    distribución, de modo que no importa ninguna librería y no tiene efecto
    sobre el tiempo de arranque de los módulos.
    """
    objetivo = paquetes if paquetes is not None else PAQUETES_VIGILADOS
    encontradas: dict[str, str] = {}
    for nombre in objetivo:
        try:
            encontradas[nombre] = metadata.version(nombre)
        except metadata.PackageNotFoundError:
            continue
    return encontradas


def resumen_entorno() -> dict[str, object]:
    """
    Reúne la información de ejecución que se escribe en la cabecera del log.

    No incluye variables de entorno ni rutas de usuario más allá del ejecutable,
    para no verter información innecesaria en los anexos del estudio.
    """
    return {
        "entorno": nombre_entorno(),
        "python": platform.python_version(),
        "implementacion": platform.python_implementation(),
        "ejecutable": sys.executable,
        "sistema": f"{platform.system()} {platform.release()}",
        "arquitectura": platform.machine(),
        "librerias": versiones_librerias(),
    }

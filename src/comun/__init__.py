# -*- coding: utf-8 -*-
"""
comun
=====
Utilidades compartidas por todos los módulos del estudio hidrológico (M00).

Restricción deliberada: este paquete se limita a la librería estándar más
PyYAML, que el Python de QGIS ya incluye. Es la única forma de que los módulos
SIG (M01, M02, M06, M08, M11, M16) y los módulos de análisis compartan una sola
implementación de configuración, rutas y logging, sin la duplicación que
CLAUDE.md, sección 3, obliga a evitar.

Ninguna función de este paquete importa pandas, numpy, geopandas ni la API de
QGIS. Cualquier necesidad de ese tipo pertenece al módulo que la requiera.

Uso típico desde un módulo:

    from comun import cargar, configurar, registrar_cabecera, rutas

    cfg = cargar()
    log = configurar("M04", nivel=cfg.obtener("ejecucion.nivel_log"))
    registrar_cabecera(log, "M04", "Ingesta IDEAM", config=cfg,
                       parametros=cfg.parametros(("ideam.fuente_primaria",)))
"""

from __future__ import annotations

from . import entorno, esquema, registro, rutas
from .config import Config, cargar, leer_yaml
from .entorno import exigir_entorno, nombre_entorno, resumen_entorno, versiones_librerias
from .errores import (
    ErrorClaveInexistente,
    ErrorConfiguracion,
    ErrorEntorno,
    ErrorHidrologia,
    ErrorRutas,
    ErrorValidacion,
)
from .esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo
from .registro import bloque, configurar, registrar_cabecera, registrar_cierre, ruta_log
from .rutas import directorio, raiz_proyecto, relativa, resolver, resolver_desde

__all__ = [
    # submódulos
    "entorno", "esquema", "registro", "rutas",
    # configuración
    "Config", "cargar", "leer_yaml",
    # entorno
    "exigir_entorno", "nombre_entorno", "resumen_entorno", "versiones_librerias",
    # errores
    "ErrorHidrologia", "ErrorEntorno", "ErrorRutas", "ErrorConfiguracion",
    "ErrorClaveInexistente", "ErrorValidacion",
    # esquema
    "BLOQUEANTE", "ADVERTENCIA", "INFORMATIVO", "Hallazgo",
    # registro
    "configurar", "registrar_cabecera", "registrar_cierre", "bloque", "ruta_log",
    # rutas
    "raiz_proyecto", "directorio", "resolver", "resolver_desde", "relativa",
]

__version__ = "0.1.0"

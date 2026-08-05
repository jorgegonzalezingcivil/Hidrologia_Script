# -*- coding: utf-8 -*-
"""
comun.errores
=============
Jerarquía de excepciones compartida por todos los módulos del estudio.

Doctrina (CLAUDE.md, sección 2): un módulo se detiene y reporta, nunca produce
un resultado incorrecto en silencio. Toda condición que impida garantizar la
validez del resultado debe elevar una de estas excepciones.

Este archivo solo usa la librería estándar, de modo que es importable tanto
desde el entorno de QGIS como desde el venv del proyecto.
"""

from __future__ import annotations

from typing import Iterable, Sequence

__all__ = [
    "ErrorHidrologia",
    "ErrorEntorno",
    "ErrorRutas",
    "ErrorConfiguracion",
    "ErrorClaveInexistente",
    "ErrorValidacion",
    "ErrorFormato",
]


class ErrorHidrologia(Exception):
    """Raíz de la jerarquía. Permite capturar cualquier fallo propio."""


class ErrorEntorno(ErrorHidrologia):
    """El intérprete o las librerías disponibles no son los esperados."""


class ErrorRutas(ErrorHidrologia):
    """No se pudo resolver la raíz del proyecto o una ruta declarada."""


class ErrorFormato(ErrorHidrologia):
    """
    Un archivo de datos no cumple el formato que declara su extensión.

    Se usa para los insumos externos, cuyo formato el repositorio no controla:
    shapefiles truncados, .dbf con cabecera inconsistente, tablas con columnas
    que no corresponden. Detener es preferible a interpretar bytes al azar.
    """


class ErrorConfiguracion(ErrorHidrologia):
    """El archivo de configuración no se pudo leer o interpretar."""


class ErrorClaveInexistente(ErrorConfiguracion):
    """Se solicitó una clave que no existe en la configuración."""

    def __init__(self, clave: str, ruta_archivo: str | None = None) -> None:
        self.clave = clave
        self.ruta_archivo = ruta_archivo
        detalle = f" en {ruta_archivo}" if ruta_archivo else ""
        super().__init__(f"La clave '{clave}' no existe{detalle}.")


class ErrorValidacion(ErrorConfiguracion):
    """
    La configuración es sintácticamente válida pero incumple el esquema.

    Transporta la lista completa de hallazgos para que el consultor corrija
    todos los problemas en una sola pasada, en lugar de uno por ejecución.
    """

    def __init__(self, hallazgos: Sequence, ruta_archivo: str | None = None) -> None:
        self.hallazgos = list(hallazgos)
        self.ruta_archivo = ruta_archivo

        # Si hay bloqueantes se detallan solo esos. Si la carga se detuvo en
        # modo estricto sin bloqueantes, se detallan las advertencias, que son
        # entonces la causa real de la detención.
        bloqueantes = [h for h in self.hallazgos if getattr(h, "es_bloqueante", False)]
        detallados = bloqueantes if bloqueantes else [
            h for h in self.hallazgos
            if getattr(h, "severidad", "") == "ADVERTENCIA"
        ]

        cabecera = (
            f"La configuración incumple el esquema: {len(detallados)} hallazgo(s) "
            f"{'bloqueante(s)' if bloqueantes else 'de advertencia en modo estricto'}"
        )
        if ruta_archivo:
            cabecera += f" en {ruta_archivo}"
        cuerpo = "\n".join(f"  - {h}" for h in detallados)
        super().__init__(f"{cabecera}.\n{cuerpo}" if cuerpo else f"{cabecera}.")

    def como_lista(self) -> Iterable:
        """Devuelve los hallazgos, incluidas advertencias e informativos."""
        return tuple(self.hallazgos)

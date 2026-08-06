# -*- coding: utf-8 -*-
"""
comun.campos
============
Declaración de campos de las capas vectoriales y su diccionario de
equivalencias.

Doctrina (CLAUDE.md, sección 5): los nombres de campo de un shapefile están
limitados a 10 caracteres, y debe existir un diccionario de equivalencias entre
el campo corto y el campo descriptivo para el informe y el Excel.

El límite no es una recomendación: el formato dBase trunca en silencio a 10
caracteres. Dos campos declarados como 'precipitacion_media' y
'precipitacion_maxima' se convertirían ambos en 'precipitac' y uno de los dos
se perdería. Por eso validar_campos() es bloqueante y no advertencia.

Solo usa la librería estándar: es importable desde el entorno de QGIS y desde el
venv. La creación de los campos de QGIS se hace en el módulo que los escribe.
"""

from __future__ import annotations

import csv
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .errores import ErrorFormato

__all__ = [
    "LONGITUD_MAXIMA_NOMBRE",
    "TIPOS",
    "CampoSalida",
    "validar_campos",
    "escribir_diccionario",
]

# Límite del formato dBase que usa el shapefile.
LONGITUD_MAXIMA_NOMBRE = 10

# Tipos admitidos, en la nomenclatura del repositorio. El módulo que escribe la
# capa los traduce al tipo de QGIS que corresponda.
TIPOS = ("texto", "entero", "decimal", "fecha")


@dataclass(frozen=True)
class CampoSalida:
    """
    Un campo de una capa de salida.

    Atributos
    ---------
    corto:        nombre en el shapefile, máximo 10 caracteres.
    descriptivo:  nombre legible para el informe y el Excel.
    tipo:         uno de TIPOS.
    longitud:     ancho del campo; para 'texto' es el número de caracteres.
    precision:    número de decimales; solo aplica a 'decimal'.
    unidad:       unidad de medida, para el diccionario. Vacía si no aplica.
    """

    corto: str
    descriptivo: str
    tipo: str
    longitud: int = 0
    precision: int = 0
    unidad: str = ""


def _sin_acentos(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def validar_campos(campos: Sequence[CampoSalida]) -> None:
    """
    Verifica que los nombres cortos sean escribibles en un shapefile.

    Excepciones
    -----------
    ErrorFormato
        Si un nombre excede 10 caracteres, se repite, está vacío, contiene
        caracteres fuera de ASCII o el tipo no está admitido. Todos los
        problemas se reportan juntos.
    """
    problemas: list[str] = []
    vistos: dict[str, str] = {}

    for campo in campos:
        nombre = campo.corto

        if not nombre:
            problemas.append("hay un campo con nombre corto vacío.")
            continue

        if len(nombre) > LONGITUD_MAXIMA_NOMBRE:
            problemas.append(
                f"{nombre!r} tiene {len(nombre)} caracteres; el formato dBase "
                f"trunca a {LONGITUD_MAXIMA_NOMBRE} y el campo se perdería."
            )

        if nombre != _sin_acentos(nombre) or not nombre.isascii():
            problemas.append(
                f"{nombre!r} contiene caracteres fuera de ASCII. Los acentos en "
                "nombres de campo se corrompen al cambiar de herramienta."
            )

        if " " in nombre:
            problemas.append(f"{nombre!r} contiene espacios.")

        if campo.tipo not in TIPOS:
            problemas.append(
                f"{nombre!r} declara el tipo {campo.tipo!r}, que no está en "
                f"{', '.join(TIPOS)}."
            )

        clave = nombre.lower()
        if clave in vistos:
            problemas.append(
                f"{nombre!r} colisiona con {vistos[clave]!r}: el .dbf no "
                "distingue mayúsculas."
            )
        else:
            vistos[clave] = nombre

    if problemas:
        raise ErrorFormato(
            "Los campos declarados no son escribibles en un shapefile:\n"
            + "\n".join(f"  - {p}" for p in problemas)
        )


def escribir_diccionario(
    campos: Iterable[CampoSalida],
    destino: Path,
    capa: str,
    delimitador: str = ";",
) -> Path:
    """
    Escribe el diccionario de equivalencias de una capa.

    Es el archivo que permite que el informe y el Excel usen el nombre
    descriptivo mientras el shapefile conserva el corto. Se escribe en utf-8-sig
    para que Excel muestre los acentos sin intervención.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        manejador.write(
            f"# Diccionario de campos de la capa {capa}.\n"
            f"# Generado automáticamente. El shapefile usa el nombre corto; el "
            f"informe y el Excel usan el descriptivo.\n"
        )
        escritor = csv.writer(manejador, delimiter=delimitador)
        escritor.writerow(
            ["campo_corto", "campo_descriptivo", "tipo", "longitud",
             "precision", "unidad"]
        )
        for campo in campos:
            escritor.writerow([
                campo.corto, campo.descriptivo, campo.tipo,
                campo.longitud, campo.precision, campo.unidad,
            ])
    return destino

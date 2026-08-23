# -*- coding: utf-8 -*-
"""
estaciones
==========
Identidad de una estación: su nombre legible y su forma apta para un archivo.

Vive en `comun` y no en un módulo concreto porque el M05, el M05b y el M15 la
necesitan igual, y tres copias de la misma normalización acabarían dando tres
nombres distintos para la misma estación. Solo usa la librería estándar, de modo
que se importa desde los dos entornos.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path

__all__ = ["nombres_de_estacion", "sin_tildes"]


def nombres_de_estacion(ruta_inventario: str | Path,
                        delimitador: str = ";") -> dict[str, str]:
    """
    Nombre legible de cada estación, indexado por código.

    El inventario publica el nombre con el código repetido entre corchetes
    ('LA BOLSA [21206690]'), que sirve en una tabla y sobra en el título de una
    figura y en un nombre de archivo. Se recorta.

    Las columnas se buscan por lo que contiene su encabezado y no por su
    posición: el inventario se escribe con nombres descriptivos y largos, y una
    reordenación no debería romper esto en silencio.

    Un inventario ausente devuelve un diccionario vacío. La figura sabe caer al
    código, que es lo único que el IDEAM garantiza único.
    """
    ruta = Path(ruta_inventario)
    if not ruta.is_file():
        return {}
    try:
        with ruta.open(encoding="utf-8-sig", newline="") as archivo:
            filas = list(csv.DictReader(archivo, delimiter=delimitador))
    except OSError:
        return {}

    nombres: dict[str, str] = {}
    for fila in filas:
        codigo = ""
        nombre = ""
        for clave, valor in fila.items():
            etiqueta = sin_tildes(clave or "")
            if "codigo" in etiqueta:
                codigo = str(valor or "").strip()
            elif "nombre" in etiqueta:
                nombre = str(valor or "").strip()
        if codigo:
            nombres[codigo] = re.sub(r"\s*\[[^\]]*\]\s*$", "", nombre).strip()
    return nombres


def sin_tildes(texto: str) -> str:
    """
    Texto reducido a minúsculas y ASCII, apto para un nombre de archivo.

    LOS NOMBRES DE ARCHIVO NO LLEVAN TILDES NI ESPACIOS. El informe los
    referencia desde una plantilla de Word y los anexos viajan entre máquinas:
    una eñe o un acento se codifican distinto según el sistema y el vínculo se
    rompe sin avisar. El nombre legible se conserva íntegro en el título de la
    figura, que es donde el lector lo necesita.
    """
    plano = unicodedata.normalize("NFKD", str(texto))
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    plano = re.sub(r"[^A-Za-z0-9]+", "_", plano).strip("_")
    return plano.lower()

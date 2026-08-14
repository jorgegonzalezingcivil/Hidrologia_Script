#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Adaptador del formato DSS de HEC
================================
Entorno: venv del proyecto.

Por qué existe y por qué aquí. HEC-HMS no admite series de tiempo escritas en
texto dentro de sus archivos: 'Manual Entry' guarda los valores en el DSS del
proyecto y en el .gage solo deja el 'Pathname'. Verificado sobre un pluviómetro
que el propio programa escribió. Escribirlas exige, por tanto, el formato DSS.

Sigue la misma separación que 'graficos.py' (CLAUDE.md, sección 3):

    src/comun/    solo librería estándar    ambos entornos
    src/dss.py    depende de hecdss          módulos de análisis

No puede vivir en src/comun: el Python de QGIS comparte ese paquete y no tiene
por qué disponer de hecdss.

QUÉ AÍSLA. 'hecdss' envuelve la librería binaria de HEC y es el punto más frágil
de la cadena ante actualizaciones (CLAUDE.md, sección 2). Todo lo que la toca
está aquí, de modo que un cambio de su interfaz se corrige en un archivo.

EL PATHNAME NO ES LIBRE. HEC lo normaliza al guardar: se pide
'//Z1_T100/PRECIP-INC//5MIN/GAGE/' y el archivo acaba conteniendo
'//Z1_T100/PRECIP-INC/01Jan2000/5Minute/GAGE/', con la fecha del bloque en la
parte D y el intervalo escrito de otra forma en la parte E. HEC-HMS acepta la
forma abreviada en su .gage y la expande al leer. Aquí se construye la
abreviada, que es la que el programa escribe en sus propios archivos.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "INTERVALOS",
    "ruta_dss",
    "escribir_serie_precipitacion",
    "ErrorDss",
]


class ErrorDss(RuntimeError):
    """Falla al escribir o leer un DSS, que el módulo debe reportar."""


# Nombres que HEC da a los intervalos regulares en la parte E del pathname. No
# son libres: '5MIN' es el que el propio HEC-HMS escribe, y otro texto produce
# una serie que el programa no encuentra.
INTERVALOS: dict[int, str] = {
    1: "1MIN", 2: "2MIN", 3: "3MIN", 4: "4MIN", 5: "5MIN", 6: "6MIN",
    10: "10MIN", 12: "12MIN", 15: "15MIN", 20: "20MIN", 30: "30MIN",
    60: "1HOUR", 120: "2HOUR", 180: "3HOUR", 360: "6HOUR", 720: "12HOUR",
    1440: "1DAY",
}


def ruta_dss(nombre: str, intervalo_min: int, parte_f: str = "GAGE",
             parametro: str = "PRECIP-INC") -> str:
    """
    Construye el pathname de una serie, en la forma abreviada.

    Las seis partes son A/B/C/D/E/F: aquí se usan B (el nombre del
    pluviómetro), C (el parámetro), E (el intervalo) y F (la etiqueta de
    versión). A y D se dejan vacías, que es como HEC-HMS escribe las suyas: la
    parte D la rellena el propio DSS con la fecha del bloque.

    Excepciones
    -----------
    ErrorDss
        Si el intervalo no es uno de los que HEC nombra. Inventar el texto de la
        parte E produce una serie que el programa no encuentra, sin dar error.
    """
    if not nombre.strip():
        raise ErrorDss("el nombre de la serie no puede estar vacío.")
    minutos = int(round(intervalo_min))
    if minutos not in INTERVALOS:
        raise ErrorDss(
            f"HEC no nombra un intervalo de {minutos} min. Admitidos: "
            f"{sorted(INTERVALOS)}.")
    return f"//{nombre.strip()}/{parametro}//{INTERVALOS[minutos]}/{parte_f}/"


def escribir_serie_precipitacion(
    destino: Path,
    nombre: str,
    valores: Sequence[float],
    inicio: _dt.datetime,
    intervalo_min: int,
    unidades: str = "MM",
    parte_f: str = "GAGE",
) -> dict[str, Any]:
    """
    Escribe una serie de precipitación incremental en el DSS.

    Las marcas de tiempo son el FINAL de cada intervalo, que es el convenio de
    HEC para datos acumulados por periodo: el valor del primer intervalo lleva
    la hora en que ese intervalo termina. Ponerlas al principio desplazaría todo
    el hietograma un paso.

    Devuelve el pathname escrito, para que quien construya el .gage declare
    exactamente el que existe en el archivo.

    Excepciones
    -----------
    ErrorDss
        Si no hay valores o si la escritura falla.
    """
    if not valores:
        raise ErrorDss(f"la serie {nombre!r} no trae ningún valor.")

    try:
        from hecdss import HecDss
        from hecdss.regular_timeseries import RegularTimeSeries
    except ImportError as error:  # pragma: no cover - depende del entorno
        raise ErrorDss(
            "no está instalado 'hecdss', que es lo que escribe el formato DSS. "
            "Instalarlo en el venv del proyecto.") from error

    pathname = ruta_dss(nombre, intervalo_min, parte_f)
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        with HecDss(str(destino)) as archivo:
            serie = RegularTimeSeries()
            serie.id = pathname
            serie.values = [float(v) for v in valores]
            serie.times = [
                inicio + _dt.timedelta(minutes=intervalo_min * (i + 1))
                for i in range(len(valores))]
            serie.units = unidades
            # PER-CUM: acumulado durante el periodo, que es lo que es una
            # lámina por intervalo. PER-AVER lo leería como una intensidad.
            serie.data_type = "PER-CUM"
            archivo.put(serie)
    except ErrorDss:
        raise
    except Exception as error:  # noqa: BLE001 - la librería no tipa sus fallos
        raise ErrorDss(
            f"no se pudo escribir la serie {nombre!r} en {destino.name}: "
            f"{error}") from error

    return {
        "nombre": nombre,
        "pathname": pathname,
        "ordenadas": len(valores),
        "inicio": inicio.isoformat(timespec="minutes"),
        "intervalo_min": intervalo_min,
        "unidades": unidades,
    }

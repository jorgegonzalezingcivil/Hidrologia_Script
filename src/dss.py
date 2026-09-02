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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "INTERVALOS",
    "Serie",
    "ruta_dss",
    "escribir_serie_precipitacion",
    "leer_series",
    "ErrorDss",
]


class ErrorDss(RuntimeError):
    """Falla al escribir o leer un DSS, que el módulo debe reportar."""


@dataclass
class Serie:
    """
    Una serie de tiempo leída del DSS, ya despegada de la librería.

    Existe para que los módulos de análisis no manejen objetos de 'hecdss': lo
    que reciben son listas de Python y dos cadenas. Un cambio en la interfaz de
    la librería se absorbe aquí y no se propaga a quien calcula.
    """

    elemento: str
    parametro: str
    unidades: str = ""
    tipo_dato: str = ""
    instantes: list[_dt.datetime] = field(default_factory=list)
    valores: list[float] = field(default_factory=list)

    @property
    def intervalo_min(self) -> float:
        """Paso de tiempo en minutos, medido sobre las dos primeras marcas."""
        if len(self.instantes) < 2:
            return 0.0
        return (self.instantes[1] - self.instantes[0]).total_seconds() / 60.0


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


def escribir_tabla_emparejada(
    destino: Path,
    nombre: str,
    valores_x: Sequence[float],
    valores_y: Sequence[float],
    parte_c: str = "STORAGE-FLOW",
    unidades_x: str = "THOU M3",
    unidades_y: str = "M3/S",
    tipo_x: str = "STORAGE",
    tipo_y: str = "FLOW",
) -> str:
    """
    Escribe una tabla emparejada en el DSS, como la curva de un embalse.

    LAS UNIDADES NO SON LAS MISMAS QUE EN EL RESTO DEL PROGRAMA. Para una tabla
    emparejada en sistema metrico HEC-HMS espera 'THOU M3'; con '1000 M3', que
    es lo que vale en los parametros de las subcuencas, rechaza la tabla con
    'Units ... are not valid for paired data'. Se comprobo sobre la 4.13.

    Devuelve el pathname escrito, para que quien construya el .pdata declare
    exactamente el que existe en el archivo.

    Excepciones
    -----------
    ErrorDss
        Si la tabla esta vacia, si las dos columnas no se corresponden o si la
        escritura falla.
    """
    if not valores_x or len(valores_x) != len(valores_y):
        raise ErrorDss(
            f"la tabla {nombre!r} trae {len(valores_x)} valor(es) en X y "
            f"{len(valores_y)} en Y.")

    try:
        from hecdss import HecDss
        from hecdss.paired_data import PairedData
    except ImportError as error:  # pragma: no cover - depende del entorno
        raise ErrorDss(
            "no esta instalado 'hecdss', que es lo que escribe el formato DSS. "
            "Instalarlo en el venv del proyecto.") from error

    pathname = f"//{nombre}/{parte_c}///TABLE/"
    tabla = PairedData.create(
        x_values=list(valores_x), y_values=[list(valores_y)],
        x_units=unidades_x, x_type=tipo_x,
        y_units=unidades_y, y_type=tipo_y, path=pathname)
    archivo = HecDss(str(destino))
    try:
        archivo.put(tabla)
    except Exception as error:                           # noqa: BLE001
        raise ErrorDss(
            f"no se pudo escribir la tabla {nombre!r} en {destino}: "
            f"{error}") from error
    finally:
        archivo.close()
    return pathname


def leer_series(
    origen: Path, parametros: Sequence[str] = (), elementos: Sequence[str] = (),
) -> list[Serie]:
    """
    Lee del DSS las series de los parámetros pedidos, ya convertidas a 'Serie'.

    EL ARCHIVO SE ABRE UNA SOLA VEZ. Un DSS de resultados de HEC-HMS trae más de
    dos mil series para un modelo de ciento veinticinco subcuencas; abrirlo y
    cerrarlo por cada una multiplica el tiempo de lectura por el número de
    series y no aporta nada.

    EL PATHNAME NO SE CONSTRUYE, SE BUSCA EN EL CATÁLOGO. La parte D lleva el
    bloque de fechas que el DSS decide al guardar, y una misma corrida produce
    '01Jan2000' para unas series y '31Dec1999-01Jan2000' para otras según dónde
    caiga el primer instante. Armar el pathname a mano acierta con unas y falla
    con otras, y una serie que no se encuentra no da error: falta y ya.

    Parámetros
    ----------
    parametros
        Partes C que interesan (FLOW, PRECIP-EXCESS...). Vacío: todas.
    elementos
        Partes B que interesan. Vacío: todos.

    Excepciones
    -----------
    ErrorDss
        Si el archivo no está, si falta la librería o si la lectura falla.
    """
    origen = Path(origen)
    if not origen.is_file():
        raise ErrorDss(f"no se encuentra el archivo DSS {origen}.")

    try:
        from hecdss import HecDss
    except ImportError as error:  # pragma: no cover - depende del entorno
        raise ErrorDss(
            "no está instalado 'hecdss', que es lo que lee el formato DSS. "
            "Instalarlo en el venv del proyecto.") from error

    quiere_parametro = {p.upper() for p in parametros}
    quiere_elemento = set(elementos)
    series: list[Serie] = []
    try:
        with HecDss(str(origen)) as archivo:
            for pathname in [str(p) for p in archivo.get_catalog()]:
                partes = pathname.split("/")
                if len(partes) < 7:
                    continue
                elemento, parametro = partes[2], partes[3]
                if quiere_parametro and parametro.upper() not in quiere_parametro:
                    continue
                if quiere_elemento and elemento not in quiere_elemento:
                    continue
                leida = archivo.get(pathname)
                # 'values' llega como arreglo de numpy: comprobar su longitud y
                # no su verdad, que en un arreglo de varios elementos es un
                # error y no un booleano.
                if leida is None or len(getattr(leida, "values", ())) == 0:
                    continue
                series.append(Serie(
                    elemento=elemento,
                    parametro=parametro.upper(),
                    unidades=str(getattr(leida, "units", "") or ""),
                    tipo_dato=str(getattr(leida, "data_type", "") or ""),
                    instantes=list(leida.times),
                    valores=[float(v) for v in leida.values],
                ))
    except ErrorDss:
        raise
    except Exception as error:  # noqa: BLE001 - la librería no tipa sus fallos
        raise ErrorDss(
            f"no se pudo leer {origen.name}: {error}") from error

    if not series:
        raise ErrorDss(
            f"{origen.name} no trae ninguna serie de {sorted(quiere_parametro) or 'ningún parámetro'}"
            f"{' para ' + str(sorted(quiere_elemento)) if quiere_elemento else ''}. "
            "Un DSS de resultados vacío suele significar que la corrida abortó.")
    return series

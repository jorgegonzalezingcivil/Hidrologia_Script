#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M13 - Actualización del proyecto de HEC-HMS y escenarios de cálculo
===================================================================
Entorno: venv del proyecto.

EL MODELO LO ENTREGA EL CONSULTOR, ESTE MÓDULO LO ACTUALIZA. La delimitación
asistida es el único paso manual del estudio (CLAUDE.md, sección 4) y produce la
topología: subcuencas, uniones, tramos y sumidero. Lo que falta es todo lo
demás, y es lo que la cadena calculó: número de curva, tiempo de rezago,
geometría de tránsito, hietogramas y escenarios.

NO SE ESCRIBE UN PROYECTO NUEVO. Rehacerlo desde cero tiraría la topología que
costó el paso manual y obligaría a repetirlo en cada corrida. Se lee el modelo
existente, se reescriben los bloques de parámetros y se conserva todo lo demás,
incluidas las coordenadas de lienzo y las conexiones aguas abajo.

SE GUARDA COPIA ANTES DE TOCAR NADA. Una escritura equivocada sobre un modelo
que costó horas de delimitación no se deshace, y el módulo escribe en el sitio
donde el consultor trabaja.

LOS MÉTODOS SE REESCRIBEN, POR DECISIÓN DECLARADA. El proyecto llegó con
Initial+Constant, Clark y Lag, y la cadena calculó para SCS Curve Number, SCS
Unit Hydrograph y Muskingum-Cunge. Cargar un número de curva en un modelo que
pierde por tasa constante no da error: da un modelo que ignora el parámetro. La
sección 7 manda verificar esa coherencia y aquí se resuelve reescribiendo el
método, no adaptando la cifra.

LOS HIETOGRAMAS VAN COMO SERIES MANUALES, no en el DSS. Escribir DSS desde
Python exige la librería binaria de HEC, que es una dependencia pesada y frágil
ante actualizaciones, justo lo que la sección 2 pide evitar. HEC-HMS lee las dos
formas.

LO QUE ESTE MÓDULO NO PUEDE VERIFICAR. Que HEC-HMS acepte los archivos. La
sintaxis de sus formatos no está publicada como especificación y solo su propio
lector es autoridad: el módulo escribe, comprueba lo que puede comprobar por su
cuenta (que cada subcuenca reciba parámetros, que cada tramo tenga geometría,
que cada pluviómetro tenga su serie) y deja constancia de que la validación
final es abrir el proyecto.

Productos:
    <proyecto>/<modelo>.basin              actualizado en su sitio
    <proyecto>/<modelo>.basin.<fecha>.bak  copia previa
    <proyecto>/hietogramas.gage
    <proyecto>/T<periodo>.met              uno por periodo de retorno
    <proyecto>/Tormenta_diseno.control
    <proyecto>/<archivo>.hms               con los componentes registrados
    data/02_procesado/M13_hec_hms.json

Uso:
    python src/M13_hec_hms.py

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import shutil
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, raster, registro, rutas  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M13"
DESCRIPCION = "Actualización del proyecto de HEC-HMS y escenarios de cálculo"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3


@dataclass
class ResultadoM13:
    proyecto: str = ""
    subcuencas: dict[str, Any] = field(default_factory=dict)
    tramos: dict[str, Any] = field(default_factory=dict)
    meteorologia: dict[str, Any] = field(default_factory=dict)
    escenarios: list[str] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Lectura del modelo entregado
# =============================================================================
def separar_bloques(texto: str) -> list[tuple[str, str, str]]:
    """
    Parte el .basin en bloques (tipo, nombre, contenido completo).

    Se conserva el TEXTO ÍNTEGRO de cada bloque, incluidos los campos que este
    módulo no toca: coordenadas de lienzo, conexiones aguas abajo, fechas. Un
    reescritor que solo emitiera lo que entiende borraría en silencio lo que el
    paso manual dejó, que es justo lo que no se puede repetir.
    """
    bloques: list[tuple[str, str, str]] = []
    actual: list[str] = []
    tipo = nombre = ""
    for linea in texto.splitlines(keepends=True):
        # El encabezado se reconoce por NO llevar sangria y por no haber uno
        # abierto todavia. Condicionarlo a que el buffer este vacio fallaba con
        # las lineas en blanco que separan bloques: quedaban en el buffer y el
        # encabezado siguiente pasaba inadvertido, de modo que ninguna subcuenca
        # se clasificaba y el modulo actualizaba cero sin decir por que.
        encabezado = re.match(r"^(\w[\w ]*): (.*)$", linea)
        if encabezado and not tipo:
            tipo, nombre = encabezado.group(1), encabezado.group(2).strip()
        actual.append(linea)
        if linea.strip() == "End:":
            bloques.append((tipo, nombre, "".join(actual)))
            actual, tipo, nombre = [], "", ""
    if actual:
        bloques.append((tipo, nombre, "".join(actual)))
    return bloques


def fijar_grupo(bloque: str, clave_metodo: str, valor_metodo: str,
                parametros: Sequence[tuple[str, str]]) -> str:
    """
    Reescribe el grupo de un metodo: su linea y los parametros que lo siguen.

    EL SITIO IMPORTA TANTO COMO EL NOMBRE. HEC-HMS lee el .basin por grupos
    separados por lineas en blanco, y cada parametro pertenece al metodo que lo
    encabeza. La primera version anadia 'Curve Number' y 'Lag' antes del 'End:',
    es decir dentro del grupo del flujo base, y HEC-HMS los rechazo uno a uno:

        WARNING 12050: Unrecognized parameter in basin model
        Line contents are "lag: 28.14"

    El proyecto abria y los ignoraba en silencio, que es peor que no abrir.

    El grupo va desde la linea del metodo hasta la siguiente linea en blanco.
    Se sustituye entero, de modo que los parametros del metodo anterior
    desaparecen sin tener que enumerarlos.
    """
    lineas = bloque.splitlines(keepends=True)
    salida: list[str] = []
    indice = 0
    reemplazado = False
    while indice < len(lineas):
        linea = lineas[indice]
        if not reemplazado and re.match(rf"^\s*{re.escape(clave_metodo)}: ", linea):
            sangria = re.match(r"^(\s*)", linea).group(1)
            salida.append(f"{sangria}{clave_metodo}: {valor_metodo}\n")
            for nombre, valor in parametros:
                salida.append(f"{sangria}{nombre}: {valor}\n")
            indice += 1
            # El grupo termina en una linea en blanco O en el 'End:' del
            # bloque, lo que llegue antes. Detenerse solo en la linea en blanco
            # se comia el 'End:' cuando el metodo era el ultimo grupo, que es
            # como HEC-HMS reescribe los tramos al guardar: el bloque quedaba
            # sin cerrar y se fusionaba con el siguiente. Se perdian la mitad de
            # las subcuencas y el modulo declaraba haber actualizado 61 de 125.
            while (indice < len(lineas) and lineas[indice].strip()
                   and lineas[indice].strip() != "End:"):
                indice += 1
            reemplazado = True
            continue
        salida.append(linea)
        indice += 1
    return "".join(salida)


def fijar_campo(bloque: str, clave: str, valor: str) -> str:
    """
    Sustituye el valor de un campo, o lo añade antes del 'End:' si no está.

    Respeta la sangría del archivo, que HEC-HMS conserva al reescribir sus
    propios modelos.
    """
    patron = re.compile(rf"^(\s*){re.escape(clave)}: .*$", re.M)
    if patron.search(bloque):
        return patron.sub(lambda m: f"{m.group(1)}{clave}: {valor}", bloque, count=1)
    return bloque.replace("End:", f"     {clave}: {valor}\nEnd:", 1)


def quitar_campos(bloque: str, claves: Sequence[str]) -> str:
    """Elimina campos que ya no aplican al método nuevo."""
    for clave in claves:
        bloque = re.sub(rf"^\s*{re.escape(clave)}: .*\n", "", bloque, flags=re.M)
    return bloque


def leer_parametros_subcuenca(ruta: Path, delimitador: str) -> dict[str, dict]:
    """
    Número de curva y tiempo de rezago por subcuenca, del M10.

    Excepciones
    -----------
    ErrorRutas
        Si no está la tabla.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}: ejecutar antes el M10, que es quien "
            "calcula el numero de curva y el rezago por subcuenca.")
    parametros: dict[str, dict] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            nombre = str(fila.get("subcuenca", "")).strip()
            if not nombre:
                continue

            def numero(clave):
                try:
                    return float(fila[clave])
                except (KeyError, TypeError, ValueError):
                    return None

            parametros[nombre] = {
                "cn": numero("cn"),
                "tlag_min": numero("tlag_minutos"),
                "area_km2": numero("area_km2"),
            }
    if not parametros:
        raise ErrorFormato(f"{ruta.name} no trae ninguna subcuenca.")
    return parametros


def geometria_de_tramos(
    ruta_sqlite: Path, ruta_dem: Path, crs_modelo: str, crs_calculo: str,
) -> dict[str, dict]:
    """
    Longitud y pendiente de cada tramo, del propio proyecto de HEC-HMS.

    La longitud sale de la tabla 'reach2d' que el análisis de terreno dejó, que
    es la del cauce trazado y no la de la recta entre extremos del esquema.

    LA PENDIENTE SE MIDE SOBRE EL DEM. Las geometrías vienen marcadas como 3D
    pero su cota es cero en todos los vértices, de modo que el propio proyecto
    no la trae: se leen las cotas de los extremos y se divide por la longitud
    del cauce. Es la pendiente de energía que Muskingum-Cunge necesita.

    Excepciones
    -----------
    ErrorRutas
        Si falta el sqlite del modelo o el DEM.
    """
    import sqlite3

    from pyproj import Transformer

    if not ruta_sqlite.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta_sqlite}: sin el sqlite del modelo no hay "
            "longitud de tramo, y Muskingum-Cunge la necesita.")
    if not ruta_dem.is_file():
        raise ErrorRutas(f"no se encuentra el DEM en {ruta_dem}.")

    info = raster.leer_info(ruta_dem)
    formato = {"<f4": "f", "<i2": "h", "<u2": "H", "<f8": "d"}.get(
        info.descriptor)
    if formato is None:
        raise ErrorFormato(
            f"{ruta_dem.name}: tipo {info.descriptor} no muestreable.")
    conversor = Transformer.from_crs(crs_modelo, crs_calculo, always_xy=True)

    def extremos(bruto: bytes) -> tuple[tuple[float, float], tuple[float, float]]:
        orden = "<" if bruto[0] == 1 else ">"
        tipo = struct.unpack_from(orden + "I", bruto, 1)[0]
        con_z = bool(tipo & 0x80000000)
        formato_punto = "3d" if con_z else "2d"
        paso = 24 if con_z else 16
        cuantos = struct.unpack_from(orden + "I", bruto, 5)[0]
        primero = struct.unpack_from(orden + formato_punto, bruto, 9)[:2]
        ultimo = struct.unpack_from(
            orden + formato_punto, bruto, 9 + (cuantos - 1) * paso)[:2]
        return primero, ultimo

    tramos: dict[str, dict] = {}
    conexion = sqlite3.connect(f"file:{ruta_sqlite}?mode=ro", uri=True)
    try:
        filas = list(conexion.execute(
            "SELECT name, GEOMETRY, length FROM reach2d"))
    finally:
        conexion.close()

    with raster.LectorRaster(ruta_dem) as lector:
        for nombre, bruto, longitud in filas:
            if not bruto or not longitud:
                continue
            try:
                inicio, final = extremos(bruto)
            except (struct.error, IndexError):
                continue
            cotas = []
            for x, y in (inicio, final):
                gx, gy = conversor.transform(x, y)
                fila_r, columna = info.fila_de(gy), info.columna_de(gx)
                if not (0 <= fila_r < info.alto and 0 <= columna < info.ancho):
                    continue
                cotas.append(struct.unpack_from(
                    "<" + formato, lector.fila(fila_r),
                    columna * info.bytes_por_muestra)[0])
            if len(cotas) != 2:
                continue
            desnivel = abs(float(cotas[0]) - float(cotas[1]))
            tramos[str(nombre)] = {
                "longitud_m": round(float(longitud), 2),
                "cota_inicio": round(float(cotas[0]), 2),
                "cota_fin": round(float(cotas[1]), 2),
                "pendiente": round(desnivel / float(longitud), 6),
            }
    return tramos


# =============================================================================
# Escritura
# =============================================================================
def actualizar_subcuenca(bloque: str, parametros: dict) -> tuple[str, str]:
    """
    Reescribe un bloque de subcuenca al método SCS con sus parámetros.

    Devuelve el bloque y el motivo si no se pudo completar. Una subcuenca sin
    número de curva o sin rezago se deja SIN tocar y se reporta: rellenarla con
    un valor por defecto produciría un modelo que corre y miente.
    """
    faltan = [c for c in ("cn", "tlag_min") if parametros.get(c) is None]
    if faltan:
        return bloque, f"sin {', '.join(faltan)}"

    bloque = fijar_grupo(bloque, "LossRate", "SCS", (
        ("Percent Impervious Area", "0.0"),
        ("Curve Number", f"{parametros['cn']:.1f}"),
    ))
    # 'Unitgraph Type: STANDARD' acompana siempre al hidrograma unitario del
    # SCS en los modelos de ejemplo de HEC-HMS 4.13.
    bloque = fijar_grupo(bloque, "Transform", "SCS", (
        ("Lag", f"{parametros['tlag_min']:.2f}"),
        ("Unitgraph Type", "STANDARD"),
    ))
    return bloque, ""


def actualizar_tramo(bloque: str, geometria: dict, n_manning: float,
                     ancho_fondo: float, talud: float,
                     celeridad: float = 1.0) -> tuple[str, str]:
    """Reescribe un bloque de tramo a Muskingum-Cunge con su geometría."""
    if not geometria:
        return bloque, "sin geometria"
    if geometria["pendiente"] <= 0:
        return bloque, "pendiente nula"

    # EL VOCABULARIO SALE DE UN TRAMO QUE HEC-HMS CONFIGURO, no de suponerlo.
    # Cinco etiquetas no eran las que parecian: 'Mannings n' lleva ese, el ancho
    # es 'Bottom Width', el metodo de paso es 'Space-Time Method: Automatic DX
    # and DT' y el indice se declara en dos lineas, tipo y valor. La primera
    # version las escribio de otra forma y HEC-HMS las rechazo una a una:
    #
    #     Section begins with label "reach: R62"
    #     Line contents are "manningn: 0.040"
    #
    # El orden tambien se respeta: es el que el programa escribe al guardar.
    bloque = fijar_grupo(bloque, "Route", "Muskingum Cunge", (
        ("Channel", "Trapezoid"),
        ("Length", f"{geometria['longitud_m']:.2f}"),
        ("Energy Slope", f"{geometria['pendiente']:.6f}"),
        ("Mannings n", f"{n_manning:.3f}"),
        ("Bottom Width", f"{ancho_fondo:.2f}"),
        ("Side Slope", f"{talud:.2f}"),
        ("Initial Variable", "Combined Inflow"),
        ("Space-Time Method", "Automatic DX and DT"),
        ("Index Parameter Type", "Index Celerity"),
        ("Index Celerity", f"{celeridad:.2f}"),
        ("Maximum Depth Iterations", "20"),
        ("Maximum Route Step Iterations", "30"),
        ("Channel Loss", "None"),
    ))
    return bloque, ""


def escribir_gage(destino_gage: Path, destino_dss: Path, hietogramas,
                  intervalo_min: float, inicio: _dt.datetime) -> dict[str, Any]:
    """
    Escribe los pluviometros: las series en el DSS y su declaracion en el .gage.

    HEC-HMS NO ADMITE SERIES EN TEXTO. Su 'Manual Entry' guarda los valores en el
    DSS del proyecto y en el .gage deja solo el 'Pathname'. La primera version
    escribia los valores dentro del .gage, sin 'Filename', y el programa caia
    con un NullPointerException al construir la ruta del archivo que no estaba.
    El vocabulario de aqui esta leido de un pluviometro que el propio HEC-HMS
    escribio.

    El nombre va DOS veces, dentro y fuera del bloque, y el archivo se abre con
    un bloque 'Gage Manager'. Las dos cosas son de su formato, no un descuido.
    """
    import dss as adaptador_dss

    por_pluviometro: dict[str, list[dict]] = {}
    for paso in hietogramas:
        clave = (f"{paso['pluviometro']}_T"
                 f"{str(paso['periodo_retorno']).replace('.', '_')}")
        por_pluviometro.setdefault(clave, []).append(paso)

    lineas = ["Gage Manager: ", "     Gage Manager: ", "     Version: 4.13",
              "     Filepath Separator: \\", "End: ", ""]
    escritas = []
    for nombre, pasos in sorted(por_pluviometro.items()):
        pasos.sort(key=lambda p: int(p["intervalo"]))
        valores = [float(p["lamina_mm"]) for p in pasos]
        escrita = adaptador_dss.escribir_serie_precipitacion(
            destino_dss, nombre, valores, inicio, int(intervalo_min))
        escritas.append(escrita)
        fin = inicio + _dt.timedelta(minutes=intervalo_min * len(valores))
        lineas += [
            f"Gage: {nombre}",
            f"     Gage: {nombre}",
            "     Gage Type: Precipitation",
            "     Description: hietograma de diseno, metodo de Huff",
            "     Reference Height Units: Meters",
            "     Reference Height: 10.0",
            "     Data Source Type: Manual Entry",
            f"     Filename: {destino_dss.name}",
            f"     Pathname: {escrita['pathname']}",
            "     Variant: Variant-1",
            f"       Start Time: {inicio.day} {inicio:%B %Y}, {inicio:%H:%M}",
            f"       End Time: {fin.day} {fin:%B %Y}, {fin:%H:%M}",
            "     End Variant: Variant-1",
            "End:",
            "",
        ]

    destino_gage.write_text("\n".join(lineas), encoding="utf-8")
    return {"pluviometros": len(por_pluviometro),
            "ordenadas": sum(e["ordenadas"] for e in escritas),
            "dss": destino_dss.name}


def escribir_met(destino: Path, nombre: str, periodo: str, asignacion,
                 pluviometro_de_zona, modelo_cuenca: str) -> None:
    """
    Modelo meteorológico de un periodo de retorno.

    LA ESTRUCTURA SALE DE LOS EJEMPLOS DE HEC-HMS 4.13, no de suponerla. Un .met
    necesita tres cosas que la primera versión no escribía y sin las cuales el
    programa no lo muestra: declarar a qué modelo de cuenca se aplica
    ('Use Basin Model'), listar los pluviómetros que usa, y enumerar todos los
    métodos meteorológicos aunque sean 'None'.

    Cada subcuenca queda enganchada al pluviómetro de SU ZONA. Es lo que hace
    que cinco series basten para ciento veinticinco subcuencas.
    """
    usados = sorted({pluviometro_de_zona(f["pluviometro"], periodo)
                     for f in asignacion})
    lineas = [
        f"Meteorology: {nombre}",
        f"     Description: tormenta de diseno, periodo de retorno {periodo} anios",
        "     Version: 4.13",
        "     Unit System: Metric",
        "     Set Missing Data to Default: Yes",
        "     Precipitation Method: Specified Hyetograph",
        "     Air Temperature Method: None",
        "     Atmospheric Pressure Method: None",
        "     Dew Point Method: None",
        "     Wind Speed Method: None",
        "     Shortwave Radiation Method: None",
        "     Longwave Radiation Method: None",
        "     Snowmelt Method: None",
        "     Evapotranspiration Method: No Evapotranspiration",
        f"     Use Basin Model: {modelo_cuenca}",
        "End:",
        "",
    ]
    for gage in usados:
        lineas += [f"Gage: {gage}", "     Type: Recording", "End:", ""]
    lineas += [
        "Precip Method Parameters: Specified Hyetograph",
        "End:",
        "",
    ]
    for fila in asignacion:
        lineas += [
            f"Subbasin: {fila['subcuenca']}",
            f"     Gage: {pluviometro_de_zona(fila['pluviometro'], periodo)}",
            "End:",
            "",
        ]
    destino.write_text("\n".join(lineas), encoding="utf-8")


def escribir_control(destino: Path, nombre: str, inicio: _dt.datetime,
                     fin: _dt.datetime, intervalo_min: float) -> None:
    """Especificaciones de control, comunes a todos los escenarios."""
    lineas = [
        f"Control: {nombre}",
        "     Description: tormenta de diseno mas recesion",
        f"     Start Date: {inicio:%d %B %Y}",
        f"     Start Time: {inicio:%H:%M}",
        f"     End Date: {fin:%d %B %Y}",
        f"     End Time: {fin:%H:%M}",
        f"     Time Interval: {intervalo_min:.0f}",
        "End:",
        "",
    ]
    destino.write_text("\n".join(lineas), encoding="utf-8")


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Actualiza el modelo de cuenca y genera la meteorología y los escenarios."""
    inicio_reloj = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM13()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M13_hec_hms.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"proyecto": configuracion.obtener(
            "hec_hms.proyecto.directorio")},
        parametros=configuracion.parametros("hec_hms"))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    directorio = str(configuracion.obtener(
        "hec_hms.proyecto.directorio", "") or "").strip()
    if not directorio:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "proyecto.sin_ruta",
            "hec_hms.proyecto.directorio esta vacio: hay que declarar donde "
            "vive el modelo que el consultor construyo. La cadena no lo busca "
            "ni lo adivina.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    proyecto = Path(directorio)
    resultado.proyecto = str(proyecto)
    ruta_basin = proyecto / str(configuracion.obtener(
        "hec_hms.proyecto.modelo_cuenca"))
    if not ruta_basin.is_file():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "proyecto.sin_modelo",
            f"no se encuentra {ruta_basin}. Revisar "
            "hec_hms.proyecto.directorio y modelo_cuenca.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    try:
        parametros = leer_parametros_subcuenca(
            rutas.directorio("procesado", base) / "morfometria"
            / "subcuencas.csv", delimitador)
        geometrias = geometria_de_tramos(
            ruta_basin.with_suffix(".sqlite"),
            rutas.resolver(configuracion.obtener("dem.delimitacion.salida_dem"),
                           base),
            str(configuracion.obtener("punto_descarga.crs")),
            str(configuracion.obtener("crs.calculo")))
        hietogramas = _leer_csv(
            rutas.directorio("procesado_tormenta", base) / "hietogramas.csv")
        asignacion = _leer_csv(
            rutas.directorio("procesado_tormenta", base)
            / "asignacion_pluviometros.csv")
    except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "proyecto.insumos", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Modelo de cuenca"):
        _actualizar_modelo(configuracion, ruta_basin, parametros, geometrias,
                           resultado, logger)

    with registro.bloque(logger, "Meteorologia y escenarios"):
        _escribir_meteorologia(configuracion, proyecto, hietogramas,
                               asignacion, resultado, logger)

    _registrar_productos(base, resultado)
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _leer_csv(ruta: Path) -> list[dict[str, str]]:
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra {ruta}.")
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=";"))
    if not filas:
        raise ErrorFormato(f"{ruta.name} esta vacio.")
    return filas


def _actualizar_modelo(configuracion, ruta_basin, parametros, geometrias,
                       resultado, logger) -> None:
    """Reescribe los bloques de subcuenca y de tramo del modelo entregado."""
    texto = ruta_basin.read_text(encoding="utf-8", errors="replace")

    if bool(configuracion.obtener("hec_hms.proyecto.copia_de_seguridad")):
        # SE RESPALDA TODO LO QUE EL MODULO TOCA, no solo el .basin. La primera
        # version copiaba unicamente el modelo de cuenca, que era lo que parecia
        # costoso de perder, y dejo sin proteger el .hms: una escritura
        # equivocada lo trunco y el proyecto dejo de abrir. El indice del
        # proyecto es tan insustituible como el modelo.
        marca = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for original in sorted(ruta_basin.parent.glob("*")):
            if original.suffix.lower() not in (".basin", ".hms", ".gage",
                                               ".met", ".control", ".dss"):
                continue
            copia = original.with_name(f"{original.name}.{marca}.bak")
            shutil.copy2(original, copia)
            resultado.productos.append(str(copia))
        logger.info("Copia previa de los archivos del proyecto: marca %s", marca)

    n_manning = float(configuracion.obtener(
        "hec_hms.transito.muskingum_cunge.n_manning"))
    ancho = float(configuracion.obtener(
        "hec_hms.transito.muskingum_cunge.ancho_fondo_m"))
    talud = float(configuracion.obtener(
        "hec_hms.transito.muskingum_cunge.talud_h_por_v"))
    celeridad = float(configuracion.obtener(
        "hec_hms.transito.muskingum_cunge.celeridad_indice_ms", 1.0))

    nuevos: list[str] = []
    sin_parametros: list[str] = []
    sin_geometria: list[str] = []
    actualizadas = tramos_ok = 0
    for tipo, nombre, bloque in separar_bloques(texto):
        if tipo == "Subbasin":
            bloque, motivo = actualizar_subcuenca(
                bloque, parametros.get(nombre, {}))
            if motivo:
                sin_parametros.append(f"{nombre} ({motivo})")
            else:
                actualizadas += 1
        elif tipo == "Reach":
            bloque, motivo = actualizar_tramo(
                bloque, geometrias.get(nombre, {}), n_manning, ancho, talud,
                celeridad)
            if motivo:
                sin_geometria.append(f"{nombre} ({motivo})")
            else:
                tramos_ok += 1
        nuevos.append(bloque)

    ruta_basin.write_text("".join(nuevos), encoding="utf-8")
    resultado.productos.append(str(ruta_basin))
    resultado.subcuencas = {"actualizadas": actualizadas,
                            "sin_parametros": sin_parametros}
    resultado.tramos = {"actualizados": tramos_ok,
                        "sin_geometria": sin_geometria,
                        "n_manning": n_manning, "ancho_fondo_m": ancho,
                        "talud_h_por_v": talud}
    logger.info("%d subcuenca(s) y %d tramo(s) actualizados",
                actualizadas, tramos_ok)

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "modelo.actualizado",
        f"{actualizadas} subcuenca(s) pasan a SCS Curve Number y SCS Unit "
        f"Hydrograph con su CN y su rezago, y {tramos_ok} tramo(s) a "
        f"Muskingum-Cunge con seccion trapezoidal, n de Manning {n_manning:.3f}, "
        f"ancho de fondo {ancho:.1f} m y talud {talud:.1f}H:1V. La topologia, "
        "las coordenadas de lienzo y las conexiones aguas abajo se conservan "
        "intactas: solo se reescriben los bloques de parametros.",
    ))
    if sin_parametros:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "modelo.subcuencas_sin_parametros",
            f"{len(sin_parametros)} subcuenca(s) quedan SIN tocar por falta de "
            f"parametros: {sin_parametros[:6]}. Se dejan con el metodo que "
            "tenian en lugar de rellenarlas con un valor por defecto, que "
            "produciria un modelo que corre y miente. Son las que el M10 no "
            "pudo resolver.",
        ))
    if sin_geometria:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "modelo.tramos_sin_geometria",
            f"{len(sin_geometria)} tramo(s) sin geometria utilizable: "
            f"{sin_geometria[:6]}.",
        ))
    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "modelo.validacion_pendiente",
        "la sintaxis de los archivos de HEC-HMS no esta publicada como "
        "especificacion y solo su propio lector es autoridad: este modulo "
        "comprueba lo que puede por su cuenta, pero LA VALIDACION FINAL ES "
        "ABRIR EL PROYECTO en HEC-HMS 4.13. La copia previa del modelo queda "
        "junto al original por si hubiera que volver atras.",
    ))


def _escribir_meteorologia(configuracion, proyecto, hietogramas, asignacion,
                           resultado, logger) -> None:
    """Escribe los pluviómetros, los modelos meteorológicos y el control."""
    intervalo = float(configuracion.obtener("tormenta.intervalo_calculo_min"))
    duracion_h = float(configuracion.obtener("tormenta.duracion_h"))
    inicio = _dt.datetime(2000, 1, 1, 0, 0)

    # El archivo de pluviometros se llama COMO EL PROYECTO: es asi como HEC-HMS
    # lo encuentra, sin declararlo en el .hms.
    gage = proyecto / f"{Path(str(configuracion.obtener('hec_hms.proyecto.archivo'))).stem}.gage"
    resumen = escribir_gage(
        gage, proyecto / str(configuracion.obtener(
            "hec_hms.proyecto.dss", "") or f"{gage.stem}.dss"),
        hietogramas, intervalo, inicio)
    resultado.productos.append(str(gage))

    periodos = sorted({str(h["periodo_retorno"]) for h in hietogramas},
                      key=float)
    modelo_cuenca = _nombre_del_modelo(
        proyecto / str(configuracion.obtener("hec_hms.proyecto.modelo_cuenca")))

    def pluviometro_de_zona(pluviometro: str, periodo: str) -> str:
        return f"{pluviometro}_T{periodo.replace('.', '_')}"

    for periodo in periodos:
        nombre = f"T{periodo.replace('.', '_')}"
        destino = proyecto / f"{nombre}.met"
        escribir_met(destino, nombre, periodo, asignacion, pluviometro_de_zona,
                     modelo_cuenca)
        resultado.productos.append(str(destino))
        resultado.escenarios.append(nombre)

    # La ventana cubre la tormenta y una recesion de tres veces su duracion,
    # para que el hidrograma llegue a agotarse dentro del calculo.
    fin = inicio + _dt.timedelta(hours=duracion_h * 4)
    control = proyecto / "Tormenta_diseno.control"
    escribir_control(control, "Tormenta_diseno", inicio, fin, intervalo)
    resultado.productos.append(str(control))

    ruta_hms = proyecto / str(configuracion.obtener("hec_hms.proyecto.archivo"))
    if ruta_hms.is_file():
        registrar_componentes(ruta_hms,
                              [f"T{p.replace('.', '_')}" for p in periodos],
                              "Tormenta_diseno")
        resultado.productos.append(str(ruta_hms))
    else:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "proyecto.sin_hms",
            f"no se encuentra {ruta_hms.name}: los componentes quedan escritos "
            "pero sin declarar en el proyecto, y HEC-HMS no los vera.",
        ))

    resultado.meteorologia = {
        "pluviometros": resumen["pluviometros"],
        "ordenadas": resumen["ordenadas"],
        "periodos": periodos,
        "ventana_horas": duracion_h * 4,
        "intervalo_min": intervalo,
    }
    logger.info("%d pluviometro(s), %d modelo(s) meteorologico(s), ventana de "
                "%.0f h", resumen["pluviometros"], len(periodos),
                duracion_h * 4)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "meteorologia.escrita",
        f"{resumen['pluviometros']} pluviometro(s) con {resumen['ordenadas']} "
        f"ordenada(s) en total, y {len(periodos)} modelo(s) meteorologico(s), "
        "uno por periodo de retorno. Cada subcuenca queda enganchada al "
        "pluviometro de SU ZONA, que es lo que hace que cinco series basten "
        f"para {len(asignacion)} subcuencas. Las series van dentro del .gage y "
        "no en el DSS, de modo que el proyecto es autocontenido.",
    ))
    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "escenarios.sin_crear",
        f"quedan escritos {len(periodos)} modelo(s) meteorologico(s) y las "
        "especificaciones de control, pero las SIMULACIONES (los 'Run') se "
        "crean en HEC-HMS combinando modelo de cuenca, meteorologia y control. "
        "Es un paso de interfaz que el M14 ejecutara, o que el consultor hace "
        "en tres clics por escenario.",
    ))


def registrar_componentes(
    ruta_hms: Path, meteorologias: Sequence[str], control: str,
) -> None:
    """
    Declara los componentes nuevos en el archivo de proyecto.

    SIN ESTO EL PROYECTO NO LOS VE. HEC-HMS no descubre los archivos de un
    directorio: el .hms es su índice.

    EL MODELO METEOROLÓGICO SE DECLARA COMO 'Precipitation', no como
    'Meteorology', que es la etiqueta que usa el .met por dentro. Los
    pluviómetros NO se declaran aquí: HEC-HMS toma el archivo .gage que se llama
    como el proyecto. Las dos cosas salen de los proyectos de ejemplo de la
    propia instalación, no de suponerlas.
    """
    texto = ruta_hms.read_text(encoding="utf-8", errors="replace")
    texto = re.sub(
        r"^(?:Precipitation Gage|Precipitation|Meteorology|Control): .*?^End:\s*\n",
        "", texto, flags=re.S | re.M)

    partes = [texto.rstrip("\n"), ""]
    for nombre in meteorologias:
        partes += [f"Precipitation: {nombre}",
                   f"     Filename: {nombre}.met",
                   "     Description: tormenta de diseno", "End:", ""]
    partes += [f"Control: {control}",
               f"     FileName: {control}.control", "End:", ""]
    ruta_hms.write_text("\n".join(partes), encoding="utf-8")


def _nombre_del_modelo(ruta_basin: Path) -> str:
    """
    Nombre interno del modelo de cuenca, que no tiene por que ser el del archivo.

    El .met lo referencia por su nombre ('Basin 1') y no por su archivo
    ('Basin_1.basin'): tomarlo del archivo dejaria la referencia rota.
    """
    if not ruta_basin.is_file():
        return ruta_basin.stem
    for linea in ruta_basin.read_text(
            encoding="utf-8", errors="replace").splitlines():
        encabezado = re.match(r"^Basin: (.+)$", linea)
        if encabezado:
            return encabezado.group(1).strip()
    return ruta_basin.stem


def _registrar_productos(base, resultado) -> None:
    """Deja los productos como rutas legibles en el reporte."""
    resultado.productos = [str(p) for p in resultado.productos]


def _cerrar(logger, resultado, base, ruta_json, inicio, codigo):
    """Emite el reporte, escribe el JSON y cierra el log."""
    orden = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}
    hallazgos = sorted(resultado.hallazgos,
                       key=lambda h: (orden.get(h.severidad, 9), h.clave))

    logger.info(registro.SEPARADOR)
    for severidad, emitir in ((BLOQUEANTE, logger.error),
                              (ADVERTENCIA, logger.warning),
                              (INFORMATIVO, logger.info)):
        grupo = [h for h in hallazgos if h.severidad == severidad]
        if not grupo:
            continue
        emitir("%s (%d)", severidad, len(grupo))
        for hallazgo in grupo:
            emitir("  %-44s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    reporte = {
        "modulo": MODULO,
        "proyecto": resultado.proyecto,
        "subcuencas": resultado.subcuencas,
        "tramos": resultado.tramos,
        "meteorologia": resultado.meteorologia,
        "escenarios": resultado.escenarios,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=1),
                         encoding="utf-8")

    productos = {f"producto {i}": p
                 for i, p in enumerate(resultado.productos, start=1)}
    productos["reporte JSON"] = rutas.relativa(ruta_json, base)
    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecucion"] = rutas.relativa(archivo_log, base)

    registro.registrar_cierre(
        logger, MODULO, "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO",
        segundos=time.perf_counter() - inicio, productos=productos)
    return codigo, hallazgos


def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None)
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json)
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato,
            ErrorHidrologia) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    sys.exit(main())

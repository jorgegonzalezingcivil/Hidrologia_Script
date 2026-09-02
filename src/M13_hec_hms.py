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
def curva_de_embalse(elevacion_volumen, cota_cresta: float,
                     longitud_cresta_m: float, coeficiente: float,
                     descarga_fondo_m3s: float,
                     lamina_maxima_m: float = 3.0,
                     pasos_sobre_cresta: int = 12,
                     ) -> tuple[list[float], list[float], list[float]]:
    """
    Curvas de elevacion contra almacenamiento y contra descarga de un embalse.

    POR QUE POR ELEVACION Y NO POR ALMACENAMIENTO. La forma almacenamiento
    contra descarga resuelve el mismo transito con una sola tabla, pero colapsa
    dos relaciones en una: no deja leer el nivel, no separa la geometria del
    vaso de la hidraulica de sus salidas, y sobre todo no permite declarar el
    ESTADO DE OPERACION del que se parte. Un embalse no llega a la creciente en
    un estado hidraulico sino en uno operativo, y esa es la decision que el
    informe tiene que declarar.

    DOS SALIDAS INDEPENDIENTES, como opera el embalse real:

      - La descarga de fondo, por valvulas, que opera de continuo.
      - El vertedero libre, que solo actua por encima de su cresta:
        Q = C * L * H^1.5, con H la lamina sobre ella.

    La curva de elevacion contra volumen es un DATO del operador y no se
    inventa. La de descarga se compone con las dos salidas.

    Devuelve tres listas alineadas: cotas, volumen en miles de m3 y descarga en
    m3/s. El almacenamiento va en 'THOU M3', que es la unidad con que HEC-HMS
    lee una tabla emparejada en sistema metrico; con '1000 M3', que vale en los
    parametros de las subcuencas, rechaza la tabla.

    Excepciones
    -----------
    ErrorHidrologia
        Si la curva no trae al menos dos puntos, si no es creciente, o si
        alguna magnitud de las salidas es imposible.
    """
    puntos = sorted((float(z), float(v)) for z, v in elevacion_volumen)
    if len(puntos) < 2:
        raise ErrorHidrologia(
            "la curva de elevacion contra volumen necesita al menos dos "
            f"puntos; llegaron {len(puntos)}.")
    for (z0, v0), (z1, v1) in zip(puntos, puntos[1:]):
        if v1 < v0:
            raise ErrorHidrologia(
                f"la curva del embalse no es creciente: en {z1} m el volumen "
                f"({v1}) es menor que en {z0} m ({v0}).")
    if longitud_cresta_m <= 0 or coeficiente <= 0:
        raise ErrorHidrologia(
            f"el vertedero necesita longitud y coeficiente positivos; "
            f"llegaron {longitud_cresta_m} y {coeficiente}.")
    if descarga_fondo_m3s < 0:
        raise ErrorHidrologia(
            f"la descarga de fondo no puede ser negativa: {descarga_fondo_m3s}.")

    # La pendiente de la ultima franja, en miles de m3 por metro, sirve para
    # prolongar el vaso por encima de la cresta, que es donde la curva del
    # operador suele terminar. NO es un area: es dV/dz ya en las unidades de la
    # tabla, y tratarla como area metia un factor de mil.
    (z0, v0), (z1, v1) = puntos[-2], puntos[-1]
    pendiente_vol = (v1 - v0) * 1000.0 / (z1 - z0) if z1 > z0 else 0.0

    cotas = [z for z, _ in puntos]
    for i in range(1, int(pasos_sobre_cresta) + 1):
        cota = cota_cresta + lamina_maxima_m * i / float(pasos_sobre_cresta)
        if cota > cotas[-1]:
            cotas.append(cota)
    if cota_cresta not in cotas:
        cotas.append(cota_cresta)
    cotas = sorted(set(cotas))

    volumenes, descargas = [], []
    for cota in cotas:
        volumenes.append(_volumen_interpolado(puntos, cota, pendiente_vol))
        salida = float(descarga_fondo_m3s)
        if cota > cota_cresta:
            salida += (float(coeficiente) * float(longitud_cresta_m)
                       * (cota - cota_cresta) ** 1.5)
        descargas.append(salida)
    return cotas, volumenes, descargas


def _volumen_interpolado(puntos, cota: float, pendiente_vol: float) -> float:
    """
    Volumen en miles de m3 a una cota, prolongando por encima del ultimo dato.

    'pendiente_vol' es dV/dz en miles de m3 por metro, no un area.
    """
    if cota <= puntos[0][0]:
        return puntos[0][1] * 1000.0
    if cota >= puntos[-1][0]:
        return (puntos[-1][1] * 1000.0
                + pendiente_vol * (cota - puntos[-1][0]))
    for (z0, v0), (z1, v1) in zip(puntos, puntos[1:]):
        if z0 <= cota <= z1:
            fraccion = (cota - z0) / (z1 - z0) if z1 > z0 else 0.0
            return (v0 + (v1 - v0) * fraccion) * 1000.0
    return puntos[-1][1] * 1000.0


def bloque_de_embalse(nombre: str, tabla_volumen: str, tabla_descarga: str,
                      volumen_inicial: float, aguas_abajo: str,
                      canvas: tuple[float, float] | None = None) -> str:
    """
    Bloque 'Reservoir' del .basin, en la forma por elevacion.

    LA SINTAXIS NO ES LA DE LA INTERFAZ. En pantalla el metodo se llama
    'Outflow Curve', pero el token que el archivo espera es 'Modified Puls': con
    la etiqueta de la interfaz HEC-HMS aborta al abrir el proyecto con
    'Unknown route method in createRouteElement: Unspecified', y NO escribe nada
    en los logs de corrida, de modo que quedan los de la vez anterior. Se
    obtuvo de las clases del propio programa, porque el formato del .basin no
    esta publicado como especificacion.

    'Initial Storage' es lo que hace declarable el ESTADO DE OPERACION del que
    se parte, que es la decision con mas peso de todo el elemento: entre partir
    del nivel normal y partir de la cresta, el caudal de salida cambia por un
    factor de treinta. Se da en miles de m3, y la curva del operador dice a que
    cota corresponde.
    """
    ahora = _dt.datetime.now()
    posicion = ""
    if canvas is not None:
        posicion = (f"     Canvas X: {float(canvas[0])}\n"
                    f"     Canvas Y: {float(canvas[1])}\n")
    return (
        f"Reservoir: {nombre}\n"
        f"     Last Modified Date: {ahora.strftime('%d %B %Y')}\n"
        f"     Last Modified Time: {ahora.strftime('%H:%M:%S')}\n"
        + posicion
        + f"     Downstream: {aguas_abajo}\n"
        "\n"
        "     Route: Modified Puls\n"
        # POR QUE ALMACENAMIENTO Y NO ELEVACION, HABIENDO CURVA DEL OPERADOR.
        # Se intento la forma por elevacion, que seria la preferible porque deja
        # leer el nivel y separa la geometria del vaso de la hidraulica de sus
        # salidas. HEC-HMS 4.13 NO la admite con Puls modificado: reescribe el
        # .basin al abrirlo, borra en silencio 'Primary Table' y
        # 'Elevation-Outflow Table', y aborta con 'No storage table type
        # selected for reservoir'. Medido sobre el modelo de este estudio.
        #
        # La tabla que se escribe SI sale de la curva del operador y de las dos
        # salidas reales, de modo que el transito es el mismo. Lo que se pierde
        # es poder leer la cota: el estado inicial se declara como volumen en
        # lugar de como nivel, y la equivalencia entre ambos es la propia curva.
        "     Routing Curve: Storage-Outflow\n"
        f"     Storage-Outflow Table: {tabla_descarga}\n"
        f"     Initial Storage: {float(volumen_inicial):.1f}\n"
        "End:\n\n"
    )


def enlazar_embalse(texto: str, nombre: str, aguas_arriba: str,
                    aguas_abajo: str, bloque: str) -> tuple[str, str]:
    """
    Mete el embalse entre dos elementos y reconecta el enlace.

    ES IDEMPOTENTE, que aqui no es un lujo: el M13 se ejecuta muchas veces sobre
    el mismo modelo, y duplicar el elemento o dejar dos enlaces produciria un
    .basin que HEC-HMS rechaza sin decir por que.

    SE COMPRUEBA LA TOPOLOGIA DECLARADA. Si el elemento de aguas arriba descarga
    en otro sitio, se reporta y no se toca nada: reconectar a ciegas moveria el
    embalse a una rama que no es la suya y laminaria area que no le corresponde.

    Devuelve el texto y el motivo si no se pudo, nunca un modelo a medias.
    """
    ya_esta = f"Reservoir: {nombre}\n" in texto
    patron = re.compile(
        r"(^(?:Junction|Reach|Subbasin): " + re.escape(aguas_arriba)
        + r"\s*$.*?)^(     Downstream: )(.+?)\s*$", re.M | re.S)
    encaje = patron.search(texto)
    if encaje is None:
        return texto, f"no existe el elemento {aguas_arriba!r} o no descarga"
    destino = encaje.group(3).strip()
    if destino not in (aguas_abajo, nombre):
        return texto, (f"{aguas_arriba} descarga en {destino!r} y no en "
                       f"{aguas_abajo!r}: la topologia no es la declarada")
    if destino != nombre:
        texto = patron.sub(lambda m: m.group(1) + m.group(2) + nombre,
                           texto, count=1)
    if ya_esta:
        # Se sustituye el bloque entero, para que un cambio de la curva o de la
        # cresta llegue al modelo en lugar de quedarse en la configuracion.
        viejo = re.compile(r"^Reservoir: " + re.escape(nombre)
                           + r"\s*$.*?^End:\s*$\n?\n?", re.M | re.S)
        return viejo.sub(lambda _: bloque, texto, count=1), ""
    marca = ""
    for clase in ("Reach", "Junction", "Reservoir", "Sink"):
        if f"{clase}: {aguas_abajo}\n" in texto:
            marca = f"{clase}: {aguas_abajo}\n"
            break
    if not marca:
        return texto, f"no existe el elemento de aguas abajo {aguas_abajo!r}"
    return texto.replace(marca, bloque + marca, 1), ""


def escribir_embalses(declarados, ruta_basin, proyecto, archivo_dss,
                      texto, logger) -> tuple[str, list[str], list[str]]:
    """
    Deja en el modelo los embalses que la configuracion declara.

    CUATRO SITIOS Y NO UNO. HEC-HMS reparte un embalse entre el .basin, que
    lleva el elemento; DOS tablas emparejadas declaradas en el .pdata, la de
    elevacion contra almacenamiento y la de elevacion contra descarga; y el DSS,
    que guarda los numeros de ambas. Escribir solo el elemento produce un modelo
    que abre y falla al computar, sin decir donde.

    Devuelve el texto del .basin, los embalses escritos y los que no se pudieron
    con su motivo.
    """
    import dss as adaptador_dss

    escritos: list[str] = []
    fallidos: list[str] = []
    curvas: list[dict] = []
    for declarado in declarados:
        nombre = str(declarado.get("nombre", "")).strip()
        if not nombre:
            fallidos.append("un embalse sin nombre")
            continue
        tabla_volumen = f"{nombre} Elevacion-Almacenamiento"
        tabla_descarga = f"{nombre} Elevacion-Descarga"
        try:
            cotas, volumenes, descargas = curva_de_embalse(
                declarado["curva_elevacion_volumen_hm3"],
                float(declarado["cota_cresta_msnm"]),
                float(declarado["longitud_cresta_m"]),
                float(declarado.get("coeficiente_vertedero", 2.0)),
                float(declarado.get("descarga_fondo_m3s", 0.0)),
                float(declarado.get("lamina_maxima_m", 3.0)))
        except (ErrorHidrologia, KeyError, TypeError, ValueError) as error:
            fallidos.append(f"{nombre} ({error})")
            continue

        try:
            # La curva de elevacion se conserva como PRODUCTO, porque el
            # informe la tabula y porque es la que traduce el estado de
            # operacion a volumen; el modelo consume la de almacenamiento.
            ruta_volumen = adaptador_dss.escribir_tabla_emparejada(
                archivo_dss, tabla_volumen, cotas, volumenes,
                parte_c="ELEVATION-STORAGE", unidades_x="M",
                unidades_y="THOU M3", tipo_x="ELEV", tipo_y="STORAGE")
            ruta_descarga = adaptador_dss.escribir_tabla_emparejada(
                archivo_dss, tabla_descarga, volumenes, descargas,
                parte_c="STORAGE-FLOW", unidades_x="THOU M3",
                unidades_y="M3/S", tipo_x="STORAGE", tipo_y="FLOW")
        except Exception as error:                       # noqa: BLE001
            fallidos.append(f"{nombre} (no se pudo escribir el DSS: {error})")
            continue

        declarar_tabla_emparejada(
            proyecto, tabla_volumen, "Elevation-Storage", ruta_volumen,
            archivo_dss.name, "M", "THOU M3",
            f"curva del operador, {len(cotas)} puntos hasta la cresta en "
            f"{float(declarado['cota_cresta_msnm']):.1f} m")
        declarar_tabla_emparejada(
            proyecto, tabla_descarga, "Storage-Outflow", ruta_descarga,
            archivo_dss.name, "THOU M3", "M3/S",
            f"descarga de fondo {float(declarado.get('descarga_fondo_m3s', 0.0)):.2f} "
            f"m3/s mas vertedero libre de {float(declarado['longitud_cresta_m']):.0f} m")

        canvas = None
        if declarado.get("canvas_x") is not None:
            canvas = (float(declarado["canvas_x"]),
                      float(declarado["canvas_y"]))
        # El estado de operacion declarado como cota se traduce a volumen con
        # la propia curva del operador, que es la unica equivalencia valida.
        volumen_inicial = _volumen_interpolado(
            sorted((float(z), float(v))
                   for z, v in declarado["curva_elevacion_volumen_hm3"]),
            float(declarado["cota_inicial_msnm"]), 0.0)
        bloque = bloque_de_embalse(
            nombre, tabla_volumen, tabla_descarga, volumen_inicial,
            str(declarado["aguas_abajo"]), canvas)
        texto, motivo = enlazar_embalse(
            texto, nombre, str(declarado["aguas_arriba"]),
            str(declarado["aguas_abajo"]), bloque)
        if motivo:
            fallidos.append(f"{nombre} ({motivo})")
            continue
        escritos.append(nombre)
        # LA CURVA ENTRA AL INFORME. El modelo la consume en volumen, pero lo
        # que el consultor tiene que poder mostrar es la cota, porque es donde
        # se lee el estado de operacion y la lamina sobre el vertedero.
        for cota, volumen, salida in zip(cotas, volumenes, descargas):
            curvas.append({
                "embalse": nombre,
                "cota_msnm": round(cota, 2),
                "volumen_miles_m3": round(volumen, 1),
                "volumen_hm3": round(volumen / 1000.0, 3),
                "lamina_sobre_cresta_m": round(
                    max(0.0, cota - float(declarado["cota_cresta_msnm"])), 2),
                "descarga_m3s": round(salida, 3),
            })
        logger.info(
            "Embalse %s: %s -> %s -> %s, arranca en %.1f msnm, cresta %.1f de "
            "%.0f m, fondo %.2f m3/s",
            nombre, declarado["aguas_arriba"], nombre, declarado["aguas_abajo"],
            float(declarado["cota_inicial_msnm"]),
            float(declarado["cota_cresta_msnm"]),
            float(declarado["longitud_cresta_m"]),
            float(declarado.get("descarga_fondo_m3s", 0.0)))
    return texto, escritos, fallidos, curvas


def declarar_tabla_emparejada(proyecto: Path, tabla: str, tipo: str,
                              pathname: str, archivo_dss: str,
                              unidades_x: str, unidades_y: str,
                              descripcion: str) -> None:
    """
    Anota una tabla emparejada en el .pdata del proyecto, o actualiza la que ya
    estaba.

    El .pdata no guarda los numeros: guarda el pathname donde el DSS los tiene.
    Sin esta entrada HEC-HMS no encuentra la curva aunque este escrita.
    """
    ruta = next(proyecto.glob("*.pdata"), None)
    if ruta is None:
        return
    texto = ruta.read_text(encoding="latin-1")
    ahora = _dt.datetime.now()
    bloque = (
        f"Table: {tabla}\n"
        f"     Table Type: {tipo}\n"
        f"     Description: {descripcion}\n"
        f"     Last Modified Date: {ahora.strftime('%d %B %Y')}\n"
        f"     Last Modified Time: {ahora.strftime('%H:%M:%S')}\n"
        f"     X-Units: {unidades_x}\n"
        f"     Y-Units: {unidades_y}\n"
        "     Use External DSS File: NO\n"
        f"     DSS File: {archivo_dss}\n"
        f"     Pathname: {pathname}\n"
        "     Interpolation: Linear Interpolation\n"
        "End:\n\n"
    )
    viejo = re.compile(r"^Table: " + re.escape(tabla) + r"\s*$.*?^End:\s*$\n?\n?",
                       re.M | re.S)
    if viejo.search(texto):
        texto = viejo.sub(lambda _: bloque, texto, count=1)
    else:
        texto = texto.rstrip("\n") + "\n\n" + bloque
    ruta.write_text(texto, encoding="latin-1")


def abstraccion_inicial(cn: float, lam: float) -> dict[str, float]:
    """
    Retencion, abstraccion inicial y CN equivalente para el lambda adoptado.

    QUE ES LAMBDA. La relacion Ia = 0,2*S no es fisica: salio de un ajuste del
    SCS en los anios cincuenta sobre pocas cuencas y con dispersion grande, y el
    propio NEH-630 capitulo 10 reconoce esa dispersion. Woodward y otros (2003)
    la reexaminaron sobre unas 300 cuencas y encontraron la mediana cerca de
    0,05; Hawkins y otros (2009) lo recogen como practica recomendada.

    LA CONVERSION DE S NO ES OPCIONAL. Las tablas de numero de curva estan
    calibradas CON lambda = 0,2. Bajar la abstraccion conservando la misma S
    seria quedarse con el beneficio sin el costo y produciria una cuenca mucho
    mas reactiva de lo que los datos respaldan. Por eso se convierte:

        S(0,05) = 1,33 * S(0,2)^1,15     con S en pulgadas

    Medido en esta cuenca con CN 75,3: la creciente frecuente se multiplica por
    5,1 y la de diseno sube solo un 12 %, y en Tr 500 baja. Corrige un umbral
    que desactivaba los eventos pequenos, no infla el caudal de diseno.

    EL CN DEL INFORME NO CAMBIA. El que sale de suelos y coberturas sigue
    siendo el de la tabla; lo que el modelo recibe es el equivalente, mas bajo,
    junto con la Ia explicita. El informe debe presentar los dos, o parecera que
    se bajo el numero de curva a conveniencia.

    Excepciones
    -----------
    ErrorHidrologia
        Si el CN esta fuera de rango, o si se pide un lambda distinto de 0,20 y
        0,05: la relacion de conversion esta publicada para esos dos y no para
        un valor cualquiera, e interpolarla seria inventar.
    """
    if not 0.0 < float(cn) <= 100.0:
        raise ErrorHidrologia(
            f"el numero de curva ({cn}) esta fuera de (0, 100].")
    s_02 = 25400.0 / float(cn) - 254.0
    if abs(float(lam) - 0.20) < 1e-9:
        s = s_02
    elif abs(float(lam) - 0.05) < 1e-9:
        s = 1.33 * (s_02 / 25.4) ** 1.15 * 25.4
    else:
        raise ErrorHidrologia(
            f"lambda = {lam} no tiene conversion publicada de S. Solo se "
            "admiten 0.20, el clasico, y 0.05, el de Hawkins y otros (2009).")
    return {
        "s_lambda_020_mm": round(s_02, 2),
        "s_adoptada_mm": round(s, 2),
        "ia_mm": round(float(lam) * s, 2),
        "cn_equivalente": round(25400.0 / (s + 254.0), 1),
    }


def actualizar_subcuenca(bloque: str, parametros: dict,
                         flujo_base: dict | None = None,
                         lam: float = 0.20,
                         factor_cn: float = 1.0) -> tuple[str, str]:
    """
    Reescribe un bloque de subcuenca al método SCS con sus parámetros.

    Devuelve el bloque y el motivo si no se pudo completar. Una subcuenca sin
    número de curva o sin rezago se deja SIN tocar y se reporta: rellenarla con
    un valor por defecto produciría un modelo que corre y miente.
    """
    faltan = [c for c in ("cn", "tlag_min") if parametros.get(c) is None]
    if faltan:
        return bloque, f"sin {', '.join(faltan)}"

    # Con lambda distinto de 0,2 el modelo NO recibe el CN de la tabla sino su
    # equivalente, junto con la Ia explicita. Escribir el CN de la tabla y
    # ademas la Ia dejaria la abstraccion baja sobre la S sin convertir, que es
    # justamente el atajo que hace indefendible el cambio.
    # EL FACTOR ES CALIBRACION Y SE APLICA ANTES DE CONVERTIR. El CN de suelos y
    # coberturas no cambia: es el que el informe tabula al lado de este.
    conversion = abstraccion_inicial(
        parametros["cn"] * float(factor_cn), lam)
    campos_perdida = [
        ("Percent Impervious Area", "0.0"),
        ("Curve Number", f"{conversion['cn_equivalente']:.1f}"),
    ]
    if abs(float(lam) - 0.20) > 1e-9:
        campos_perdida.append(("Initial Abstraction",
                               f"{conversion['ia_mm']:.2f}"))
    bloque = fijar_grupo(bloque, "LossRate", "SCS", tuple(campos_perdida))
    # 'Unitgraph Type: STANDARD' acompana siempre al hidrograma unitario del
    # SCS en los modelos de ejemplo de HEC-HMS 4.13.
    bloque = fijar_grupo(bloque, "Transform", "SCS", (
        ("Lag", f"{parametros['tlag_min']:.2f}"),
        ("Unitgraph Type", "STANDARD"),
    ))
    bloque = fijar_grupo(bloque, *grupo_de_flujo_base(flujo_base))
    return bloque, ""


def grupo_de_flujo_base(flujo_base: dict | None) -> tuple[str, str, tuple]:
    """
    Bloque de flujo base de una subcuenca, segun lo declarado.

    POR QUE IMPORTA. El hidrograma de una tormenta de diseno sin flujo base
    arranca en cero y vuelve a cero, y el rio no hace eso: lleva su caudal
    ordinario antes de que llueva. Comparar ese hidrograma contra una creciente
    medida obliga a descontarle al dato observado un caudal base estimado, y esa
    resta mete una hipotesis en el lado de la EVIDENCIA, que es justo donde no
    debe haberla. Con el flujo base dentro del modelo la comparacion es directa.

    RECESION Y NO CONSTANTE. La recesion arranca en el caudal antecedente, cede
    durante el evento y vuelve a mandar en la rama de agotamiento cuando el
    caudal directo baja del umbral. Los tres parametros se DECLARAN: ninguno
    sale de la cadena.

    El caudal inicial se da por unidad de area, de modo que cada subcuenca
    recibe el que le corresponde por su tamano sin repartir nada a mano.

    La sintaxis esta tomada de los proyectos de muestra que HEC-HMS 4.13
    distribuye en samples.zip, que es la unica fuente con autoridad: el formato
    del .basin no esta publicado como especificacion.
    """
    if not flujo_base or str(flujo_base.get("metodo", "ninguno")) == "ninguno":
        # Dejar 'Recession' sin sus parametros daria un metodo declarado y vacio.
        return "Baseflow", "None", ()
    return "Baseflow", "Recession", (
        ("Recession Factor", f"{float(flujo_base['factor_recesion']):.3f}"),
        ("Initial Flow/Area Ratio",
         f"{float(flujo_base['caudal_especifico_m3s_km2']):.6f}"),
        ("Threshold Flow to Peak Ratio",
         f"{float(flujo_base['umbral_pico']):.3f}"),
        ("Initial Variable", "Combined Inflow"),
    )


def topologia_del_basin(
    texto_basin: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, float]]:
    """
    Tipo, enlace aguas abajo y area propia de cada elemento del modelo.

    SE LEE UNA VEZ Y SIRVE PARA DOS COSAS: el area acumulada con que se
    dimensiona el ancho de los tramos, y la microcuenca que descarga en cada
    tramo, que el informe tabula junto a la K de Muskingum.
    """
    tipo: dict[str, str] = {}
    aguas_abajo: dict[str, str] = {}
    area: dict[str, float] = {}
    nombre = ""
    for linea in texto_basin.splitlines():
        encabezado = re.match(
            r"^(Subbasin|Reach|Junction|Sink|Reservoir|Source|Diversion): (.+)$",
            linea)
        if encabezado:
            nombre = encabezado.group(2).strip()
            tipo[nombre] = encabezado.group(1)
            continue
        if not nombre:
            continue
        enlace = re.match(r"^\s+Downstream: (.+)$", linea)
        if enlace:
            aguas_abajo[nombre] = enlace.group(1).strip()
            continue
        medida = re.match(r"^\s+Area: ([0-9.eE+-]+)\s*$", linea)
        if medida and tipo.get(nombre) == "Subbasin":
            try:
                area[nombre] = float(medida.group(1))
            except ValueError:
                pass
    return tipo, aguas_abajo, area


def areas_acumuladas(texto_basin: str) -> dict[str, float]:
    """
    Área de drenaje que llega a cada elemento, recorriendo la topología.

    Cada subcuenca vierte su área a TODO lo que tiene aguas abajo, siguiendo la
    cadena de enlaces 'Downstream:' hasta el cierre. Es la misma información que
    produjo la delimitación asistida, sin volver a tocar el DEM.

    LA SUMA EN EL CIERRE ES LA COMPROBACIÓN. Si el elemento final no acumula el
    área total de la cuenca, la topología tiene ramas sueltas y el ancho de los
    tramos saldría de un área que no es la suya. Medido sobre el modelo del
    estudio: R1 acumula 220,57 km2 y la cuenca tiene 220,57 km2.

    El recorrido lleva control de visitados: un enlace circular, que la
    delimitación no debería producir pero tampoco impide, colgaría el módulo.
    """
    tipo, aguas_abajo, area = topologia_del_basin(texto_basin)

    acumulada = {n: 0.0 for n in tipo}
    for subcuenca, propia in area.items():
        visitados: set[str] = set()
        actual = subcuenca
        while actual and actual in acumulada and actual not in visitados:
            visitados.add(actual)
            acumulada[actual] += propia
            actual = aguas_abajo.get(actual, "")
    return acumulada


def ancho_por_geometria_hidraulica(
    area_km2: float, coeficiente: float, exponente: float,
    minimo_m: float = 1.0,
) -> float:
    """
    Ancho de fondo de un tramo a partir de su área de drenaje acumulada.

    w = a * A^b, geometría hidráulica de aguas abajo. UN ANCHO ÚNICO PARA TODA
    LA RED NO ES DEFENDIBLE: el cauce de cierre y el de una quebrada de cabecera
    no tienen la misma sección, y Muskingum-Cunge atenúa por el almacenamiento
    que esa sección ofrece. Medido con 1 m de fondo en los 62 tramos del
    estudio: atenuación media del 0,25 % y 27 tramos sin desfase alguno, es
    decir, traslación pura.

    El mínimo evita que un área acumulada nula, que solo puede venir de una
    topología rota, produzca un ancho cero y una sección sin área.
    """
    if area_km2 <= 0 or coeficiente <= 0:
        return minimo_m
    return max(minimo_m, coeficiente * (area_km2 ** exponente))


def leer_geometria_hidraulica(ruta: Path, delimitador: str,
                              variable: str = "ancho_fondo") -> dict[str, Any]:
    """
    Lee la relación de geometría hidráulica de la tabla de doctrina.

    Es una REGIONALIZACIÓN sin datos de campo del proyecto y la tabla lo dice.
    Vive en data/referencia porque es doctrina técnica y no código (CLAUDE.md,
    sección 2), de modo que un estudio con secciones levantadas la sustituye.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si no trae la variable pedida o sus valores no son números.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra la tabla de geometria hidraulica en {ruta}.")
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            if str(fila.get("variable", "")).strip() != variable:
                continue
            try:
                return {
                    "coeficiente": float(fila["coeficiente"]),
                    "exponente": float(fila["exponente"]),
                    "fuente": str(fila.get("fuente", "")).strip(),
                    "validado": str(fila.get("validado", "")).strip().lower()
                    in ("si", "sí", "true"),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ErrorFormato(
                    f"{ruta.name}: la fila de {variable!r} no es legible "
                    f"({exc}).") from exc
    raise ErrorFormato(f"{ruta.name} no trae la variable {variable!r}.")


def parametros_por_clase(geometrias: dict[str, dict], tipo: dict[str, str],
                         clases: Sequence[dict]) -> dict[str, dict]:
    """
    K y X de cada tramo a partir de su clase de pendiente, sin datos de caudal.

    POR QUE SE CALCULAN AQUI Y NO SE LEEN DEL M14. Dependen SOLO de la geometria
    del tramo: la longitud y la pendiente, que este modulo ya tiene. Leerlas de
    la tabla que escribe el M14 metia una dependencia de orden que la cadena no
    cumple, porque el M13 corre ANTES: en la primera pasada el modelo salia con
    los parametros de la corrida anterior y la tabla con los nuevos, y las dos
    cosas no coincidian. Medido sobre el estudio entregado, eso dejo el modelo
    con X de 0,497, el valor de Cunge, en lugar del 0,250 de las clases, y un
    caudal de diseno un 26,5 % mas alto en los 251 elementos.
    """
    import hms

    salida: dict[str, dict] = {}
    for tramo, geometria in geometrias.items():
        if tipo.get(tramo) != "Reach":
            continue
        longitud_m = float(geometria.get("longitud_m") or 0.0)
        pendiente_pct = float(geometria.get("pendiente") or 0.0) * 100.0
        clase = hms.clase_por_pendiente(pendiente_pct, clases)
        if clase is None or longitud_m <= 0:
            continue
        celeridad = float(clase["celeridad_ms"])
        if celeridad <= 0:
            continue
        salida[tramo] = {
            "clase": str(clase.get("nombre", "")),
            "celeridad_ms": celeridad,
            "k_min": longitud_m / celeridad / 60.0,
            "x": float(clase["x"]),
        }
    return salida


def parametros_de_transito(geometrias: dict[str, dict],
                           aguas_abajo: dict[str, str],
                           tipo: dict[str, str], anchos: dict[str, float],
                           celeridad: float) -> list[dict[str, Any]]:
    """
    K de Muskingum por tramo, con lo que la sostiene.

    SE PERSISTE PORQUE EL INFORME LA TABULA. HEC-HMS calcula el tránsito por
    Muskingum-Cunge y resuelve K dentro; el consultor tiene que presentar el
    parámetro y de qué sale, y hasta ahora solo existía dentro del archivo del
    modelo.

    K ES EL TIEMPO DE VIAJE, longitud sobre celeridad. La celeridad es la que se
    declara como índice en la configuración y NO una medida del cauce: se
    reporta como asumida, que es lo que el informe debe decir.

    LA MICROCUENCA ES LA QUE DESCARGA EN EL TRAMO, leída de la topología del
    propio modelo. NO son los elementos cuyo 'Downstream' es el tramo: en este
    modelo ninguna subcuenca vierte directamente a un tramo, todas pasan por
    una unión, y con esa lectura los 125 tramos salían sin microcuenca. Se
    sigue la cadena desde cada subcuenca y se le adjudica el PRIMER tramo que
    encuentra: el que transita su caudal sin haberlo mezclado con el de otro
    tramo. Si son varias se nombran todas; adjudicar una sola sería inventar el
    reparto.
    """
    entrantes: dict[str, list[str]] = {}
    for elemento in aguas_abajo:
        if tipo.get(elemento) != "Subbasin":
            continue
        actual = aguas_abajo.get(elemento, "")
        visitados: set[str] = set()
        while actual and actual not in visitados:
            visitados.add(actual)
            if tipo.get(actual) == "Reach":
                entrantes.setdefault(actual, []).append(elemento)
                break
            actual = aguas_abajo.get(actual, "")

    filas: list[dict[str, Any]] = []
    for tramo in sorted(geometrias):
        # SOLO LOS TRAMOS DEL MODELO. La geometria se lee del sqlite del
        # proyecto, que trae 125 entradas incluyendo los nombres de las
        # subcuencas; los tramos que HEC-HMS transita son 62. Sin este filtro la
        # tabla del informe listaba 63 filas que no son tramos.
        if tipo.get(tramo) != "Reach":
            continue
        geometria = geometrias[tramo]
        longitud_m = float(geometria.get("longitud_m") or 0.0)
        pendiente = float(geometria.get("pendiente") or 0.0)
        k_s = longitud_m / celeridad if celeridad > 0 else None
        filas.append({
            "tramo": tramo,
            "microcuenca": "+".join(sorted(entrantes.get(tramo, []))),
            "longitud_m": round(longitud_m, 2),
            "longitud_km": round(longitud_m / 1000.0, 3),
            # LA PENDIENTE EN POR CIENTO, que es como la titula el informe. La
            # geometria la trae en m/m, que es lo que Muskingum-Cunge pide.
            "pendiente_pct": round(pendiente * 100.0, 3),
            "ancho_fondo_m": (round(anchos[tramo], 2)
                              if tramo in anchos else None),
            "celeridad_ms": round(celeridad, 2),
            "k_s": round(k_s, 1) if k_s is not None else None,
            "k_min": round(k_s / 60.0, 2) if k_s is not None else None,
            "k_h": round(k_s / 3600.0, 3) if k_s is not None else None,
        })
    return filas


def fusionar_transito(ruta: Path, filas: list[dict],
                      delimitador: str) -> list[dict]:
    """
    Conserva de la tabla anterior las columnas que este modulo no escribe.

    La tabla de transito la llenan DOS modulos: el M13 pone la geometria y el
    M14 le anade despues la parametrizacion de Muskingum. El M13 la lee en la
    corrida siguiente para escribir el modelo, de modo que si al reescribirla
    borra lo que el M14 puso, se queda sin lo que necesita y cae en los valores
    por omision. Ese fallo no da error: produce un modelo que corre y una tabla
    de informe que no coincide con el.
    """
    if not Path(ruta).is_file():
        return filas
    try:
        with Path(ruta).open(encoding="utf-8-sig", newline="") as manejador:
            previas = {str(f.get("tramo", "")).strip(): f
                       for f in csv.DictReader(manejador, delimiter=delimitador)}
    except OSError:
        return filas
    propias = set(filas[0]) if filas else set()
    # LA CELERIDAD Y LA K LAS CALCULAN LOS DOS MODULOS, con criterios distintos:
    # aqui con la celeridad unica de la configuracion, y en el M14 con la que
    # corresponde a la clase de pendiente del tramo. Cuando el M14 ya ha pasado,
    # lo suyo manda: recalcularlas aqui las devolvia a 1 m/s y dejaba la tabla
    # del informe diciendo una cosa y el modelo otra, sin ninguna senal.
    del_m14 = {"celeridad_ms", "k_s", "k_min", "k_h", "celeridad_origen"}
    for fila in filas:
        anterior = previas.get(str(fila.get("tramo", "")).strip())
        if not anterior:
            continue
        manda_el_m14 = bool(str(anterior.get("clase_pendiente", "")).strip())
        for clave, valor in anterior.items():
            if valor in (None, ""):
                continue
            if clave not in propias or (manda_el_m14 and clave in del_m14):
                fila[clave] = valor
    return filas


def leer_x_muskingum(ruta: Path, delimitador: str) -> dict[str, float]:
    """
    X de cada tramo, del transito.csv que deja el M14.

    NO SE RECALCULA AQUI, por lo mismo que la K: si el valor viviera en dos
    sitios, la tabla del informe y el modelo podrian dejar de coincidir sin que
    nada avisara.
    """
    salida: dict[str, float] = {}
    if not Path(ruta).is_file():
        return salida
    with Path(ruta).open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            try:
                salida[str(fila["tramo"]).strip()] = float(
                    str(fila["x"]).replace(",", "."))
            except (KeyError, TypeError, ValueError):
                continue
    return salida


def leer_k_muskingum(ruta: Path, delimitador: str) -> dict[str, float]:
    """
    K de Muskingum por tramo, del transito.csv que deja el M14.

    Se lee de un archivo y no se recalcula aqui: la K sale de la hidraulica del
    tramo (longitud sobre celeridad) y el M14 ya la resuelve con la geometria y
    el caudal de referencia declarados. Recalcularla en dos sitios es la manera
    de que un dia dejen de coincidir.
    """
    if not Path(ruta).is_file():
        return {}
    salida: dict[str, float] = {}
    with Path(ruta).open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            try:
                salida[fila["tramo"].strip()] = float(fila["k_min"])
            except (KeyError, TypeError, ValueError):
                continue
    return salida


def subtramos_estables(k_min: float, x: float, dt_min: float) -> int:
    """
    Subtramos que hace falta partir para que Muskingum sea estable.

    LA CONDICION ES 2*K*X <= dt <= 2*K*(1-X), y con K grande frente al intervalo
    de calculo el limite inferior se incumple: la solucion oscila y puede dar
    caudales NEGATIVOS, que HEC-HMS calcula sin protestar. Partir el tramo en n
    subtramos divide la K de cada uno y devuelve la estabilidad.

    Se devuelve al menos 1, y se redondea hacia arriba porque quedarse corto es
    justo lo que hay que evitar.
    """
    if dt_min <= 0 or k_min <= 0 or not 0 <= x < 0.5:
        return 1
    return max(1, int(-(-2.0 * k_min * x // dt_min)))


def k_minima_representable(x: float, dt_min: float) -> float:
    """
    K por debajo de la cual el intervalo de calculo no puede transitar nada.

    Es el OTRO extremo de 2*K*X <= dt <= 2*K*(1-X): despejando, K >= dt/(2*(1-X)).
    Partir en subtramos no lo arregla, porque cada subtramo recibe K/n y el
    incumplimiento empeora. HEC-HMS lo rechaza con 'Invalid Muskingum K'.

    Un tramo con K por debajo de este limite es tan corto que la onda lo cruza
    dentro de un solo paso de calculo: no hay transito que resolver a esa escala.
    """
    return dt_min / (2.0 * (1.0 - x)) if 0.0 <= x < 1.0 and dt_min > 0 else 0.0


def actualizar_tramo_muskingum(bloque: str, k_min: float, x: float,
                               dt_min: float) -> tuple[str, str, str]:
    """
    Reescribe un bloque de tramo al Muskingum CLASICO, con K y X declarados.

    POR QUE EXISTE ESTA ALTERNATIVA. Muskingum-Cunge deriva la atenuacion de la
    geometria que se le entrega, y la que este estudio puede entregar es un
    TRAPECIO PRISMATICO: sin pozos, sin meandros y sin zonas de desborde. Medido
    sobre este modelo, la atenuacion que resulta es del 0,0 % en los 62 tramos,
    y ninguna combinacion de rugosidad y ancho la mueve.

    Esa respuesta no dice que el rio no atenue: dice que un canal prismatico con
    esta pendiente no atenua. El almacenamiento que de verdad aplana la creciente
    esta en la irregularidad del cauce y en el desborde, que la seccion idealizada
    no representa y que sin secciones levantadas no se puede representar.

    Con Muskingum clasico la atenuacion se DECLARA en X en lugar de derivarse. Es
    una decision del consultor y hay que decirlo: X no sale de la hidraulica del
    tramo sino de un valor adoptado, y el informe debe declararlo junto con el
    contraste contra el caudal observado que lo respalde.
    """
    if k_min is None or k_min <= 0:
        return bloque, "sin K de Muskingum", ""
    if not 0.0 <= x < 0.5:
        return bloque, f"X fuera del rango fisico admisible: {x}", ""

    # Un tramo mas rapido que el paso de calculo se lleva a la K minima que ese
    # paso puede representar. NO se hace en silencio: se devuelve el aviso para
    # que quede en el log y en el reporte. El error que introduce esta acotado
    # por el propio intervalo, y la alternativa (bajar el dt de todo el modelo)
    # cambiaria el tiempo de rezago de TODAS las subcuencas, porque el criterio
    # adoptado es dt/2 + 0,6*Tc.
    aviso = ""
    admisible = k_minima_representable(x, dt_min)
    if k_min < admisible:
        aviso = (f"K = {k_min:.2f} min por debajo de lo representable con un "
                 f"paso de {dt_min:.0f} min; se eleva a {admisible:.2f} min")
        k_min = admisible

    pasos = subtramos_estables(k_min, x, dt_min)
    bloque = fijar_grupo(bloque, "Route", "Muskingum", (
        # HEC-HMS pide la K en HORAS.
        ("Muskingum K", f"{k_min / 60.0:.4f}"),
        ("Muskingum X", f"{x:.3f}"),
        ("Muskingum Steps", str(pasos)),
    ))
    return bloque, "", aviso


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


SUFIJO_SIN_FACTOR = "_SF"


def duplicar_sin_factor(hietogramas, factor: float) -> list[dict]:
    """
    El mismo hietograma sin el factor de cambio climatico, para el segundo
    escenario.

    POR QUE SE DIVIDE Y NO SE VUELVE A CALCULAR. El factor es un multiplicador
    UNICO sobre la lamina de diseno: no toca la distribucion de Huff ni el
    factor de reduccion por area, de modo que dividir por el devuelve
    exactamente el hietograma que el M12b produciria con la clave en falso.
    Comprobado sobre este estudio: la lluvia areal de Tr 100 pasa de 53,68 a
    48,54 mm, que es 53,68 / 1,1058.

    POR QUE SE COMPUTA Y NO SE ESCALA EL RESULTADO. Las perdidas del SCS no son
    lineales: un 9,6 % menos de lluvia da un 18,7 % menos de escorrentia. El
    segundo escenario hay que correrlo, no deducirlo del primero.

    Los pluviometros del escenario sin factor llevan sufijo para que HEC-HMS los
    distinga: dos series con el mismo nombre en el DSS se pisan.
    """
    if factor is None or float(factor) <= 0:
        return []
    copia = []
    for paso in hietogramas:
        nuevo = dict(paso)
        nuevo["pluviometro"] = f"{paso['pluviometro']}{SUFIJO_SIN_FACTOR}"
        nuevo["lamina_mm"] = float(paso["lamina_mm"]) / float(factor)
        copia.append(nuevo)
    return copia


def escenario_de_referencia(hietogramas, comparar: bool,
                            ) -> tuple[list[dict], Hallazgo | None]:
    """
    Decide si se escribe el segundo escenario y lo dice siempre.

    POR QUE DEVUELVE UN HALLAZGO Y NO SOLO LA COPIA. Cuando la configuracion
    pide los dos escenarios y aqui sale uno, el estudio queda afirmando algo
    que sus productos no contienen y el informe compararia el escenario de
    diseno consigo mismo. Ocurrio de verdad: los hietogramas del estudio
    entregado eran de una version anterior del M12b, sin la columna
    'factor_cc', y el M13 escribio ocho corridas en lugar de dieciseis sin una
    sola linea de aviso. Es exactamente el resultado incorrecto en silencio que
    la seccion 2 de CLAUDE.md prohibe.

    Devuelve el juego de hietogramas de referencia (vacio si no se escribe) y
    el hallazgo que explica cual de los cuatro casos se dio.
    """
    if not comparar:
        return [], None
    factores = {str(h.get("factor_cc") or "") for h in hietogramas}
    factores.discard("")
    if not factores:
        return [], Hallazgo(
            BLOQUEANTE, "escenarios.sin_factor_en_hietogramas",
            "se piden los dos escenarios de cambio climatico pero los "
            "hietogramas no traen la columna 'factor_cc': son de una version "
            "anterior del M12b. Volver a ejecutarlo; sin ella no hay con que "
            "deshacer el factor y solo se escribiria el escenario de diseno.")
    if len(factores) > 1:
        return [], Hallazgo(
            BLOQUEANTE, "escenarios.factor_no_unico",
            f"se piden dos escenarios pero los hietogramas traen "
            f"{len(factores)} factores de cambio climatico distintos: "
            f"{sorted(factores)}. NO se puede deshacer uno solo, de modo que "
            "solo se escribiria el escenario de diseno.")
    factor_cc = float(next(iter(factores)))
    if factor_cc <= 1.0:
        # No es un fallo: la regla condicional manda no aplicar el factor
        # cuando la proyeccion es a la baja. Pero entonces los dos escenarios
        # serian el mismo, y correr el segundo no anadiria nada.
        return [], Hallazgo(
            INFORMATIVO, "escenarios.factor_no_aplicado",
            f"el factor de cambio climatico es {factor_cc} y no incrementa la "
            "lluvia, de modo que los dos escenarios serian identicos. Se "
            "escribe uno solo y el informe no lleva la comparacion.")
    return duplicar_sin_factor(hietogramas, factor_cc), Hallazgo(
        INFORMATIVO, "escenarios.dos_escenarios",
        f"se escriben los dos escenarios de cambio climatico con factor "
        f"{factor_cc}: el de diseno lo lleva y el de referencia representa la "
        f"lluvia registrada, con sufijo '{SUFIJO_SIN_FACTOR}'. HEC-HMS los "
        "resuelve en la misma sesion y el M14 entrega la comparacion.")


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

    LA ESTRUCTURA ESTÁ LEÍDA DE UN .met QUE EL PROPIO HEC-HMS REESCRIBIÓ, que es
    la única autoridad sobre un formato sin especificación publicada. De ahí
    salen las dos cosas que la primera versión no acertaba: el método se llama
    'Specified Average' (no 'Specified Hyetograph', que es la etiqueta de la
    interfaz), y el pluviómetro se engancha con un 'Gage:' DENTRO del bloque de
    cada subcuenca.

    NO SE LISTAN LOS PLUVIÓMETROS APARTE. Una versión previa abría el archivo con
    un bloque por pluviómetro usado; HEC-HMS los borró al guardar. Se declaran en
    el .gage y el .met solo los referencia por nombre.

    Hay que enumerar todos los métodos meteorológicos aunque sean 'None' y
    declarar a qué modelo de cuenca se aplica ('Use Basin Model'): sin eso el
    programa no muestra el modelo meteorológico.

    Cada subcuenca queda enganchada al pluviómetro de SU ZONA. Es lo que hace
    que cinco series basten para ciento veinticinco subcuencas.
    """
    lineas = [
        f"Meteorology: {nombre}",
        f"     Description: tormenta de diseno, periodo de retorno {periodo} anios",
        "     Version: 4.13",
        "     Unit System: Metric",
        "     Set Missing Data to Default: Yes",
        "     Precipitation Method: Specified Average",
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
    lineas += [
        "Precip Method Parameters: Specified Average",
        "     Allow Depth Override: No",
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


def escribir_runs(destino: Path, escenarios, modelo_cuenca: str,
                  control: str) -> int:
    """
    Escribe las simulaciones, una por periodo de retorno.

    UN 'RUN' ES SOLO LA COMBINACION de modelo de cuenca, meteorologia y control:
    no lleva parametros propios. El formato esta leido de una simulacion que el
    consultor creo en la interfaz, y la clave del modelo meteorologico es
    'Precip', no 'Meteorology'.

    Van todas en un unico archivo con el nombre del proyecto, como los
    pluviometros.
    """
    lineas: list[str] = []
    for nombre in escenarios:
        corrida = f"TR_{nombre.lstrip('T')}"
        lineas += [
            f"Run: {corrida}",
            "     Default Description: Yes",
            f"     Log File: {corrida}.log",
            f"     DSS File: {corrida}.dss",
            "     Is Save Spatial Results: No",
            f"     Basin: {modelo_cuenca}",
            f"     Precip: {nombre}",
            f"     Control: {control}",
            "     Time-Series Output: Save All",
            "     Time Series Results Manager Start:",
            "     Time Series Results Manager End:",
            "End:",
            "",
        ]
    destino.write_text("\n".join(lineas), encoding="utf-8")
    return len(escenarios)


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
                           resultado, logger, base)

    with registro.bloque(logger, "Meteorologia y escenarios"):
        _escribir_meteorologia(configuracion, proyecto, hietogramas,
                               asignacion, resultado, logger)

    _registrar_productos(base, resultado)
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _verificar_topologia(acumuladas, texto, resultado, logger) -> None:
    """
    Comprueba que el cierre acumule el area total de la cuenca.

    ES LA VERIFICACION DE QUE LA RED CIERRA. Si el elemento final no reune todas
    las subcuencas, hay ramas sueltas y el ancho de los tramos saldria de un
    area que no es la suya, sin que nada lo senale.
    """
    total = sum(
        float(m.group(1))
        for m in re.finditer(r"^\s+Area: ([0-9.eE+-]+)\s*$", texto, re.M))
    mayor = max(acumuladas.values()) if acumuladas else 0.0
    logger.info("Area acumulada en el cierre: %.2f km2 de %.2f km2 de cuenca",
                mayor, total)
    if total <= 0 or abs(mayor - total) / total <= 0.001:
        return
    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "transito.topologia_incompleta",
        f"el elemento que mas area acumula reune {mayor:.2f} km2 y las "
        f"subcuencas suman {total:.2f} km2. La red tiene ramas que no llegan al "
        "cierre, de modo que el ancho de los tramos aguas abajo sale de un area "
        "menor que la suya y su seccion queda subestimada.",
    ))


def _leer_csv(ruta: Path) -> list[dict[str, str]]:
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra {ruta}.")
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=";"))
    if not filas:
        raise ErrorFormato(f"{ruta.name} esta vacio.")
    return filas


def _actualizar_modelo(configuracion, ruta_basin, parametros, geometrias,
                       resultado, logger, base=None) -> None:
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

    # QUE METODO DE TRANSITO SE ESCRIBE. La configuracion lo declaraba y el
    # modulo lo ignoraba: escribia Muskingum-Cunge siempre. Un config que dice
    # una cosa y un codigo que hace otra es lo que la seccion 2 prohibe.
    lam = float(configuracion.obtener(
        "numero_curva.abstraccion_inicial_lambda", 0.20))
    factor_cn = float(configuracion.obtener(
        "numero_curva.factor_calibracion", 1.0))

    metodo_flujo_base = str(configuracion.obtener(
        "hec_hms.flujo_base.metodo", "ninguno")).strip().lower()
    flujo_base = None
    if metodo_flujo_base != "ninguno":
        flujo_base = {
            "metodo": metodo_flujo_base,
            "factor_recesion": configuracion.obtener(
                "hec_hms.flujo_base.factor_recesion"),
            "caudal_especifico_m3s_km2": configuracion.obtener(
                "hec_hms.flujo_base.caudal_especifico_m3s_km2"),
            "umbral_pico": configuracion.obtener(
                "hec_hms.flujo_base.umbral_pico"),
        }

    metodo_transito = str(configuracion.obtener(
        "hec_hms.transito.metodo_adoptado", "muskingum_cunge")).strip().lower()
    x_muskingum = float(configuracion.obtener(
        "hec_hms.transito.muskingum.x", 0.2))
    dt_min = float(configuracion.obtener("hec_hms.control.intervalo_min", 5.0))
    k_muskingum: dict[str, float] = {}
    x_por_tramo: dict[str, float] = {}
    if metodo_transito == "muskingum":
        ruta_transito = (rutas.directorio("procesado", base) / "hidrologia"
                         / "transito.csv")
        delimitador_transito = str(
            configuracion.obtener("insumos_usuario.delimitador_csv"))
        k_muskingum = leer_k_muskingum(ruta_transito, delimitador_transito)
        x_por_tramo = leer_x_muskingum(ruta_transito, delimitador_transito)

        # LAS CLASES MANDAN SOBRE LA TABLA. Salen de la geometria, que este
        # modulo ya tiene, de modo que no dependen de que el M14 haya corrido
        # antes. Sin esto el modelo quedaba con los parametros de la corrida
        # anterior mientras la tabla llevaba los nuevos.
        clases_transito = configuracion.obtener(
            "hec_hms.transito.muskingum.clases_pendiente", []) or []
        if clases_transito:
            tipos_de = topologia_del_basin(texto)[0]
            por_clase = parametros_por_clase(
                geometrias, tipos_de, clases_transito)
            k_muskingum.update({n: d["k_min"] for n, d in por_clase.items()})
            x_por_tramo.update({n: d["x"] for n, d in por_clase.items()})
        if not k_muskingum:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "transito.sin_k",
                "se declara Muskingum clasico pero no hay transito.csv con la K "
                "de cada tramo. La produce el M14: ejecutarlo antes, o volver a "
                "muskingum_cunge."))

    criterio_ancho = str(configuracion.obtener(
        "hec_hms.transito.muskingum_cunge.criterio_ancho", "fijo")).strip()
    relacion, acumuladas = None, {}
    if criterio_ancho == "geometria_hidraulica":
        relacion = leer_geometria_hidraulica(
            rutas.resolver(configuracion.obtener(
                "hec_hms.transito.muskingum_cunge.tabla_geometria"), base),
            str(configuracion.obtener("insumos_usuario.delimitador_csv")))
        acumuladas = areas_acumuladas(texto)
        _verificar_topologia(acumuladas, texto, resultado, logger)

    anchos: dict[str, float] = {}
    nuevos: list[str] = []
    sin_parametros: list[str] = []
    sin_geometria: list[str] = []
    tramos_elevados: list[str] = []
    tabla_abstraccion: list[dict] = []
    actualizadas = tramos_ok = 0
    for tipo, nombre, bloque in separar_bloques(texto):
        if tipo == "Subbasin":
            bloque, motivo = actualizar_subcuenca(
                bloque, parametros.get(nombre, {}), flujo_base, lam, factor_cn)
            datos = parametros.get(nombre, {})
            if not motivo and datos.get("cn") is not None:
                tabla_abstraccion.append(
                    {"subcuenca": nombre, "cn_suelos_cobertura": datos["cn"],
                     "factor_calibracion": factor_cn,
                     "cn_calibrado": round(datos["cn"] * factor_cn, 1),
                     "lambda": lam,
                     **abstraccion_inicial(datos["cn"] * factor_cn, lam)})
            if motivo:
                sin_parametros.append(f"{nombre} ({motivo})")
            else:
                actualizadas += 1
        elif tipo == "Reach":
            del_tramo = ancho
            if relacion is not None:
                del_tramo = ancho_por_geometria_hidraulica(
                    acumuladas.get(nombre, 0.0), relacion["coeficiente"],
                    relacion["exponente"], minimo_m=ancho)
            if metodo_transito == "muskingum":
                # La K sale de la hidraulica del tramo, igual que en
                # Muskingum-Cunge; lo que se declara es la X.
                # La X del tramo manda sobre la declarada: la fija el M14
                # por clase de pendiente, y un cauce plano con llanura no
                # almacena como uno encanonado.
                bloque, motivo, aviso = actualizar_tramo_muskingum(
                    bloque, k_muskingum.get(nombre),
                    x_por_tramo.get(nombre, x_muskingum), dt_min)
                if aviso:
                    tramos_elevados.append(f"{nombre} ({aviso})")
            else:
                bloque, motivo = actualizar_tramo(
                    bloque, geometrias.get(nombre, {}), n_manning, del_tramo,
                    talud, celeridad)
            if motivo:
                sin_geometria.append(f"{nombre} ({motivo})")
            else:
                tramos_ok += 1
                anchos[nombre] = del_tramo
        nuevos.append(bloque)

    texto_final = "".join(nuevos)

    # LOS EMBALSES VAN DESPUES de reescribir subcuencas y tramos, porque
    # insertan un elemento nuevo y reconectan un enlace: hacerlo antes obligaria
    # al bucle a tratar un bloque que la delimitacion no produjo.
    embalses = configuracion.obtener("hec_hms.embalses", []) or []
    embalses_escritos: list[str] = []
    embalses_fallidos: list[str] = []
    curvas_embalse: list[dict] = []
    if embalses:
        (texto_final, embalses_escritos, embalses_fallidos,
         curvas_embalse) = escribir_embalses(
            embalses, ruta_basin, ruta_basin.parent,
            Path(str(configuracion.obtener("hec_hms.proyecto.directorio")))
            / f"{Path(str(configuracion.obtener('hec_hms.proyecto.archivo'))).stem}.dss",
            texto_final, logger)

    ruta_basin.write_text(texto_final, encoding="utf-8")
    resultado.productos.append(str(ruta_basin))

    # LA CURVA DEL EMBALSE, PARA EL INFORME. El modelo la consume en volumen y
    # el consultor la necesita en cota, que es donde se lee el estado de
    # operacion adoptado y la lamina sobre el vertedero.
    if curvas_embalse:
        destino = (rutas.directorio("procesado", base) / "hidrologia"
                   / "embalses.csv")
        destino.parent.mkdir(parents=True, exist_ok=True)
        with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
            escritor = csv.DictWriter(
                manejador, fieldnames=list(curvas_embalse[0]),
                delimiter=str(configuracion.obtener(
                    "insumos_usuario.delimitador_csv")))
            escritor.writeheader()
            escritor.writerows(curvas_embalse)
        resultado.productos.append(rutas.relativa(destino, base))
        logger.info("Curva de %d embalse(s) en %s",
                    len(embalses_escritos), destino.name)

    # LAS DOS TABLAS, JUNTAS Y EN EL MISMO ARCHIVO. El informe tiene que poder
    # mostrar el CN que sale de suelos y coberturas al lado del que recibe el
    # modelo. Un revisor que vea solo el segundo concluira que se bajo el numero
    # de curva para obtener un resultado, y tendria razon en preguntarlo.
    if tabla_abstraccion:
        destino = (rutas.directorio("procesado", base) / "hidrologia"
                   / "abstraccion_inicial.csv")
        destino.parent.mkdir(parents=True, exist_ok=True)
        columnas = list(tabla_abstraccion[0])
        with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
            escritor = csv.DictWriter(
                manejador, fieldnames=columnas,
                delimiter=str(configuracion.obtener(
                    "insumos_usuario.delimitador_csv")))
            escritor.writeheader()
            escritor.writerows(tabla_abstraccion)
        resultado.productos.append(rutas.relativa(destino, base))
        logger.info("Abstraccion inicial (lambda = %.2f) en %s",
                    lam, destino.name)
    resultado.subcuencas = {"actualizadas": actualizadas,
                            "sin_parametros": sin_parametros,
                            "flujo_base": flujo_base or {"metodo": "ninguno"},
                            "abstraccion_inicial_lambda": lam,
                            "factor_calibracion_cn": factor_cn,
                            "embalses": embalses_escritos}
    resultado.tramos = {"actualizados": tramos_ok,
                        "sin_geometria": sin_geometria,
                        "k_elevada_al_minimo": tramos_elevados,
                        "n_manning": n_manning, "ancho_fondo_m": ancho,
                        "talud_h_por_v": talud,
                        "criterio_ancho": criterio_ancho,
                        "anchos": {n: round(a, 2) for n, a in sorted(anchos.items())}}
    logger.info("%d subcuenca(s) y %d tramo(s) actualizados",
                actualizadas, tramos_ok)

    # LA K DE MUSKINGUM SE PERSISTE PORQUE EL INFORME LA TABULA. HEC-HMS la
    # resuelve dentro de Muskingum-Cunge y hasta ahora solo existia en el
    # archivo del modelo, de donde no se puede citar.
    if base is not None and geometrias:
        tipo, aguas_abajo, _area = topologia_del_basin(texto)
        filas = parametros_de_transito(
            geometrias, aguas_abajo, tipo, anchos, celeridad)
        destino = (rutas.directorio("procesado", base, crear=True)
                   / "hidrologia")
        destino.mkdir(parents=True, exist_ok=True)
        destino = destino / "transito.csv"
        delimitador = configuracion.obtener(
            "insumos_usuario.delimitador_csv", ";")

        # SE CONSERVA LO QUE ESTE MODULO NO ESCRIBE. La tabla la comparten dos
        # modulos: aqui salen la geometria y la longitud, y el M14 le anade
        # despues la K, la X y la clase de pendiente, que son las que el propio
        # M13 lee en la corrida siguiente para escribir el modelo. Sobrescribir
        # el archivo entero borraba esas columnas, y la corrida siguiente caia
        # en la X por omision con una celeridad de 1 m/s, sin que nada avisara:
        # el modelo y la tabla del informe quedaban diciendo cosas distintas.
        filas = fusionar_transito(destino, filas, str(delimitador))
        with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
            escritor = csv.DictWriter(manejador, fieldnames=list(filas[0]),
                                      delimiter=delimitador, restval="")
            escritor.writeheader()
            escritor.writerows(filas)
        resultado.productos.append(rutas.relativa(destino, base))
        sin_microcuenca = sum(1 for f in filas if not f["microcuenca"])
        if sin_microcuenca:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "transito.sin_microcuenca",
                f"{sin_microcuenca} de {len(filas)} tramo(s) no reciben "
                "ninguna subcuenca directamente: reciben el caudal ya "
                "transitado de aguas arriba. Su celda de microcuenca va vacia, "
                "que es lo que corresponde, y no el nombre de una subcuenca "
                "lejana.",
            ))

    if relacion is not None and anchos:
        valores = sorted(anchos.values())
        descripcion = (
            f"ancho de fondo por geometria hidraulica, w = "
            f"{relacion['coeficiente']:g}*A^{relacion['exponente']:g} con A el "
            f"area de drenaje ACUMULADA del tramo: de {valores[0]:.1f} a "
            f"{valores[-1]:.1f} m, mediana {valores[len(valores)//2]:.1f} m")
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "transito.geometria_regionalizada",
            f"el {descripcion}. Es una REGIONALIZACION ({relacion['fuente']}) "
            f"sin datos de campo del proyecto"
            f"{', y la tabla no esta validada' if not relacion['validado'] else ''}"
            ". El informe debe presentarla como tal: la seccion gobierna el "
            "amortiguamiento de la onda y con ella cambia el caudal pico. Si el "
            "estudio dispone de secciones levantadas, sustituyen esta tabla.",
        ))
    else:
        descripcion = f"ancho de fondo {ancho:.1f} m, igual en todos los tramos"

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "modelo.actualizado",
        f"{actualizadas} subcuenca(s) pasan a SCS Curve Number y SCS Unit "
        f"Hydrograph con su CN y su rezago, y {tramos_ok} tramo(s) a "
        f"Muskingum-Cunge con seccion trapezoidal, n de Manning {n_manning:.3f}, "
        f"{descripcion} y talud {talud:.1f}H:1V. La topologia, "
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
    if tramos_elevados:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "modelo.k_elevada_al_minimo",
            f"{len(tramos_elevados)} tramo(s) con K por debajo de lo que el "
            f"paso de calculo puede representar: {tramos_elevados[:6]}. Se "
            "elevan a la K minima admisible y el transito de esos tramos queda "
            "sobrestimado en menos de un intervalo. Bajar el paso de calculo "
            "los resolveria, pero cambiaria el tiempo de rezago de TODAS las "
            "subcuencas, porque el criterio adoptado lo hace depender de el. "
            "El informe debe declararlo.",
        ))
    if embalses_fallidos:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "modelo.embalse_no_escrito",
            f"{len(embalses_fallidos)} embalse(s) declarado(s) no se pudieron "
            f"escribir: {embalses_fallidos}. NO se continua: el modelo correria "
            "sin ellos y entregaria un caudal de diseno mas alto, con formato "
            "correcto y sin ninguna senal de que falta la regulacion."))
    if embalses_escritos:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "modelo.embalses",
            f"{len(embalses_escritos)} embalse(s) escritos: "
            f"{embalses_escritos}. Se transitan por Puls modificado sobre la "
            "curva de elevacion contra almacenamiento del operador, con dos "
            "salidas independientes: la descarga de fondo, que opera de "
            "continuo, y el vertedero libre, que solo actua por encima de su "
            "cresta. LA COTA INICIAL ES EL ESTADO DE OPERACION DEL QUE SE "
            "PARTE y es la decision con mas peso del elemento: entre el nivel "
            "normal y la cresta el caudal de salida cambia por un factor de "
            "treinta. El informe debe declararla, junto con que las curvas son "
            "informacion secundaria del operador tomada para la modelacion "
            "hidrologica y no un estudio de operacion del embalse."))
    if abs(factor_cn - 1.0) > 1e-9:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "modelo.cn_calibrado",
            f"el numero de curva se afecto por un factor de {factor_cn:.3f}: "
            "el modelo NO corre con el CN que salen de suelos y coberturas. "
            "Esto es CALIBRACION y no verificacion, de modo que la coincidencia "
            "posterior con el caudal observado deja de ser evidencia de que el "
            "modelo sea bueno y pasa a ser el resultado de haberla buscado. El "
            "informe debe declarar el factor, su motivo y contra que se ajusto, "
            "y presentar las dos columnas de CN que deja "
            "hidrologia/abstraccion_inicial.csv."))
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
    # EL SEGUNDO ESCENARIO, sin el factor de cambio climatico. Se escribe como
    # un juego paralelo de pluviometros, meteorologias y corridas, de modo que
    # HEC-HMS resuelva los dos en la misma sesion. El de diseno es el que lleva
    # el factor; el otro representa la lluvia registrada y va como referencia.
    comparar = bool(configuracion.obtener(
        "cambio_climatico.comparar_escenarios", False))
    hietogramas_sf, hallazgo = escenario_de_referencia(hietogramas, comparar)
    if hallazgo is not None:
        resultado.hallazgos.append(hallazgo)
    if hietogramas_sf:
        hietogramas = list(hietogramas) + hietogramas_sf

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

    sufijos = [""] + ([SUFIJO_SIN_FACTOR] if hietogramas_sf else [])
    for sufijo in sufijos:
        def pluviometro_del_escenario(pluviometro: str, periodo: str,
                                      _s=sufijo) -> str:
            return pluviometro_de_zona(f"{pluviometro}{_s}", periodo)

        for periodo in periodos:
            nombre = f"T{periodo.replace('.', '_')}{sufijo}"
            destino = proyecto / f"{nombre}.met"
            escribir_met(destino, nombre, periodo, asignacion,
                         pluviometro_del_escenario, modelo_cuenca)
            resultado.productos.append(str(destino))
            resultado.escenarios.append(nombre)

    # LA VENTANA SE DECLARA, no se deduce de un multiplicador embebido. Era
    # 'duracion_h * 4', es decir 12 horas con la tormenta de 3, suficiente para
    # que el hidrograma se agote pero NO para promediarlo a escala diaria.
    #
    # La verificacion de crecientes contrasta contra el maximo de los caudales
    # MEDIOS DIARIOS observados, y para comparar magnitudes homogeneas hay que
    # calcular la media movil de 24 h del hidrograma simulado. Con una ventana
    # de 12 h esa media no existe.
    #
    # Alargar la ventana NO cambia el pico: la tormenta dura lo mismo y lo que
    # se anade es recesion. Solo cuesta ordenadas.
    ventana_h = float(configuracion.obtener(
        "tormenta.ventana_simulacion_h", duracion_h * 4))
    fin = inicio + _dt.timedelta(hours=ventana_h)
    control = proyecto / "Tormenta_diseno.control"
    escribir_control(control, "Tormenta_diseno", inicio, fin, intervalo)
    resultado.productos.append(str(control))

    ruta_hms = proyecto / str(configuracion.obtener("hec_hms.proyecto.archivo"))
    if ruta_hms.is_file():
        registrar_componentes(ruta_hms, list(resultado.escenarios),
                              "Tormenta_diseno")
        resultado.productos.append(str(ruta_hms))
    else:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "proyecto.sin_hms",
            f"no se encuentra {ruta_hms.name}: los componentes quedan escritos "
            "pero sin declarar en el proyecto, y HEC-HMS no los vera.",
        ))

    corridas = escribir_runs(
        proyecto / f"{Path(str(configuracion.obtener('hec_hms.proyecto.archivo'))).stem}.run",
        resultado.escenarios, modelo_cuenca, "Tormenta_diseno")
    resultado.productos.append(
        str(proyecto / f"{Path(str(configuracion.obtener('hec_hms.proyecto.archivo'))).stem}.run"))
    logger.info("%d simulacion(es) escritas", corridas)

    resultado.meteorologia = {
        "pluviometros": resumen["pluviometros"],
        "ordenadas": resumen["ordenadas"],
        "periodos": periodos,
        "ventana_horas": ventana_h,
        "intervalo_min": intervalo,
    }
    logger.info("%d pluviometro(s), %d modelo(s) meteorologico(s), ventana de "
                "%.0f h", resumen["pluviometros"], len(periodos),
                ventana_h)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "meteorologia.escrita",
        f"{resumen['pluviometros']} pluviometro(s) con {resumen['ordenadas']} "
        f"ordenada(s) en total, y {len(periodos)} modelo(s) meteorologico(s), "
        "uno por periodo de retorno. Cada subcuenca queda enganchada al "
        "pluviometro de SU ZONA, que es lo que hace que cinco series basten "
        f"para {len(asignacion)} subcuencas. Las ordenadas van al DSS del "
        f"proyecto ({resumen['dss']}) y el .gage guarda solo su ruta interna, "
        "que es como HEC-HMS almacena una serie introducida a mano.",
    ))
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "escenarios.creados",
        f"{corridas} simulacion(es) escritas, una por periodo de retorno, cada "
        f"una combinando el modelo {modelo_cuenca!r}, su meteorologia y las "
        "especificaciones de control. El M14 las ejecutara.",
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

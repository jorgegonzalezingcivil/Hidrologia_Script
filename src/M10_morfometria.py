#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M10 - Caracterización morfométrica e hidrológica
================================================
Entorno: venv del proyecto.

Caracteriza la cuenca o las subcuencas según el MODO DE ANÁLISIS declarado:

    general    una sola unidad, la que entrega el M02. Sin modelo HEC-HMS.
    detallado  las subcuencas que el M09 importó de HEC-HMS.

El modo no es una comodidad de ejecución: cambia lo que significa cada
resultado. Un tiempo de concentración de una cuenca de miles de kilómetros
cuadrados no tiene el mismo sentido que el de una subcuenca de decenas, y el
módulo lo dice en lugar de entregar una cifra sin contexto.

Sobre el tiempo de concentración. CLAUDE.md, sección 6, fija una matriz de
aplicabilidad por tipo de cuenca y adopta la MEDIANA del subconjunto aplicable.
La sección 7 añade la cautela: si ese subconjunto tiene menos de cinco elementos
o la dispersión es alta, se advierte y NO se adopta la mediana automáticamente.

Esa cautela no es decorativa. Las fórmulas de Tc se calibraron casi todas en
cuencas pequeñas: Kirpich sobre siete cuencas agrícolas de 0,4 a 45 hectáreas.
Aplicarlas fuera de su rango es la extrapolación más frecuente y menos
justificada de la práctica, y por eso la matriz vive en data/referencia con el
rango de cada una y su procedencia, nunca en el código.

Sobre el RELIEVE. Cotas, curva hipsométrica y pendiente salen de leer el DEM
celda a celda, con el adaptador 'comun/raster.py', sin GDAL y sin salir del
venv. La pendiente no se entrega como una sola cifra: se calcula además sobre
el DEM agregado a resoluciones más gruesas, porque la pendiente de una celda no
es una propiedad del terreno sino del terreno Y de la resolución con que se
mide. Ese contraste separa el relieve del error del sensor, que sobre terreno
plano domina por completo la diferencia de cota entre celdas contiguas y, al
carecer de signo el módulo del gradiente, solo puede inflar el resultado.

Productos:
    data/02_procesado/morfometria/parametros.csv
    data/02_procesado/morfometria/tiempo_concentracion.csv
    data/02_procesado/morfometria/curva_hipsometrica.csv
    data/02_procesado/morfometria/distribucion_altimetrica.csv
    data/02_procesado/morfometria/pendiente_por_escala.csv
    data/02_procesado/morfometria/M10_morfometria.md
    data/02_procesado/M10_morfometria.json
    data/05_resultados/graficos/M10_*.png

Uso:
    python src/M10_morfometria.py

Códigos de salida:
    0  caracterización producida
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import red_drenaje  # noqa: E402  (solo sus funciones de grafo, sin QGIS)
import tiempo_concentracion  # noqa: E402
from comun import (  # noqa: E402
    esquema, geometria, raster, registro, rutas, shapefile,
)
from comun.campos import CampoSalida  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M10"
DESCRIPCION = "Caracterización morfométrica e hidrológica"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Parámetros que exigen el DEM. Si falta, se declaran uno a uno para que el
# informe sepa qué falta, en lugar de que su ausencia pase inadvertida.
PARAMETROS_DE_RELIEVE = (
    "cota_max", "cota_min", "cota_media", "desnivel_altitudinal",
    "curva_hipsometrica", "pendiente_media_cuenca",
)


@dataclass
class ResultadoM10:
    modo: str = ""
    unidades: list[dict[str, Any]] = field(default_factory=list)
    relieve: dict[str, Any] = field(default_factory=dict)
    drenaje: dict[str, Any] = field(default_factory=dict)
    magnitudes: dict[str, Any] = field(default_factory=dict)
    rezago: dict[str, Any] = field(default_factory=dict)
    suelos: dict[str, Any] = field(default_factory=dict)
    subcuencas: list[dict[str, Any]] = field(default_factory=list)
    tiempos_por_subcuenca: list[dict[str, Any]] = field(
        default_factory=list)
    tiempos: list[dict[str, Any]] = field(default_factory=list)
    adoptados: dict[str, Any] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Geometría de la cuenca
# =============================================================================
def parametros_geometricos(ruta: Path) -> dict[str, float]:
    """
    Área, perímetro y los índices de forma que se derivan de ambos.

    El coeficiente de compacidad de Gravelius compara el perímetro con el del
    círculo de igual área: vale 1 en una cuenca circular y crece con la
    irregularidad. Por encima de 1,5 la cuenca es alargada, y eso amortigua el
    hidrograma porque el agua de las cabeceras llega desfasada.

    EL PERÍMETRO ES EL DEL CONTORNO, no la suma del de cada subcuenca. Cuando la
    delimitación llega partida en unidades, sumar sus perímetros cuenta dos
    veces cada linde interior. Medido sobre las 125 subcuencas de este estudio:
    1.002,6 km sumando frente a 145,3 km de contorno, y un Gravelius de 19,06
    donde correspondía 2,74. Diecinueve es imposible en una cuenca real, pero
    nada en el cálculo lo señalaba: el número salía, entraba en la tabla y de
    ahí al informe.

    Si el mosaico no es una cobertura limpia, el contorno no se puede obtener
    por conteo de aristas y se cae a la suma, declarándolo en 'perimetro_metodo'
    para que quien lea la tabla sepa qué tiene delante.
    """
    area_m2 = float(shapefile.area_poligonos(ruta))
    axial_m = float(shapefile.distancia_maxima(ruta))
    if area_m2 <= 0:
        raise ErrorFormato(f"{ruta.name} no encierra área positiva.")

    contorno = geometria.perimetro_exterior(shapefile.leer_geometrias(ruta))
    if contorno["cobertura_limpia"]:
        perimetro_m = float(contorno["perimetro_m"])
        metodo = "contorno"
    else:
        perimetro_m = float(shapefile.perimetro_poligonos(ruta))
        metodo = "suma_de_piezas"

    area_km2 = area_m2 / 1e6
    perimetro_km = perimetro_m / 1000.0
    axial_km = axial_m / 1000.0
    return {
        "area_km2": round(area_km2, 3),
        "area_ha": round(area_km2 * 100.0, 2),
        "perimetro_km": round(perimetro_km, 3),
        "perimetro_metodo": metodo,
        "aristas_frontera": contorno["aristas_frontera"],
        "aristas_compartidas": contorno["aristas_compartidas"],
        "aristas_repetidas": contorno["aristas_repetidas"],
        "contornos": contorno["contornos"],
        "cadenas_degeneradas": contorno["cadenas_degeneradas"],
        "longitud_degenerada_km": round(
            contorno["longitud_degenerada_m"] / 1000.0, 3),
        "longitud_axial_km": round(axial_km, 3),
        "ancho_medio_km": round(area_km2 / axial_km, 3) if axial_km else None,
        # Coeficiente de forma de Horton: área sobre el cuadrado de la longitud.
        "coef_forma": round(area_km2 / (axial_km ** 2), 4) if axial_km else None,
        # Gravelius: 0,2821 es 1/(2*sqrt(pi)) con las unidades en km y km2.
        "coef_compacidad": round(
            0.2821 * perimetro_km / math.sqrt(area_km2), 4),
    }


def _centro(segmento: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Punto medio de un segmento de dos vértices."""
    return ((segmento[0][0] + segmento[1][0]) / 2.0,
            (segmento[0][1] + segmento[1][1]) / 2.0)


def parametros_de_drenaje(
    ruta_drenaje: Path, poligonos, ) -> dict[str, Any]:
    """
    Longitud de cauce dentro de la cuenca, sobre una capa de líneas cualquiera.

    Es el cálculo de respaldo, el único posible cuando no existe la red con
    topología del M02b. Da longitud y densidad, y nada más: sin saber qué tramo
    desemboca en cuál no hay orden de corrientes ni cauce principal.

    El recorte del M02 llega a la envolvente, que es mayor que la cuenca: usar
    su longitud entera sobrestimaría la densidad. Se filtra segmento a segmento
    por el punto medio.

    Es una aproximación declarada: un segmento que cruza la divisoria cuenta
    entero adentro o entero afuera según dónde caiga su centro. Con segmentos de
    decenas de metros sobre una cuenca de miles de kilómetros cuadrados, el
    error es despreciable frente a la incertidumbre de la propia cartografía.
    """
    longitud_m = 0.0
    tramos = 0
    indice = geometria.IndicePoligonos(poligonos)
    for entidad in shapefile.leer_geometrias(ruta_drenaje):
        dentro_alguno = False
        for parte in entidad:
            for uno, otro in zip(parte, parte[1:]):
                centro = _centro((uno, otro))
                if not indice.contiene(centro[0], centro[1]):
                    continue
                longitud_m += math.hypot(otro[0] - uno[0], otro[1] - uno[1])
                dentro_alguno = True
        if dentro_alguno:
            tramos += 1
    return {"long_cauces_km": round(longitud_m / 1000.0, 2),
            "tramos_dentro": tramos}


# =============================================================================
# Drenaje sobre la red con topología
# =============================================================================
def recortar_red(ruta_red: Path, poligonos) -> dict[str, Any]:
    """
    Selecciona de la red del M02b los tramos que caen dentro de la unidad.

    Se decide por el punto MEDIO de cada tramo, la misma convención que usa el
    cálculo de respaldo. La relación de afluencia se conserva solo entre tramos
    que quedan los dos dentro: un tramo cuyo receptor queda fuera pasa a ser
    desembocadura de la unidad, que es lo que corresponde.
    """
    registros = list(shapefile.leer_registros(
        ruta_red, ["id_tramo", "receptor", "orden", "long_m", "origen", "nombre"]))
    geometrias = shapefile.leer_geometrias(ruta_red)
    if len(registros) != len(geometrias):
        raise ErrorFormato(
            f"{ruta_red.name}: {len(registros)} registros y "
            f"{len(geometrias)} geometrías.")

    indice = geometria.IndicePoligonos(poligonos)
    dentro: dict[int, dict[str, Any]] = {}
    vertices: dict[int, list[tuple[float, float]]] = {}

    for registro_tramo, entidad in zip(registros, geometrias):
        puntos = [punto for parte in entidad for punto in parte]
        if len(puntos) < 2:
            continue
        medio = puntos[len(puntos) // 2]
        if not indice.contiene(medio[0], medio[1]):
            continue
        identificador = int(registro_tramo["id_tramo"])
        dentro[identificador] = {
            "receptor": int(registro_tramo["receptor"]),
            "long_m": float(registro_tramo["long_m"]),
            "origen": registro_tramo["origen"],
            "nombre": registro_tramo["nombre"],
        }
        vertices[identificador] = puntos

    afluentes: dict[int, list[int]] = {}
    for identificador, datos in dentro.items():
        receptor = datos["receptor"]
        if receptor in dentro:
            afluentes.setdefault(receptor, []).append(identificador)

    return {"tramos": dentro, "vertices": vertices, "afluentes": afluentes}


def parametros_de_red(
    ruta_red: Path, poligonos, area_km2: float, cota=None,
) -> dict[str, Any]:
    """
    Jerarquía y cauce principal a partir de la red con topología del M02b.

    El orden de Strahler se RECALCULA sobre el subconjunto que cae dentro de la
    unidad, no se hereda del que traía la capa. En modo general las dos cifras
    coinciden, porque la unidad es la cuenca entera; en modo detallado no, y la
    que describe una subcuenca es la suya, calculada con sus propias cabeceras.

    El cauce principal es el recorrido de mayor longitud acumulada hasta la
    salida, no el río con nombre ni el de mayor orden. Cuando la unidad tiene
    varias salidas, se adopta la que produce el recorrido más largo.

    La pendiente del cauce se toma entre los EXTREMOS y no promediando las
    pendientes locales. Medido sobre el cauce de este estudio, el 24 % de los
    tramos sube más de un metro respecto del anterior, lo que es imposible en
    un cauce y corresponde al error vertical del DEM. Promediar diferencias
    locales sobre ese perfil mide el ruido, no la pendiente.
    """
    recorte = recortar_red(ruta_red, poligonos)
    tramos = recorte["tramos"]
    afluentes = recorte["afluentes"]
    vertices = recorte["vertices"]
    if not tramos:
        raise ErrorHidrologia(
            "ningún tramo de la red del M02b cae dentro de la unidad.")

    longitudes = {i: datos["long_m"] for i, datos in tramos.items()}
    longitud_total_m = sum(longitudes.values())

    orden, ciclos = red_drenaje.orden_strahler(afluentes, list(tramos))
    corrientes = red_drenaje.contar_corrientes(orden, afluentes)
    bifurcacion = red_drenaje.razon_bifurcacion(corrientes)
    total_corrientes = sum(corrientes.values())

    salidas = [i for i, datos in tramos.items()
               if datos["receptor"] not in tramos]
    mejor_camino: list[int] = []
    mejor_longitud = 0.0
    for salida in salidas:
        camino = red_drenaje.camino_mas_largo(afluentes, longitudes, salida)
        largo = sum(longitudes.get(t, 0.0) for t in camino)
        if largo > mejor_longitud:
            mejor_camino, mejor_longitud = camino, largo

    resultado: dict[str, Any] = {
        "red": str(ruta_red),
        "tramos_dentro": len(tramos),
        "long_cauces_km": round(longitud_total_m / 1000.0, 2),
        "densidad_drenaje_km_km2": (round(longitud_total_m / 1000.0 / area_km2, 4)
                                    if area_km2 else None),
        "orden_corrientes": max(orden.values()) if orden else 0,
        "corrientes_por_orden": corrientes,
        "corrientes_totales": total_corrientes,
        "frecuencia_corrientes_km2": (round(total_corrientes / area_km2, 4)
                                      if area_km2 else None),
        "razon_bifurcacion": bifurcacion.get("adoptada"),
        "razon_bifurcacion_simple": bifurcacion.get("media_simple"),
        "bifurcacion_en_rango_natural": bifurcacion.get(
            "dentro_del_rango_natural"),
        "bifurcacion_pares": bifurcacion.get("pares"),
        "salidas_de_la_unidad": len(salidas),
        "tramos_en_ciclo": len(ciclos),
    }

    if not mejor_camino:
        return resultado

    mejor_camino, recorte_cola = recortar_cola_ajena(
        mejor_camino, tramos, longitudes)
    mejor_longitud = sum(longitudes.get(t, 0.0) for t in mejor_camino)
    resultado["recorte_cola"] = recorte_cola
    if not mejor_camino:
        return resultado

    nacimiento = vertices[mejor_camino[0]][0]
    cierre = vertices[mejor_camino[-1]][-1]
    recta_m = math.hypot(cierre[0] - nacimiento[0], cierre[1] - nacimiento[1])

    resultado.update({
        "long_cauce_principal_km": round(mejor_longitud / 1000.0, 2),
        "tramos_del_cauce_principal": len(mejor_camino),
        "indice_sinuosidad": (round(mejor_longitud / recta_m, 3)
                              if recta_m > 0 else None),
        "distancia_recta_cauce_km": round(recta_m / 1000.0, 2),
        "nombres_del_cauce_principal": _nombres_del_camino(mejor_camino, tramos),
    })

    if cota is not None:
        cota_alta = cota(nacimiento[0], nacimiento[1])
        cota_baja = cota(cierre[0], cierre[1])
        if cota_alta == cota_alta and cota_baja == cota_baja:
            desnivel = cota_alta - cota_baja
            resultado.update({
                "cota_nacimiento": round(cota_alta, 2),
                "cota_cierre": round(cota_baja, 2),
                "desnivel_cauce_m": round(desnivel, 2),
                "pendiente_media_cauce": (round(desnivel / mejor_longitud, 6)
                                          if mejor_longitud else None),
                "pendiente_media_cauce_pct": (
                    round(100.0 * desnivel / mejor_longitud, 3)
                    if mejor_longitud else None),
            })
    return resultado


def recortar_cola_ajena(
    camino: Sequence[int], tramos: dict, longitudes: dict,
    fraccion_maxima: float = 0.02, fraccion_dominante: float = 0.05,
) -> tuple[list[int], dict[str, Any]]:
    """
    Quita del final del cauce el trozo que ya pertenece a otro río.

    El polígono de la unidad puede rebasar unos metros la confluencia con el
    cauce receptor, y entonces el recorrido más largo continúa por él. Medido
    sobre este estudio, el cauce trazado termina con 0,22 km de Río Magdalena
    tras 242,85 km de Río Bogotá: el 0,07 % de la longitud, irrelevante en
    magnitud pero incorrecto, y suficiente para invalidar el nombre con el que
    el cauce se identifica en el informe.

    El nombre geográfico del IGAC es la autoridad sobre a qué río pertenece
    cada tramo, de modo que se usa como criterio. El recorte está acotado por
    los dos lados: solo se quita lo que sigue al último bloque SUSTANCIAL del
    recorrido, y solo si lo quitado es una fracción menor del total. Si el
    trozo ajeno fuera grande, no sería un rebase del polígono sino un error de
    delimitación, y entonces debe verse en el resultado y no taparse.
    """
    if not camino:
        return [], {"recortado": False}

    total = sum(longitudes.get(t, 0.0) for t in camino)
    if total <= 0:
        return list(camino), {"recortado": False}

    # Bloques consecutivos del mismo nombre, de aguas arriba a aguas abajo.
    bloques: list[tuple[str, float, int]] = []
    for indice, identificador in enumerate(camino):
        nombre = str(tramos[identificador].get("nombre", "")).strip()
        largo = longitudes.get(identificador, 0.0)
        if bloques and bloques[-1][0] == nombre:
            anterior = bloques[-1]
            bloques[-1] = (nombre, anterior[1] + largo, anterior[2])
        else:
            bloques.append((nombre, largo, indice))

    dominante = None
    for nombre, largo, indice in bloques:
        if nombre and largo >= fraccion_dominante * total:
            dominante = (nombre, indice)
    if dominante is None:
        return list(camino), {"recortado": False}

    ultimo_nombre, _ = dominante
    corte = len(camino)
    for nombre, _largo, indice in bloques:
        if indice > 0 and nombre != ultimo_nombre:
            posterior = [b for b in bloques if b[2] >= indice]
            if all(b[0] != ultimo_nombre for b in posterior):
                corte = min(corte, indice)
    if corte >= len(camino):
        return list(camino), {"recortado": False}

    quitado = sum(longitudes.get(t, 0.0) for t in camino[corte:])
    if quitado > fraccion_maxima * total:
        return list(camino), {
            "recortado": False,
            "cola_ajena_km": round(quitado / 1000.0, 3),
            "cola_ajena_nombre": str(
                tramos[camino[corte]].get("nombre", "")).strip(),
            "excede_el_limite": True,
        }

    return list(camino[:corte]), {
        "recortado": True,
        "tramos_quitados": len(camino) - corte,
        "cola_ajena_km": round(quitado / 1000.0, 3),
        "cola_ajena_nombre": str(tramos[camino[corte]].get("nombre", "")).strip(),
        "cauce_adoptado": ultimo_nombre,
    }


def _nombres_del_camino(camino: Sequence[int], tramos: dict) -> str:
    """
    Nombres geográficos que atraviesa el cauce principal, sin repetir.

    Sirve para verificar el trazado de un vistazo: si el recorrido más largo de
    la cuenca del Río Bogotá no menciona ese río, algo va mal en la red.
    """
    vistos: list[str] = []
    for identificador in camino:
        nombre = str(tramos[identificador].get("nombre", "")).strip()
        if nombre and nombre not in vistos:
            vistos.append(nombre)
    return "; ".join(vistos)


# =============================================================================
# Relieve
# =============================================================================
# Constante del estimador de ruido vertical. La pendiente de Horn sobre un
# plano contaminado con ruido blanco de desviación sigma tiene componentes de
# desviación sigma*raiz(3)/(4*dx), y su módulo sigue una Rayleigh, cuya mediana
# vale raiz(2*ln2) veces esa desviación. Invirtiendo:
#
#       sigma = mediana_de_pendiente * dx / (raiz(2*ln2) * raiz(3)/4)
#
# El divisor es 0,5098, de modo que el factor es 1,9615.
FACTOR_RUIDO = 1.0 / (math.sqrt(2.0 * math.log(2.0)) * math.sqrt(3.0) / 4.0)

# Casillas del histograma de pendiente, en m/m. Cubre hasta la vertical con
# resolución suficiente para leer una mediana fiable.
PASO_PENDIENTE = 0.002
CASILLAS_PENDIENTE = 2500


def _mascara_de_fila(info, aristas, fila: int, np_) -> Any:
    """
    Devuelve, para una fila del ráster, qué celdas caen dentro de la cuenca.

    Se resuelve por el CENTRO de la celda, que es la convención de estadística
    zonal de GDAL y de QGIS. Cambiarla por el criterio de superficie tocada
    inflaría el área en un borde de media celda alrededor de toda la divisoria.
    """
    mascara = np_.zeros(info.ancho, dtype=bool)
    for desde, hasta in geometria.columnas_de_fila(
            aristas, info.y_de_fila(fila), info.origen_x, info.tamano_x,
            info.ancho):
        mascara[desde:hasta + 1] = True
    return mascara


def pendiente_de_horn(bloque, valido, tamano_x: float, tamano_y: float, np_):
    """
    Pendiente por el método de Horn (1981) sobre la fila central del bloque.

    Es el método de la ventana de 3 x 3 con pesos 1-2-1 que usan GDAL, QGIS y
    ArcGIS. Se prefiere al de máxima diferencia porque promedia las ocho
    vecinas, lo que atenúa (sin eliminar) el ruido de celda del DEM de radar.

    Devuelve el módulo del gradiente en m/m y la máscara de celdas donde se
    pudo calcular. Una celda con cualquier vecina sin dato queda excluida: dar
    por buena una ventana incompleta introduce un escalón artificial en el
    borde de cada hueco del DEM.
    """
    z = bloque
    v = valido
    # Ventana completa: las nueve celdas con dato.
    entera = (v[0, :-2] & v[0, 1:-1] & v[0, 2:]
              & v[1, :-2] & v[1, 1:-1] & v[1, 2:]
              & v[2, :-2] & v[2, 1:-1] & v[2, 2:])

    dzdx = ((z[0, 2:] + 2.0 * z[1, 2:] + z[2, 2:])
            - (z[0, :-2] + 2.0 * z[1, :-2] + z[2, :-2])) / (8.0 * tamano_x)
    dzdy = ((z[2, :-2] + 2.0 * z[2, 1:-1] + z[2, 2:])
            - (z[0, :-2] + 2.0 * z[0, 1:-1] + z[0, 2:])) / (8.0 * tamano_y)
    return np_.hypot(dzdx, dzdy), entera


def _pendiente_de_malla(malla, valido, paso: float, np_):
    """Aplica Horn a una malla completa en memoria. Para las mallas gruesas."""
    alto, ancho = malla.shape
    if alto < 3 or ancho < 3:
        return np_.zeros((0,)), np_.zeros((0,), dtype=bool)
    pendientes = np_.zeros((alto - 2, ancho - 2))
    enteras = np_.zeros((alto - 2, ancho - 2), dtype=bool)
    for j in range(1, alto - 1):
        valor, entera = pendiente_de_horn(
            malla[j - 1:j + 2], valido[j - 1:j + 2], paso, paso, np_)
        pendientes[j - 1] = valor
        enteras[j - 1] = entera
    return pendientes, enteras


def _percentil_de_histograma(cuentas, bordes, fraccion: float, np_) -> float:
    """
    Percentil leído sobre un histograma acumulado.

    Se trabaja sobre histograma y no sobre el vector de valores porque la
    cuenca tiene decenas de millones de celdas: guardarlas todas para ordenar
    costaría cientos de megabytes sin mejorar una cifra que se reporta al metro.
    """
    total = float(cuentas.sum())
    if total <= 0:
        return float("nan")
    acumulado = np_.cumsum(cuentas)
    objetivo = fraccion * total
    indice = int(np_.searchsorted(acumulado, objetivo, side="left"))
    indice = min(max(indice, 0), len(cuentas) - 1)
    previo = float(acumulado[indice - 1]) if indice > 0 else 0.0
    en_casilla = float(cuentas[indice])
    if en_casilla <= 0:
        return float(bordes[indice])
    interpolado = (objetivo - previo) / en_casilla
    return float(bordes[indice] + interpolado * (bordes[indice + 1] - bordes[indice]))


def estadisticas_de_relieve(
    ruta_dem: Path,
    poligonos,
    intervalo_m: float = 25.0,
    escalas: Sequence[int] = (4, 8, 16),
    pendiente_llana: float = 0.005,
) -> dict[str, Any]:
    """
    Cotas, curva hipsométrica y pendiente de la cuenca, leyendo el DEM.

    Recorre el ráster tres veces y nunca lo carga entero: 456 MB de terreno no
    caben con holgura junto a las mallas de agregación.

        1. extremos y media, que fijan el rango del histograma
        2. mallas agregadas, que dan la pendiente a varias resoluciones
        3. histogramas de cota y de pendiente

    El tercer recorrido separa además la pendiente de las celdas que caen en
    zona LLANA según la malla más gruesa. Esa separación es el diagnóstico que
    justifica la advertencia sobre el DEM de radar: sobre terreno plano, la
    diferencia de cota entre celdas contiguas es del orden del error vertical
    del propio DEM, no del relieve, y como el módulo del gradiente no tiene
    signo, ese error nunca se compensa: solo infla la pendiente.

    Excepciones
    -----------
    ErrorRutas
        No está el DEM.
    ErrorHidrologia
        La cuenca no cae sobre el ráster, o no queda ninguna celda con dato.
    """
    import numpy as np  # noqa: PLC0415  (solo el venv lo necesita)

    info = raster.leer_info(ruta_dem)
    aristas = geometria.aristas_de(poligonos)
    if not aristas:
        raise ErrorHidrologia("la cuenca no tiene aristas legibles.")
    xmin, ymin, xmax, ymax = geometria.envolvente(poligonos)

    rxmin, rymin, rxmax, rymax = info.extension
    if xmin >= rxmax or xmax <= rxmin or ymin >= rymax or ymax <= rymin:
        raise ErrorHidrologia(
            f"la cuenca no se superpone con el DEM. Cuenca "
            f"({xmin:.0f}, {ymin:.0f}) a ({xmax:.0f}, {ymax:.0f}); DEM "
            f"({rxmin:.0f}, {rymin:.0f}) a ({rxmax:.0f}, {rymax:.0f}). "
            "Suele ser un CRS distinto del declarado.")

    fuera = not info.contiene(xmin, ymin, xmax, ymax)
    fila_ini = max(0, info.fila_de(ymax))
    fila_fin = min(info.alto - 1, info.fila_de(ymin))
    nodato = info.nodato

    # --- Recorrido 1: extremos ----------------------------------------------
    cota_min = float("inf")
    cota_max = float("-inf")
    suma = 0.0
    validas = 0
    dentro = 0
    with raster.LectorRaster(ruta_dem) as lector:
        for fila in range(fila_ini, fila_fin + 1):
            mascara = _mascara_de_fila(info, aristas, fila, np)
            if not mascara.any():
                continue
            z = np.frombuffer(lector.fila(fila), dtype=info.descriptor)
            dentro += int(mascara.sum())
            if nodato is not None:
                mascara &= z != nodato
            valores = z[mascara]
            if not valores.size:
                continue
            validas += valores.size
            suma += float(valores.sum(dtype=np.float64))
            cota_min = min(cota_min, float(valores.min()))
            cota_max = max(cota_max, float(valores.max()))

    if not validas:
        raise ErrorHidrologia(
            "ninguna celda del DEM dentro de la cuenca tiene dato.")

    media = suma / validas
    area_celda = info.area_celda_m2

    # --- Recorrido 2: mallas agregadas ---------------------------------------
    columna_ini = max(0, info.columna_de(xmin))
    columna_fin = min(info.ancho - 1, info.columna_de(xmax))
    ancho_util = columna_fin - columna_ini + 1
    alto_util = fila_fin - fila_ini + 1

    escalas = tuple(int(f) for f in escalas if int(f) >= 2)
    sumas: dict[int, Any] = {}
    cuentas: dict[int, Any] = {}
    for factor in escalas:
        filas_gruesas = (alto_util + factor - 1) // factor
        columnas_gruesas = (ancho_util + factor - 1) // factor
        sumas[factor] = np.zeros((filas_gruesas, columnas_gruesas), np.float64)
        cuentas[factor] = np.zeros((filas_gruesas, columnas_gruesas), np.int32)

    if escalas:
        with raster.LectorRaster(ruta_dem) as lector:
            for fila in range(fila_ini, fila_fin + 1):
                mascara = _mascara_de_fila(info, aristas, fila, np)
                if not mascara.any():
                    continue
                z = np.frombuffer(lector.fila(fila), dtype=info.descriptor)
                if nodato is not None:
                    mascara = mascara & (z != nodato)
                ventana = mascara[columna_ini:columna_fin + 1]
                if not ventana.any():
                    continue
                cotas = np.where(
                    ventana, z[columna_ini:columna_fin + 1], 0.0
                ).astype(np.float64)
                unos = ventana.astype(np.int32)
                indice = fila - fila_ini
                for factor in escalas:
                    relleno = (-ancho_util) % factor
                    if relleno:
                        bloque = np.concatenate([cotas, np.zeros(relleno)])
                        marcas = np.concatenate(
                            [unos, np.zeros(relleno, np.int32)])
                    else:
                        bloque, marcas = cotas, unos
                    sumas[factor][indice // factor] += bloque.reshape(
                        -1, factor).sum(axis=1)
                    cuentas[factor][indice // factor] += marcas.reshape(
                        -1, factor).sum(axis=1)

    # Una celda gruesa vale si la mayoría de sus celdas finas tiene dato: con
    # menos, su cota media representa el borde y no la celda.
    pendiente_por_escala: list[dict[str, Any]] = []
    llanas = None
    for factor in escalas:
        completas = cuentas[factor] >= (factor * factor) // 2
        malla = np.zeros_like(sumas[factor])
        np.divide(sumas[factor], cuentas[factor], out=malla,
                  where=cuentas[factor] > 0)
        paso = info.tamano_x * factor
        valores, enteras = _pendiente_de_malla(malla, completas, paso, np)
        utiles = valores[enteras] if enteras.size else np.zeros(0)
        pendiente_por_escala.append({
            "factor": factor,
            "tamano_celda_m": round(paso, 2),
            "celdas": int(utiles.size),
            "pendiente_media_mm": round(float(utiles.mean()), 5)
            if utiles.size else None,
            "pendiente_mediana_mm": round(float(np.median(utiles)), 5)
            if utiles.size else None,
        })
        if factor == max(escalas) and enteras.size:
            llanas = (valores < pendiente_llana) & enteras

    # --- Recorrido 3: histogramas -------------------------------------------
    borde_inferior = math.floor(cota_min / intervalo_m) * intervalo_m
    borde_superior = math.ceil(cota_max / intervalo_m) * intervalo_m
    if borde_superior <= borde_inferior:
        borde_superior = borde_inferior + intervalo_m
    n_casillas = int(round((borde_superior - borde_inferior) / intervalo_m))
    bordes_cota = borde_inferior + intervalo_m * np.arange(n_casillas + 1)
    cuentas_cota = np.zeros(n_casillas, np.int64)

    bordes_pendiente = PASO_PENDIENTE * np.arange(CASILLAS_PENDIENTE + 1)
    cuentas_pendiente = np.zeros(CASILLAS_PENDIENTE, np.int64)
    cuentas_llana = np.zeros(CASILLAS_PENDIENTE, np.int64)
    suma_pendiente = 0.0
    celdas_pendiente = 0
    factor_grueso = max(escalas) if escalas else 0

    def _acumular_pendiente(fila: int, bloque, validez, mascara) -> None:
        nonlocal suma_pendiente, celdas_pendiente
        valores, enteras = pendiente_de_horn(
            bloque, validez, info.tamano_x, info.tamano_y, np)
        usar = enteras & mascara[1:-1]
        if not usar.any():
            return
        utiles = valores[usar]
        suma_pendiente += float(utiles.sum())
        celdas_pendiente += int(utiles.size)
        cuentas_pendiente[:] += np.bincount(
            np.clip((utiles / PASO_PENDIENTE).astype(np.int64),
                    0, CASILLAS_PENDIENTE - 1),
            minlength=CASILLAS_PENDIENTE)
        if llanas is None or not factor_grueso:
            return
        # ¿En qué celda gruesa cae cada celda fina de esta fila?
        fila_gruesa = (fila - fila_ini) // factor_grueso - 1
        if not 0 <= fila_gruesa < llanas.shape[0]:
            return
        columnas = np.nonzero(usar)[0] + 1  # índice en la fila del ráster
        gruesas = (columnas - columna_ini) // factor_grueso - 1
        dentro_malla = (gruesas >= 0) & (gruesas < llanas.shape[1])
        if not dentro_malla.any():
            return
        es_llana = np.zeros(columnas.size, dtype=bool)
        es_llana[dentro_malla] = llanas[fila_gruesa, gruesas[dentro_malla]]
        if not es_llana.any():
            return
        cuentas_llana[:] += np.bincount(
            np.clip((utiles[es_llana] / PASO_PENDIENTE).astype(np.int64),
                    0, CASILLAS_PENDIENTE - 1),
            minlength=CASILLAS_PENDIENTE)

    with raster.LectorRaster(ruta_dem) as lector:
        anterior = valido_ant = None
        actual = valido_act = None
        for fila in range(max(0, fila_ini - 1), min(info.alto, fila_fin + 2)):
            crudo = np.frombuffer(lector.fila(fila), dtype=info.descriptor)
            siguiente = crudo.astype(np.float64)
            valido_sig = (siguiente != nodato if nodato is not None
                          else np.ones(siguiente.size, dtype=bool))
            centro = fila - 1
            if anterior is not None and fila_ini <= centro <= fila_fin:
                mascara = _mascara_de_fila(info, aristas, centro, np)
                if mascara.any():
                    dentro_validas = mascara & valido_act
                    if dentro_validas.any():
                        indices = np.clip(
                            ((actual[dentro_validas] - borde_inferior)
                             / intervalo_m).astype(np.int64),
                            0, n_casillas - 1)
                        cuentas_cota[:] += np.bincount(
                            indices, minlength=n_casillas)
                    _acumular_pendiente(
                        centro,
                        np.vstack([anterior, actual, siguiente]),
                        np.vstack([valido_ant, valido_act, valido_sig]),
                        mascara)
            anterior, valido_ant = actual, valido_act
            actual, valido_act = siguiente, valido_sig

    # --- Síntesis ------------------------------------------------------------
    cota_p1 = _percentil_de_histograma(cuentas_cota, bordes_cota, 0.01, np)
    cota_p50 = _percentil_de_histograma(cuentas_cota, bordes_cota, 0.50, np)
    cota_p99 = _percentil_de_histograma(cuentas_cota, bordes_cota, 0.99, np)
    desnivel = cota_max - cota_min

    pendiente_media = (suma_pendiente / celdas_pendiente
                       if celdas_pendiente else float("nan"))
    pendiente_p50 = _percentil_de_histograma(
        cuentas_pendiente, bordes_pendiente, 0.50, np)
    pendiente_p90 = _percentil_de_histograma(
        cuentas_pendiente, bordes_pendiente, 0.90, np)

    mediana_llana = (_percentil_de_histograma(
        cuentas_llana, bordes_pendiente, 0.50, np)
        if cuentas_llana.sum() else float("nan"))
    ruido_m = (FACTOR_RUIDO * mediana_llana * info.tamano_x
               if mediana_llana == mediana_llana else float("nan"))

    curva = curva_hipsometrica(cuentas_cota, bordes_cota, area_celda)
    integral = (media - cota_min) / desnivel if desnivel > 0 else float("nan")

    return {
        "dem": str(ruta_dem),
        "crs_dem": info.crs_epsg,
        "tamano_celda_m": info.tamano_x,
        "celdas_en_cuenca": dentro,
        "celdas_con_dato": validas,
        "cobertura_dem_pct": round(100.0 * validas / dentro, 3) if dentro else 0.0,
        "dem_no_cubre_la_envolvente": fuera,
        "area_por_dem_km2": round(validas * area_celda / 1e6, 3),
        "cota_min": round(cota_min, 2),
        "cota_max": round(cota_max, 2),
        "cota_media": round(media, 2),
        "cota_mediana": round(cota_p50, 2),
        "cota_p1": round(cota_p1, 2),
        "cota_p99": round(cota_p99, 2),
        "desnivel_altitudinal": round(desnivel, 2),
        "desnivel_robusto": round(cota_p99 - cota_p1, 2),
        "integral_hipsometrica": round(integral, 4),
        "pendiente_media_cuenca": round(pendiente_media, 5),
        "pendiente_media_pct": round(100.0 * pendiente_media, 2),
        "pendiente_media_grados": round(math.degrees(math.atan(pendiente_media)), 2),
        "pendiente_mediana": round(pendiente_p50, 5),
        "pendiente_p90": round(pendiente_p90, 5),
        "celdas_con_pendiente": celdas_pendiente,
        "pendiente_por_escala": pendiente_por_escala,
        "celdas_llanas": int(cuentas_llana.sum()),
        "pendiente_mediana_en_llano": (round(mediana_llana, 5)
                                       if mediana_llana == mediana_llana else None),
        "ruido_vertical_estimado_m": (round(ruido_m, 3)
                                      if ruido_m == ruido_m else None),
        "curva_hipsometrica": curva,
        "histograma_cota": [
            {"cota_inf": round(float(bordes_cota[i]), 1),
             "cota_sup": round(float(bordes_cota[i + 1]), 1),
             "celdas": int(cuentas_cota[i]),
             "area_km2": round(float(cuentas_cota[i]) * area_celda / 1e6, 3)}
            for i in range(n_casillas) if cuentas_cota[i]
        ],
    }


def curva_hipsometrica(cuentas, bordes,
                       area_celda_m2: float) -> list[dict[str, float]]:
    """
    Fracción de área POR ENCIMA de cada cota, normalizada entre 0 y 1.

    Es la forma en que Strahler la definió y la que permite comparar cuencas de
    tamaño distinto. Su integral clasifica el estado de la cuenca: por encima
    de 0,60 se lee como cuenca joven, en desequilibrio; entre 0,35 y 0,60 como
    cuenca madura; por debajo de 0,35 como cuenca erosionada.
    """
    total = float(cuentas.sum())
    if total <= 0:
        return []
    cota_min = float(bordes[0])
    cota_max = float(bordes[-1])
    rango = cota_max - cota_min
    encima = float(total)
    curva: list[dict[str, float]] = []
    for indice in range(len(cuentas)):
        cota = float(bordes[indice])
        curva.append({
            "cota": round(cota, 1),
            "cota_relativa": round((cota - cota_min) / rango, 4) if rango else 0.0,
            "area_encima_km2": round(encima * area_celda_m2 / 1e6, 3),
            "area_relativa": round(encima / total, 4),
        })
        encima -= float(cuentas[indice])
    curva.append({
        "cota": round(cota_max, 1),
        "cota_relativa": 1.0,
        "area_encima_km2": 0.0,
        "area_relativa": 0.0,
    })
    return curva


# =============================================================================
# Tiempo de concentración
# =============================================================================
def leer_matriz_aplicabilidad(
    ruta: Path, delimitador: str,
) -> list[dict[str, Any]]:
    """
    Lee la matriz de aplicabilidad, que es doctrina y vive en data/referencia.

    CLAUDE.md, sección 2: las tablas y coeficientes nunca van en el código. Que
    el rango de cada fórmula sea un dato y no una constante permite que el
    consultor lo revise y lo discuta sin tocar el programa.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra la matriz de aplicabilidad en {ruta}. Es doctrina "
            "técnica y debe existir antes de calcular tiempos de concentración."
        )
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        raise ErrorFormato(f"{ruta.name} está vacío.")
    return filas


def es_aplicable(
    fila: dict[str, Any], area_km2: float, pendiente: float | None,
) -> tuple[bool, str]:
    """
    Comprueba si una fórmula puede usarse con esta cuenca, y por qué no si no.

    Devolver el motivo importa tanto como la decisión: un informe que dice "no
    aplica" sin decir que la cuenca es 130 veces mayor que el rango de
    calibración no permite discutir el descarte.
    """
    try:
        minimo = float(fila["area_min_km2"])
        maximo = float(fila["area_max_km2"])
    except (KeyError, TypeError, ValueError):
        return False, "rango de área ilegible en la matriz"

    if area_km2 < minimo:
        return False, (f"área {area_km2:.2f} km2 por debajo del mínimo "
                       f"{minimo:g} km2 de calibración")
    if area_km2 > maximo:
        veces = area_km2 / maximo if maximo else float("inf")
        return False, (f"área {area_km2:.2f} km2 excede el máximo {maximo:g} "
                       f"km2 de calibración ({veces:.0f} veces)")

    if pendiente is not None:
        try:
            pmin = float(fila["pendiente_min"])
            pmax = float(fila["pendiente_max"])
        except (KeyError, TypeError, ValueError):
            return True, ""
        if not (pmin <= pendiente <= pmax):
            return False, (f"pendiente {pendiente:.3f} fuera del rango "
                           f"{pmin:g} a {pmax:g}")
    return True, ""


def evaluar_aplicabilidad(
    matriz: Sequence[dict[str, Any]],
    area_km2: float,
    pendiente: float | None,
    magnitudes: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Aplica la matriz y devuelve una fila por fórmula, con su veredicto.

    Si se pasan las magnitudes de la cuenca, cada fórmula se CALCULA además de
    evaluarse. Se calculan todas, incluidas las que la matriz descarta: el
    informe gana el contraste entre lo que habría dado una fórmula fuera de su
    rango y lo que da el subconjunto adoptable, que es la forma de mostrar por
    qué el descarte importa. Aplicable y calculada son dos columnas distintas y
    solo la primera decide la adopción.
    """
    calculadas = (tiempo_concentracion.calcular_todas(**magnitudes)
                  if magnitudes else {})
    salida: list[dict[str, Any]] = []
    for fila in matriz:
        clave = fila.get("formula", "")
        aplicable, motivo = es_aplicable(fila, area_km2, pendiente)
        registro_formula = {
            "formula": clave,
            "nombre": fila.get("nombre", ""),
            "origen": fila.get("origen", ""),
            "area_min_km2": fila.get("area_min_km2"),
            "area_max_km2": fila.get("area_max_km2"),
            "tipo_cuenca": fila.get("tipo_cuenca", ""),
            "aplicable": aplicable,
            "motivo": motivo,
        }
        calculo = calculadas.get(clave)
        if calculo is not None:
            registro_formula.update({
                "tc_horas": calculo["horas"],
                "tc_minutos": calculo["minutos"],
                "motivo_calculo": calculo["motivo"],
            })
        salida.append(registro_formula)
    return salida


def resumir_adopcion(
    evaluadas: Sequence[dict[str, Any]],
    minimo_formulas: int,
    cv_maximo: float | None = None,
    criterio: str = "mediana",
) -> dict[str, Any]:
    """
    Decide si procede adoptar un valor, según la cautela de la sección 7.

    Con menos fórmulas aplicables que el mínimo declarado NO se adopta la
    mediana: se advierte y la decisión queda para el consultor. Adoptarla de
    todos modos daría una cifra con la misma apariencia que una bien sustentada.

    La dispersión es la segunda condición y se comprueba sobre el subconjunto
    ADOPTABLE, el de las fórmulas que además se pudieron calcular. Un
    coeficiente de variación alto entre fórmulas que la matriz declara
    aplicables no es ruido de cálculo: significa que la cuenca no se parece a
    ninguna de las poblaciones en que se calibraron.
    """
    aplicables = [e for e in evaluadas if e["aplicable"]]
    adoptables = [e for e in aplicables if e.get("tc_horas") is not None]
    resumen = tiempo_concentracion.estadisticos(
        [e["tc_horas"] for e in adoptables])

    suficientes = len(aplicables) >= minimo_formulas
    disperso = (cv_maximo is not None and resumen["cv"] is not None
                and resumen["cv"] > cv_maximo)

    adoptado = None
    if suficientes and not disperso and resumen["n"]:
        adoptado = (resumen["mediana"] if criterio == "mediana"
                    else resumen["media"])

    return {
        "formulas_evaluadas": len(evaluadas),
        "formulas_aplicables": len(aplicables),
        "formulas_adoptables": len(adoptables),
        "minimo_exigido": minimo_formulas,
        "procede_adoptar": bool(suficientes and not disperso and resumen["n"]),
        "aplicables": [e["formula"] for e in aplicables],
        "adoptables": [e["formula"] for e in adoptables],
        "criterio": criterio,
        "cv_maximo_admisible": cv_maximo,
        "dispersion_excesiva": disperso,
        "estadisticos": resumen,
        "tc_horas": adoptado,
        "tc_minutos": round(adoptado * 60.0, 2) if adoptado else None,
    }


def tiempo_de_rezago(
    tc_horas: float | None, criterio: str, intervalo_min: float,
) -> dict[str, Any]:
    """
    Tiempo de rezago a partir del tiempo de concentración.

        scs      Tlag = 0,6 * Tc
        hechms   Tlag = Δt/2 + 0,6 * Tc

    Δt es el INTERVALO DE CÁLCULO del modelo, no la duración de la tormenta.
    Confundirlos es un error de consecuencias grandes: con la tormenta de tres
    horas del estudio, el término valdría 90 minutos en lugar de 2,5, y el
    hidrograma saldría desplazado más de una hora.
    """
    if tc_horas is None or tc_horas <= 0:
        return {"criterio": criterio, "tlag_horas": None, "tlag_minutos": None,
                "motivo": "sin tiempo de concentración adoptado"}

    base = 0.6 * tc_horas
    if criterio == "hechms":
        rezago = intervalo_min / 120.0 + base
    elif criterio == "scs":
        rezago = base
    else:
        return {"criterio": criterio, "tlag_horas": None, "tlag_minutos": None,
                "motivo": f"criterio {criterio!r} no reconocido"}

    return {
        "criterio": criterio,
        "tlag_horas": round(rezago, 4),
        "tlag_minutos": round(rezago * 60.0, 2),
        "intervalo_calculo_min": intervalo_min if criterio == "hechms" else None,
        "motivo": "",
    }


# =============================================================================
# Caracterización por subcuenca
# =============================================================================
# Campos que HEC-HMS escribe en la exportación de subcuencas. Son los que su
# propio análisis de terreno derivó del mismo DEM, y aquí se usan como fuente de
# la trayectoria de flujo. No es un atajo: la longitud que piden las fórmulas de
# Tc es la del RECORRIDO DEL AGUA, que sale de las direcciones de flujo. La red
# del IGAC no sirve para eso a esta escala, porque no entra en las subcuencas
# pequeñas: de las 125, la mediana tiene 1,35 km2 y la menor 0,006 km2, y en
# ellas no hay ningún cauce cartografiado. Medirlas contra esa red daría
# longitud cero y ninguna fórmula aplicable.
#
# El contraste independiente existe y sale bien: la pendiente media ponderada
# por área que declara HEC-HMS es 23,1 % y la que este módulo calcula con Horn
# sobre el DEM es 24,2 %, un 4,7 % de diferencia entre dos cálculos que no
# comparten una sola línea de código.
CAMPO_LONGITUD = "long_len"      # recorrido de flujo más largo, en metros
CAMPO_PENDIENTE = "long_slo"     # su pendiente, en m/m
CAMPO_PENDIENTE_CUENCA = "basin_slo"
CAMPO_RELIEVE = "basin_rel"
CAMPO_UNIDADES = "len_units"
CAMPO_NOMBRE_SUB = "name"


def _numero(fila: dict, campo: str) -> float | None:
    """Lee un campo numérico del .dbf sin suponer que existe ni que es legible."""
    try:
        valor = float(str(fila.get(campo, "")).strip())
    except (TypeError, ValueError):
        return None
    return valor if math.isfinite(valor) else None



CAMPOS_CUENCA_COMPLETA = (
    CampoSalida("nombre", "Nombre", "texto", 60),
    CampoSalida("area_km2", "Área", "decimal", 14, 4, "km2"),
    CampoSalida("area_ha", "Área", "decimal", 14, 2, "ha"),
    CampoSalida("perim_km", "Perímetro", "decimal", 14, 3, "km"),
    CampoSalida("subcuencas", "Subcuencas que la componen", "entero", 6),
    CampoSalida("contornos", "Contornos exteriores", "entero", 4),
)


def escribir_cuenca_completa(ruta_subcuencas: Path, destino: Path,
                             nombre: str = "") -> dict[str, Any]:
    """
    Escribe el contorno disuelto de las subcuencas como una sola capa.

    LA CARTOGRAFIA LA NECESITA Y NADIE LA PRODUCIA. Hasta ahora el contorno de
    la cuenca habia que disolverlo a mano en QGIS, y el resultado quedaba en una
    capa TEMPORAL que el proyecto no guarda: al reabrirlo salia vacia y las
    planchas perdian el contorno sin avisar.

    NO ES UN DISUELTO GEOMETRICO SINO EL CONTORNO POR CONTEO: se conservan las
    aristas que aparecen una sola vez, que en un mosaico sin solapes son
    exactamente las del borde. Es el mismo calculo con que se mide el perimetro,
    de modo que la capa y la cifra del informe no pueden discrepar.

    SE ESCRIBEN TODOS LOS CONTORNOS COMO UNA ENTIDAD DE VARIAS PIEZAS. Una
    cuenca partida en dos trozos es un caso posible y quedarse con el mayor
    perderia el otro en silencio.

    Excepciones
    -----------
    ErrorRutas
        Si no esta la capa de subcuencas.
    ErrorHidrologia
        Si el mosaico no deja ningun contorno cerrado.
    """
    ruta_subcuencas = Path(ruta_subcuencas)
    if not ruta_subcuencas.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta_subcuencas}: sin las subcuencas no hay "
            "contorno de cuenca que escribir.")

    poligonos = shapefile.leer_geometrias(ruta_subcuencas)
    anillos = geometria.contorno_exterior(poligonos)
    if not anillos:
        raise ErrorHidrologia(
            f"{ruta_subcuencas.name} no deja ningun contorno cerrado: la capa "
            "tiene solapes o huecos y el disuelto no es fiable.")

    area_m2 = sum(abs(geometria._area_con_signo(a)) for a in anillos)
    perimetro_m = sum(geometria._longitud_de_cadena(a) for a in anillos)
    atributos = {
        "nombre": nombre or ruta_subcuencas.stem,
        "area_km2": round(area_m2 / 1e6, 4),
        "area_ha": round(area_m2 / 1e4, 2),
        "perim_km": round(perimetro_m / 1000.0, 3),
        "subcuencas": len(poligonos),
        "contornos": len(anillos),
    }

    # 'conservar' y no 'primero_exterior': los contornos son piezas separadas y
    # no un exterior con huecos, y la regla de huecos invertiria el sentido de
    # todas menos la primera, restando su area en silencio.
    shapefile.escribir_poligonos(
        destino, [list(anillos)], CAMPOS_CUENCA_COMPLETA, [atributos],
        shapefile.leer_shapefile(ruta_subcuencas).crs_wkt or "",
        estructura=shapefile.ESTRUCTURA_CONSERVAR)
    return atributos

def parametros_por_subcuenca(ruta: Path) -> list[dict[str, Any]]:
    """
    Geometría y trayectoria de flujo de cada subcuenca.

    El área y el perímetro salen de la geometría; la longitud y la pendiente del
    recorrido de flujo, de los atributos que HEC-HMS calculó sobre el terreno.

    Se comprueba que las unidades declaradas sean metros. Un shapefile en pies
    no da error en ninguna parte y multiplica la longitud por 3,28, que en
    Kirpich (L elevado a 0,77) se traduce en un Tc 2,4 veces mayor.

    Excepciones
    -----------
    ErrorHidrologia
        Si la capa declara unidades distintas de metros.
    """
    areas = shapefile.areas_poligonos(ruta)
    entidades = shapefile.leer_geometrias(ruta)
    registros = list(shapefile.leer_registros(ruta))

    unidades = {str(f.get(CAMPO_UNIDADES, "")).strip().lower()
                for f in registros} - {""}
    if unidades and not unidades <= {"metre", "meter", "metros", "m"}:
        raise ErrorHidrologia(
            f"la capa de subcuencas declara unidades {sorted(unidades)} y se "
            "esperan metros. Una longitud en pies no da error en ninguna parte "
            "y multiplica el tiempo de concentración por más de dos.")

    subcuencas: list[dict[str, Any]] = []
    for indice, (area_m2, anillos) in enumerate(zip(areas, entidades)):
        fila = registros[indice] if indice < len(registros) else {}
        nombre = str(fila.get(CAMPO_NOMBRE_SUB, "")).strip() or f"S{indice + 1}"
        perimetro_m = sum(
            math.hypot(otro[0] - uno[0], otro[1] - uno[1])
            for anillo in anillos for uno, otro in zip(anillo, anillo[1:]))
        area_km2 = area_m2 / 1e6
        longitud_m = _numero(fila, CAMPO_LONGITUD)
        pendiente = _numero(fila, CAMPO_PENDIENTE)
        subcuencas.append({
            "subcuenca": nombre,
            "area_km2": round(area_km2, 4),
            # LA HECTAREA NO ES UNA UNIDAD DE ADORNO. El informe tabula el área
            # en las dos, y es la unidad en que se contrata y se compara con
            # los usos del suelo. Se deriva aquí y no en el informe: convertir
            # al escribir dejaría el factor repartido por la cadena.
            "area_ha": round(area_km2 * 100.0, 2),
            "perimetro_km": round(perimetro_m / 1000.0, 3),
            "long_flujo_km": round(longitud_m / 1000.0, 4)
            if longitud_m else None,
            "pendiente_flujo": round(pendiente, 5) if pendiente else None,
            "desnivel_flujo_m": round(longitud_m * pendiente, 2)
            if longitud_m and pendiente else None,
            "pendiente_cuenca": _numero(fila, CAMPO_PENDIENTE_CUENCA),
            "relieve_m": _numero(fila, CAMPO_RELIEVE),
            "origen_trayectoria": "hec_hms",
        })
        # LA CARACTERIZACION VA POR SUBCUENCA. Compacidad, forma, longitud axial
        # y ancho medio se calculaban solo para la cuenca entera, y son los
        # parametros que el informe presenta unidad por unidad.
        subcuencas[-1].update(parametros_de_forma_de(
            area_km2, perimetro_m / 1000.0,
            _longitud_axial_de(anillos) / 1000.0))
    return subcuencas


def _longitud_axial_de(anillos) -> float:
    """
    Mayor distancia entre dos vértices del polígono, en metros.

    Se pasa primero por la envolvente convexa: la distancia máxima entre dos
    puntos de un conjunto se alcanza siempre entre dos vértices de su
    envolvente, y eso baja el coste de cuadrático sobre miles de vértices a
    cuadrático sobre unas decenas.
    """
    puntos = [p for anillo in anillos for p in anillo]
    if len(puntos) < 2:
        return 0.0
    casco = shapefile._envolvente_convexa(puntos) or puntos
    mayor = 0.0
    for indice, uno in enumerate(casco):
        for otro in casco[indice + 1:]:
            distancia = math.hypot(otro[0] - uno[0], otro[1] - uno[1])
            if distancia > mayor:
                mayor = distancia
    return mayor


def parametros_de_forma_de(area_km2: float, perimetro_km: float,
                           axial_km: float) -> dict[str, Any]:
    """
    Compacidad, forma y ancho medio de una unidad, con las mismas fórmulas que
    la cuenca.

    SE CALCULAN IGUAL EN LOS DOS NIVELES a propósito. Un coeficiente de
    Gravelius de la cuenca obtenido de una manera y el de sus subcuencas de
    otra no serían comparables, y el informe los presenta en la misma tabla.
    """
    forma: dict[str, Any] = {
        "longitud_axial_km": round(axial_km, 4) if axial_km else None,
        "ancho_medio_km": None,
        "coef_forma": None,
        "coef_compacidad": None,
    }
    if axial_km and axial_km > 0:
        forma["ancho_medio_km"] = round(area_km2 / axial_km, 4)
        # Coeficiente de forma de Horton: área sobre el cuadrado de la longitud.
        forma["coef_forma"] = round(area_km2 / (axial_km ** 2), 4)
    if area_km2 > 0 and perimetro_km:
        # Gravelius: 0,2821 es 1/(2*sqrt(pi)) con las unidades en km y km2.
        forma["coef_compacidad"] = round(
            0.2821 * perimetro_km / math.sqrt(area_km2), 4)
    return forma


def _punto_medio(linea, largo: float) -> tuple[float, float]:
    """Punto que deja la mitad de la longitud a cada lado de la polilinea."""
    objetivo = largo / 2.0
    recorrido = 0.0
    for uno, otro in zip(linea, linea[1:]):
        tramo = math.hypot(otro[0] - uno[0], otro[1] - uno[1])
        if recorrido + tramo >= objetivo and tramo > 0:
            fraccion = (objetivo - recorrido) / tramo
            return (uno[0] + fraccion * (otro[0] - uno[0]),
                    uno[1] + fraccion * (otro[1] - uno[1]))
        recorrido += tramo
    return linea[-1]


# Codificacion de r.watershed de GRASS. Los ocho vecinos, en sentido
# antihorario desde el noreste, tal como los numera el algoritmo:
#
#     3 2 1
#     4   8
#     5 6 7
#
# El signo negativo marca una celda que desagua fuera del borde de la region, y
# la direccion es la misma: se toma el valor absoluto.
_VECINO_DE_DIRECCION = {
    1: (-1, 1), 2: (-1, 0), 3: (-1, -1), 4: (0, -1),
    5: (1, -1), 6: (1, 0), 7: (1, 1), 8: (0, 1),
}


def _celdas_de_poligono(anillos, info) -> set[tuple[int, int]]:
    """Celdas del ráster cuyo centro cae dentro del polígono."""
    aristas = geometria.aristas_de([anillos])
    if not aristas:
        return set()
    x_min, y_min, x_max, y_max = geometria.envolvente([anillos])
    fila_inicial = max(0, info.fila_de(y_max))
    fila_final = min(info.alto - 1, info.fila_de(y_min))
    celdas: set[tuple[int, int]] = set()
    for fila in range(fila_inicial, fila_final + 1):
        y = info.y_de_fila(fila)
        for desde, hasta in geometria.columnas_de_fila(
                aristas, y, info.origen_x, info.tamano_x, info.ancho):
            for columna in range(desde, hasta + 1):
                celdas.add((fila, columna))
    return celdas


def recorrido_de_flujo(direcciones, info, celdas) -> dict[str, Any] | None:
    """
    Recorrido más largo del agua dentro de un conjunto de celdas.

    Devuelve longitud del recorrido, distancia recta entre sus extremos y la
    sinuosidad, o None si no hay recorrido medible.

    ES LA LONGITUD QUE RECORRE EL AGUA, no la del cauce cartografiado. Se sigue
    la dirección de flujo celda a celda desde el punto más remoto hasta la
    salida de la unidad, que es la definición de 'longest flow path' y la misma
    magnitud que HEC-HMS calcula como 'long_len': las dos son contrastables.

    LA DIAGONAL MIDE MAS QUE EL LADO. Contar celdas y multiplicar por el tamaño
    subestima el recorrido en un 41 por ciento en los tramos diagonales, que en
    terreno de montaña son la mayoría.

    El recorrido se calcula hacia AGUAS ABAJO con memoria, no hacia aguas
    arriba: cada celda tiene una sola salida y muchas entradas, de modo que el
    grafo se recorre una vez y no una por cada cabecera.
    """
    if not celdas:
        return None
    lado_x = abs(info.tamano_x)
    lado_y = abs(info.tamano_y)
    diagonal = math.hypot(lado_x, lado_y)

    def aguas_abajo(fila, columna):
        """Celda a la que drena esta, y la distancia recorrida al hacerlo."""
        if not (0 <= fila < info.alto and 0 <= columna < info.ancho):
            return None, 0.0
        valor = int(direcciones[fila][columna])
        salto = _VECINO_DE_DIRECCION.get(abs(valor))
        if salto is None:
            return None, 0.0
        siguiente = (fila + salto[0], columna + salto[1])
        if siguiente not in celdas:
            return None, 0.0
        distancia = (diagonal if salto[0] and salto[1]
                     else (lado_y if salto[0] else lado_x))
        return siguiente, distancia

    # Distancia de cada celda hasta la salida de la unidad, sin recursión: una
    # cuenca alargada encadena miles de celdas y el límite de pila de Python se
    # alcanza mucho antes.
    recorrido: dict[tuple[int, int], float] = {}
    for celda in celdas:
        if celda in recorrido:
            continue
        pila = []
        actual = celda
        visitadas = set()
        while actual is not None and actual not in recorrido:
            if actual in visitadas:      # ciclo: se corta y se declara final
                recorrido[actual] = 0.0
                break
            visitadas.add(actual)
            siguiente, distancia = aguas_abajo(*actual)
            if siguiente is None:
                recorrido[actual] = 0.0
                break
            pila.append((actual, distancia))
            actual = siguiente
        acumulado = recorrido.get(actual, 0.0) if actual is not None else 0.0
        for celda_previa, distancia in reversed(pila):
            acumulado += distancia
            recorrido[celda_previa] = acumulado

    if not recorrido:
        return None
    origen = max(recorrido, key=recorrido.get)
    largo = recorrido[origen]
    if largo <= 0:
        return None

    # Se sigue el recorrido para saber DONDE termina: la distancia recta va
    # entre sus dos extremos, no entre el origen y un punto cualquiera.
    actual = origen
    for _ in range(len(recorrido) + 1):
        siguiente, _distancia = aguas_abajo(*actual)
        if siguiente is None:
            break
        actual = siguiente
    x_origen = info.x_de_columna(origen[1])
    y_origen = info.y_de_fila(origen[0])
    x_final = info.x_de_columna(actual[1])
    y_final = info.y_de_fila(actual[0])
    recta = math.hypot(x_final - x_origen, y_final - y_origen)

    return {
        "long_recorrido_km": round(largo / 1000.0, 4),
        "distancia_recta_km": round(recta / 1000.0, 4),
        "indice_sinuosidad": round(largo / recta, 3) if recta > 0 else None,
        "celdas": len(celdas),
    }


def _contrastar_recorrido(subcuencas, resultado) -> None:
    """
    Contrasta el recorrido trazado contra el que declara HEC-HMS.

    SON DOS IMPLEMENTACIONES INDEPENDIENTES de la misma magnitud: HEC-HMS traza
    sobre su propio modelo relleno y con su propio umbral, y este modulo sobre
    la direccion de flujo del M02. Que coincidan es la comprobacion de que el
    trazado esta bien; que discrepen en una subcuenca concreta senala que su
    salida se resolvio en celdas distintas, y eso hay que verlo antes de usar la
    longitud en una formula de tiempo de concentracion.
    """
    pares = [(s["subcuenca"], s["long_flujo_km"], s["long_cauce_principal_km"])
             for s in subcuencas
             if s.get("long_flujo_km") and s.get("long_cauce_principal_km")]
    if not pares:
        return
    razones = sorted((trazado / declarado, nombre)
                     for nombre, declarado, trazado in pares if declarado > 0)
    if not razones:
        return
    mediana = razones[len(razones) // 2][0]
    dispares = [n for r, n in razones if r < 0.8 or r > 1.2]
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO if len(dispares) <= len(pares) // 4 else ADVERTENCIA,
        "morfometria.recorrido_contrastado",
        f"el recorrido trazado sobre la direccion de flujo coincide con el que "
        f"declara HEC-HMS en {len(pares) - len(dispares)} de {len(pares)} "
        f"subcuenca(s) dentro del 20 por ciento, con una razon mediana de "
        f"{mediana:.3f}. Son dos implementaciones independientes de la misma "
        "magnitud, de modo que la coincidencia es la comprobacion del trazado. "
        + (f"Discrepan {len(dispares)}: {dispares[:8]}. En ellas la salida se "
           "resolvio en celdas distintas y conviene mirarlas antes de usar la "
           "longitud en una formula de tiempo de concentracion."
           if dispares else ""),
    ))


def recorridos_por_subcuenca(ruta_direccion: Path, entidades,
                             nombres: Sequence[str],
                             logger=None) -> dict[str, dict[str, Any]]:
    """
    Longitud de recorrido, distancia recta y sinuosidad de cada subcuenca.

    El ráster se lee ENTERO una vez. Son 2.170 por 2.925 celdas de dos bytes,
    doce megabytes: leerlo por subcuenca supondría recorrerlo ciento veinticinco
    veces para acabar tocando las mismas celdas.
    """
    import struct

    ruta_direccion = Path(ruta_direccion)
    if not ruta_direccion.is_file():
        return {}
    try:
        info = raster.leer_info(ruta_direccion)
    except (ErrorFormato, ErrorRutas):
        return {}
    formato = {"<i2": "h", "<i4": "i", "<u1": "B", "<i1": "b",
               "<f4": "f"}.get(info.descriptor)
    if formato is None:
        return {}

    direcciones: list[Any] = []
    with raster.LectorRaster(ruta_direccion) as lector:
        for fila in range(info.alto):
            try:
                contenido = lector.fila(fila)
            except (ErrorFormato, ErrorRutas, IndexError):
                direcciones.append([0] * info.ancho)
                continue
            direcciones.append(list(struct.unpack_from(
                "<" + formato * info.ancho, contenido, 0)))

    salida: dict[str, dict[str, Any]] = {}
    for anillos, nombre in zip(entidades, nombres):
        celdas = _celdas_de_poligono(anillos, info)
        detalle = recorrido_de_flujo(direcciones, info, celdas)
        if detalle is not None:
            salida[str(nombre)] = detalle
    if logger is not None:
        logger.info("recorrido de flujo trazado en %d de %d subcuenca(s)",
                    len(salida), len(nombres))
    return salida


def drenaje_por_subcuenca(ruta_red: Path, entidades,
                          nombres: Sequence[str]) -> dict[str, dict[str, Any]]:
    """
    Densidad de drenaje, frecuencia de corrientes y orden de cada subcuenca.

    EL REPARTO ES POR SEGMENTO, no por tramo entero. Un tramo de la red es una
    corriente con nombre, y mide kilómetros: asignarlo completo a la subcuenca
    que contiene su punto medio le atribuye longitud que discurre por las
    vecinas. Medido sobre este estudio, ese reparto daba densidades de hasta
    83 km/km2, que no existe en ningún terreno, y dejaba 42 de las 125
    subcuencas sin drenaje porque la red solo tiene 230 corrientes dentro de la
    cuenca y no alcanza a una por unidad.

    Repartiendo segmento a segmento, la unidad de asignación pasa de kilómetros
    a la veintena de metros que separa dos vértices, y el error queda por debajo
    de la resolución del modelo de elevación del que sale la propia red.

    UNA CORRIENTE SE CUENTA UNA SOLA VEZ, en la subcuenca donde discurre la
    mayor parte de su longitud. Contarla en cada subcuenca que atraviesa
    inflaría la frecuencia de corrientes en las cuencas de aguas abajo, que son
    justo las que cruzan los cauces largos.

    Devuelve un diccionario por nombre de subcuenca. Las que no reciben ningún
    segmento quedan fuera: densidad cero y densidad desconocida no son lo mismo.
    """
    ruta_red = Path(ruta_red)
    if not ruta_red.is_file():
        return {}
    try:
        tramos = shapefile.leer_geometrias(ruta_red)
        info = shapefile.leer_shapefile(ruta_red)
        campo_orden = next((c for c in ("orden", "ORDEN", "strahler")
                            if info.tiene_campo(c)), "")
        registros = (shapefile.leer_registros(ruta_red, [campo_orden])
                     if campo_orden else [{} for _ in tramos])
    except (ErrorFormato, ErrorRutas):
        return {}

    # Envolvente de cada subcuenca, para descartar sin evaluar el polígono.
    cajas = [geometria.envolvente([anillos]) for anillos in entidades]

    acumulado: dict[int, dict[str, Any]] = {}
    for tramo, registro in zip(tramos, registros):
        try:
            orden = int(float(registro.get(campo_orden) or 0))
        except (TypeError, ValueError):
            orden = 0
        por_unidad: dict[int, float] = {}
        for parte in tramo:
            for uno, otro in zip(parte, parte[1:]):
                largo = math.hypot(otro[0] - uno[0], otro[1] - uno[1])
                if largo <= 0:
                    continue
                medio = ((uno[0] + otro[0]) / 2.0, (uno[1] + otro[1]) / 2.0)
                for indice, (anillos, caja) in enumerate(zip(entidades, cajas)):
                    if not (caja[0] <= medio[0] <= caja[2]
                            and caja[1] <= medio[1] <= caja[3]):
                        continue
                    if not geometria.punto_en_poligono(medio[0], medio[1],
                                                       anillos):
                        continue
                    por_unidad[indice] = por_unidad.get(indice, 0.0) + largo
                    break
        if not por_unidad:
            continue
        principal = max(por_unidad, key=por_unidad.get)
        for indice, largo in por_unidad.items():
            destino = acumulado.setdefault(
                indice, {"longitud_m": 0.0, "corrientes": 0, "orden": 0})
            destino["longitud_m"] += largo
            destino["orden"] = max(destino["orden"], orden)
            if indice == principal:
                destino["corrientes"] += 1
    return {nombres[i]: v for i, v in acumulado.items() if i < len(nombres)}


def relieve_por_subcuenca(ruta_dem: Path, entidades) -> list[dict[str, Any]]:
    """
    Cotas de cada subcuenca, en un solo recorrido del DEM.

    Giandotti necesita la cota media SOBRE la salida, y sin ella se pierde una
    fórmula de las pocas que aplican a subcuencas de este tamaño.

    Se recorre el ráster una vez y en cada fila solo se evalúan las subcuencas
    cuya envolvente vertical la alcanza. Sin ese filtro, ciento veinticinco
    barridos por fila sobre un DEM de miles de filas multiplican el coste por
    dos órdenes de magnitud sin cambiar el resultado.
    """
    import numpy as np  # noqa: PLC0415

    info = raster.leer_info(ruta_dem)
    nodato = info.nodato

    preparadas = []
    for anillos in entidades:
        poligono = [list(anillo) for anillo in anillos]
        aristas = geometria.aristas_de([poligono])
        if not aristas:
            preparadas.append(None)
            continue
        _, ymin, _, ymax = geometria.envolvente([poligono])
        preparadas.append({
            "aristas": aristas,
            "fila_ini": max(0, info.fila_de(ymax)),
            "fila_fin": min(info.alto - 1, info.fila_de(ymin)),
        })

    acumulado = [{"celdas": 0, "suma": 0.0,
                  "minimo": float("inf"), "maximo": float("-inf")}
                 for _ in preparadas]

    filas_activas = [p for p in preparadas if p]
    if not filas_activas:
        return [dict(cota_min=None, cota_max=None, cota_media=None,
                     celdas_dem=0) for _ in preparadas]
    fila_ini = min(p["fila_ini"] for p in filas_activas)
    fila_fin = max(p["fila_fin"] for p in filas_activas)

    with raster.LectorRaster(ruta_dem) as lector:
        for fila in range(fila_ini, fila_fin + 1):
            candidatas = [i for i, p in enumerate(preparadas)
                          if p and p["fila_ini"] <= fila <= p["fila_fin"]]
            if not candidatas:
                continue
            z = np.frombuffer(lector.fila(fila), dtype=info.descriptor)
            for indice in candidatas:
                mascara = _mascara_de_fila(
                    info, preparadas[indice]["aristas"], fila, np)
                if not mascara.any():
                    continue
                if nodato is not None:
                    mascara &= z != nodato
                valores = z[mascara]
                if not valores.size:
                    continue
                registro = acumulado[indice]
                registro["celdas"] += int(valores.size)
                registro["suma"] += float(valores.sum(dtype=np.float64))
                registro["minimo"] = min(registro["minimo"],
                                         float(valores.min()))
                registro["maximo"] = max(registro["maximo"],
                                         float(valores.max()))

    salida = []
    for registro in acumulado:
        if not registro["celdas"]:
            salida.append({"cota_min": None, "cota_max": None,
                           "cota_media": None, "celdas_dem": 0})
            continue
        salida.append({
            "cota_min": round(registro["minimo"], 2),
            "cota_max": round(registro["maximo"], 2),
            "cota_media": round(registro["suma"] / registro["celdas"], 2),
            "celdas_dem": registro["celdas"],
        })
    return salida


def tiempos_de_subcuenca(
    subcuenca: dict[str, Any],
    filas_matriz,
    minimo_formulas: int,
    cv_maximo: float,
    criterio_rezago: str,
    intervalo_min: float,
    formula_adoptada: str = "",
    respaldo_fuera_de_rango: bool = False,
) -> dict[str, Any]:
    """
    Tiempo de concentración y de rezago de UNA subcuenca.

    Se aplica la misma regla que a la cuenca completa (CLAUDE.md, sección 7):
    matriz de aplicabilidad, mínimo de fórmulas y control de dispersión, y la
    mediana del subconjunto aplicable. La diferencia es que aquí la regla suele
    poder cumplirse: las fórmulas de Tc se calibraron en cuencas pequeñas, que
    es justo lo que son las subcuencas, mientras que la cuenca completa de 220
    km2 queda fuera del rango de casi todas.

    El rezago se compara con el intervalo de cálculo. Un rezago por debajo del
    paso de tiempo no produce un pico pequeño: produce un pico que el modelo no
    puede representar, y esa subcuenca aporta un hidrograma sin sentido.
    """
    magnitudes = {
        "area_km2": subcuenca.get("area_km2"),
        "longitud_km": subcuenca.get("long_flujo_km"),
        "pendiente": subcuenca.get("pendiente_flujo"),
        "desnivel_m": subcuenca.get("desnivel_flujo_m"),
        "cota_media_m": subcuenca.get("cota_media_sobre_salida_m"),
        "cn": subcuenca.get("cn"),
    }
    evaluadas = evaluar_aplicabilidad(
        filas_matriz, subcuenca.get("area_km2"),
        subcuenca.get("pendiente_flujo"), magnitudes)
    resumen = resumir_adopcion(evaluadas, minimo_formulas, cv_maximo)

    # UNA FORMULA DECLARADA MANDA SOBRE LA MEDIANA. La seccion 7 exige coherencia
    # entre el parametro calculado y el metodo de transformacion de HEC-HMS: con
    # 'scs_uh' la formula coherente es la de retardo del SCS, que no promedia
    # trece formulas calibradas en poblaciones distintas sino que usa la del
    # propio metodo. Medido en esta cuenca, la mediana no era adoptable en 116
    # de 124 subcuencas por dispersion, con valores entre 0,55 y 6,30 h en una
    # misma unidad: promediar eso no da un valor central, da uno arbitrario.
    #
    # La formula declarada tiene que seguir siendo APLICABLE segun la matriz.
    # Declararla no exime de su rango de calibracion.
    if formula_adoptada:
        elegida = next((e for e in evaluadas
                        if e["formula"] == formula_adoptada), None)
        if elegida is not None and elegida["aplicable"] \
                and elegida.get("tc_horas") is not None:
            resumen = dict(resumen, procede_adoptar=True,
                           criterio=f"formula {formula_adoptada}",
                           tc_horas=elegida["tc_horas"],
                           tc_minutos=elegida["tc_minutos"])
        else:
            motivo = "no aplicable" if elegida is not None and not elegida[
                "aplicable"] else "no calculable"
            resumen = dict(resumen, procede_adoptar=False,
                           criterio=f"formula {formula_adoptada}",
                           tc_horas=None, tc_minutos=None,
                           motivo_formula=(
                               f"{formula_adoptada}: {motivo}"
                               + (f" ({elegida['motivo']})" if elegida
                                  and elegida.get("motivo") else "")))

    tc_horas = resumen.get("tc_horas")

    # RESPALDO PARA LAS QUE QUEDAN FUERA DE RANGO. Sin valor, esas subcuencas no
    # entran en HEC-HMS y el modelo se detiene: el consultor decidio incluirlas
    # con un parametro de referencia antes que dejarlas fuera. Se usa la MISMA
    # formula declarada, aplicada fuera de su rango de calibracion, y se marca
    # como tal para que el informe no la presente como equivalente a las demas.
    # Extrapolar una formula empirica y decirlo es defendible; hacerlo callando
    # no lo es.
    fuera_de_rango = False
    if tc_horas is None and formula_adoptada and respaldo_fuera_de_rango:
        elegida = next((e for e in evaluadas
                        if e["formula"] == formula_adoptada), None)
        if elegida is not None and elegida.get("tc_horas") is not None:
            tc_horas = elegida["tc_horas"]
            fuera_de_rango = True

    rezago = tiempo_de_rezago(tc_horas, criterio_rezago, intervalo_min)
    tlag_min = rezago.get("tlag_minutos")

    if resumen["procede_adoptar"]:
        motivo = ""
    elif resumen.get("motivo_formula"):
        motivo = resumen["motivo_formula"]
    elif resumen["formulas_aplicables"] < minimo_formulas:
        motivo = "menos formulas aplicables que el minimo"
    elif resumen["dispersion_excesiva"]:
        motivo = "dispersion excesiva"
    else:
        motivo = "ninguna formula aplicable se pudo calcular"

    return {
        "formulas_aplicables": resumen["formulas_aplicables"],
        "formulas_adoptables": resumen["formulas_adoptables"],
        "cv": resumen.get("estadisticos", {}).get("cv"),
        "tc_horas": tc_horas,
        "tc_minutos": round(tc_horas * 60.0, 2) if tc_horas else None,
        "procede_adoptar": resumen["procede_adoptar"] or fuera_de_rango,
        "tc_fuera_de_rango": fuera_de_rango,
        "criterio_tc": ("formula fuera de su rango de calibracion"
                        if fuera_de_rango else resumen.get("criterio")),
        "motivo_sin_tc": motivo,
        "tlag_horas": rezago.get("tlag_horas"),
        "tlag_minutos": tlag_min,
        "tlag_bajo_el_intervalo": bool(
            tlag_min is not None and tlag_min < intervalo_min),
        "evaluadas": evaluadas,
    }


def _magnitudes_de_la_cuenca(parametros, resultado) -> dict[str, Any]:
    """
    Reúne lo que las fórmulas de Tc necesitan, con las unidades que esperan.

    La cota media que pide Giandotti es la media de la cuenca SOBRE la cota de
    salida, no la cota media absoluta. Usar la absoluta sobrestima el
    denominador y acorta el tiempo, que es el sentido inseguro.
    """
    relieve = resultado.relieve
    drenaje = resultado.drenaje
    cota_salida = (drenaje.get("cota_cierre")
                   if drenaje.get("cota_cierre") is not None
                   else relieve.get("cota_min"))
    cota_media_sobre_salida = None
    if relieve.get("cota_media") is not None and cota_salida is not None:
        cota_media_sobre_salida = relieve["cota_media"] - cota_salida

    return {
        "area_km2": parametros.get("area_km2"),
        "longitud_km": drenaje.get("long_cauce_principal_km"),
        "pendiente": drenaje.get("pendiente_media_cauce"),
        "desnivel_m": drenaje.get("desnivel_cauce_m"),
        "cota_media_m": cota_media_sobre_salida,
        "cn": resultado.suelos.get("cn_ponderado"),
    }


def _resolver_rezago(resultado, configuracion, logger) -> None:
    """Registra el tiempo de rezago y comprueba su coherencia con HEC-HMS."""
    rezago = resultado.rezago
    if rezago.get("tlag_horas") is None:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "tiempo_rezago.sin_valor",
            f"no se calculo el tiempo de rezago: {rezago.get('motivo')}. "
            "Depende del tiempo de concentracion adoptado.",
        ))
        return

    logger.info("Tiempo de rezago %.3f h (%.1f min) por criterio %s",
                rezago["tlag_horas"], rezago["tlag_minutos"],
                rezago["criterio"])
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "tiempo_rezago.adoptado",
        f"tiempo de rezago {rezago['tlag_horas']:.3f} h "
        f"({rezago['tlag_minutos']:.1f} min), criterio {rezago['criterio']}."
        + (" Incluye el termino Δt/2 con el INTERVALO DE CALCULO de "
           f"{rezago['intervalo_calculo_min']:.0f} min, no la duracion de la "
           "tormenta." if rezago["criterio"] == "hechms" else ""),
    ))

    if not bool(configuracion.obtener(
            "tiempo_rezago.validar_coherencia_con_transform")):
        return

    transformacion = str(configuracion.obtener("hec_hms.transform", "")).strip()
    if transformacion == "scs_uh" and rezago["criterio"] not in ("scs", "hechms"):
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "tiempo_rezago.coherencia",
            f"el metodo de transformacion declarado es {transformacion!r}, que "
            "trabaja con tiempo de rezago, y el criterio de rezago es "
            f"{rezago['criterio']!r}. CLAUDE.md, seccion 7, exige verificar esa "
            "coherencia.",
        ))
    elif transformacion in ("clark", "modclark") and rezago["criterio"] != "scs":
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "tiempo_rezago.coherencia",
            f"la transformacion {transformacion!r} usa el tiempo de "
            "CONCENTRACION y el coeficiente de almacenamiento, no el de rezago. "
            "El rezago se reporta de todos modos, pero no es el parametro que "
            "consume el modelo.",
        ))


def _resolver_numero_curva(configuracion, base, resultado, poligonos,
                           parametros, logger) -> None:
    """Grupo hidrológico desde la capa de suelos y, si es posible, el CN."""
    aportado = rutas.resolver(
        configuracion.obtener("referencia_nacional.salida_recorte_suelos"), base)
    if aportado.is_file():
        ruta_suelos = aportado
        procedencia = "recorte del area"
    else:
        directorio = Path(configuracion.obtener("referencia_nacional.directorio"))
        ruta_suelos = directorio / str(
            configuracion.obtener("referencia_nacional.suelos_hsg"))
        procedencia = "capa de base nacional"

    if not ruta_suelos.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "numero_curva.suelos",
            f"no se encuentra la capa de suelos en {ruta_suelos}: no hay grupo "
            "hidrologico ni numero de curva.",
        ))
        return

    try:
        suelos = grupos_hidrologicos(
            ruta_suelos, poligonos,
            str(configuracion.obtener("crs.calculo")),
            paso_m=float(configuracion.obtener("numero_curva.muestreo_suelos_m")),
            duales=str(configuracion.obtener("numero_curva.grupos_duales")))
    except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "numero_curva.suelos",
            f"no se pudo leer el grupo hidrologico: {error}"))
        return

    suelos["procedencia"] = procedencia
    suelos["raster"] = rutas.relativa(Path(suelos["raster"]), base) \
        if Path(suelos["raster"]).is_relative_to(base) else suelos["raster"]
    resultado.suelos = suelos

    reparto = "; ".join(f"{r['grupo']} {r['porcentaje']:.1f} %"
                        for r in suelos["reparto"])
    logger.info("Grupo hidrologico dominante %s | %s",
                suelos["grupo_dominante"], reparto)

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "numero_curva.grupo_hidrologico",
        f"grupo hidrologico de suelo leido de la {procedencia}, sobre "
        f"{suelos['muestras_validas']:,} muestras a {suelos['paso_muestreo_m']:.0f} m "
        f"({suelos['cobertura_pct']:.1f} % de la malla con dato). Reparto: "
        f"{reparto}. Dominante {suelos['grupo_dominante']}.",
    ))

    if suelos["pct_dual"] > 0:
        criterio = suelos["criterio_duales"]
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "numero_curva.grupos_duales",
            f"el {suelos['pct_dual']:.1f} % del area cae en grupos DUALES, cuyo "
            "grupo depende de si el suelo esta drenado. Se adopto el criterio "
            f"{criterio!r}, que "
            + ("los asigna al grupo D, el mas desfavorable."
               if criterio == "no_drenado" else
               "los asigna a su grupo drenado, el mas favorable.")
            + " La eleccion cambia el numero de curva en decenas de unidades "
            "sobre esa fraccion del area y debe quedar declarada en el informe. "
            "Si el estudio dispone de informacion de drenaje, esta es la "
            "decision que debe sustituirla.",
        ))

    if suelos["procedencia"] == "capa de base nacional":
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "numero_curva.procedencia",
            "el grupo hidrologico procede de una capa GLOBAL de 250 m y no de "
            "un estudio de suelos del proyecto. Sirve para una caracterizacion "
            "general; el informe debe distinguirlo de un levantamiento propio.",
        ))

    # --- Numero de curva ------------------------------------------------------
    faltan = []
    for clave in ("numero_curva.tabla_cn",
                  "numero_curva.homologacion_cobertura",
                  "referencia_nacional.salida_recorte_cobertura"):
        destino = rutas.resolver(configuracion.obtener(clave), base)
        if not destino.is_file():
            faltan.append(f"{clave} ({rutas.relativa(destino, base)})")

    if faltan:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "numero_curva.sin_calcular",
            "el numero de curva NO se calculo. El grupo hidrologico esta "
            "resuelto, pero el CN exige ademas la cobertura y la tabla que "
            "cruza cobertura con grupo. Falta: " + "; ".join(faltan)
            + ". La homologacion de clases de cobertura a clases del SCS es "
            "una decision del consultor y no puede derivarse sin criterio: la "
            "misma clase Corine admite numeros de curva distintos segun como se "
            "interprete su condicion hidrologica.",
        ))


def leer_clasificacion(ruta: Path,
                       delimitador: str) -> dict[str, list[dict[str, Any]]]:
    """
    Rangos con que se nombra cada parámetro morfométrico.

    ES DOCTRINA Y VIVE EN data/referencia. Están transcritos de las tablas de
    interpretación de la plantilla del consultor, que son las que el informe
    cita: no se inventan aquí ni se toman de un manual distinto del suyo.

    'desde' vacío significa sin límite inferior y 'hasta' vacío sin límite
    superior. Devuelve las clases de cada parámetro ordenadas por 'desde'.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si una clase no trae nombre o sus límites no son números.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra la tabla de clasificacion en {ruta}.")

    def limite(texto: Any) -> float | None:
        texto = str(texto or "").strip()
        return float(texto) if texto else None

    tabla: dict[str, list[dict[str, Any]]] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for numero, fila in enumerate(
                csv.DictReader(manejador, delimiter=delimitador), start=2):
            parametro = str(fila.get("parametro", "")).strip()
            if not parametro or parametro.startswith("#"):
                continue
            clase = str(fila.get("clase", "")).strip()
            if not clase:
                raise ErrorFormato(
                    f"la fila {numero} de {ruta.name} no trae nombre de clase.")
            try:
                entrada = {"desde": limite(fila.get("desde")),
                           "hasta": limite(fila.get("hasta")),
                           "clase": clase,
                           "origen": str(fila.get("origen", "")).strip()}
            except ValueError as exc:
                raise ErrorFormato(
                    f"la fila {numero} de {ruta.name} tiene un limite que no "
                    f"es un numero: {exc}.") from exc
            tabla.setdefault(parametro, []).append(entrada)
    if not tabla:
        raise ErrorFormato(f"{ruta.name} no contiene ninguna clase.")
    for clases in tabla.values():
        clases.sort(key=lambda c: (c["desde"] is not None, c["desde"] or 0.0))
    return tabla


CLASIFICABLES: tuple[tuple[str, str, str], ...] = (
    # (columna del valor, parametro de la tabla, columna de la clase)
    ("coef_forma", "coef_forma", "clase_forma"),
    ("coef_compacidad", "coef_compacidad", "clase_compacidad"),
    ("indice_sinuosidad", "indice_sinuosidad", "clase_sinuosidad"),
    ("pendiente_flujo_pct", "pendiente_cauce_pct", "clase_pendiente_cauce"),
    ("pendiente_cuenca_pct", "pendiente_cuenca_pct", "clase_pendiente_cuenca"),
)


def clasificar_subcuencas(subcuencas: Sequence[dict[str, Any]],
                          tabla: dict[str, list[dict[str, Any]]],
                          ) -> dict[str, int]:
    """
    Escribe la pendiente en por ciento y el nombre de cada parámetro.

    LA PENDIENTE SE GUARDA EN LAS DOS UNIDADES. La cadena la calcula en m/m,
    que es lo que piden las fórmulas de tiempo de concentración, y las tablas
    de clasificación y del informe están en por ciento. Sin la columna en por
    ciento, la tabla del informe mostraría 0.43 bajo un encabezado que dice
    'Pendiente (%)', y una pendiente del 43% se leería como del 0,43%.

    Devuelve cuántas subcuencas quedaron sin clase en cada parámetro, que es lo
    que el reporte necesita para decir que la tabla del consultor no cubre todo
    el rango del estudio.
    """
    for subcuenca in subcuencas:
        for origen, destino in (("pendiente_flujo", "pendiente_flujo_pct"),
                                ("pendiente_cuenca", "pendiente_cuenca_pct")):
            valor = subcuenca.get(origen)
            subcuenca[destino] = (round(float(valor) * 100.0, 2)
                                  if valor not in (None, "") else None)

    sin_clase: dict[str, int] = {}
    for columna, parametro, destino in CLASIFICABLES:
        clases = tabla.get(parametro) or []
        for subcuenca in subcuencas:
            nombre = clasificar_valor(subcuenca.get(columna), clases)
            subcuenca[destino] = nombre
            if not nombre and subcuenca.get(columna) not in (None, ""):
                sin_clase[parametro] = sin_clase.get(parametro, 0) + 1
    return sin_clase


def clasificar_valor(valor: Any, clases: Sequence[dict[str, Any]]) -> str:
    """
    Cómo se llama un valor según los rangos declarados.

    'desde' INCLUSIVE Y 'hasta' EXCLUSIVO. Las tablas del consultor encadenan
    los límites ('0.22 - 0.30' y luego '0.30 - 0.37'), de modo que sin una
    convención un valor que cae justo en el borde pertenecería a dos clases y
    el resultado dependería del orden en que se recorren.

    LO QUE NO CAE EN NINGUN RANGO DEVUELVE CADENA VACIA, no la clase más
    cercana. La tabla de sinuosidad del consultor se cierra en 1.50 y 19 de las
    125 subcuencas de este estudio la superan: adjudicarles 'Meandriforme'
    sería extender su doctrina sin decirlo.
    """
    if valor is None or valor == "":
        return ""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return ""
    for entrada in clases:
        desde, hasta = entrada["desde"], entrada["hasta"]
        if desde is not None and numero < desde:
            continue
        if hasta is not None and numero >= hasta:
            continue
        return str(entrada["clase"])
    return ""


def leer_tabla_cn(ruta: Path, delimitador: str) -> dict[str, dict[str, float]]:
    """
    Tabla del SCS: número de curva de cada clase de cobertura por grupo de suelo.

    Es doctrina y vive en data/referencia. Se lee entera, con su origen, para
    que el informe pueda citar de dónde sale cada cifra.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si a una clase le falta el CN de algún grupo.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la tabla de CN en {ruta}.")
    tabla: dict[str, dict[str, float]] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            clase = str(fila.get("clase", "")).strip()
            if not clase or clase.startswith("#"):
                continue
            valores: dict[str, float] = {}
            for grupo in ("A", "B", "C", "D"):
                try:
                    valores[grupo] = float(fila[f"cn_{grupo.lower()}"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ErrorFormato(
                        f"la clase {clase!r} de {ruta.name} no trae un CN "
                        f"legible para el grupo {grupo}: {exc}.") from exc
            valores["descripcion"] = str(fila.get("descripcion", "")).strip()
            valores["origen"] = str(fila.get("origen", "")).strip()
            tabla[clase] = valores
    if not tabla:
        raise ErrorFormato(f"{ruta.name} no contiene ninguna clase.")
    return tabla


def leer_homologacion_cobertura(
    ruta: Path, delimitador: str,
) -> dict[str, str]:
    """
    Correspondencia entre cada clase de la capa de cobertura y una del SCS.

    La diligencia el consultor. Es el punto donde una decisión de criterio entra
    en el número de curva, y por eso no se deduce: la misma clase Corine admite
    valores muy distintos según cómo se interprete su condición hidrológica.
    Sobre esta cuenca, 'Pastos limpios' cubre el 18% del área y separar buena de
    mala condición son veinte unidades de CN.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si no hay ninguna fila diligenciada.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la homologación en {ruta}.")
    equivalencias: dict[str, str] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        lineas = [linea for linea in manejador
                  if not linea.lstrip().startswith("#")]
    for fila in csv.DictReader(lineas, delimiter=delimitador):
        origen = str(fila.get("valor_origen", "")).strip()
        clase = str(fila.get("clase_cn", "")).strip()
        if origen and clase:
            equivalencias[origen] = clase
    if not equivalencias:
        raise ErrorFormato(
            f"{ruta.name} no tiene ninguna fila diligenciada: sin la columna "
            "clase_cn no hay con qué cruzar la cobertura y el grupo de suelo.")
    return equivalencias


def numero_curva_por_subcuenca(
    entidades_subcuencas,
    ruta_cobertura: Path,
    campo_cobertura: str,
    homologacion: dict[str, str],
    tabla_cn: dict[str, dict[str, float]],
    muestreador_grupo,
    paso_m: float,
) -> list[dict[str, Any]]:
    """
    Número de curva ponderado de cada subcuenca, por muestreo regular.

    Se recorre una malla de paso 'paso_m' sobre cada subcuenca y en cada punto
    se resuelven las dos entradas: la clase de cobertura, por índice espacial
    sobre la capa recortada, y el grupo hidrológico, leyendo el ráster de
    suelos. El CN de la subcuenca es la media de los CN puntuales.

    SE PONDERA POR ÁREA Y NO POR NÚMERO DE POLÍGONOS. Un mosaico Corine tiene
    cientos de polígonos diminutos junto a unos pocos grandes, y contar
    entidades daría el peso al ruido de la digitalización.

    El paso importa: una subcuenca de hectáreas no recibe ninguna muestra con
    una malla de 250 m. Se devuelve el recuento por subcuenca para que el
    módulo pueda declarar cuáles quedaron con muestreo insuficiente en vez de
    publicar un CN apoyado en dos puntos.
    """
    indice = geometria.IndiceEtiquetado(
        shapefile.leer_geometrias(ruta_cobertura))
    clases = list(shapefile.leer_registros(ruta_cobertura, [campo_cobertura]))

    # Las muestras de todas las subcuencas se resuelven de una vez contra el
    # raster de suelos: reproyectar y leer punto a punto multiplicaria el coste
    # por el numero de muestras sin cambiar el resultado.
    puntos: list[tuple[float, float]] = []
    tramos: list[tuple[int, int]] = []
    poligonos: list[Any] = []
    for anillos in entidades_subcuencas:
        poligono = [list(anillo) for anillo in anillos]
        poligonos.append(poligono)
        aristas = geometria.aristas_de([poligono])
        _, ymin, _, ymax = geometria.envolvente([poligono])
        desde = len(puntos)
        y = ymin + paso_m / 2.0
        while y < ymax:
            for x_inicio, x_fin in geometria.tramos_de_barrido(aristas, y):
                x = x_inicio + paso_m / 2.0
                while x < x_fin:
                    puntos.append((x, y))
                    x += paso_m
            y += paso_m
        tramos.append((desde, len(puntos)))

    grupos = muestreador_grupo([p[0] for p in puntos], [p[1] for p in puntos])

    salida: list[dict[str, Any]] = []
    for (desde, hasta), poligono in zip(tramos, poligonos):
        valores: list[float] = []
        sin_clase = sin_grupo = 0
        reparto: dict[str, int] = {}
        reparto_grupo: dict[str, int] = {}
        for posicion in range(desde, hasta):
            x, y = puntos[posicion]
            cual = indice.indice_en(x, y)
            clase = None
            if cual is not None:
                codigo = str(clases[cual].get(campo_cobertura, "")).strip()
                clase = homologacion.get(codigo)
            if clase is None or clase not in tabla_cn:
                sin_clase += 1
                continue
            grupo = grupos[posicion]
            if not grupo:
                sin_grupo += 1
                continue
            valores.append(float(tabla_cn[clase][grupo]))
            reparto[clase] = reparto.get(clase, 0) + 1
            reparto_grupo[grupo] = reparto_grupo.get(grupo, 0) + 1

        dominante = max(reparto, key=reparto.get) if reparto else ""
        # EL GRUPO DOMINANTE SE CUENTA AQUI PORQUE AQUI SE CONOCE. Ya se
        # muestrea para el CN, y el informe tabula el tipo de suelo hidrológico
        # junto al número de curva. Recuperarlo después obligaría a volver a
        # muestrear el ráster de suelos.
        grupo_dominante, grupo_fraccion = dominante_de(reparto_grupo)
        salida.append({
            "cn": round(sum(valores) / len(valores), 1) if valores else None,
            "muestras_cn": len(valores),
            "muestras_sin_clase": sin_clase,
            "muestras_sin_grupo": sin_grupo,
            "cobertura_dominante": dominante,
            "grupo_hidrologico": grupo_dominante,
            "grupo_fraccion": grupo_fraccion,
            "paso_muestreo_cn_m": paso_m,
        })
    return salida


def dominante_de(reparto: dict[str, int]) -> tuple[str, float | None]:
    """
    Cuál es la clase dominante de un reparto de muestras, y cuánto domina.

    SIN LA FRACCION EL NOMBRE ENGAÑA. La tabla del informe muestra un solo
    grupo hidrológico por subcuenca, y no es lo mismo un 95% que un 34% repartido
    entre cuatro grupos: en el segundo caso el valor tabulado simplifica una
    subcuenca heterogénea y el consultor tiene que saberlo.

    SIN MUESTRAS NO HAY DOMINANTE. Devuelve cadena vacía y None, no la primera
    clase de la tabla: una subcuenca sin suelo clasificado no puede declarar un
    grupo, y el que saliera sería inventado.

    Con empate se devuelve la clase menor en orden alfabético, para que dos
    corridas de los mismos datos den el mismo resultado.
    """
    if not reparto:
        return "", None
    total = sum(reparto.values())
    if total <= 0:
        return "", None
    clave = min(sorted(reparto), key=lambda c: -reparto[c])
    return clave, round(reparto[clave] / total, 3)


def muestreador_de_grupo(ruta_suelos: Path, crs_calculo: str, duales: str):
    """
    Devuelve una función que da el grupo hidrológico de una lista de puntos.

    El ráster HYSOGs es geográfico y trae el grupo YA ASIGNADO: 1=A, 2=B, 3=C,
    4=D, y 11=A/D, 12=B/D, 13=C/D, 14=D/D para los DUALES, cuyo grupo depende de
    si el suelo está drenado. El criterio con que se resuelven es una decisión
    con margen y llega declarada desde la configuración.

    Se lee por lotes y ordenando por fila: el lector recorre el ráster de
    principio a fin, y saltar de una fila a otra en desorden multiplica el coste
    de lectura sin cambiar nada del resultado.
    """
    import struct

    from pyproj import Transformer

    info = raster.leer_info(ruta_suelos)
    conversor = Transformer.from_crs(crs_calculo, info.crs_epsg or "EPSG:4326",
                                     always_xy=True)
    formato = {"<u1": "B", "<i1": "b", "<u2": "H", "<i2": "h",
               "<u4": "I", "<i4": "i", "<f4": "f", "<f8": "d"}.get(
        info.descriptor)
    if formato is None:
        raise ErrorFormato(
            f"{ruta_suelos.name}: tipo {info.descriptor} no muestreable como "
            "grupo hidrologico.")

    simples = {1: "A", 2: "B", 3: "C", 4: "D"}
    if duales == "drenado":
        duales_mapa = {11: "A", 12: "B", 13: "C", 14: "D"}
    else:
        duales_mapa = {11: "D", 12: "D", 13: "D", 14: "D"}

    def grupo_de(equis: Sequence[float], griegas: Sequence[float]) -> list:
        if not equis:
            return []
        geo_x, geo_y = conversor.transform(list(equis), list(griegas))
        pedidos = []
        for posicion, (gx, gy) in enumerate(zip(geo_x, geo_y)):
            if not info.contiene(gx, gy, gx, gy):
                continue
            pedidos.append((info.fila_de(gy), info.columna_de(gx), posicion))
        pedidos.sort()

        salida: list = [None] * len(equis)
        with raster.LectorRaster(ruta_suelos) as lector:
            fila_actual = None
            contenido = b""
            for fila, columna, posicion in pedidos:
                if fila != fila_actual:
                    contenido = lector.fila(fila)
                    fila_actual = fila
                bruto = struct.unpack_from(
                    "<" + formato, contenido,
                    columna * info.bytes_por_muestra)[0]
                valor = int(bruto)
                if info.nodato is not None and valor == int(info.nodato):
                    continue
                salida[posicion] = simples.get(valor) or duales_mapa.get(valor)
        return salida

    return grupo_de


def _resolver_cn_por_subcuenca(configuracion, base, ruta_cuenca, subcuencas,
                               resultado, logger) -> None:
    """
    Número de curva de cada subcuenca, cruzando cobertura y grupo de suelo.

    La raíz llega como argumento y NO se resuelve aquí. 'raiz_proyecto' asciende
    desde el directorio de trabajo, que al ejecutar con --raiz es el de la
    herramienta y no el del estudio: las rutas salían del proyecto equivocado y
    el módulo concluía que faltaban insumos que sí estaban.
    """
    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    ruta_tabla = rutas.resolver(
        configuracion.obtener("numero_curva.tabla_cn"), base)
    ruta_homologacion = rutas.resolver(
        configuracion.obtener("numero_curva.homologacion_cobertura"), base)
    ruta_cobertura = rutas.resolver(
        configuracion.obtener("referencia_nacional.salida_recorte_cobertura"),
        base)
    ruta_suelos = rutas.resolver(
        configuracion.obtener("referencia_nacional.salida_recorte_suelos"), base)
    if not ruta_suelos.is_file():
        ruta_suelos = Path(
            configuracion.obtener("referencia_nacional.directorio")) / str(
            configuracion.obtener("referencia_nacional.suelos_hsg"))

    faltan = [rutas.relativa(r, base) for r in
              (ruta_tabla, ruta_homologacion, ruta_cobertura, ruta_suelos)
              if not r.is_file()]
    if faltan:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.sin_cn",
            f"el numero de curva por subcuenca NO se calculo, falta: {faltan}. "
            "Sin CN no hay rezago por la ecuacion del SCS, que es la coherente "
            "con el metodo de transformacion declarado.",
        ))
        return

    try:
        tabla = leer_tabla_cn(ruta_tabla, delimitador)
        homologacion = leer_homologacion_cobertura(ruta_homologacion,
                                                   delimitador)
        muestreador = muestreador_de_grupo(
            ruta_suelos, str(configuracion.obtener("crs.calculo")),
            str(configuracion.obtener("numero_curva.grupos_duales")))
        valores = numero_curva_por_subcuenca(
            shapefile.leer_geometrias(ruta_cuenca), ruta_cobertura,
            str(configuracion.obtener("referencia_nacional.cobertura_clc_campo")),
            homologacion, tabla, muestreador,
            float(configuracion.obtener("numero_curva.muestreo_cobertura_m")))
    except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.sin_cn",
            f"el numero de curva por subcuenca NO se calculo: {error}"))
        return

    for subcuenca, cn in zip(subcuencas, valores):
        subcuenca.update(cn)

    con_cn = [s for s in subcuencas if s.get("cn")]
    logger.info("%d de %d subcuenca(s) con CN", len(con_cn), len(subcuencas))
    if not con_cn:
        return

    ponderado = (sum(s["cn"] * s["area_km2"] for s in con_cn)
                 / sum(s["area_km2"] for s in con_cn))
    resultado.suelos["cn_ponderado"] = round(ponderado, 1)
    extremos = sorted(con_cn, key=lambda s: s["cn"])
    sin_clase = sum(s.get("muestras_sin_clase", 0) for s in subcuencas)
    total = sum(s.get("muestras_cn", 0) for s in subcuencas) + sin_clase
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "subcuencas.numero_curva",
        f"numero de curva de {len(con_cn)} de {len(subcuencas)} subcuenca(s), "
        f"de {extremos[0]['cn']:.0f} ({extremos[0]['subcuenca']}) a "
        f"{extremos[-1]['cn']:.0f} ({extremos[-1]['subcuenca']}), ponderado por "
        f"area {ponderado:.1f}. Cruza la cobertura recortada con el grupo "
        f"hidrologico sobre {total:,} muestra(s), ponderando por AREA y no por "
        "numero de poligonos: un mosaico Corine tiene cientos de poligonos "
        "diminutos junto a unos pocos grandes.",
    ))

    pobres = [s for s in subcuencas if s.get("muestras_cn", 0) < 5]
    if pobres:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.cn_poco_muestreado",
            f"{len(pobres)} subcuenca(s) con menos de 5 muestras de cobertura: "
            f"{[s['subcuenca'] for s in pobres[:6]]}. Su CN se apoya en unos "
            "pocos puntos y no representa un reparto de coberturas. Son las "
            "subcuencas diminutas que el M09 conservo por decision declarada.",
        ))

    if sin_clase and total:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.cobertura_sin_homologar",
            f"{100.0 * sin_clase / total:.1f} % de las muestras cayeron en una "
            "clase de cobertura sin homologar o fuera de la capa recortada, y "
            "no entraron en el CN. Revisar que la tabla de homologacion cubra "
            "todas las clases presentes.",
        ))


def _resolver_subcuencas(configuracion, base, ruta_cuenca, ruta_dem, matriz,
                         minimo, resultado, logger) -> None:
    """
    Caracteriza cada subcuenca por separado, que es como entra en HEC-HMS.

    La cuenca completa da un Tc que el modelo no usa: HEC-HMS transforma la
    lluvia en cada subcuenca con SU rezago y transita el resultado por los
    tramos. Sin estos valores el modelo no se puede escribir.

    La curva hipsométrica, el orden de Strahler y la razón de bifurcación
    siguen siendo de la cuenca completa, porque describen la red y no la
    respuesta de cada unidad.
    """
    subcuencas = parametros_por_subcuenca(ruta_cuenca)
    if not subcuencas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.sin_unidades",
            "no se leyo ninguna subcuenca de la capa: la caracterizacion "
            "individual queda sin hacer y el M13 no podra escribir el modelo.",
        ))
        return

    sin_trayectoria = [s["subcuenca"] for s in subcuencas
                       if not s.get("long_flujo_km")]
    if sin_trayectoria:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.sin_trayectoria",
            f"{len(sin_trayectoria)} subcuenca(s) sin longitud de flujo en los "
            f"atributos ({CAMPO_LONGITUD}): {sin_trayectoria[:5]}. Sin ella no "
            "hay tiempo de concentracion, porque la red del IGAC no entra en "
            "las subcuencas pequenas y medirlas contra ella daria longitud "
            "cero. Reexportar de HEC-HMS con los parametros de subcuenca.",
        ))

    relieve = relieve_por_subcuenca(ruta_dem, shapefile.leer_geometrias(ruta_cuenca))
    for subcuenca, cotas in zip(subcuencas, relieve):
        subcuenca.update(cotas)
        # Giandotti pide la cota media SOBRE la salida. Se toma la minima de la
        # propia subcuenca como cota de salida: es donde entrega su caudal.
        if cotas["cota_media"] is not None and cotas["cota_min"] is not None:
            subcuenca["cota_media_sobre_salida_m"] = round(
                cotas["cota_media"] - cotas["cota_min"], 2)

    # DENSIDAD DE DRENAJE Y ORDEN, TAMBIEN POR SUBCUENCA. Estaban solo para la
    # cuenca entera, y son los que dicen si una unidad evacua rapido o encharca.
    entidades_sub = shapefile.leer_geometrias(ruta_cuenca)
    red_por_unidad = drenaje_por_subcuenca(
        rutas.resolver(configuracion.obtener("red_topologica.salida_red"), base),
        entidades_sub, [s["subcuenca"] for s in subcuencas])
    for subcuenca in subcuencas:
        datos_red = red_por_unidad.get(subcuenca["subcuenca"])
        if not datos_red or not subcuenca.get("area_km2"):
            continue
        largo_km = datos_red["longitud_m"] / 1000.0
        subcuenca.update({
            "long_cauces_km": round(largo_km, 4),
            "densidad_drenaje_km_km2": round(largo_km / subcuenca["area_km2"], 4),
            "corrientes": datos_red["corrientes"],
            "frecuencia_corrientes_km2": round(
                datos_red["corrientes"] / subcuenca["area_km2"], 3),
            "orden_corrientes": datos_red["orden"] or None,
        })

    # RECORRIDO DEL AGUA POR SUBCUENCA. Da la longitud del cauce principal, la
    # distancia recta entre sus extremos y la sinuosidad, que ninguna otra via
    # permitia: la red topologica tiene 230 corrientes para 125 subcuencas y no
    # deja trazar ni una, y la longitud axial es el diametro del poligono, no la
    # recta del cauce.
    recorridos = recorridos_por_subcuenca(
        rutas.resolver(configuracion.obtener("dem.delimitacion.salida_direccion"),
                       base),
        entidades_sub, [s["subcuenca"] for s in subcuencas], logger)
    for subcuenca in subcuencas:
        traza = recorridos.get(subcuenca["subcuenca"])
        if not traza:
            continue
        subcuenca.update({
            "long_cauce_principal_km": traza["long_recorrido_km"],
            "distancia_recta_cauce_km": traza["distancia_recta_km"],
            "indice_sinuosidad": traza["indice_sinuosidad"],
        })
    _contrastar_recorrido(subcuencas, resultado)

    con_red = [s for s in subcuencas if s.get("densidad_drenaje_km_km2")]
    if subcuencas and len(con_red) < len(subcuencas):
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "morfometria.drenaje_por_subcuenca",
            f"{len(con_red)} de {len(subcuencas)} subcuenca(s) reciben algun "
            "segmento de la red. Las demas quedan SIN densidad, que no es lo "
            "mismo que densidad cero: la red del estudio tiene pocas corrientes "
            "frente al numero de subcuencas, y una unidad pequena puede no "
            "contener ninguna. La densidad por subcuenca se reparte segmento a "
            "segmento y no por corriente entera; con el reparto por corriente "
            "salian densidades de hasta 83 km/km2, que no existen en ningun "
            "terreno.",
        ))

    _resolver_cn_por_subcuenca(configuracion, base, ruta_cuenca, subcuencas,
                               resultado, logger)

    criterio_rezago = str(configuracion.obtener("tiempo_rezago.criterio"))
    intervalo = float(configuracion.obtener("tormenta.intervalo_calculo_min"))
    cv_maximo = float(configuracion.obtener(
        "tiempo_concentracion.cv_maximo_admisible"))
    formula_adoptada = str(configuracion.obtener(
        "tiempo_concentracion.formula_adoptada", "") or "").strip()
    respaldo = str(configuracion.obtener(
        "tiempo_concentracion.fuera_de_rango",
        "omitir")).strip().lower() == "calcular_y_declarar"

    por_formula: list[dict[str, Any]] = []
    for subcuenca in subcuencas:
        tiempos = tiempos_de_subcuenca(
            subcuenca, matriz, minimo, cv_maximo, criterio_rezago, intervalo,
            formula_adoptada, respaldo)
        # EL DESGLOSE POR FORMULA SE GUARDA, no solo la mediana adoptada. Es la
        # memoria de calculo del parametro que gobierna el pico del hidrograma:
        # sin ella el estudio no puede mostrar de que trece formulas salio, ni
        # cuales se descartaron y por que. Se calculaba y se tiraba.
        for evaluada in tiempos.get("evaluadas") or ():
            tc_horas = evaluada.get("tc_horas")
            rezago = (tiempo_de_rezago(tc_horas, criterio_rezago, intervalo)
                      if tc_horas else {})
            por_formula.append({
                "subcuenca": subcuenca.get("subcuenca", ""),
                "formula": evaluada.get("formula", ""),
                "nombre": evaluada.get("nombre", ""),
                "aplicable": evaluada.get("aplicable"),
                "motivo": evaluada.get("motivo", ""),
                "tc_horas": tc_horas,
                "tc_minutos": evaluada.get("tc_minutos"),
                "tlag_horas": rezago.get("tlag_horas"),
                "tlag_minutos": rezago.get("tlag_minutos"),
            })
        subcuenca.update({c: v for c, v in tiempos.items() if c != "evaluadas"})
    resultado.tiempos_por_subcuenca = por_formula

    # LA CLASIFICACION VA AQUI, con las subcuencas ya completas: nombra
    # parametros que se calculan en pasos distintos y no habria un solo sitio
    # anterior donde esten todos.
    ruta_clases = rutas.resolver(configuracion.obtener(
        "morfometria.tabla_clasificacion",
        "data/referencia/clasificacion_morfometrica.csv"), base)
    sin_clase = clasificar_subcuencas(
        subcuencas, leer_clasificacion(
            ruta_clases,
            str(configuracion.obtener("insumos_usuario.delimitador_csv"))))
    if sin_clase:
        detalle = ", ".join(f"{p}: {n}" for p, n in sorted(sin_clase.items()))
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.sin_clase",
            f"la tabla de clasificacion no cubre todo el rango medido en este "
            f"estudio y quedan subcuencas SIN NOMBRE ({detalle}). La celda va "
            f"vacia y no con la clase mas cercana: adjudicarsela seria "
            f"extender la doctrina del consultor sin decirlo. Se corrige "
            f"abriendo el ultimo rango en "
            f"{rutas.relativa(ruta_clases, base)}.",
        ))
    resultado.subcuencas = subcuencas

    con_tc = [s for s in subcuencas if s.get("tc_horas")]
    logger.info("%d subcuenca(s) | %d con Tc adoptado | %d sin adoptar",
                len(subcuencas), len(con_tc), len(subcuencas) - len(con_tc))

    if con_tc:
        valores = sorted(s["tc_horas"] for s in con_tc)
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "subcuencas.tiempos",
            f"{len(con_tc)} de {len(subcuencas)} subcuenca(s) con tiempo de "
            f"concentracion adoptado, de {valores[0] * 60:.1f} a "
            f"{valores[-1] * 60:.1f} minutos. La longitud y la pendiente del "
            f"recorrido de flujo proceden de los atributos que HEC-HMS derivo "
            "del terreno; el area y las cotas se midieron aqui. A esta escala "
            "la matriz de aplicabilidad si se cumple, porque las formulas de "
            "Tc se calibraron en cuencas de este tamano y no en una de 220 km2.",
        ))

    sin_tc = [s for s in subcuencas if not s.get("tc_horas")]
    if sin_tc:
        motivos: dict[str, int] = {}
        for subcuenca in sin_tc:
            motivo = subcuenca.get("motivo_sin_tc") or "sin motivo registrado"
            motivos[motivo] = motivos.get(motivo, 0) + 1
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.sin_tiempo",
            f"{len(sin_tc)} de {len(subcuencas)} subcuenca(s) sin tiempo de "
            f"concentracion adoptado. Motivos: "
            + "; ".join(f"{m} ({n})" for m, n in sorted(motivos.items()))
            + f". Ejemplos: {[s['subcuenca'] for s in sin_tc[:5]]}. Sin Tc no "
            "hay rezago, y sin rezago esa subcuenca no se puede transformar en "
            "HEC-HMS: la decision es del consultor y debe quedar escrita.",
        ))

    extrapoladas = [s for s in subcuencas if s.get("tc_fuera_de_rango")]
    if extrapoladas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.tc_fuera_de_rango",
            f"{len(extrapoladas)} subcuenca(s) reciben tiempo de concentracion "
            f"con la formula {formula_adoptada!r} aplicada FUERA de su rango de "
            f"calibracion: {[(s['subcuenca'], s['motivo_sin_tc']) for s in extrapoladas]}. "
            "Es una decision del consultor, para que entren en el modelo en "
            "lugar de quedarse fuera. El informe debe presentarlas como "
            "extrapoladas y no como equivalentes a las demas.",
        ))

    cortas = [s for s in subcuencas if s.get("tlag_bajo_el_intervalo")]
    if cortas:
        menor = min(s["tlag_minutos"] for s in cortas)
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "subcuencas.rezago_bajo_el_intervalo",
            f"{len(cortas)} subcuenca(s) con tiempo de rezago por debajo del "
            f"intervalo de calculo de {intervalo:.0f} min, la menor de "
            f"{menor:.2f} min: {[s['subcuenca'] for s in cortas[:6]]}. HEC-HMS "
            "no puede resolver un hidrograma cuyo rezago es menor que su paso "
            "de tiempo, y lo que produce no es un pico pequeno sino un pico que "
            "el modelo no representa. Caben dos salidas, y ambas se declaran: "
            "bajar el intervalo de calculo, o fusionar esas subcuencas en "
            "HEC-HMS. Es la consecuencia de conservarlas, que ya advirtio el "
            "M09.",
        ))


def _resolver_tiempo_viaje(resultado, modo, parametros, logger) -> None:
    """Tiempo de viaje por los tramos de tránsito, cuando los hay."""
    if modo == "general":
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "tiempo_viaje.no_aplica",
            "el tiempo de viaje NO aplica en modo general: describe el recorrido "
            "de la onda por los tramos de transito ENTRE subcuencas, y aqui hay "
            "una sola unidad. El recorrido dentro de ella ya lo describe el "
            "tiempo de concentracion. En modo detallado se calcula sobre los "
            "tramos que el M09 importa de HEC-HMS.",
        ))
        return

    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "tiempo_viaje.pendiente",
        "el tiempo de viaje por los tramos de transito no se calculo: depende "
        "de la geometria de cada tramo y del metodo de transito, que el M13 "
        "declara. Queda para cuando el M09 entregue los tramos.",
    ))


def _muestreador_de_cota(ruta_dem: Path):
    """
    Devuelve una función (x, y) -> cota, con un método 'cerrar'.

    Es la misma que usa el M02b para orientar el eje del cauce doble. Que las
    dos salgan del mismo lector importa: si la cota que decide el sentido de
    flujo y la que mide la pendiente del cauce vinieran de implementaciones
    distintas, una discrepancia entre ellas no tendría dónde detectarse.
    """
    import struct

    info = raster.leer_info(ruta_dem)
    lector = raster.LectorRaster(ruta_dem)
    formato = {"<f4": "f", "<u2": "H", "<i2": "h", "<f8": "d"}.get(
        info.descriptor)
    if formato is None:
        lector.cerrar()
        raise ErrorFormato(
            f"{ruta_dem.name}: tipo {info.descriptor} no muestreable como cota.")

    def cota(x: float, y: float) -> float:
        columna, fila = info.columna_de(x), info.fila_de(y)
        if not (0 <= columna < info.ancho and 0 <= fila < info.alto):
            return float("nan")
        valor = struct.unpack_from(
            "<" + formato, lector.fila(fila), columna * info.bytes_por_muestra)[0]
        if info.nodato is not None and valor == info.nodato:
            return float("nan")
        return float(valor)

    cota.cerrar = lector.cerrar  # type: ignore[attr-defined]
    return cota


def _resolver_drenaje(drenaje, resultado, logger) -> None:
    """Registra la jerarquía de la red y las cautelas que la acompañan."""
    logger.info("%.0f km de cauce | densidad %.3f km/km2 | orden %d",
                drenaje["long_cauces_km"],
                drenaje.get("densidad_drenaje_km_km2") or 0.0,
                drenaje["orden_corrientes"])

    principal = drenaje.get("long_cauce_principal_km")
    if principal:
        logger.info("Cauce principal %.1f km | sinuosidad %.2f | pendiente %s",
                    principal, drenaje.get("indice_sinuosidad") or 0.0,
                    drenaje.get("pendiente_media_cauce_pct"))
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "drenaje.cauce_principal",
            f"cauce principal de {principal:.1f} km en "
            f"{drenaje['tramos_del_cauce_principal']} tramos, adoptado como el "
            "recorrido de mayor longitud acumulada hasta la salida y no como el "
            "rio con nombre ni el de mayor orden. "
            + (f"Atraviesa: {drenaje['nombres_del_cauce_principal']}. "
               if drenaje.get("nombres_del_cauce_principal") else "")
            + (f"Desciende de {drenaje['cota_nacimiento']:.0f} a "
               f"{drenaje['cota_cierre']:.0f} m, con pendiente media "
               f"{drenaje['pendiente_media_cauce_pct']:.2f} %, tomada entre los "
               "extremos y no promediando pendientes locales, que sobre este DEM "
               "medirian el ruido."
               if drenaje.get("cota_nacimiento") is not None else ""),
        ))
    else:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "drenaje.cauce_principal",
            "no se pudo trazar ningun cauce principal: la red no tiene ninguna "
            "cadena que llegue a una salida de la unidad."))

    cola = drenaje.get("recorte_cola") or {}
    if cola.get("recortado"):
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "drenaje.cola_recortada",
            f"se recortaron {cola['cola_ajena_km']:.2f} km de "
            f"{cola['cola_ajena_nombre']} del final del cauce. El poligono de "
            "la unidad rebasa unos metros la confluencia con el cauce receptor, "
            "y el recorrido mas largo continuaba por el. La longitud reportada "
            f"corresponde al {cola['cauce_adoptado']}.",
        ))
    elif cola.get("excede_el_limite"):
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "drenaje.cola_ajena",
            f"el cauce trazado termina con {cola['cola_ajena_km']:.2f} km de "
            f"{cola['cola_ajena_nombre']}, demasiado para ser un rebase del "
            "poligono. NO se recorto: un trozo ajeno de esa magnitud no es un "
            "detalle de borde sino un problema de delimitacion, y debe verse en "
            "el resultado en lugar de taparse.",
        ))

    sinuosidad = drenaje.get("indice_sinuosidad")
    if sinuosidad and sinuosidad > 2.5:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "drenaje.sinuosidad",
            f"indice de sinuosidad {sinuosidad:.2f}, muy por encima del 1,5 que "
            "separa un cauce meandriforme de uno recto. Sobre una cuenca "
            "alargada la cifra mezcla dos cosas distintas: los meandros del "
            "cauce y la curvatura de la propia cuenca, porque se mide contra la "
            "recta que une nacimiento y cierre. Conviene leerla junto al "
            "coeficiente de compacidad y no como sinuosidad de cauce sin mas.",
        ))

    if not drenaje.get("bifurcacion_en_rango_natural") and drenaje.get(
            "razon_bifurcacion"):
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "drenaje.bifurcacion",
            f"razon de bifurcacion ponderada {drenaje['razon_bifurcacion']:.2f}, "
            "fuera del rango de 3 a 5 que Horton observo en redes naturales. "
            "Suele delatar un control estructural del terreno o una cartografia "
            "con detalle desigual: si la parte alta se levanto con mas densidad "
            "que la baja, las corrientes de orden 1 salen infladas.",
        ))

    salidas = drenaje.get("salidas_de_la_unidad", 0)
    if salidas > 1:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "drenaje.salidas",
            f"la unidad tiene {salidas} tramos sin receptor dentro de ella. En "
            "una cuenca cerrada deberia haber uno. La adyacencia de la red se "
            "resuelve por proximidad sobre una cartografia que no es topologica, "
            "de modo que quedan cadenas sueltas. El cauce principal se traza "
            "desde la salida que produce el recorrido mas largo, pero la "
            "densidad de drenaje y el conteo de corrientes se calculan sobre "
            "toda la red de la unidad y no dependen de esa eleccion.",
        ))

    if drenaje.get("tramos_en_ciclo"):
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "drenaje.ciclos",
            f"{drenaje['tramos_en_ciclo']} tramo(s) forman ciclo en la relacion "
            "de afluencia dentro de la unidad. Su orden queda indefinido."))


def _resolver_relieve(relieve, parametros, resultado, configuracion,
                      logger) -> float | None:
    """
    Incorpora el relieve a los parámetros y decide qué pendiente se adopta.

    La elección tiene margen, de modo que queda registrada de forma explícita
    (CLAUDE.md, sección 7): un estudio que no puede explicar por qué adoptó una
    pendiente y no otra no es defendible ante interventoría.
    """
    escalares = [c for c in relieve
                 if c not in ("curva_hipsometrica", "histograma_cota",
                              "pendiente_por_escala")]
    for clave in escalares:
        parametros[clave] = relieve[clave]

    logger.info("Cota %.0f a %.0f m | media %.0f | desnivel %.0f m | HI %.3f",
                relieve["cota_min"], relieve["cota_max"],
                relieve["cota_media"], relieve["desnivel_altitudinal"],
                relieve["integral_hipsometrica"])

    if relieve["dem_no_cubre_la_envolvente"] or relieve["cobertura_dem_pct"] < 99.9:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "relieve.cobertura",
            f"el DEM cubre el {relieve['cobertura_dem_pct']:.2f} % de la "
            "cuenca. Las cotas y la pendiente describen solo esa parte, y la "
            "curva hipsometrica esta sesgada hacia ella.",
        ))

    integral = relieve["integral_hipsometrica"]
    lectura = ("cuenca joven, en desequilibrio, con relieve por desmantelar"
               if integral > 0.60 else
               "cuenca erosionada, en fase de monadnock" if integral < 0.35
               else "cuenca madura, en equilibrio")
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "relieve.hipsometria",
        f"integral hipsometrica {integral:.3f}: {lectura} (Strahler). "
        f"Cota de {relieve['cota_min']:.0f} a {relieve['cota_max']:.0f} m, "
        f"media {relieve['cota_media']:.0f} m, desnivel "
        f"{relieve['desnivel_altitudinal']:.0f} m. El desnivel robusto, entre "
        f"los percentiles 1 y 99, es {relieve['desnivel_robusto']:.0f} m: la "
        "diferencia con el desnivel total mide cuanto pesan las celdas "
        "extremas, que en un DEM de radar pueden ser un pozo o un pico de "
        "ruido y no terreno.",
    ))

    # --- ¿Representa algo la cota media? ------------------------------------
    # En una cuenca de un solo modo, la cota media cae donde hay terreno. En
    # una de dos plataformas separadas por un salto, cae en el hueco entre
    # ellas y no describe ningun sitio de la cuenca. La diferencia importa
    # aguas abajo: la zonificacion pluviometrica y el gradiente de temperatura
    # usan la cota media como si fuera representativa.
    histograma = relieve["histograma_cota"]
    if histograma:
        mayor = max(p["area_km2"] for p in histograma)
        media = relieve["cota_media"]
        en_la_media = next(
            (p["area_km2"] for p in histograma
             if p["cota_inf"] <= media < p["cota_sup"]), 0.0)
        if mayor > 0 and en_la_media / mayor < 0.10:
            modal = max(histograma, key=lambda p: p["area_km2"])
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "relieve.cota_media_no_representativa",
                f"la cota media, {media:.0f} m, cae en una franja que apenas "
                f"tiene {en_la_media:.1f} km2, frente a los {mayor:.1f} km2 de "
                f"la franja mas extensa, entre {modal['cota_inf']:.0f} y "
                f"{modal['cota_sup']:.0f} m. La distribucion altimetrica no "
                "tiene un solo modo: la cuenca son dos plataformas separadas "
                "por un salto, y el promedio cae en el hueco entre ellas. "
                "Consecuencia: la cota media NO debe usarse como cota "
                "representativa para el gradiente altitudinal de precipitacion "
                "(M11) ni para el de temperatura y evapotranspiracion (M18a). "
                f"Para eso sirve la mediana, {relieve['cota_mediana']:.0f} m, o "
                "mejor una particion por franjas.",
            ))

    # --- Que pendiente se adopta --------------------------------------------
    nativa = relieve["pendiente_media_cuenca"]
    escalas = relieve["pendiente_por_escala"]
    gruesa = escalas[-1]["pendiente_media_mm"] if escalas else None
    razon_maxima = float(configuracion.obtener(
        "morfometria.relieve.razon_maxima_nativa_gruesa"))
    razon = (nativa / gruesa) if gruesa else None

    if razon is None:
        adoptada, criterio = nativa, "nativa"
    elif razon <= razon_maxima:
        adoptada, criterio = nativa, "nativa"
    else:
        adoptada, criterio = gruesa, f"agregada a {escalas[-1]['tamano_celda_m']:.0f} m"

    parametros["pendiente_adoptada"] = round(adoptada, 5)
    parametros["pendiente_criterio"] = criterio
    escalera = "; ".join(
        f"{e['tamano_celda_m']:.0f} m: {100 * e['pendiente_media_mm']:.1f} %"
        for e in escalas if e["pendiente_media_mm"] is not None)

    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA if criterio != "nativa" else INFORMATIVO,
        "relieve.pendiente_adoptada",
        f"pendiente media {100 * nativa:.1f} % a la resolucion nativa de "
        f"{relieve['tamano_celda_m']:.1f} m. Al agregar el DEM baja asi: "
        f"{escalera}. La razon entre la nativa y la mas gruesa es "
        + (f"{razon:.2f}" if razon else "indeterminada")
        + f", frente al maximo admitido de {razon_maxima:.2f}. Se adopta la "
        f"pendiente {criterio}: {100 * adoptada:.1f} %."
        + ("" if criterio == "nativa" else
           " La cifra nativa se descarta porque no describe el terreno sino el "
           "error vertical del DEM, que el modulo del gradiente nunca compensa "
           "por carecer de signo."),
    ))

    # --- Diagnostico del terreno llano --------------------------------------
    umbral_llano = float(configuracion.obtener(
        "morfometria.relieve.pendiente_llana_mm"))
    en_llano = relieve.get("pendiente_mediana_en_llano")
    ruido = relieve.get("ruido_vertical_estimado_m")
    if en_llano and relieve["celdas_llanas"]:
        proporcion = 100.0 * relieve["celdas_llanas"] / relieve["celdas_con_dato"]
        exceso = en_llano / umbral_llano
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA if exceso >= 3.0 else INFORMATIVO,
            "relieve.ruido_en_llano",
            f"el {proporcion:.1f} % de la cuenca es terreno llano (pendiente "
            f"por debajo de {100 * umbral_llano:.1f} % medida a "
            f"{escalas[-1]['tamano_celda_m']:.0f} m). Ahi la pendiente "
            f"calculada celda a celda da una mediana de {100 * en_llano:.1f} %, "
            f"{exceso:.0f} veces el umbral que define ese terreno como llano. "
            f"La diferencia no es relieve: corresponde a un error vertical del "
            f"DEM del orden de {ruido:.2f} m, estimado invirtiendo la respuesta "
            "de Horn ante ruido blanco. Consecuencia practica: en las partes "
            "planas la pendiente esta sobrestimada en cerca de un orden de "
            "magnitud, y toda formula que dependa de ella (el tiempo de "
            "concentracion varia con S elevado a -0,385 en Kirpich) devuelve "
            "ahi un valor corto, del lado inseguro para el volumen y del lado "
            "conservador para el pico.",
        ))

    logger.info("Pendiente media %.2f %% (nativa) | adoptada %.2f %% (%s)",
                100 * nativa, 100 * adoptada, criterio)
    return adoptada


# =============================================================================
# Grupo hidrológico de suelo
# =============================================================================
# Códigos del ráster HYSOGs250m. Los simples son los cuatro grupos del SCS; los
# duales describen un suelo cuyo grupo depende de si está drenado. El estudio
# debe declarar cuál adopta, y la diferencia no es menor: pasar de A a D sube
# el número de curva en decenas de unidades.
GRUPOS_HSG = {1: "A", 2: "B", 3: "C", 4: "D",
              11: "A/D", 12: "B/D", 13: "C/D", 14: "D/D"}
GRUPO_SI_DRENADO = {11: "A", 12: "B", 13: "C", 14: "D"}
GRUPO_SI_NO_DRENADO = {11: "D", 12: "D", 13: "D", 14: "D"}


def grupos_hidrologicos(
    ruta_raster: Path, poligonos, crs_cuenca: str, paso_m: float = 250.0,
    duales: str = "no_drenado",
) -> dict[str, Any]:
    """
    Reparto del grupo hidrológico de suelo dentro de la cuenca.

    El ráster es global y está en coordenadas geográficas; la cuenca está en el
    CRS de cálculo. Se muestrea sobre una malla regular en el CRS de la cuenca y
    se reproyecta cada punto, que es la única forma de que el reparto de áreas
    sea correcto: muestrear en grados daría más peso a las latitudes altas.

    Las muestras se ordenan por fila del ráster antes de leer. El archivo es
    BigTIFF teselado con LZW, de modo que leer una fila descomprime la hilera de
    teselas entera; con las muestras desordenadas, esa hilera se descomprimiría
    una y otra vez y el muestreo pasaría de segundos a horas.

    Excepciones
    -----------
    ErrorRutas
        No está el ráster.
    ErrorHidrologia
        Ninguna muestra cae sobre dato válido.
    """
    from pyproj import Transformer

    info = raster.leer_info(ruta_raster)
    conversor = Transformer.from_crs(crs_cuenca, info.crs_epsg or "EPSG:4326",
                                     always_xy=True)
    aristas = geometria.aristas_de(poligonos)
    xmin, ymin, xmax, ymax = geometria.envolvente(poligonos)

    # Centros de celda de la malla de muestreo, solo los que caen dentro.
    puntos: list[tuple[float, float]] = []
    y = ymin + paso_m / 2.0
    while y < ymax:
        for x_inicio, x_fin in geometria.tramos_de_barrido(aristas, y):
            x = math.ceil((x_inicio - xmin) / paso_m) * paso_m + xmin
            while x < x_fin:
                puntos.append((x, y))
                x += paso_m
        y += paso_m

    if not puntos:
        raise ErrorHidrologia(
            "la malla de muestreo no cayó dentro de la cuenca; revisar el paso.")

    equis, griegas = conversor.transform([p[0] for p in puntos],
                                         [p[1] for p in puntos])
    celdas = sorted(
        (info.fila_de(gy), info.columna_de(gx))
        for gx, gy in zip(equis, griegas))

    import struct

    conteo: dict[int, int] = {}
    fuera = 0
    sin_dato = 0
    with raster.LectorRaster(ruta_raster) as lector:
        for fila, columna in celdas:
            if not (0 <= fila < info.alto and 0 <= columna < info.ancho):
                fuera += 1
                continue
            crudo = lector.fila(fila)
            valor = struct.unpack_from(
                "<" + {1: "B", 2: "H"}[info.bytes_por_muestra], crudo,
                columna * info.bytes_por_muestra)[0]
            if info.nodato is not None and valor == info.nodato:
                sin_dato += 1
                continue
            conteo[int(valor)] = conteo.get(int(valor), 0) + 1

    validas = sum(conteo.values())
    if not validas:
        raise ErrorHidrologia(
            f"ninguna de las {len(celdas)} muestras cayó sobre dato válido del "
            f"ráster de suelos ({fuera} fuera del ráster, {sin_dato} sin dato).")

    area_muestra_km2 = paso_m * paso_m / 1e6
    resolucion = {11: GRUPO_SI_NO_DRENADO, 14: GRUPO_SI_NO_DRENADO}
    tabla = (GRUPO_SI_DRENADO if duales == "drenado" else GRUPO_SI_NO_DRENADO)
    del resolucion

    por_grupo: dict[str, int] = {}
    duales_muestras = 0
    for codigo, cuantas in conteo.items():
        if codigo in tabla:
            duales_muestras += cuantas
        etiqueta = tabla.get(codigo) or GRUPOS_HSG.get(codigo, f"codigo {codigo}")
        por_grupo[etiqueta] = por_grupo.get(etiqueta, 0) + cuantas

    reparto = [
        {"grupo": grupo, "muestras": cuantas,
         "area_km2": round(cuantas * area_muestra_km2, 3),
         "porcentaje": round(100.0 * cuantas / validas, 2)}
        for grupo, cuantas in sorted(por_grupo.items(),
                                     key=lambda x: -x[1])
    ]
    crudo_reparto = [
        {"codigo": codigo, "etiqueta": GRUPOS_HSG.get(codigo, "desconocido"),
         "muestras": cuantas,
         "porcentaje": round(100.0 * cuantas / validas, 2)}
        for codigo, cuantas in sorted(conteo.items())
    ]

    return {
        "raster": str(ruta_raster),
        "crs_raster": info.crs_epsg,
        "paso_muestreo_m": paso_m,
        "muestras": len(celdas),
        "muestras_validas": validas,
        "muestras_sin_dato": sin_dato,
        "muestras_fuera": fuera,
        "cobertura_pct": round(100.0 * validas / len(celdas), 2),
        "criterio_duales": duales,
        "muestras_en_grupo_dual": duales_muestras,
        "pct_dual": round(100.0 * duales_muestras / validas, 2),
        "grupo_dominante": reparto[0]["grupo"] if reparto else None,
        "reparto": reparto,
        "reparto_crudo": crudo_reparto,
    }


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Caracteriza la cuenca o las subcuencas segun el modo declarado."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    modo = str(configuracion.obtener("analisis.modo"))
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")

    if modo == "general":
        ruta_cuenca = rutas.resolver(
            configuracion.obtener("analisis.cuenca_general"), base)
    else:
        ruta_cuenca = rutas.directorio("sig_vector", base) / "subcuencas.shp"

    registro.registrar_cabecera(
        logger, MODULO, f"{DESCRIPCION} (modo {modo})", config=configuracion,
        insumos={"cuenca": rutas.relativa(ruta_cuenca, base),
                 "matriz de Tc": configuracion.obtener(
                     "tiempo_concentracion.tabla_aplicabilidad")},
        parametros={
            "analisis.modo": modo,
            "tiempo_concentracion.valor_adoptado": configuracion.obtener(
                "tiempo_concentracion.valor_adoptado"),
            "tiempo_concentracion.min_formulas_aplicables":
                configuracion.obtener(
                    "tiempo_concentracion.min_formulas_aplicables"),
        },
    )

    resultado = ResultadoM10(modo=modo)

    if not ruta_cuenca.is_file():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "morfometria.cuenca",
            f"no se encuentra {rutas.relativa(ruta_cuenca, base)}."
            + (" Ejecutar el M02." if modo == "general" else
               " Ejecutar el M09 --importar tras la delimitacion en HEC-HMS."),
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    if modo == "general":
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "analisis.modo",
            "modo GENERAL: una sola unidad y sin modelo HEC-HMS. "
            + str(configuracion.obtener("analisis.motivo_general") or "").strip()
            + " Los parametros que siguen describen el conjunto, no una "
            "desagregacion por subcuencas.",
        ))

    # --- Geometria -----------------------------------------------------------
    with registro.bloque(logger, "Parametros geometricos"):
        parametros = parametros_geometricos(ruta_cuenca)
        info = shapefile.leer_shapefile(ruta_cuenca)
        parametros["unidades"] = info.n_registros
        parametros["modo"] = modo
        logger.info("Area %.2f km2 | perimetro %.2f km | axial %.2f km | "
                    "Gravelius %.3f",
                    parametros["area_km2"], parametros["perimetro_km"],
                    parametros["longitud_axial_km"],
                    parametros["coef_compacidad"])
        if parametros["perimetro_metodo"] == "suma_de_piezas":
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "morfometria.perimetro",
                f"el perimetro es la SUMA del de cada pieza y no el contorno de "
                f"la cuenca, porque el mosaico no es una cobertura limpia: "
                f"{parametros['aristas_repetidas']} arista(s) aparecen mas de "
                "dos veces. Sumar cuenta dos veces cada linde interior y el "
                "coeficiente de compacidad sale inflado en la misma proporcion: "
                "no usarlo hasta corregir la topologia de las subcuencas.",
            ))
        elif parametros["cadenas_degeneradas"]:
            # NO ES UN DETALLE DE DIBUJO: entra directo al Gravelius.
            bruto = (parametros["perimetro_km"]
                     + parametros["longitud_degenerada_km"])
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "morfometria.contorno_depurado",
                f"del contorno se descartaron {parametros['longitud_degenerada_km']:.2f} "
                f"km en {parametros['cadenas_degeneradas']} recorrido(s) que no "
                "encierran superficie. Salen de que dos subcuencas vecinas "
                "describen el mismo linde con distinto numero de vertices: una "
                "lo da como un segmento y la otra lo parte con un vertice "
                "colineal, de modo que las tres mitades se cuentan como "
                f"frontera. Sin depurar, el perimetro seria {bruto:.2f} km en "
                f"lugar de {parametros['perimetro_km']:.2f} y el coeficiente de "
                f"compacidad "
                f"{0.2821 * bruto / math.sqrt(parametros['area_km2']):.2f} en "
                f"lugar de {parametros['coef_compacidad']:.2f}. La "
                "comprobacion de que el depurado es correcto es que el contorno "
                "encierra exactamente la suma de las areas de las piezas.",
            ))

        if parametros["coef_compacidad"] > 1.5:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "morfometria.forma",
                f"coeficiente de compacidad {parametros['coef_compacidad']:.2f}, "
                f"sobre un contorno de {parametros['perimetro_km']:.1f} km "
                f"obtenido de las {parametros['aristas_frontera']} aristas que "
                f"no comparten dos subcuencas: la cuenca es alargada. Eso "
                "amortigua el hidrograma, porque el agua de las cabeceras llega "
                "desfasada respecto a la cercana al cierre.",
            ))

    # --- Drenaje -------------------------------------------------------------
    poligonos_cuenca = shapefile.leer_geometrias(ruta_cuenca)

    # --- Relieve -------------------------------------------------------------
    pendiente_adoptada: float | None = None
    with registro.bloque(logger, "Parametros de relieve"):
        ruta_dem = rutas.resolver(
            configuracion.obtener("dem.delimitacion.salida_dem"), base)
        if not ruta_dem.is_file():
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "morfometria.relieve",
                f"no se encuentra el DEM en {rutas.relativa(ruta_dem, base)}: "
                "los parametros de relieve quedan sin calcular ("
                + ", ".join(PARAMETROS_DE_RELIEVE)
                + "). Ejecutar el M02.",
            ))
        else:
            try:
                relieve = estadisticas_de_relieve(
                    ruta_dem, poligonos_cuenca,
                    intervalo_m=float(configuracion.obtener(
                        "morfometria.relieve.intervalo_hipsometrico_m")),
                    escalas=configuracion.obtener(
                        "morfometria.relieve.escalas_diagnostico"),
                    pendiente_llana=float(configuracion.obtener(
                        "morfometria.relieve.pendiente_llana_mm")),
                )
            except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
                resultado.hallazgos.append(Hallazgo(
                    BLOQUEANTE, "morfometria.relieve",
                    f"no se pudo leer el relieve del DEM: {error}"))
                relieve = None
            if relieve is not None:
                relieve["dem"] = rutas.relativa(ruta_dem, base)
                resultado.relieve = relieve
                pendiente_adoptada = _resolver_relieve(
                    relieve, parametros, resultado, configuracion, logger)

    # --- Drenaje -------------------------------------------------------------
    with registro.bloque(logger, "Parametros de drenaje"):
        ruta_red = rutas.resolver(
            configuracion.obtener("red_topologica.salida_red"), base)
        respaldo = rutas.resolver(
            configuracion.obtener("referencia_nacional.salida_recorte_sencillo"),
            base)

        if ruta_red.is_file():
            muestra = None
            if ruta_dem.is_file():
                muestra = _muestreador_de_cota(ruta_dem)
            try:
                drenaje = parametros_de_red(
                    ruta_red, poligonos_cuenca, parametros["area_km2"], muestra)
            finally:
                if muestra is not None:
                    muestra.cerrar()
            drenaje["red"] = rutas.relativa(ruta_red, base)
            parametros.update({c: v for c, v in drenaje.items()
                               if c not in ("corrientes_por_orden",
                                            "bifurcacion_pares")})
            resultado.drenaje = drenaje
            _resolver_drenaje(drenaje, resultado, logger)
        elif respaldo.is_file():
            drenaje = parametros_de_drenaje(respaldo, poligonos_cuenca)
            parametros.update(drenaje)
            if parametros["area_km2"]:
                parametros["densidad_drenaje_km_km2"] = round(
                    drenaje["long_cauces_km"] / parametros["area_km2"], 4)
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "morfometria.drenaje",
                "no existe la red con topologia del M02b, de modo que solo se "
                "calcularon longitud y densidad de drenaje. Sin saber que tramo "
                "desemboca en cual no hay orden de corrientes, ni razon de "
                "bifurcacion, ni cauce principal. Ejecutar el M02b en el "
                "entorno de QGIS.",
            ))
        else:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "morfometria.drenaje",
                "no se pudo calcular ningun parametro de drenaje: falta tanto "
                "la red del M02b como el recorte del drenaje del M02.",
            ))

    resultado.unidades.append(parametros)

    # --- Tiempo de concentracion ---------------------------------------------
    with registro.bloque(logger, "Tiempo de concentracion"):
        matriz = leer_matriz_aplicabilidad(
            rutas.resolver(
                configuracion.obtener("tiempo_concentracion.tabla_aplicabilidad"),
                base),
            delimitador)
        magnitudes = _magnitudes_de_la_cuenca(parametros, resultado)
        resultado.magnitudes = magnitudes
        resultado.tiempos = evaluar_aplicabilidad(
            matriz, parametros["area_km2"], pendiente_adoptada, magnitudes)
        if pendiente_adoptada is None:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "tiempo_concentracion.sin_pendiente",
                "sin pendiente media de cuenca, la matriz se aplico solo por "
                "area. Se declara porque filtrar tambien por pendiente "
                "descartaria mas formulas, no menos.",
            ))
        minimo = int(configuracion.obtener(
            "tiempo_concentracion.min_formulas_aplicables"))
        resultado.adoptados = resumir_adopcion(
            resultado.tiempos, minimo,
            cv_maximo=float(configuracion.obtener(
                "tiempo_concentracion.cv_maximo_admisible")),
            criterio=str(configuracion.obtener(
                "tiempo_concentracion.valor_adoptado")))

        aplicables = resultado.adoptados["formulas_aplicables"]
        calculadas = sum(1 for e in resultado.tiempos
                         if e.get("tc_horas") is not None)
        logger.info("%d de %d formula(s) aplicables (minimo exigido %d) | "
                    "%d calculadas", aplicables,
                    resultado.adoptados["formulas_evaluadas"], minimo,
                    calculadas)
        for evaluada in resultado.tiempos:
            if evaluada.get("tc_horas") is not None:
                logger.debug("  %-12s %7.2f h  %s", evaluada["formula"],
                             evaluada["tc_horas"],
                             "aplicable" if evaluada["aplicable"] else "fuera de rango")

        if resultado.adoptados["dispersion_excesiva"]:
            estadistica = resultado.adoptados["estadisticos"]
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "tiempo_concentracion.dispersion",
                f"las {estadistica['n']} formulas adoptables dan un coeficiente "
                f"de variacion de {estadistica['cv']:.2f}, por encima del maximo "
                f"admitido de {resultado.adoptados['cv_maximo_admisible']:.2f}. "
                f"Van de {estadistica['minimo']:.2f} a {estadistica['maximo']:.2f} "
                f"horas, una razon de {estadistica['razon_extremos']:.1f} entre "
                "extremos. NO se adopta ningun valor. Con formulas calibradas en "
                "poblaciones distintas, esa dispersion no es ruido de calculo: "
                "significa que la cuenca no se parece a ninguna de ellas y que "
                "la eleccion debe hacerla el consultor con criterio, no una "
                "mediana.",
            ))

        if not resultado.adoptados["procede_adoptar"]:
            fuera = [e for e in resultado.tiempos if not e["aplicable"]][:4]
            # La causa de no adoptar es una de dos, y el mensaje debe decir
            # cual. Anunciar siempre que faltan formulas afirmaba algo falso
            # cuando lo que sobraba era dispersion: aqui habia 6 aplicables
            # frente a un minimo de 5, y el texto sostenia lo contrario.
            if aplicables < minimo:
                causa = (f"solo {aplicables} formula(s) aplican a una cuenca de "
                         f"{parametros['area_km2']:.0f} km2, por debajo del "
                         f"minimo de {minimo}")
            else:
                causa = (f"{aplicables} formula(s) aplican a una cuenca de "
                         f"{parametros['area_km2']:.0f} km2, por encima del "
                         f"minimo de {minimo}, pero su dispersion excede lo "
                         "admitido y ninguna mediana representa a ese conjunto")
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA if aplicables else BLOQUEANTE,
                "tiempo_concentracion.no_adoptable",
                f"{causa}. NO se adopta la mediana, como exige CLAUDE.md, "
                "seccion 7. Las formulas de Tc se calibraron casi todas en "
                "cuencas pequenas, y usarlas fuera de su rango es la "
                "extrapolacion mas frecuente y menos justificada de la "
                "practica. Ejemplos: "
                + "; ".join(f"{e['formula']} ({e['motivo']})" for e in fuera)
                + ". Para una cuenca de esta magnitud el tiempo de "
                "concentracion no es el parametro que gobierna la respuesta, "
                "sino el transito hidraulico."
                + (" Coherente con el modo de analisis 'general' declarado."
                   if modo == "general" else
                   " En modo 'detallado' esto no bloquea el modelo: HEC-HMS "
                   "transforma la lluvia en cada SUBCUENCA con su propio "
                   "rezago, y esos si se resuelven, porque a esa escala las "
                   "formulas si aplican."),
            ))
        elif not resultado.adoptados["dispersion_excesiva"]:
            estadistica = resultado.adoptados["estadisticos"]
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "tiempo_concentracion.adoptado",
                f"{aplicables} formula(s) aplicables: "
                f"{resultado.adoptados['aplicables']}. Se adopta la "
                f"{resultado.adoptados['criterio']} del subconjunto: "
                f"{resultado.adoptados['tc_horas']:.3f} h "
                f"({resultado.adoptados['tc_minutos']:.1f} min). Van de "
                f"{estadistica['minimo']:.2f} a {estadistica['maximo']:.2f} h, "
                f"con coeficiente de variacion {estadistica['cv']:.2f}.",
            ))

    # --- Tiempo de rezago ----------------------------------------------------
    with registro.bloque(logger, "Tiempo de rezago"):
        resultado.rezago = tiempo_de_rezago(
            resultado.adoptados.get("tc_horas"),
            str(configuracion.obtener("tiempo_rezago.criterio")),
            float(configuracion.obtener("tormenta.intervalo_calculo_min")))
        _resolver_rezago(resultado, configuracion, logger)

    # --- Numero de curva y grupo hidrologico ---------------------------------
    with registro.bloque(logger, "Numero de curva"):
        _resolver_numero_curva(configuracion, base, resultado,
                               poligonos_cuenca, parametros, logger)

    # --- Tiempo de viaje -----------------------------------------------------
    with registro.bloque(logger, "Tiempo de viaje"):
        _resolver_tiempo_viaje(resultado, modo, parametros, logger)

    # --- Caracterizacion por subcuenca ---------------------------------------
    with registro.bloque(logger, "Parametros por subcuenca"):
        _resolver_subcuencas(configuracion, base, ruta_cuenca, ruta_dem,
                             matriz, minimo, resultado, logger)

    # LA CUENCA COMPLETA COMO CAPA, para la cartografia. Va aqui porque el
    # contorno ya esta calculado para el perimetro y porque solo tiene sentido
    # cuando hay subcuencas: en modo 'general' la cuenca YA es una sola capa.
    if modo != "general":
        with registro.bloque(logger, "Cuenca completa"):
            destino = rutas.resolver(configuracion.obtener(
                "morfometria.salida_cuenca_completa",
                "data/03_SIG/vector/cuenca_completa.shp"), base)
            try:
                ficha = escribir_cuenca_completa(
                    ruta_cuenca, destino,
                    str(configuracion.obtener("proyecto.nombre", "") or ""))
            except (ErrorRutas, ErrorHidrologia) as error:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, "cuenca_completa", str(error)))
            else:
                resultado.productos.append(rutas.relativa(destino, base))
                logger.info("cuenca completa: %s km2 en %d contorno(s) desde "
                            "%d subcuenca(s)", ficha["area_km2"],
                            ficha["contornos"], ficha["subcuencas"])

    if resultado.unidades:
        resultado.unidades[0].update({
            "tc_horas": resultado.adoptados.get("tc_horas"),
            "tc_minutos": resultado.adoptados.get("tc_minutos"),
            "tlag_horas": resultado.rezago.get("tlag_horas"),
            "tlag_minutos": resultado.rezago.get("tlag_minutos"),
            "tlag_criterio": resultado.rezago.get("criterio"),
        })
        resultado.unidades[0].update(
            {c: v for c, v in resultado.suelos.items()
             if not isinstance(v, (list, dict))})

    with registro.bloque(logger, "Escritura de productos"):
        _escribir_productos(configuracion, base, resultado, delimitador, logger)

    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    filas = list(filas)
    if not filas:
        destino.write_text("", encoding="utf-8-sig")
        return
    campos: list[str] = []
    for fila in filas:
        for clave in fila:
            if clave not in campos:
                campos.append(clave)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=campos,
                                  delimiter=delimitador, restval="")
        escritor.writeheader()
        escritor.writerows(filas)


def _escribir_productos(configuracion, base, resultado, delimitador,
                        logger) -> None:
    """Escribe parametros, matriz de Tc e informe."""
    directorio = rutas.directorio("procesado", base, crear=True) / "morfometria"
    directorio.mkdir(parents=True, exist_ok=True)

    contenidos = [("parametros.csv", resultado.unidades),
                  ("tiempo_concentracion.csv", resultado.tiempos)]
    if resultado.subcuencas:
        # La tabla que el M13 necesita: una fila por subcuenca, que es la
        # unidad con la que HEC-HMS transforma la lluvia.
        contenidos.append(("subcuencas.csv", resultado.subcuencas))
    if resultado.tiempos_por_subcuenca:
        contenidos.append(("tiempo_concentracion_por_subcuenca.csv",
                           resultado.tiempos_por_subcuenca))
    if resultado.relieve:
        contenidos += [
            ("curva_hipsometrica.csv", resultado.relieve["curva_hipsometrica"]),
            ("distribucion_altimetrica.csv", resultado.relieve["histograma_cota"]),
            ("pendiente_por_escala.csv", resultado.relieve["pendiente_por_escala"]),
        ]
    for nombre, contenido in contenidos:
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))

    if resultado.relieve:
        _escribir_figuras(configuracion, base, resultado, logger)

    informe = directorio / "M10_morfometria.md"
    _escribir_informe(informe, resultado, configuracion)
    resultado.productos.append(rutas.relativa(informe, base))
    logger.info("%d unidad(es) caracterizada(s)", len(resultado.unidades))


def _escribir_figuras(configuracion, base, resultado, logger) -> None:
    """Curva hipsométrica, distribución altimétrica y escalera de pendiente."""
    try:
        import graficos
    except ImportError as exc:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron generar las figuras: {exc}.",
        ))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(configuracion.obtener("graficos.directorio"), base)
    directorio.mkdir(parents=True, exist_ok=True)
    relieve = resultado.relieve

    # --- Curva hipsometrica --------------------------------------------------
    curva = relieve["curva_hipsometrica"]
    with graficos.figura(
        estilo, titulo="Curva hipsométrica de la cuenca",
        etiqueta_x="área acumulada por encima de la cota (%)",
        etiqueta_y="cota (m s. n. m.)",
    ) as (fig, ax):
        ax.plot([100.0 * p["area_relativa"] for p in curva],
                [p["cota"] for p in curva],
                color=estilo.color(0), linewidth=1.8)
        ax.axhline(relieve["cota_media"], color="#c00000", linestyle="--",
                   linewidth=1.2)
        ax.annotate(f"cota media {relieve['cota_media']:.0f} m",
                    xy=(0.98, relieve["cota_media"]),
                    xycoords=("axes fraction", "data"),
                    xytext=(0, 4), textcoords="offset points",
                    ha="right", fontsize=estilo.tamano_fuente - 1,
                    color="#c00000")
        ax.annotate(
            f"integral hipsométrica {relieve['integral_hipsometrica']:.3f}",
            xy=(0.04, 0.06), xycoords="axes fraction",
            fontsize=estilo.tamano_fuente - 1)
        ax.set_xlim(0, 100)
        fig.tight_layout()
        for ruta in graficos.guardar(
                fig, directorio / "M10_curva_hipsometrica", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Distribucion altimetrica -------------------------------------------
    histograma = relieve["histograma_cota"]
    with graficos.figura(
        estilo, titulo="Distribución altimétrica de la cuenca",
        etiqueta_x="cota (m s. n. m.)", etiqueta_y="área (km²)",
    ) as (fig, ax):
        anchura = (histograma[0]["cota_sup"] - histograma[0]["cota_inf"]
                   if histograma else 1.0)
        ax.bar([p["cota_inf"] for p in histograma],
               [p["area_km2"] for p in histograma],
               width=anchura, align="edge", color=estilo.color(0),
               edgecolor="white", linewidth=0.2)
        ax.axvline(relieve["cota_media"], color="#c00000", linestyle="--",
                   linewidth=1.2)
        ax.annotate(f"media {relieve['cota_media']:.0f} m",
                    xy=(relieve["cota_media"], 1),
                    xycoords=("data", "axes fraction"),
                    xytext=(4, -10), textcoords="offset points",
                    fontsize=estilo.tamano_fuente - 1, color="#c00000")
        graficos.rotular_en_miles(ax)
        fig.tight_layout()
        for ruta in graficos.guardar(
                fig, directorio / "M10_distribucion_altimetrica", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Pendiente frente a resolucion ---------------------------------------
    # Es la figura que sostiene la advertencia sobre el DEM de radar. Sin ella,
    # la afirmacion de que la pendiente nativa esta inflada por el ruido seria
    # una opinion; con ella es una medicion que el lector puede comprobar.
    escalas = relieve["pendiente_por_escala"]
    if escalas:
        tamanos = ([relieve["tamano_celda_m"]]
                   + [e["tamano_celda_m"] for e in escalas])
        medias = ([100.0 * relieve["pendiente_media_cuenca"]]
                  + [100.0 * e["pendiente_media_mm"] for e in escalas])
        medianas = ([100.0 * relieve["pendiente_mediana"]]
                    + [100.0 * e["pendiente_mediana_mm"] for e in escalas])
        with graficos.figura(
            estilo,
            titulo="Pendiente media según la resolución con que se calcula",
            etiqueta_x="tamaño de celda (m)",
            etiqueta_y="pendiente (%)",
        ) as (fig, ax):
            graficos.lineas(
                ax,
                {"media": (tamanos, medias), "mediana": (tamanos, medianas)},
                estilo)
            ax.set_xscale("log")
            # Sin apagar las marcas menores, la escala logaritmica rotula
            # ademas sus propias potencias y la figura queda ilegible.
            ax.minorticks_off()
            ax.set_xticks(tamanos)
            ax.set_xticklabels(
                [f"{t:g}" for t in tamanos])
            en_llano = relieve.get("pendiente_mediana_en_llano")
            if en_llano:
                ax.annotate(
                    "en terreno llano la mediana a resolución nativa es "
                    f"{100 * en_llano:.1f} %,\nequivalente a un error vertical "
                    f"de {relieve['ruido_vertical_estimado_m']:.2f} m",
                    xy=(0.03, 0.06), xycoords="axes fraction",
                    fontsize=estilo.tamano_fuente - 1, color="#c00000")
            fig.tight_layout()
            for ruta in graficos.guardar(
                    fig, directorio / "M10_pendiente_por_resolucion", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))

    if resultado.subcuencas:
        _figuras_de_subcuenca(graficos, configuracion, base, resultado, estilo,
                              directorio, logger)
    _figuras_de_delimitacion(graficos, configuracion, base, resultado, estilo,
                             directorio, logger)

    logger.info("Figuras de relieve escritas en %s",
                rutas.relativa(directorio, base))


# =============================================================================
# Figuras de delimitación y de insumos temáticos
# -----------------------------------------------------------------------------
# SON FIGURAS DE INFORME, NO PLANCHAS DE ANEXO. El M16 produce la cartografía
# formal, con rótulo, grilla y escala normalizada; esto son las ilustraciones
# que van embebidas en el capítulo, con el tamaño y la tipografía del documento.
# Las dos cosas coexisten a propósito: una plancha A3 reducida a media página no
# se lee, y una figura de informe ampliada a A3 no tiene la información que una
# plancha debe llevar.
# =============================================================================
def _ventana_de(poligonos, margen: float = 0.06):
    """Encuadre de un conjunto de polígonos, con holgura relativa."""
    if not poligonos:
        return None
    x_min, y_min, x_max, y_max = geometria.envolvente(poligonos)
    ancho, alto = x_max - x_min, y_max - y_min
    if ancho <= 0 or alto <= 0:
        return None
    holgura = margen * max(ancho, alto)
    return (x_min - holgura, y_min - holgura, x_max + holgura, y_max + holgura)


def _dentro_de(ventana, x_min, y_min, x_max, y_max) -> bool:
    """Cierto si dos envolventes se tocan. Evita dibujar lo que no se ve."""
    return not (x_max < ventana[0] or x_min > ventana[2]
                or y_max < ventana[1] or y_min > ventana[3])


def _lineas_en(ruta: Path, ventana):
    """
    Polilíneas de un shapefile que caen dentro de la ventana.

    SE FILTRA ANTES DE DIBUJAR. La red de drenaje del IGAC recortada al área
    trae 8.330 tramos que abarcan 213 por 143 km, mucho más que la cuenca:
    dibujarlos todos cuesta tiempo y no aporta un solo trazo visible.
    """
    if not Path(ruta).is_file():
        return []
    try:
        geometrias = shapefile.leer_geometrias(ruta)
    except (ErrorFormato, ErrorRutas):
        return []
    salida = []
    for entidad in geometrias:
        for parte in entidad:
            if len(parte) < 2:
                continue
            equis = [p[0] for p in parte]
            griegas = [p[1] for p in parte]
            if _dentro_de(ventana, min(equis), min(griegas), max(equis),
                          max(griegas)):
                salida.append(parte)
    return salida


def _poligonos_en(ruta: Path, ventana, campo: str = ""):
    """
    Polígonos de un shapefile dentro de la ventana, con el valor de un campo.

    Devuelve pares (polígono, valor). El valor es None cuando no se pide campo.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        return []
    try:
        geometrias = shapefile.leer_geometrias(ruta)
        registros = (shapefile.leer_registros(ruta, [campo]) if campo
                     else [None] * len(geometrias))
    except (ErrorFormato, ErrorRutas, KeyError):
        return []

    salida = []
    for entidad, registro in zip(geometrias, registros):
        anillos = [a for a in entidad if len(a) >= 3]
        if not anillos:
            continue
        equis = [p[0] for a in anillos for p in a]
        griegas = [p[1] for a in anillos for p in a]
        if not _dentro_de(ventana, min(equis), min(griegas), max(equis),
                          max(griegas)):
            continue
        valor = (str(registro.get(campo, "")).strip()
                 if isinstance(registro, dict) else None)
        salida.append((anillos, valor or None))
    return salida


def segmentos_de_frontera(poligonos, tolerancia_m: float = 0.01):
    """
    Aristas que pertenecen a un solo polígono: el contorno del mosaico.

    ES EL MISMO CONTEO QUE USA EL PERIMETRO EXTERIOR. En un mosaico sin huecos
    ni solapes cada linde interior aparece dos veces, una por cada pieza que lo
    comparte, y cada tramo del contorno una sola. Dibujar las 125 subcuencas con
    su borde da una maraña de lindes internos; dibujar solo estas aristas da la
    cuenca.

    Se devuelven segmentos sueltos y no un anillo encadenado: para una figura
    basta, y encadenarlos exigiría resolver los nodos de tres aristas que
    aparecen donde la delimitación toca una esquina de celda.
    """
    escala = 1.0 / tolerancia_m if tolerancia_m > 0 else 1.0
    cuenta: dict = {}
    coordenadas: dict = {}
    for anillos in poligonos:
        for anillo in anillos:
            for uno, otro in zip(anillo, anillo[1:]):
                izquierda = (round(uno[0] * escala), round(uno[1] * escala))
                derecha = (round(otro[0] * escala), round(otro[1] * escala))
                if izquierda == derecha:
                    continue
                clave = ((izquierda, derecha) if izquierda < derecha
                         else (derecha, izquierda))
                cuenta[clave] = cuenta.get(clave, 0) + 1
                coordenadas.setdefault(clave, (uno, otro))
    return [coordenadas[clave] for clave, veces in cuenta.items()
            if veces == 1]


def cadenas_de_frontera(segmentos, tolerancia_m: float = 0.01):
    """
    Encadena las aristas de frontera en anillos cerrados, de mayor a menor.

    POR QUE HACE FALTA ENCADENAR. El conteo de aristas devuelve la frontera del
    mosaico entera, y esa frontera no es solo el contorno de la cuenca: incluye
    el borde de cada hueco que las piezas dejen entre si. Medido sobre las 125
    subcuencas de este estudio, de los 145,3 km que el conteo da como frontera,
    100,8 km son el contorno exterior y los otros 44,5 km bordean huecos
    interiores. Dibujarlos todos llena la cuenca de trazos sueltos que parecen
    ruido y no lo son.

    Se recorre por ARISTAS y no por nodos: en los pocos nodos donde concurren
    tres aristas, marcar el nodo como visto cortaria las dos cadenas restantes.

    Devuelve una lista de listas de vertices, ordenada por longitud decreciente.
    """
    escala = 1.0 / tolerancia_m if tolerancia_m > 0 else 1.0

    def clave(punto):
        return (round(punto[0] * escala), round(punto[1] * escala))

    adyacencia: dict = {}
    for indice, (uno, otro) in enumerate(segmentos):
        adyacencia.setdefault(clave(uno), []).append((indice, uno, otro))
        adyacencia.setdefault(clave(otro), []).append((indice, otro, uno))

    usadas = set()
    cadenas = []
    for indice, (uno, _otro) in enumerate(segmentos):
        if indice in usadas:
            continue
        cadena = [uno]
        nodo = clave(uno)
        while True:
            siguiente = next((t for t in adyacencia.get(nodo, [])
                              if t[0] not in usadas), None)
            if siguiente is None:
                break
            usadas.add(siguiente[0])
            cadena.append(siguiente[2])
            nodo = clave(siguiente[2])
        if len(cadena) > 2:
            cadenas.append(cadena)

    def longitud(cadena):
        return sum(math.hypot(b[0] - a[0], b[1] - a[1])
                   for a, b in zip(cadena, cadena[1:]))

    cadenas.sort(key=longitud, reverse=True)
    return cadenas


def contorno_para_dibujo(poligonos, tolerancia_m: float = 0.01):
    """
    El contorno más largo de un mosaico, como una sola lista de vértices.

    ES SOLO PARA DIBUJAR el borde de la cuenca al fondo de las figuras, donde un
    trazo basta. NO SIRVE PARA MEDIR NI PARA ESCRIBIR LA CAPA: se queda con la
    cadena más larga y una cuenca partida en dos piezas perdería la segunda sin
    decirlo. Para eso está geometria.contorno_exterior, que las devuelve todas y
    descarta las que no encierran superficie, y es la que usa
    'escribir_cuenca_completa'.

    Se conserva el nombre distinto a propósito: dos funciones que se llamaban
    igual y devolvían cosas distintas son una trampa.
    """
    cadenas = cadenas_de_frontera(
        segmentos_de_frontera(poligonos, tolerancia_m), tolerancia_m)
    return cadenas[0] if cadenas else []


def _malla_de_raster(ruta: Path, ventana, crs_calculo: str,
                     celdas_objetivo: int = 400):
    """
    Ráster remuestreado a una malla ligera sobre la ventana, para dibujarlo.

    Devuelve (matriz, extensión) o (None, None). La matriz lleva NaN donde no
    hay dato.

    SE REMUESTREA Y NO SE LEE ENTERO. El modelo de elevación de este estudio son
    2.170 por 2.925 celdas; una figura de doce centímetros no distingue más de
    unos cientos, de modo que leer las seis millones sobraría por dos órdenes de
    magnitud. Se toma una de cada k filas y columnas.

    El muestreo es por VECINO MAS PROXIMO, no promediado, porque la misma
    función sirve al grupo hidrológico de suelo, que es una clase: promediar un
    A con un C daría un B que no existe en el terreno.
    """
    import numpy as np
    from pyproj import Transformer

    ruta = Path(ruta)
    if not ruta.is_file():
        return None, None
    try:
        info = raster.leer_info(ruta)
    except (ErrorFormato, ErrorRutas):
        return None, None

    formato = {"<u1": "B", "<i1": "b", "<u2": "H", "<i2": "h", "<u4": "I",
               "<i4": "i", "<f4": "f", "<f8": "d"}.get(info.descriptor)
    if formato is None:
        return None, None

    lado = max(ventana[2] - ventana[0], ventana[3] - ventana[1])
    paso = max(lado / float(celdas_objetivo), abs(info.tamano_x) or 1.0)
    columnas = max(2, int((ventana[2] - ventana[0]) / paso))
    filas = max(2, int((ventana[3] - ventana[1]) / paso))

    equis = ventana[0] + paso * (np.arange(columnas) + 0.5)
    griegas = ventana[3] - paso * (np.arange(filas) + 0.5)
    malla_x, malla_y = np.meshgrid(equis, griegas)

    destino = info.crs_epsg or crs_calculo
    if destino != crs_calculo:
        conversor = Transformer.from_crs(crs_calculo, destino, always_xy=True)
        muestreo_x, muestreo_y = conversor.transform(malla_x.ravel(),
                                                     malla_y.ravel())
        muestreo_x = np.asarray(muestreo_x).reshape(malla_x.shape)
        muestreo_y = np.asarray(muestreo_y).reshape(malla_y.shape)
    else:
        muestreo_x, muestreo_y = malla_x, malla_y

    salida = np.full((filas, columnas), np.nan, dtype=float)
    pedidos: dict[int, list[tuple[int, int, int]]] = {}
    for j in range(filas):
        for i in range(columnas):
            gx, gy = float(muestreo_x[j, i]), float(muestreo_y[j, i])
            if not info.contiene(gx, gy, gx, gy):
                continue
            pedidos.setdefault(info.fila_de(gy), []).append(
                (info.columna_de(gx), j, i))

    if not pedidos:
        return None, None

    with raster.LectorRaster(ruta) as lector:
        for fila in sorted(pedidos):
            try:
                contenido = lector.fila(fila)
            except (ErrorFormato, ErrorRutas, IndexError):
                continue
            for columna, j, i in pedidos[fila]:
                desplazamiento = columna * info.bytes_por_muestra
                if desplazamiento + info.bytes_por_muestra > len(contenido):
                    continue
                valor = struct.unpack_from("<" + formato, contenido,
                                           desplazamiento)[0]
                if info.nodato is not None and float(valor) == float(info.nodato):
                    continue
                salida[j, i] = float(valor)

    if not np.isfinite(salida).any():
        return None, None
    extension = (ventana[0], ventana[0] + paso * columnas,
                 ventana[3] - paso * filas, ventana[3])
    return salida, extension


def _fondo_geografico(ax, graficos, estilo, ventana) -> None:
    """Aspecto común a las cinco figuras: encuadre, proporción y rótulos."""
    ax.set_xlim(ventana[0], ventana[2])
    ax.set_ylim(ventana[1], ventana[3])
    ax.set_aspect("equal", adjustable="box")
    graficos.rotular_en_miles(ax, maximo_marcas=4)
    for etiqueta in ax.get_xticklabels():
        etiqueta.set_rotation(30)
        etiqueta.set_horizontalalignment("right")
    ax.tick_params(labelsize=estilo.tamano_fuente - 2)


def _dibujar_red(ax, sencillos, dobles, cuerpos) -> None:
    """La red hídrica en sus tres representaciones, con una entrada por tipo."""
    primera = True
    for linea in sencillos:
        ax.plot([p[0] for p in linea], [p[1] for p in linea],
                color="#5b8db8", linewidth=0.45, zorder=3,
                label="drenaje sencillo" if primera else None)
        primera = False
    primera = True
    for anillos, _ in dobles:
        for anillo in anillos:
            ax.fill([p[0] for p in anillo], [p[1] for p in anillo],
                    facecolor="#aed6f1", edgecolor="#2874a6", linewidth=0.3,
                    zorder=4, label="cauce doble" if primera else None)
            primera = False
    primera = True
    for anillos, _ in cuerpos:
        for anillo in anillos:
            ax.fill([p[0] for p in anillo], [p[1] for p in anillo],
                    facecolor="#85c1e9", edgecolor="#1b4f72", linewidth=0.4,
                    zorder=5, label="cuerpo de agua" if primera else None)
            primera = False


def _prefijo_comun(textos) -> str:
    """
    Prefijo alfabético que comparten todos los identificadores, si lo hay.

    Las subcuencas del geoprocesamiento asistido salen como 'SB1', 'SB2'... y
    ese 'SB' repetido 125 veces sobre el mapa ocupa espacio sin distinguir nada.
    Se detecta en lugar de fijarlo: otro proyecto puede nombrarlas de otro modo,
    y quitar dos letras a ciegas mutilaría el identificador.
    """
    if not textos:
        return ""
    letras = ""
    for caracter in textos[0]:
        if not caracter.isalpha():
            break
        letras += caracter
    while letras and not all(t.startswith(letras) for t in textos):
        letras = letras[:-1]
    # Si al quitarlo quedara algo vacío o repetido, no compensa.
    restos = {t[len(letras):] for t in textos}
    return letras if len(restos) == len(set(textos)) else ""


def _identificadores_de_subcuencas(base):
    """
    Polígonos de las subcuencas con su identificador, para rotular el mapa.

    El campo es el que el proyecto de HEC-HMS usa como nombre de elemento, que
    es el mismo con el que el M14 reporta los caudales.
    """
    ruta = rutas.directorio("sig_vector", base) / "subcuencas.shp"
    if not ruta.is_file():
        return []
    try:
        info = shapefile.leer_shapefile(ruta)
    except (ErrorFormato, ErrorRutas):
        return []
    campo = next((c for c in ("name", "Name", "NAME", "subcuenca")
                  if info.tiene_campo(c)), "")
    if not campo:
        return []
    return _poligonos_en(ruta, (-1e12, -1e12, 1e12, 1e12), campo=campo)


def _figuras_de_delimitacion(graficos, configuracion, base, resultado, estilo,
                             directorio, logger) -> None:
    """
    Las cinco figuras que el capítulo de caracterización necesita.

    Cada una se omite sin ruido si le falta su insumo: son ilustraciones de
    informe, y la ausencia de una no invalida la caracterización. Lo que sí se
    reporta es cuáles se omitieron, para que el M15 no declare una figura que no
    existe.
    """
    import numpy as np

    vector = rutas.directorio("sig_vector", base)
    crs_calculo = configuracion.obtener("crs.calculo")
    # POR LA CLAVE Y NO POR EL NOMBRE. Estas capas son SALIDAS declaradas, y
    # fijar aquí su nombre las desliga de la ruta que el estudio declara: si un
    # estudio la mueve, la figura se omite en silencio por 'no existe'.
    ruta_sencillo = rutas.resolver(configuracion.obtener(
        "referencia_nacional.salida_recorte_sencillo"), base)
    ruta_doble = rutas.resolver(configuracion.obtener(
        "referencia_nacional.salida_recorte_doble"), base)
    ruta_embalses = rutas.resolver(configuracion.obtener(
        "referencia_nacional.salida_recorte_embalses"), base)
    ruta_cobertura_clc = rutas.resolver(configuracion.obtener(
        "referencia_nacional.salida_recorte_cobertura"), base)
    ruta_subzona_int = rutas.resolver(configuracion.obtener(
        "subzonas_hidrograficas.salida_subzona"), base)
    ruta_dem = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_dem"), base)

    subcuencas = _geometrias_de_subcuencas(base)
    if not subcuencas:
        logger.info("sin subcuencas: se omiten las figuras de delimitación")
        return

    ventana = _ventana_de(subcuencas)
    if ventana is None:
        return

    sencillos = _lineas_en(ruta_sencillo, ventana)
    dobles = _poligonos_en(ruta_doble, ventana)
    cuerpos = _poligonos_en(ruta_embalses, ventana)
    frontera = contorno_para_dibujo(subcuencas)
    logger.info("red en la ventana: %d tramo(s), %d cauce(s) doble(s), "
                "%d cuerpo(s) de agua", len(sencillos), len(dobles),
                len(cuerpos))

    def registrar(escritas):
        for ruta in escritas or ():
            resultado.productos.append(rutas.relativa(ruta, base))

    def relieve_de_fondo(ax, ventana_local):
        """El modelo de elevación bajo el tema. Devuelve el mapeador o None."""
        malla, extension = _malla_de_raster(ruta_dem, ventana_local,
                                            crs_calculo)
        if malla is None:
            return None
        return ax.imshow(malla, extent=extension, origin="upper",
                         cmap="terrain", zorder=0, interpolation="nearest")

    # --- 1. Delimitación de la cuenca ---------------------------------------
    with graficos.figura(
            estilo, titulo="Delimitación de la cuenca",
            etiqueta_x="Este (m)", etiqueta_y="Norte (m)") as (fig, ax):
        imagen = relieve_de_fondo(ax, ventana)
        _dibujar_red(ax, sencillos, dobles, cuerpos)
        ax.plot([p[0] for p in frontera], [p[1] for p in frontera],
                color="#c0392b", linewidth=1.8, zorder=6,
                label="cuenca de estudio")
        if imagen is not None:
            graficos.barra_de_color(fig, ax, imagen, estilo,
                                    "Elevación (m s. n. m.)")
        _fondo_geografico(ax, graficos, estilo, ventana)
        # LA LEYENDA VA FUERA DEL MAPA. Dentro tapaba la cabecera de la cuenca,
        # que es la parte con mas contenido de la figura.
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2,
                  frameon=False, fontsize=estilo.tamano_fuente - 2)
        registrar(graficos.guardar(
            fig, directorio / "M10_delimitacion_cuenca", estilo))

    # --- 2. Delimitación de las subcuencas ----------------------------------
    # ESTA FIGURA PIDE MAS PAPEL QUE LAS DEMAS: lleva un rotulo por cada una
    # de las 125 subcuencas, y con el tamano de las otras los numeros se pisan
    # entre si hasta no poder leerse ninguno.
    amplio = graficos.estilo_individual(
        estilo, ancho_cm=estilo.ancho_cm * 1.25,
        alto_cm=estilo.alto_cm * 1.25)
    with graficos.figura(
            amplio, titulo="Delimitación de las subcuencas del modelo",
            etiqueta_x="Este (m)", etiqueta_y="Norte (m)") as (fig, ax):
        imagen = relieve_de_fondo(ax, ventana)
        primera = True
        for anillos in subcuencas:
            for anillo in anillos:
                ax.plot([p[0] for p in anillo], [p[1] for p in anillo],
                        color="#2c3e50", linewidth=0.45, zorder=6,
                        label="subcuenca" if primera else None)
                primera = False
        _dibujar_red(ax, sencillos, dobles, cuerpos)
        ax.plot([p[0] for p in frontera], [p[1] for p in frontera],
                color="#c0392b", linewidth=1.6, zorder=7)
        # EL IDENTIFICADOR SOBRE CADA SUBCUENCA. El modelo reporta caudales por
        # nombre de elemento y las tablas del informe se ordenan por el mismo
        # nombre: sin el rotulo, localizar SB73 en el mapa exige contar.
        from matplotlib import patheffects

        # SE QUITA EL PREFIJO COMUN Y SE PONE HALO. 'SB' delante de los 125
        # identificadores ocupa el doble de ancho sin distinguir nada, y sobre
        # el relieve un texto sin contorno se pierde justo donde el terreno es
        # mas oscuro.
        etiquetas = _identificadores_de_subcuencas(base)
        prefijo = _prefijo_comun([i for _, i in etiquetas if i])
        for anillos, identificador in etiquetas:
            if not identificador:
                continue
            centro_x, centro_y = geometria.centroide(anillos)
            ax.annotate(
                identificador[len(prefijo):] or identificador,
                xy=(centro_x, centro_y), ha="center", va="center",
                fontsize=max(3.4, amplio.tamano_fuente - 4),
                color="#17202a", zorder=8,
                path_effects=[patheffects.withStroke(linewidth=1.4,
                                                     foreground="white")])
        if prefijo:
            # Bajo la leyenda, no sobre el titulo: ahi lo pisaba.
            fig.text(0.5, 0.005,
                     f"los identificadores se rotulan sin el prefijo «{prefijo}»",
                     ha="center", fontsize=amplio.tamano_fuente - 2,
                     color="#555555")
        if imagen is not None:
            graficos.barra_de_color(fig, ax, imagen, amplio,
                                    "Elevación (m s. n. m.)")
        _fondo_geografico(ax, graficos, amplio, ventana)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=4,
                  frameon=False, fontsize=amplio.tamano_fuente - 2)
        registrar(graficos.guardar(
            fig, directorio / "M10_delimitacion_subcuencas", amplio))

    # --- 3. Localización en la subzona hidrográfica -------------------------
    ruta_subzona = ruta_subzona_int
    subzonas = _poligonos_en(ruta_subzona, (-1e12, -1e12, 1e12, 1e12),
                             campo="cod_szh")
    if subzonas:
        ventana_regional = _ventana_de([a for a, _ in subzonas], margen=0.03)
        # Cada nivel se lee por su par de campos. La capa del IDEAM trae los
        # tres en el mismo registro.
        areas = _poligonos_en(ruta_subzona, (-1e12, -1e12, 1e12, 1e12),
                              campo="cod_ah")
        zonas = _poligonos_en(ruta_subzona, (-1e12, -1e12, 1e12, 1e12),
                              campo="cod_zh")
        nombres_por_nivel = {
            "AH": _poligonos_en(ruta_subzona, (-1e12, -1e12, 1e12, 1e12),
                                campo="nom_ah"),
            "ZH": _poligonos_en(ruta_subzona, (-1e12, -1e12, 1e12, 1e12),
                                campo="nom_zh"),
            "SZH": _poligonos_en(ruta_subzona, (-1e12, -1e12, 1e12, 1e12),
                                 campo="nom_szh"),
        }
        red_regional = _lineas_en(ruta_sencillo,
                                  ventana_regional)
        dobles_regional = _poligonos_en(ruta_doble,
                                        ventana_regional)
        cuerpos_regional = _poligonos_en(ruta_embalses,
                                         ventana_regional)
        with graficos.figura(
                estilo, titulo="Localización en la subzona hidrográfica",
                etiqueta_x="Este (m)", etiqueta_y="Norte (m)") as (fig, ax):
            # EL LIMITE DE LA SUBZONA TIENE QUE LEERSE. En gris claro y a
            # un milimetro se perdia contra el fondo, y es la unidad que da
            # sentido a la figura: es el marco con el que el Estudio Nacional
            # del Agua compara el rendimiento del estudio.
            primera = True
            for anillos, _ in subzonas:
                for anillo in anillos:
                    ax.fill([p[0] for p in anillo], [p[1] for p in anillo],
                            facecolor="#eef3f7", edgecolor="#2c3e50",
                            linewidth=1.8, zorder=1,
                            label="subzona hidrográfica" if primera else None)
                    primera = False
            _dibujar_red(ax, red_regional, dobles_regional, cuerpos_regional)
            ax.plot([p[0] for p in frontera], [p[1] for p in frontera],
                    color="#c0392b", linewidth=1.8, zorder=6,
                    label="cuenca de estudio")
            # EL CODIGO Y EL NOMBRE JUNTOS. El codigo es lo que se cita en el
            # informe y en el Estudio Nacional del Agua; el nombre es lo que
            # permite reconocerla sin consultar la tabla.
            # LOS TRES NIVELES DE LA ZONIFICACION, no solo la subzona. El
            # IDEAM clasifica en area, zona y subzona hidrografica, y el codigo
            # de la subzona no dice a que zona ni a que area pertenece: el
            # informe cita los tres y la figura debe sustentarlos.
            # EL CUADRO VA EN UNA ESQUINA, no sobre el centroide: ahi
            # tapaba la cuenca de estudio, que es lo que la figura debe situar.
            for indice, (anillos, codigo) in enumerate(subzonas):
                lineas = []
                for etiqueta, valores in (("AH", areas), ("ZH", zonas),
                                          ("SZH", subzonas)):
                    if indice >= len(valores):
                        continue
                    identificador = valores[indice][1] or ""
                    titulo_nivel = (nombres_por_nivel[etiqueta][indice][1]
                                    if indice < len(nombres_por_nivel[etiqueta])
                                    else "")
                    if identificador or titulo_nivel:
                        lineas.append(
                            f"{etiqueta} {identificador} {titulo_nivel}".strip())
                if not lineas:
                    continue
                ax.annotate("\n".join(lineas), xy=(0.02, 0.98),
                            xycoords="axes fraction", ha="left", va="top",
                            fontsize=estilo.tamano_fuente - 1,
                            color="#2c3e50", zorder=8, linespacing=1.35,
                            bbox={"boxstyle": "round,pad=0.35",
                                  "facecolor": "white", "alpha": 0.9,
                                  "edgecolor": "#b9c8d6"})
                break
            _fondo_geografico(ax, graficos, estilo, ventana_regional)
            ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=2,
                      frameon=False, fontsize=estilo.tamano_fuente - 2)
            registrar(graficos.guardar(
                fig, directorio / "M10_zonificacionhidrografica", estilo))
    else:
        logger.info("sin subzona intersectada: se omite la figura de "
                    "zonificación")

    # --- 4. Grupo hidrológico de suelo --------------------------------------
    ruta_suelos = _ruta_de_suelos(configuracion, base)
    malla, extension = ((None, None) if ruta_suelos is None
                        else _malla_de_raster(ruta_suelos, ventana,
                                              crs_calculo))
    if malla is not None:
        # Los códigos duales (11 a 14) se llevan a su grupo según el criterio
        # declarado, el mismo que aplicó el número de curva.
        duales = str(configuracion.obtener(
            "numero_curva.grupos_duales", "no_drenado"))
        equivalencia = {1: 0, 2: 1, 3: 2, 4: 3}
        equivalencia.update({11: 0, 12: 1, 13: 2, 14: 3} if duales == "drenado"
                            else {11: 3, 12: 3, 13: 3, 14: 3})
        clases = np.full(malla.shape, np.nan)
        for codigo, posicion in equivalencia.items():
            clases[malla == codigo] = posicion

        if np.isfinite(clases).any():
            from matplotlib.colors import BoundaryNorm, ListedColormap
            from matplotlib.patches import Patch

            colores = ["#f6d55c", "#c9d98a", "#7fb069", "#3d6b4a"]
            mapa = ListedColormap(colores)
            # SIN BARRA DE COLOR, EL LIENZO SOBRA POR LA DERECHA. El ancho
            # del estilo esta pensado para las figuras que la llevan; aqui la
            # leyenda va dentro del mapa y el resto queda en blanco.
            estrecho = graficos.estilo_individual(
                estilo, ancho_cm=estilo.ancho_cm * 0.62,
                alto_cm=estilo.alto_cm)
            with graficos.figura(
                    estrecho, titulo="Grupo hidrológico de suelo",
                    etiqueta_x="Este (m)",
                    etiqueta_y="Norte (m)") as (fig, ax):
                ax.imshow(clases, extent=extension, origin="upper", cmap=mapa,
                          norm=BoundaryNorm([-0.5, 0.5, 1.5, 2.5, 3.5], 4),
                          zorder=1, interpolation="nearest")
                for anillos in subcuencas:
                    for anillo in anillos:
                        ax.plot([p[0] for p in anillo],
                                [p[1] for p in anillo], color="#2c3e50",
                                linewidth=0.4, zorder=5)
                ax.plot([p[0] for p in frontera], [p[1] for p in frontera],
                        color="#c0392b", linewidth=1.6, zorder=6)
                presentes = sorted({int(v) for v in
                                    clases[np.isfinite(clases)].ravel()})
                ax.legend(handles=[Patch(facecolor=colores[p],
                                         edgecolor="#7f8c8d",
                                         label=f"grupo {'ABCD'[p]}")
                                   for p in presentes],
                          loc="upper center", bbox_to_anchor=(0.5, -0.22),
                          ncol=3, frameon=False,
                          fontsize=estrecho.tamano_fuente - 2)
                _fondo_geografico(ax, graficos, estilo, ventana)
                fig.text(0.0, -0.13,
                         "Capa global de 250 m (Ross et al., 2018), no un "
                         "levantamiento de suelos del proyecto. Los grupos "
                         f"duales se asignaron con el criterio «{duales}».",
                         fontsize=estilo.tamano_fuente - 2, color="#555555")
                registrar(graficos.guardar(
                    fig, directorio / "M10_tiposuelohidrologico", estrecho))
    else:
        logger.info("sin capa de suelos legible: se omite la figura de grupo "
                    "hidrológico")

    # --- 5. Cobertura de la tierra ------------------------------------------
    ruta_cobertura = ruta_cobertura_clc
    campo_cobertura = _campo_de_cobertura(ruta_cobertura)
    coberturas = (_poligonos_en(ruta_cobertura, ventana, campo=campo_cobertura)
                  if campo_cobertura else [])
    if coberturas:
        from matplotlib import colormaps
        from matplotlib.patches import Patch

        # LAS CLASES DE COBERTURA SON CATEGORIAS, no una magnitud. Con una rampa
        # secuencial, bosque y tejido urbano salen en dos tonos del mismo azul y
        # el mapa deja de distinguirlos. Se usa una paleta cualitativa.
        #
        # Y SOLO LAS QUE CABEN EN LA LEYENDA llevan color propio: con treinta
        # entradas la leyenda tapa el mapa, y treinta colores arbitrarios no se
        # distinguen entre si. El resto va en gris, declarado como «otras».
        extension_por_clase: dict[str, float] = {}
        for anillos, valor in coberturas:
            if not valor:
                continue
            extension_por_clase[valor] = (
                extension_por_clase.get(valor, 0.0)
                + sum(abs(_area_de_anillo(a)) for a in anillos))
        principales = sorted(extension_por_clase,
                             key=lambda c: -extension_por_clase[c])[:8]
        cualitativa = colormaps["tab10"]
        color_de = {clase: cualitativa(i % 10)
                    for i, clase in enumerate(principales)}
        gris = "#c8ccd0"

        estrecho = graficos.estilo_individual(
            estilo, ancho_cm=estilo.ancho_cm * 0.62, alto_cm=estilo.alto_cm)
        with graficos.figura(
                estrecho, titulo="Cobertura de la tierra",
                etiqueta_x="Este (m)", etiqueta_y="Norte (m)") as (fig, ax):
            for anillos, valor in coberturas:
                for anillo in anillos:
                    ax.fill([p[0] for p in anillo], [p[1] for p in anillo],
                            facecolor=color_de.get(valor, gris),
                            edgecolor="none", zorder=1)
            for anillos in subcuencas:
                for anillo in anillos:
                    ax.plot([p[0] for p in anillo], [p[1] for p in anillo],
                            color="#4d5656", linewidth=0.35, zorder=5)
            ax.plot([p[0] for p in frontera], [p[1] for p in frontera],
                    color="#c0392b", linewidth=1.6, zorder=6)
            _fondo_geografico(ax, graficos, estrecho, ventana)

            entradas = [Patch(facecolor=color_de[c], edgecolor="#7f8c8d",
                              linewidth=0.3, label=c[:38])
                        for c in principales]
            if len(extension_por_clase) > len(principales):
                sobrantes = len(extension_por_clase) - len(principales)
                entradas.append(Patch(
                    facecolor=gris, edgecolor="#7f8c8d", linewidth=0.3,
                    label=f"otras {sobrantes} clases"))
            # LA LEYENDA VA FUERA DEL MAPA. Dentro tapaba la cabecera de la
            # cuenca, que es justo donde mas cambia la cobertura.
            ax.legend(handles=entradas, loc="upper center",
                      bbox_to_anchor=(0.5, -0.30), ncol=2, frameon=False,
                      fontsize=estrecho.tamano_fuente - 2)
            registrar(graficos.guardar(
                fig, directorio / "M10_mapa_cobertura", estrecho))
    else:
        logger.info("sin capa de cobertura legible: se omite su figura")


def _area_de_anillo(anillo) -> float:
    """Área con signo de un anillo, por la fórmula del cordón de zapato."""
    total = 0.0
    for uno, otro in zip(anillo, anillo[1:]):
        total += uno[0] * otro[1] - otro[0] * uno[1]
    return total / 2.0


def _ruta_de_suelos(configuracion, base) -> Path | None:
    """
    Dónde está la capa de grupo hidrológico, sea del estudio o la nacional.

    El manifiesto declara si el consultor aportó su propio estudio de suelos o
    si se usa la capa base compartida, que vive fuera del árbol del estudio en
    una sola copia por máquina.
    """
    try:
        manifiesto = leer_yaml(rutas.ruta_manifiesto(base)) or {}
    except (ErrorRutas, ErrorFormato):
        return None
    suelos = manifiesto.get("suelos") or {}
    archivo = str(suelos.get("base_archivo") or suelos.get("archivo") or "")
    if not archivo:
        return None
    propia = rutas.directorio("insumos", base) / archivo
    if propia.is_file():
        return propia
    raiz_nacional = configuracion.obtener("referencia_nacional.directorio", "")
    if raiz_nacional:
        candidata = Path(raiz_nacional) / archivo
        if candidata.is_file():
            return candidata
    return None


def _campo_de_cobertura(ruta: Path) -> str:
    """
    Campo con el que se clasifica la cobertura, entre los que la capa traiga.

    Se prefiere el nivel 2 de Corine: el nivel 1 agrupa demasiado para
    distinguir bosque de pastizal, y del 3 en adelante la leyenda deja de caber
    en una figura de informe.
    """
    if not Path(ruta).is_file():
        return ""
    try:
        info = shapefile.leer_shapefile(ruta)
    except (ErrorFormato, ErrorRutas):
        return ""
    for candidato in ("nivel_2", "nivel_3", "leyenda", "nivel_1"):
        if info.tiene_campo(candidato):
            return candidato
    return ""


def _geometrias_de_subcuencas(base):
    """Poligonos de las subcuencas, en el orden en que se caracterizaron."""
    ruta = rutas.directorio("sig_vector", base) / "subcuencas.shp"
    if not ruta.is_file():
        return []
    try:
        return shapefile.leer_geometrias(ruta)
    except (ErrorFormato, ErrorRutas):
        return []


def _mapa_de_subcuencas(graficos, estilo, entidades, resultado, columna,
                        titulo, etiqueta_barra, destino, base, rampa=""):
    """
    Coropleta de una magnitud por subcuenca, con su barra de color.

    LA TABLA NO CONTESTA LA PRIMERA PREGUNTA que hace quien revisa: si los
    valores altos se agrupan en la cabecera o se reparten. Ciento veinticinco
    filas ordenadas por nombre no lo dicen; el mapa si. Asi lo presenta el
    informe de referencia con el numero de curva (Ilustracion 48) y con las
    pendientes de terreno (Ilustracion 46).
    """
    valores = [s.get(columna) for s in resultado.subcuencas]
    if not any(v is not None for v in valores):
        return None
    with graficos.figura(estilo, titulo=titulo, etiqueta_x="Este (m)",
                         etiqueta_y="Norte (m)") as (fig, ax):
        mapeador = graficos.coropleta(ax, entidades, valores, estilo,
                                      rampa_color=rampa)
        graficos.barra_de_color(fig, ax, mapeador, estilo, etiqueta_barra)
        # Pocas marcas y giradas: las coordenadas de CTM12 tienen siete cifras
        # y con el paso automatico se solapan entre si.
        graficos.rotular_en_miles(ax, maximo_marcas=4)
        for etiqueta in ax.get_xticklabels():
            etiqueta.set_rotation(30)
            etiqueta.set_horizontalalignment("right")
        if any(v is None for v in valores):
            ax.legend(loc="lower left", frameon=False,
                      fontsize=estilo.tamano_fuente - 1)
        return graficos.guardar(fig, destino, estilo)


def _figuras_de_subcuenca(graficos, configuracion, base, resultado, estilo,
                          directorio, logger) -> None:
    """
    Las figuras del bloque por subcuenca: cada magnitud en grafica y en mapa.

    Se emiten en pares. La grafica dice CUANTO varia una magnitud y el mapa dice
    DONDE, y en una caracterizacion hidrologica las dos preguntas cuentan: un
    numero de curva alto disperso por la cuenca y otro concentrado aguas arriba
    del cierre producen hidrogramas distintos.
    """
    subcuencas = resultado.subcuencas
    entidades = _geometrias_de_subcuencas(base)
    intervalo = float(configuracion.obtener("tormenta.intervalo_calculo_min"))
    minimo_area = float(configuracion.obtener(
        "hec_hms.intercambio.area_minima_subcuenca_km2"))

    def registrar(escritas):
        for ruta in escritas or ():
            resultado.productos.append(rutas.relativa(ruta, base))

    con_cn = [s for s in subcuencas if s.get("cn") is not None]
    if con_cn:
        ponderado = resultado.suelos.get("cn_ponderado")
        with graficos.figura(
                estilo, titulo="Número de curva por subcuenca",
                etiqueta_x="Subcuenca, ordenada por CN",
                etiqueta_y="CN (condicion II)") as (fig, ax):
            ordenadas = sorted(con_cn, key=lambda s: s["cn"])
            ax.bar(range(len(ordenadas)), [s["cn"] for s in ordenadas],
                   color=estilo.color(0), width=1.0, linewidth=0)
            if ponderado:
                ax.axhline(ponderado, color="#b03a2e", linewidth=1.2,
                           linestyle="--",
                           label=f"ponderado por área: {ponderado:.1f}")
                ax.legend(loc="upper left", frameon=False,
                          fontsize=estilo.tamano_fuente - 1)
            ax.set_xlim(-0.5, len(ordenadas) - 0.5)
            registrar(graficos.guardar(fig, directorio / "M10_cn_subcuencas",
                                       estilo))
        if entidades:
            registrar(_mapa_de_subcuencas(
                graficos, estilo, entidades, resultado, "cn",
                "Número de curva por subcuenca", "CN (condicion II)",
                directorio / "M10_mapa_cn", base, rampa="YlOrBr"))

    con_tc = [s for s in subcuencas if s.get("tc_minutos") is not None]
    if con_tc:
        with graficos.figura(
                estilo, titulo="Tiempo de concentracion y rezago por subcuenca",
                etiqueta_x="Área de la subcuenca (km2)",
                etiqueta_y="Tiempo (min)") as (fig, ax):
            ax.scatter([s["area_km2"] for s in con_tc],
                       [s["tc_minutos"] for s in con_tc],
                       s=18, color=estilo.color(0), label="Tc", zorder=3)
            con_rezago = [s for s in con_tc if s.get("tlag_minutos")]
            if con_rezago:
                ax.scatter([s["area_km2"] for s in con_rezago],
                           [s["tlag_minutos"] for s in con_rezago],
                           s=18, marker="^", color="#7a97b5", label="rezago",
                           zorder=3)
            sin_tc = [s for s in subcuencas if s.get("tc_minutos") is None]
            if sin_tc:
                ax.scatter([s["area_km2"] for s in sin_tc],
                           [0.0] * len(sin_tc), s=30, marker="x",
                           color="#b03a2e", zorder=4,
                           label=f"fuera del rango de la formula ({len(sin_tc)})")
            ax.axhline(intervalo, color="#b03a2e", linewidth=1.0,
                       linestyle=":",
                       label=f"intervalo de cálculo, {intervalo:.0f} min")
            ax.set_xscale("log")
            ax.legend(loc="upper left", frameon=False,
                      fontsize=estilo.tamano_fuente - 1)
            registrar(graficos.guardar(
                fig, directorio / "M10_tiempos_subcuencas", estilo))
        if entidades:
            registrar(_mapa_de_subcuencas(
                graficos, estilo, entidades, resultado, "tlag_minutos",
                "Tiempo de rezago por subcuenca", "Rezago (min)",
                directorio / "M10_mapa_rezago", base))

    areas = sorted(s["area_km2"] for s in subcuencas if s.get("area_km2"))
    if areas:
        pequenas = [a for a in areas if a < minimo_area]
        with graficos.figura(
                estilo, titulo="Distribución de áreas de subcuenca",
                etiqueta_x="Área (km2)",
                etiqueta_y="Subcuencas acumuladas") as (fig, ax):
            ax.step(areas, range(1, len(areas) + 1), where="post",
                    color=estilo.color(0), linewidth=1.4)
            ax.axvline(minimo_area, color="#b03a2e", linewidth=1.0,
                       linestyle="--",
                       label=f"{minimo_area:g} km2: {len(pequenas)} por debajo")
            ax.set_xscale("log")
            ax.legend(loc="lower right", frameon=False,
                      fontsize=estilo.tamano_fuente - 1)
            registrar(graficos.guardar(
                fig, directorio / "M10_areas_subcuencas", estilo))
        if entidades:
            registrar(_mapa_de_subcuencas(
                graficos, estilo, entidades, resultado, "area_km2",
                "Area de las subcuencas", "Área (km2)",
                directorio / "M10_mapa_areas", base, rampa="Greens"))

    # Dos calculos que no comparten una linea de codigo sobre el mismo DEM. En
    # el agregado dan 23,1 % y 24,2 %; la figura dice si alguna subcuenca se
    # aparta de esa coincidencia.
    con_pendiente = [s for s in subcuencas
                     if s.get("pendiente_cuenca") is not None]
    nativa = resultado.relieve.get("pendiente_media_cuenca")
    if con_pendiente and nativa:
        with graficos.figura(
                estilo,
                titulo="Pendiente por subcuenca frente al valor de la cuenca",
                etiqueta_x="Área de la subcuenca (km2)",
                etiqueta_y="Pendiente media (%)") as (fig, ax):
            ax.scatter([s["area_km2"] for s in con_pendiente],
                       [100.0 * s["pendiente_cuenca"] for s in con_pendiente],
                       s=16, color=estilo.color(0), alpha=0.7,
                       label="HEC-HMS, por subcuenca")
            ax.axhline(100.0 * nativa, color="#b03a2e", linewidth=1.2,
                       linestyle="--",
                       label=f"Horn sobre el DEM, cuenca: {100 * nativa:.1f} %")
            ax.set_xscale("log")
            ax.legend(loc="upper right", frameon=False,
                      fontsize=estilo.tamano_fuente - 1)
            registrar(graficos.guardar(
                fig, directorio / "M10_pendiente_contraste", estilo))
        if entidades:
            registrar(_mapa_de_subcuencas(
                graficos, estilo, entidades, resultado, "pendiente_cuenca",
                "Pendiente media por subcuenca", "Pendiente (m/m)",
                directorio / "M10_mapa_pendiente", base, rampa="PuBuGn"))

    logger.info("Figuras por subcuenca escritas")


def _tabla_markdown(filas, columnas) -> list[str]:
    lineas = ["| " + " | ".join(str(c) for c in columnas) + " |",
              "|" + "|".join("---" for _ in columnas) + "|"]
    for fila in filas:
        lineas.append("| " + " | ".join(str(fila.get(c, "")) for c in columnas) + " |")
    return lineas


def _escribir_informe(destino, resultado, configuracion) -> None:
    unidad = resultado.unidades[0] if resultado.unidades else {}
    lineas = [
        "# M10 - Caracterizacion morfometrica e hidrologica",
        "",
        f"* Modo de analisis: **{resultado.modo}**",
        f"* Unidades caracterizadas: {unidad.get('unidades', 0)}",
        "",
    ]
    if resultado.modo == "general":
        lineas += [
            "## Salvedad del modo general",
            "",
            str(configuracion.obtener("analisis.motivo_general") or "").strip(),
            "",
            "Los parametros describen el CONJUNTO de la cuenca. No hay",
            "desagregacion por subcuencas ni modelo lluvia-escorrentia, y las",
            "cifras deben leerse con ese alcance.",
            "",
        ]
    lineas += ["## Parametros geometricos", ""]
    lineas += _tabla_markdown(
        [{"parametro": k, "valor": v} for k, v in unidad.items()
         if k not in ("modo", "unidades")],
        ["parametro", "valor"])
    lineas += ["", "## Parametros de relieve", ""]
    relieve = resultado.relieve
    if not relieve:
        lineas += [
            "NO se calcularon: " + ", ".join(PARAMETROS_DE_RELIEVE) + ".",
            "",
            "Falta el DEM que produce el M02. Sin el no hay pendiente media de",
            "cuenca, que varias formulas de tiempo de concentracion necesitan.",
            "",
        ]
    else:
        lineas += [
            f"Leidos del DEM `{relieve['dem']}` ({relieve['crs_dem']}), con",
            f"celda de {relieve['tamano_celda_m']:.1f} m, sobre",
            f"{relieve['celdas_con_dato']:,} celdas con dato que cubren el",
            f"{relieve['cobertura_dem_pct']:.2f} % de la cuenca. El area que",
            f"resulta de contarlas, {relieve['area_por_dem_km2']:.1f} km2,",
            f"contrasta con la del poligono, {unidad.get('area_km2', 0)} km2:",
            "coincidir valida a la vez la delimitacion y el barrido del raster.",
            "",
        ]
        lineas += _tabla_markdown(
            [{"parametro": k, "valor": relieve[k]} for k in (
                "cota_min", "cota_p1", "cota_media", "cota_mediana",
                "cota_p99", "cota_max", "desnivel_altitudinal",
                "desnivel_robusto", "integral_hipsometrica",
                "pendiente_media_cuenca", "pendiente_media_pct",
                "pendiente_media_grados", "pendiente_mediana",
                "pendiente_p90") if k in relieve],
            ["parametro", "valor"])
        lineas += [
            "",
            "### Pendiente segun la resolucion",
            "",
            "La pendiente de una celda no es una propiedad del terreno sino del",
            "terreno Y de la resolucion con que se mide. Agregando el DEM se",
            "separa una cosa de la otra: sobre relieve real la pendiente media",
            "baja poco al agregar, mientras que la que produce el ruido del",
            "sensor se desploma, porque el promedio la cancela.",
            "",
        ]
        lineas += _tabla_markdown(
            [{"tamano_celda_m": relieve["tamano_celda_m"],
              "pendiente_media_mm": relieve["pendiente_media_cuenca"],
              "pendiente_mediana_mm": relieve["pendiente_mediana"],
              "celdas": relieve["celdas_con_pendiente"]}] + [
                {k: e[k] for k in ("tamano_celda_m", "pendiente_media_mm",
                                   "pendiente_mediana_mm", "celdas")}
                for e in relieve["pendiente_por_escala"]],
            ["tamano_celda_m", "pendiente_media_mm", "pendiente_mediana_mm",
             "celdas"])
        if relieve.get("ruido_vertical_estimado_m"):
            lineas += [
                "",
                f"Se adopta la pendiente **{unidad.get('pendiente_criterio')}**:",
                f"{100 * unidad.get('pendiente_adoptada', 0):.2f} %.",
                "",
                "Sobre el subconjunto de terreno LLANO, donde la señal de",
                "relieve es despreciable, la pendiente calculada celda a celda",
                f"tiene mediana {100 * relieve['pendiente_mediana_en_llano']:.1f} %.",
                "Invirtiendo la respuesta del operador de Horn ante ruido",
                "blanco, eso corresponde a un error vertical del DEM de",
                f"{relieve['ruido_vertical_estimado_m']:.2f} m. No es una",
                "objecion al DEM, que es el mejor disponible: es la razon por la",
                "que en las partes planas la pendiente no debe leerse a",
                "resolucion nativa.",
                "",
            ]
        lineas += [
            "",
            "La curva hipsometrica y la distribucion altimetrica se entregan en",
            "`curva_hipsometrica.csv` y `distribucion_altimetrica.csv`, y como",
            "figuras del informe.",
            "",
        ]
    lineas += ["", "## Parametros de drenaje", ""]
    drenaje = resultado.drenaje
    if not drenaje:
        lineas += [
            "Sin la red con topologia del M02b solo hay longitud y densidad.",
            "Ejecutar el M02b en el entorno de QGIS.",
            "",
        ]
    else:
        lineas += [
            f"Sobre la red `{drenaje.get('red')}`, que el M02b construye",
            "reponiendo el eje de los cauces que la cartografia representa como",
            "poligono. Sin ese eje la red queda cortada justo en el cauce",
            "principal y su longitud sale reducida a una fraccion, sin ninguna",
            "senal de error.",
            "",
        ]
        lineas += _tabla_markdown(
            [{"parametro": k, "valor": drenaje[k]} for k in (
                "tramos_dentro", "long_cauces_km", "densidad_drenaje_km_km2",
                "orden_corrientes", "corrientes_totales",
                "frecuencia_corrientes_km2", "razon_bifurcacion",
                "long_cauce_principal_km", "distancia_recta_cauce_km",
                "indice_sinuosidad", "cota_nacimiento", "cota_cierre",
                "desnivel_cauce_m", "pendiente_media_cauce",
                "pendiente_media_cauce_pct") if k in drenaje],
            ["parametro", "valor"])
        if drenaje.get("nombres_del_cauce_principal"):
            lineas += [
                "",
                "El cauce principal es el recorrido de mayor longitud acumulada",
                "hasta la salida, que es la definicion que gobierna el tiempo de",
                "concentracion, y no el rio con nombre ni el de mayor orden.",
                "Atraviesa, de la cabecera al cierre:",
                "",
                f"> {drenaje['nombres_del_cauce_principal']}",
                "",
                "Esa lista no es decorativa: es el control que permite verificar",
                "el trazado de un vistazo. Si el recorrido mas largo de esta",
                "cuenca no mencionara su rio, la red estaria mal empalmada.",
                "",
            ]
        corrientes = drenaje.get("corrientes_por_orden") or {}
        if corrientes:
            lineas += ["### Corrientes por orden", ""]
            lineas += _tabla_markdown(
                [{"orden": o, "corrientes": c} for o, c in sorted(
                    corrientes.items(), key=lambda x: int(x[0]))],
                ["orden", "corrientes"])
            lineas += [
                "",
                "Se cuentan CORRIENTES y no tramos. Una corriente de orden n es",
                "la cadena completa de tramos consecutivos de ese orden: la",
                "cartografia parte un mismo rio en decenas de piezas por razones",
                "de dibujo.",
                "",
            ]
    lineas += [
        "## Tiempo de concentracion",
        "",
        f"* Formulas evaluadas: {resultado.adoptados.get('formulas_evaluadas', 0)}",
        f"* Aplicables: {resultado.adoptados.get('formulas_aplicables', 0)}",
        f"* Minimo exigido: {resultado.adoptados.get('minimo_exigido', 0)}",
        f"* Procede adoptar: "
        f"**{'si' if resultado.adoptados.get('procede_adoptar') else 'NO'}**",
        "",
        "La matriz de aplicabilidad vive en `data/referencia/`, con el rango de",
        "calibracion de cada formula y su procedencia. Que sea dato y no",
        "constante permite revisarla sin tocar el programa.",
        "",
    ]
    lineas += _tabla_markdown(
        resultado.tiempos,
        ["formula", "nombre", "area_min_km2", "area_max_km2", "tc_horas",
         "aplicable", "motivo"])
    adoptados = resultado.adoptados
    estadistica = adoptados.get("estadisticos") or {}
    if estadistica.get("n"):
        lineas += [
            "",
            f"Sobre el subconjunto adoptable ({estadistica['n']} formulas): "
            f"mediana {estadistica['mediana']} h, media {estadistica['media']} h,",
            f"de {estadistica['minimo']} a {estadistica['maximo']} h, "
            f"coeficiente de variacion {estadistica['cv']}.",
        ]
    if adoptados.get("tc_horas"):
        lineas += [
            "",
            f"**Tc adoptado: {adoptados['tc_horas']} h "
            f"({adoptados['tc_minutos']} min)**, por "
            f"{adoptados['criterio']} del subconjunto aplicable.",
        ]
    else:
        lineas += [
            "",
            "**NO se adopta ningun valor de Tc.** Las formulas se calcularon",
            "todas, incluidas las que la matriz descarta, para que se vea el",
            "contraste; pero ninguna esta dentro de su rango de calibracion y",
            "la mediana de un conjunto extrapolado no es defendible.",
        ]

    lineas += ["", "## Tiempo de rezago", ""]
    rezago = resultado.rezago
    if rezago.get("tlag_horas"):
        lineas += [
            f"* Criterio: **{rezago['criterio']}**",
            f"* Tlag: **{rezago['tlag_horas']} h ({rezago['tlag_minutos']} min)**",
            "",
            "El criterio 'hechms' anade el intervalo de CALCULO dividido por dos,",
            "no la duracion de la tormenta. Confundirlos desplaza el hidrograma.",
            "",
        ]
    else:
        lineas += [f"No se calculo: {rezago.get('motivo', 'sin datos')}.", ""]

    lineas += ["## Grupo hidrologico de suelo", ""]
    suelos = resultado.suelos
    if not suelos:
        lineas += ["No se pudo leer la capa de suelos.", ""]
    else:
        lineas += [
            f"Leido de la {suelos['procedencia']} `{suelos['raster']}`",
            f"({suelos['crs_raster']}), sobre {suelos['muestras_validas']:,}",
            f"muestras de una malla de {suelos['paso_muestreo_m']:.0f} m, con",
            f"{suelos['cobertura_pct']:.1f} % de la malla sobre dato valido.",
            "",
        ]
        lineas += _tabla_markdown(suelos["reparto"],
                                  ["grupo", "muestras", "area_km2", "porcentaje"])
        lineas += [
            "",
            f"Criterio para los grupos DUALES: **{suelos['criterio_duales']}**.",
            f"Afectan al {suelos['pct_dual']:.1f} % del area. Un grupo dual",
            "describe un suelo cuyo grupo depende de si esta drenado, y la",
            "eleccion cambia el numero de curva en decenas de unidades sobre esa",
            "fraccion. Debe quedar declarada en el informe.",
            "",
        ]
        lineas += _tabla_markdown(suelos["reparto_crudo"],
                                  ["codigo", "etiqueta", "muestras", "porcentaje"])
        lineas.append("")
    lineas.append("")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def _cerrar(logger, resultado, base, ruta_json, inicio, codigo):
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

    if ruta_json is None:
        ruta_json = (rutas.directorio("procesado", base, crear=True)
                     / "M10_morfometria.json")
    reporte = {
        "modulo": MODULO,
        "modo": resultado.modo,
        "unidades": resultado.unidades,
        "drenaje": resultado.drenaje,
        "magnitudes": resultado.magnitudes,
        "rezago": resultado.rezago,
        "suelos": resultado.suelos,
        "tiempo_concentracion": resultado.adoptados,
        "matriz_aplicabilidad": resultado.tiempos,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=2),
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
    analizador = argparse.ArgumentParser(
        prog="M10_morfometria.py",
        description="Caracterizacion morfometrica e hidrologica.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorRutas, ErrorConfiguracion, ErrorFormato) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR


if __name__ == "__main__":
    sys.exit(main())

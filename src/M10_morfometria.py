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
from comun.config import Config, cargar  # noqa: E402
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
        "perimetro_km": round(perimetro_km, 3),
        "perimetro_metodo": metodo,
        "aristas_frontera": contorno["aristas_frontera"],
        "aristas_compartidas": contorno["aristas_compartidas"],
        "aristas_repetidas": contorno["aristas_repetidas"],
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
    y = info.y_de_fila(fila)
    for x_inicio, x_fin in geometria.tramos_de_barrido(aristas, y):
        desde = math.ceil((x_inicio - info.origen_x) / info.tamano_x - 0.5)
        hasta = math.ceil((x_fin - info.origen_x) / info.tamano_x - 0.5) - 1
        desde = max(int(desde), 0)
        hasta = min(int(hasta), info.ancho - 1)
        if hasta >= desde:
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
        elif parametros["coef_compacidad"] > 1.5:
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
                "concentracion no es el parametro que gobierna la respuesta: "
                "corresponde transito hidraulico, coherente con el modo de "
                "analisis general declarado.",
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

    logger.info("Figuras de relieve escritas en %s",
                rutas.relativa(directorio, base))


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

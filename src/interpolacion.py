#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Interpolación de estaciones a superficie, compartida por los módulos SIG
========================================================================
Entorno: Python de QGIS. NO importa librerías del venv.

La usan el M06 (isoyetas de precipitación multianual por fase ENSO) y el M08
(isoyetas de Pmáx por periodo de retorno), que hacen lo mismo sobre magnitudes
distintas: convertir un conjunto de puntos con valor en una superficie y en
curvas, y medir cuánto vale esa superficie.

Vive aparte porque duplicar el algoritmo en dos módulos garantiza que se
corrijan por separado. La doctrina de un módulo por script (CLAUDE.md, sección
2) se refiere a los ejecutables; las piezas compartidas son justamente lo que
esta capa existe para concentrar, igual que comun, sig, graficos, estadistica y
frecuencia.

Sobre la malla. La resolución de las isoyetas es propia y no la del DEM: se
interpolan decenas de estaciones separadas kilómetros, y una malla mil veces más
fina que ese espaciamiento aparenta una precisión que la información no tiene.

Sobre la validación. Interpolar siempre produce una superficie; la pregunta es
cuánto se parece a lo que habría medido una estación que no participó. Sin esa
cifra el método adoptado no se puede defender frente a otro.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable, Sequence

from comun.errores import ErrorFormato, ErrorHidrologia, ErrorRutas

__all__ = [
    "idw",
    "validacion_dejando_uno_fuera",
    "gradiente_altitudinal",
    "rango_de_curvas",
    "interpolar_idw",
    "recortar_a_area",
    "generar_curvas",
    "estadisticas_raster",
    "extension_de_area",
    "leer_raster",
    "reproyectar_raster",
    "METODOS_IMPLEMENTADOS",
]

METODOS_IMPLEMENTADOS = ("IDW",)


# =============================================================================
# Funciones puras
# =============================================================================
def idw(
    x: float, y: float,
    muestras: Sequence[tuple[float, float, float]],
    potencia: float = 2.0,
    radio: float | None = None,
) -> float | None:
    """
    Estimación por distancia inversa ponderada en un punto.

    Se implementa aquí y no se delega para poder hacer validación cruzada sin
    generar un raster por cada punto excluido. El resultado debe coincidir con
    el del algoritmo de QGIS, que usa la misma formulación.

    Si el punto coincide con una muestra devuelve su valor: la ponderación por
    distancia no está definida a distancia cero, y aproximarla introduciría un
    valor que no es el medido.
    """
    numerador = 0.0
    denominador = 0.0
    for punto_x, punto_y, valor in muestras:
        distancia = math.hypot(x - punto_x, y - punto_y)
        if distancia == 0.0:
            return float(valor)
        if radio is not None and distancia > radio:
            continue
        peso = 1.0 / (distancia ** potencia)
        numerador += peso * valor
        denominador += peso
    if denominador == 0.0:
        return None
    return numerador / denominador


def validacion_dejando_uno_fuera(
    muestras: Sequence[tuple[float, float, float]],
    potencia: float = 2.0,
    radio: float | None = None,
) -> dict[str, Any]:
    """
    Deja fuera cada estación por turno y estima su valor con las demás.

    Es la misma lógica que la validación cruzada del complemento en el M05: sin
    ella no se puede decir si la superficie describe algo o solo interpola
    ruido. Un Nash-Sutcliffe cercano a cero significa que el campo no mejora
    usar el promedio.
    """
    if len(muestras) < 3:
        return {"n": len(muestras), "error": "menos de tres estaciones"}
    reales: list[float] = []
    estimados: list[float] = []
    for indice in range(len(muestras)):
        resto = [m for j, m in enumerate(muestras) if j != indice]
        estimado = idw(muestras[indice][0], muestras[indice][1], resto,
                       potencia, radio)
        if estimado is None:
            continue
        reales.append(float(muestras[indice][2]))
        estimados.append(float(estimado))
    if not reales:
        return {"n": len(muestras), "error": "ninguna estimación posible"}

    residuos = [e - r for e, r in zip(estimados, reales)]
    n = len(residuos)
    media_real = sum(reales) / n
    variacion = sum((r - media_real) ** 2 for r in reales)
    rmse = math.sqrt(sum(d * d for d in residuos) / n)
    return {
        "n": n,
        "rmse_mm": round(rmse, 2),
        "mae_mm": round(sum(abs(d) for d in residuos) / n, 2),
        "sesgo_mm": round(sum(residuos) / n, 2),
        "rmse_relativo_pct": round(100.0 * rmse / media_real, 1)
        if media_real else None,
        "nash_sutcliffe": round(
            1.0 - sum(d * d for d in residuos) / variacion, 4)
        if variacion > 0 else None,
    }


def gradiente_altitudinal(
    muestras: Sequence[tuple[float, float]],
) -> dict[str, Any]:
    """
    Ajusta la magnitud contra la altitud y devuelve su poder explicativo.

    CLAUDE.md, sección 6, define la zonificación pluviométrica del M11 con
    "gradiente altitudinal". Estos módulos miden si ese gradiente EXISTE en la
    red disponible, porque prescribir un método no garantiza que el dato lo
    sustente.
    """
    lista = [(float(z), float(p)) for z, p in muestras]
    if len(lista) < 3:
        return {"n": len(lista), "error": "menos de tres estaciones"}
    zetas = [z for z, _ in lista]
    pes = [p for _, p in lista]
    n = len(lista)
    media_z = sum(zetas) / n
    media_p = sum(pes) / n
    covarianza = sum((z - media_z) * (p - media_p) for z, p in lista)
    varianza_z = sum((z - media_z) ** 2 for z in zetas)
    varianza_p = sum((p - media_p) ** 2 for p in pes)
    if varianza_z == 0 or varianza_p == 0:
        return {"n": n, "error": "sin variación en altitud o magnitud"}
    pendiente = covarianza / varianza_z
    correlacion = covarianza / math.sqrt(varianza_z * varianza_p)
    return {
        "n": n,
        "pendiente_mm_por_m": round(pendiente, 4),
        "r": round(correlacion, 3),
        "r2": round(correlacion ** 2, 3),
        "altitud_min_m": round(min(zetas), 0),
        "altitud_max_m": round(max(zetas), 0),
    }


def rango_de_curvas(
    valores: Iterable[float], intervalo: float,
) -> tuple[float, float]:
    """
    Primera y última curva múltiplo del intervalo dentro del rango de datos.

    Se ajusta a múltiplos exactos para que las curvas de tandas distintas sean
    comparables entre sí: si cada una empezara en su propio mínimo, dos mapas
    del mismo estudio tendrían leyendas que no se corresponden.
    """
    lista = [float(v) for v in valores]
    if not lista:
        return (0.0, 0.0)
    primera = math.ceil(min(lista) / intervalo) * intervalo
    ultima = math.floor(max(lista) / intervalo) * intervalo
    return (primera, ultima)


# =============================================================================
# Geoprocesamiento
# =============================================================================
def extension_de_area(ruta_area: Path, margen_m: float):
    """Extensión de la capa de área, holgada por el margen pedido."""
    from qgis.core import QgsVectorLayer
    capa = QgsVectorLayer(str(ruta_area), "area", "ogr")
    if not capa.isValid():
        raise ErrorRutas(f"no se pudo abrir la capa de área {ruta_area}.")
    extension = capa.extent()
    extension.grow(margen_m)
    return extension, capa


def interpolar_idw(puntos_shp: Path, campo: str, extension, resolucion: float,
                   potencia: float, destino: Path, crs_calculo: str) -> Path:
    """
    Ejecuta la interpolación IDW de QGIS sobre la capa de puntos.

    La cadena INTERPOLATION_DATA es el punto frágil del algoritmo: codifica
    fuente, proveedor, índice de campo y tipo en un solo texto separado por
    '::~::'. Se construye aquí y en un solo sitio, de modo que un cambio de
    formato en QGIS se corrija una vez.
    """
    import processing
    from qgis.core import QgsVectorLayer

    capa = QgsVectorLayer(str(puntos_shp), "puntos", "ogr")
    if not capa.isValid():
        raise ErrorRutas(f"no se pudo abrir {puntos_shp}.")
    indice = capa.fields().indexFromName(campo)
    if indice < 0:
        raise ErrorFormato(
            f"la capa {puntos_shp.name} no tiene el campo {campo!r}. "
            f"Disponibles: {[f.name() for f in capa.fields()]}."
        )
    datos = f"{capa.source()}::~::0::~::{indice}::~::0"
    columnas = max(1, int(round(extension.width() / resolucion)))
    filas = max(1, int(round(extension.height() / resolucion)))
    processing.run("qgis:idwinterpolation", {
        "INTERPOLATION_DATA": datos,
        "DISTANCE_COEFFICIENT": float(potencia),
        "EXTENT": (f"{extension.xMinimum()},{extension.xMaximum()},"
                   f"{extension.yMinimum()},{extension.yMaximum()}"
                   f" [{crs_calculo}]"),
        "PIXEL_SIZE": float(resolucion),
        "COLUMNS": columnas,
        "ROWS": filas,
        "OUTPUT": str(destino),
    })
    if not destino.is_file():
        raise ErrorHidrologia(f"la interpolación no produjo {destino.name}.")
    return destino


def recortar_a_area(raster: Path, ruta_area: Path, destino: Path) -> Path:
    """Recorta el raster al área de influencia, con nodata fuera."""
    import processing
    processing.run("gdal:cliprasterbymasklayer", {
        "INPUT": str(raster),
        "MASK": str(ruta_area),
        "SOURCE_CRS": None,
        "TARGET_CRS": None,
        "NODATA": -9999,
        "ALPHA_BAND": False,
        "CROP_TO_CUTLINE": True,
        "KEEP_RESOLUTION": True,
        "OUTPUT": str(destino),
    })
    if not destino.is_file():
        raise ErrorHidrologia(f"el recorte no produjo {destino.name}.")
    return destino


def generar_curvas(raster: Path, destino: Path, intervalo: float,
                   crs_calculo: str, campo: str = "P_mm") -> Path:
    """Deriva las curvas de isoyeta del raster recortado."""
    import processing
    import sig
    processing.run("gdal:contour", {
        "INPUT": str(raster),
        "BAND": 1,
        "INTERVAL": float(intervalo),
        "FIELD_NAME": campo,
        "CREATE_3D": False,
        "IGNORE_NODATA": True,
        "NODATA": -9999,
        "OFFSET": 0,
        "OUTPUT": str(destino),
    })
    if not destino.is_file():
        raise ErrorHidrologia(f"el trazado de curvas no produjo {destino.name}.")
    sig.reescribir_prj_con_autoridad(destino, crs_calculo)
    return destino


def estadisticas_raster(ruta: Path) -> dict[str, float]:
    """Mínimo, máximo y media del raster, ignorando nodata."""
    from osgeo import gdal
    conjunto = gdal.Open(str(ruta))
    if conjunto is None:
        return {}
    banda = conjunto.GetRasterBand(1)
    minimo, maximo, media, desviacion = banda.ComputeStatistics(False)
    return {
        "min_mm": round(float(minimo), 1),
        "max_mm": round(float(maximo), 1),
        "media_mm": round(float(media), 1),
        "desviacion_mm": round(float(desviacion), 1),
    }


def leer_raster(ruta: Path, nodata: float = -9999.0):
    """
    Devuelve el arreglo del raster y su extensión en coordenadas del mapa.

    El nodata se convierte en nan para que matplotlib lo deje transparente y el
    recorte al área de influencia se vea como tal.
    """
    from osgeo import gdal
    import numpy as np

    conjunto = gdal.Open(str(ruta))
    if conjunto is None:
        return None, None
    banda = conjunto.GetRasterBand(1)
    datos = banda.ReadAsArray().astype("float64")
    propio = banda.GetNoDataValue()
    for valor in (propio, nodata):
        if valor is not None:
            datos[datos == valor] = np.nan
    gt = conjunto.GetGeoTransform()
    izquierda = gt[0]
    arriba = gt[3]
    derecha = izquierda + gt[1] * conjunto.RasterXSize
    abajo = arriba + gt[5] * conjunto.RasterYSize
    return datos, (izquierda, derecha, abajo, arriba)


def reproyectar_raster(origen: Path, destino: Path, crs_destino: str) -> Path:
    """Lleva el raster al sistema en que se rotulan las figuras."""
    import processing
    processing.run("gdal:warpreproject", {
        "INPUT": str(origen),
        "TARGET_CRS": crs_destino,
        "RESAMPLING": 1,
        "NODATA": -9999,
        "DATA_TYPE": 0,
        "OUTPUT": str(destino),
    })
    return destino if destino.is_file() else origen

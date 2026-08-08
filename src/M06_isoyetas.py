#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M06 - Isoyetas de precipitación total multianual por fase ENSO
==============================================================
Entorno: Python de QGIS (OSGeo4W Shell). NO importa librerías del venv.

CLAUDE.md, sección 6, cierra dos decisiones que este módulo materializa:
"Isoyetas totales | Precipitación total mensual multianual por fase ENSO" e
"Interpolación | IDW por defecto (Vargas et al.). Kriging configurable".

Qué interpola. El M05b entrega, por estación y por fase, el total anual que
cabría esperar de un año completo bajo esa fase. Este módulo convierte esos
puntos en superficie y en curvas de isoyeta, una tanda por fase.

Sobre la malla. Las isoyetas usan malla propia, declarada en 'isoyetas', y NO la
del DEM. El área mide 146 por 115 km: a la resolución del terreno serían 107
millones de celdas y 429 MB por raster. El argumento de fondo no es el peso sino
el soporte del dato, porque se interpolan unas decenas de estaciones separadas
kilómetros y una malla mil veces más fina que ese espaciamiento aparenta una
precisión que la información no tiene. Las curvas se entregan además en
vectorial, que se dibuja bien a cualquier escala del pliego.

Solo entran los totales completos. El M05b marca con 'completo' en falso los que
no cubren los doce meses del año; mezclarlos produciría un campo con saltos que
son del muestreo y no del clima.

La validación cruzada deja uno fuera. Interpolar siempre produce una superficie,
y la pregunta es cuánto se parece a lo que habría medido una estación que no
participó. Sin esa cifra, el método adoptado no se puede defender frente a otro.

ADVERTENCIA que el módulo reporta y no puede resolver. La red conservada tras el
M05 se extiende entre 1850 y 3378 m, porque el criterio de consistencia eliminó
las estaciones de cota baja. Todo valor que la interpolación produzca por debajo
de esa franja es extrapolación, no interpolación.

Productos:
    data/03_SIG/raster/isoyetas/isoyetas_<fase>.tif
    data/03_SIG/vector/isoyetas/isoyetas_<fase>.shp
    data/03_SIG/vector/isoyetas/estaciones_isoyetas_<fase>.shp
    data/02_procesado/M06_isoyetas.json

Uso (desde el OSGeo4W Shell):
    python-qgis src/M06_isoyetas.py

Códigos de salida:
    0  isoyetas producidas
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
from typing import Any, Iterable, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import campos as mod_campos  # noqa: E402
from comun import entorno, esquema, registro, rutas, shapefile  # noqa: E402
from comun.campos import CampoSalida  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M06"
DESCRIPCION = "Isoyetas de precipitación total multianual por fase ENSO"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# El Catálogo Nacional publica la ubicación en MAGNA-SIRGAS geográfico, no en
# WGS84. Se declara igual que en el M04b y el M05.
CRS_CATALOGO = "EPSG:4686"

METODOS_IMPLEMENTADOS = ("IDW",)

CAMPOS_ESTACION: tuple[CampoSalida, ...] = (
    CampoSalida("codigo", "Código de la estación", "texto", 15),
    CampoSalida("fase", "Fase ENSO", "texto", 10),
    CampoSalida("total_mm", "Precipitación total anual", "decimal", 12, 1, "mm"),
    CampoSalida("n_muestras", "Meses que sustentan el total", "entero", 8),
    CampoSalida("anios_eq", "Años equivalentes de muestra", "decimal", 8, 1),
    CampoSalida("altitud", "Altitud", "decimal", 12, 2, "m"),
)


@dataclass
class ResultadoM06:
    fases: list[str] = field(default_factory=list)
    por_fase: dict[str, dict[str, Any]] = field(default_factory=dict)
    validacion: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def leer_totales_por_fase(
    ruta: Path, delimitador: str, solo_completos: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """
    Lee el producto del M05b y agrupa por fase.

    Devuelve también cuántas parejas se excluyeron por total incompleto. El
    M05b marca con 'completo' en falso el total que no cubre los doce meses:
    mezclarlo con los completos produciría un campo con saltos que son del
    muestreo y no del clima.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}. Ejecutar el M05b antes que este módulo."
        )
    por_fase: dict[str, list[dict[str, Any]]] = {}
    excluidos = 0
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            completo = str(fila.get("completo", "")).strip().lower() == "true"
            if solo_completos and not completo:
                excluidos += 1
                continue
            try:
                registro_fila = {
                    "codigo": fila["codigo"].strip(),
                    "fase": fila["fase"].strip(),
                    "total_mm": float(fila["total_anual_mm"]),
                    "n_muestras": int(fila.get("n_muestras") or 0),
                    "anios_eq": float(fila.get("anios_equivalentes") or 0.0),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ErrorFormato(
                    f"fila ilegible en {ruta.name}: {fila} ({exc})."
                ) from exc
            por_fase.setdefault(registro_fila["fase"], []).append(registro_fila)
    return por_fase, excluidos


def leer_ubicaciones(ruta: Path) -> dict[str, dict[str, float]]:
    """Coordenadas geográficas y altitud de cada estación, desde la capa del M03."""
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la capa de estaciones en {ruta}.")
    ubicaciones: dict[str, dict[str, float]] = {}
    for fila in shapefile.leer_registros(
        ruta, ["codigo", "latitud", "longitud", "altitud"],
    ):
        codigo = str(fila.get("codigo", "")).strip()
        try:
            ubicaciones[codigo] = {
                "lon": float(fila["longitud"]),
                "lat": float(fila["latitud"]),
                "altitud": float(fila["altitud"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return ubicaciones


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

    Interpolar siempre produce una superficie; la pregunta es cuánto se parece a
    lo que habría medido una estación que no participó. Sin esta cifra el método
    adoptado no se puede defender frente a otro, que es lo mismo que exigía la
    validación cruzada del complemento en el M05.
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
    Ajusta precipitación contra altitud y devuelve su poder explicativo.

    CLAUDE.md, sección 6, define la zonificación pluviométrica del M11 con
    "gradiente altitudinal". Este módulo mide si ese gradiente EXISTE en la red
    disponible, porque prescribir un método no garantiza que el dato lo
    sustente, y aplicarlo sin comprobarlo produciría una zonificación apoyada en
    una relación inexistente.

    Recibe parejas (altitud, precipitación) y devuelve la pendiente, la
    correlación y el coeficiente de determinación.
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
        return {"n": n, "error": "sin variación en altitud o precipitación"}
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

    Se ajusta a múltiplos exactos para que las curvas de fases distintas sean
    comparables entre sí: si cada fase empezara en su propio mínimo, dos mapas
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
def _reproyectar(muestras, ubicaciones, crs_calculo):
    """
    Lleva las estaciones al CRS de cálculo y devuelve (x, y, valor, fila).

    La reproyección es explícita (CLAUDE.md, sección 5): el catálogo publica en
    MAGNA-SIRGAS geográfico y el cálculo ocurre en CTM12.
    """
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
        QgsProject,
    )
    conversor = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem(CRS_CATALOGO),
        QgsCoordinateReferenceSystem(crs_calculo),
        QgsProject.instance().transformContext(),
    )
    salida = []
    sin_ubicacion = []
    for fila in muestras:
        sitio = ubicaciones.get(fila["codigo"])
        if sitio is None:
            sin_ubicacion.append(fila["codigo"])
            continue
        punto = conversor.transform(QgsPointXY(sitio["lon"], sitio["lat"]))
        salida.append((punto.x(), punto.y(), fila["total_mm"],
                       dict(fila, altitud=sitio["altitud"])))
    return salida, sin_ubicacion


def _extension_de_area(ruta_area, margen_m: float):
    """Extensión de la capa de área, holgada por el margen pedido."""
    from qgis.core import QgsVectorLayer
    capa = QgsVectorLayer(str(ruta_area), "area", "ogr")
    if not capa.isValid():
        raise ErrorRutas(f"no se pudo abrir la capa de área {ruta_area}.")
    extension = capa.extent()
    extension.grow(margen_m)
    return extension, capa


def _interpolar_idw(puntos_shp, campo, extension, resolucion, potencia,
                    destino, crs_calculo):
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
        raise ErrorHidrologia(
            f"la interpolación no produjo {destino.name}."
        )
    return destino


def _recortar_a_area(raster, ruta_area, destino):
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


def _generar_curvas(raster, destino, intervalo, crs_calculo):
    """Deriva las curvas de isoyeta del raster recortado."""
    import processing
    processing.run("gdal:contour", {
        "INPUT": str(raster),
        "BAND": 1,
        "INTERVAL": float(intervalo),
        "FIELD_NAME": "P_mm",
        "CREATE_3D": False,
        "IGNORE_NODATA": True,
        "NODATA": -9999,
        "OFFSET": 0,
        "OUTPUT": str(destino),
    })
    if not destino.is_file():
        raise ErrorHidrologia(f"el trazado de curvas no produjo {destino.name}.")
    from comun import campos as _campos  # noqa: F401
    import sig
    sig.reescribir_prj_con_autoridad(destino, crs_calculo)
    return destino


def _estadisticas_raster(ruta):
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


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    con_graficas: bool = True,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Interpola por fase, recorta, traza curvas y valida."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    entorno.exigir_entorno(entorno.ENTORNO_QGIS, MODULO)

    import sig
    sig.iniciar_qgis(configuracion.obtener("entornos.qgis.prefix_path"))
    sig.inicializar_processing(
        configuracion.obtener("entornos.qgis.prefix_path"), logger)

    crs_calculo = configuracion.obtener("crs.calculo")
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")
    metodo = str(configuracion.obtener("interpolacion.metodo")).upper()
    potencia = float(configuracion.obtener("interpolacion.idw.potencia"))
    radio = configuracion.obtener("interpolacion.idw.radio_busqueda")
    resolucion = float(configuracion.obtener("isoyetas.resolucion_m"))
    intervalo = float(configuracion.obtener("isoyetas.intervalo_mm"))
    minimo_estaciones = int(configuracion.obtener("isoyetas.minimo_estaciones"))
    solo_completos = bool(
        configuracion.obtener("isoyetas.solo_totales_completos"))
    crs_figuras_efectivo = (configuracion.obtener("graficos.crs_figuras")
                            or configuracion.obtener("punto_descarga.crs"))

    totales = rutas.directorio("procesado_enso", base) / \
        "precipitacion_por_fase.csv"
    capa_estaciones = rutas.resolver(
        configuracion.obtener("estaciones.salida_seleccionadas"), base)
    ruta_area = rutas.directorio("sig_vector", base) / "area_influencia.shp"

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"totales por fase": rutas.relativa(totales, base),
                 "estaciones": rutas.relativa(capa_estaciones, base),
                 "area de influencia": rutas.relativa(ruta_area, base)},
        parametros={
            "interpolacion.metodo": metodo,
            "interpolacion.idw.potencia": potencia,
            "isoyetas.resolucion_m": resolucion,
            "isoyetas.intervalo_mm": intervalo,
            "isoyetas.solo_totales_completos": solo_completos,
        },
    )

    resultado = ResultadoM06()

    if metodo not in METODOS_IMPLEMENTADOS:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "interpolacion.metodo",
            f"el metodo declarado es {metodo!r} y este modulo implementa "
            f"{list(METODOS_IMPLEMENTADOS)}. CLAUDE.md, seccion 6, fija IDW como "
            "predeterminado citando a Vargas et al.; el kriging no viene en el "
            "nucleo de QGIS y su proveedor debe declararse antes de usarlo. Se "
            "detiene en lugar de sustituir el metodo en silencio.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    por_fase, excluidos = leer_totales_por_fase(
        totales, delimitador, solo_completos)
    ubicaciones = leer_ubicaciones(capa_estaciones)
    if excluidos:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "isoyetas.totales_incompletos",
            f"{excluidos} pareja(s) estacion-fase excluidas por no cubrir los "
            "doce meses del anio. Mezclarlas produciria un campo con saltos que "
            "son del muestreo y no del clima.",
        ))

    # El rango debe salir de las estaciones que REALMENTE se interpolan, no de
    # todas las del catalogo del M03: aquellas incluyen las que el M05 elimino.
    usadas = {f["codigo"] for filas in por_fase.values() for f in filas}
    cotas = [ubicaciones[c]["altitud"] for c in usadas
             if c in ubicaciones and ubicaciones[c].get("altitud") is not None]
    if cotas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "isoyetas.rango_altitudinal",
            f"las {len(cotas)} estaciones que se interpolan se extienden entre "
            f"{min(cotas):.0f} y {max(cotas):.0f} m. Todo valor que el campo "
            "produzca fuera de esa franja es EXTRAPOLACION, no interpolacion, y "
            "debe declararse asi en el informe.",
        ))

    directorio_raster = rutas.resolver(
        configuracion.obtener("isoyetas.salida_raster"), base)
    directorio_curvas = rutas.resolver(
        configuracion.obtener("isoyetas.salida_curvas"), base)
    directorio_raster.mkdir(parents=True, exist_ok=True)
    directorio_curvas.mkdir(parents=True, exist_ok=True)
    temporal = rutas.directorio("sig_temp", base, crear=True)

    extension, _ = _extension_de_area(ruta_area, resolucion * 10)
    rasters_por_fase: dict[str, Path] = {}
    puntos_por_fase: dict[str, list] = {}

    for fase in sorted(por_fase):
        with registro.bloque(logger, f"Fase {fase}"):
            muestras, sin_ubicacion = _reproyectar(
                por_fase[fase], ubicaciones, crs_calculo)
            if sin_ubicacion:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"isoyetas.{fase}.sin_ubicacion",
                    f"{len(sin_ubicacion)} estacion(es) sin coordenadas en la "
                    f"capa del M03: {sin_ubicacion[:6]}. No entran al campo.",
                ))
            if len(muestras) < minimo_estaciones:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"isoyetas.{fase}.pocas_estaciones",
                    f"solo {len(muestras)} estacion(es) con total completo en la "
                    f"fase {fase}, por debajo del minimo de {minimo_estaciones}. "
                    "No se interpola: seria extrapolacion entre unos pocos "
                    "puntos.",
                ))
                continue

            puntos_shp = directorio_curvas / f"estaciones_isoyetas_{fase}.shp"
            geometrias = []
            valores = []
            from qgis.core import QgsGeometry, QgsPointXY
            for x, y, valor, fila in muestras:
                geometrias.append(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
                valores.append({
                    "codigo": fila["codigo"], "fase": fase,
                    "total_mm": valor, "n_muestras": fila["n_muestras"],
                    "anios_eq": fila["anios_eq"], "altitud": fila["altitud"],
                })
            sig.escribir_capa(puntos_shp, CAMPOS_ESTACION, geometrias, valores,
                              crs_calculo, "Point")
            mod_campos.escribir_diccionario(
                CAMPOS_ESTACION,
                puntos_shp.with_name(f"{puntos_shp.stem}_campos.csv"),
                puntos_shp.stem, delimitador)
            resultado.productos.append(rutas.relativa(puntos_shp, base))

            crudo = temporal / f"idw_{fase}.tif"
            _interpolar_idw(puntos_shp, "total_mm", extension, resolucion,
                            potencia, crudo, crs_calculo)
            raster = directorio_raster / f"isoyetas_{fase}.tif"
            _recortar_a_area(crudo, ruta_area, raster)
            resultado.productos.append(rutas.relativa(raster, base))
            rasters_por_fase[fase] = raster
            puntos_por_fase[fase] = [(x, y) for x, y, _, _ in muestras]

            curvas = directorio_curvas / f"isoyetas_{fase}.shp"
            _generar_curvas(raster, curvas, intervalo, crs_calculo)
            resultado.productos.append(rutas.relativa(curvas, base))

            solo_xyz = [(x, y, v) for x, y, v, _ in muestras]
            gradiente = gradiente_altitudinal(
                [(fila["altitud"], valor) for _, _, valor, fila in muestras])
            validacion = validacion_dejando_uno_fuera(
                solo_xyz, potencia, float(radio) if radio else None)
            validacion["fase"] = fase
            resultado.validacion.append(validacion)

            primera, ultima = rango_de_curvas(
                [v for _, _, v in solo_xyz], intervalo)
            resultado.por_fase[fase] = {
                "estaciones": len(muestras),
                "min_estacion_mm": round(min(v for _, _, v in solo_xyz), 1),
                "max_estacion_mm": round(max(v for _, _, v in solo_xyz), 1),
                "primera_curva_mm": primera,
                "ultima_curva_mm": ultima,
                "raster": _estadisticas_raster(raster),
                "validacion": validacion,
                "gradiente_altitudinal": gradiente,
            }
            resultado.fases.append(fase)
            logger.info(
                "%d estacion(es) | %.0f a %.0f mm | RMSE %s mm (%s%%)",
                len(muestras), min(v for _, _, v in solo_xyz),
                max(v for _, _, v in solo_xyz),
                validacion.get("rmse_mm", "?"),
                validacion.get("rmse_relativo_pct", "?"))

    if rasters_por_fase and con_graficas:
        with registro.bloque(logger, "Figuras"):
            puntos_xy = {
                fase: _puntos_estaciones(puntos, crs_figuras_efectivo,
                                         crs_calculo)
                for fase, puntos in puntos_por_fase.items()
            }
            _figuras(configuracion, base, resultado, rasters_por_fase,
                     puntos_xy, ruta_area, crs_calculo, temporal, logger)

    resultado.hallazgos.extend(_resumir(resultado, configuracion))
    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


# =============================================================================
# Figuras
# =============================================================================
def _leer_raster(ruta, nodata=-9999.0):
    """
    Devuelve el arreglo del raster y su extension en coordenadas del mapa.

    El nodata se convierte en nan para que matplotlib lo deje transparente y el
    recorte al area de influencia se vea como tal.
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


def _reproyectar_raster(origen, destino, crs_destino):
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


def _contorno_area(ruta_area, crs_destino):
    """Anillos del area de influencia, en el sistema de la figura."""
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
        QgsVectorLayer, QgsWkbTypes,
    )
    capa = QgsVectorLayer(str(ruta_area), "area", "ogr")
    if not capa.isValid():
        return []
    conversor = QgsCoordinateTransform(
        capa.crs(), QgsCoordinateReferenceSystem(crs_destino),
        QgsProject.instance().transformContext())
    anillos = []
    for entidad in capa.getFeatures():
        geometria = entidad.geometry()
        geometria.transform(conversor)
        if QgsWkbTypes.isMultiType(geometria.wkbType()):
            partes = geometria.asMultiPolygon()
        else:
            partes = [geometria.asPolygon()]
        for parte in partes:
            for anillo in parte:
                anillos.append([(punto.x(), punto.y()) for punto in anillo])
    return anillos


def _puntos_estaciones(muestras_por_fase, crs_destino, crs_calculo):
    """Ubicacion de las estaciones en el sistema de la figura."""
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
        QgsProject,
    )
    conversor = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem(crs_calculo),
        QgsCoordinateReferenceSystem(crs_destino),
        QgsProject.instance().transformContext())
    salida = []
    for x, y in muestras_por_fase:
        punto = conversor.transform(QgsPointXY(x, y))
        salida.append((punto.x(), punto.y()))
    return salida


def _figuras(configuracion, base, resultado, rasters, puntos_xy, ruta_area,
             crs_calculo, temporal, logger) -> None:
    """
    Emite las figuras de isoyetas para el informe.

    El Python de QGIS 4.2.0 trae matplotlib, de modo que se usa el mismo modulo
    de estilo que el resto del estudio y las figuras comparten aspecto vengan
    del entorno que vengan. Si faltara, el modulo lo reporta y sigue: las
    figuras son producto secundario frente al raster y las curvas.
    """
    try:
        import graficos
        import numpy as np
    except ImportError as exc:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron generar las figuras: {exc}. El raster y las curvas "
            "si se escribieron.",
        ))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(configuracion.obtener("graficos.directorio"), base)
    crs_figuras = (configuracion.obtener("graficos.crs_figuras")
                   or configuracion.obtener("punto_descarga.crs"))
    intervalo = float(configuracion.obtener("isoyetas.intervalo_mm"))

    campos = {}
    for fase, ruta in rasters.items():
        reproyectado = _reproyectar_raster(
            ruta, temporal / f"fig_{fase}.tif", crs_figuras)
        datos, extension = _leer_raster(reproyectado)
        if datos is not None:
            campos[fase] = (datos, extension)
    if not campos:
        return

    anillos = _contorno_area(ruta_area, crs_figuras)
    orden = [f for f in ("nino", "neutral", "nina") if f in campos]
    titulo_de = {"nino": "El Niño", "nina": "La Niña", "neutral": "Neutral"}

    # Escala comun a las tres fases: sin ella, tres mapas del mismo estudio
    # tendrian leyendas distintas y el contraste entre fases no se veria.
    todos = np.concatenate([d[np.isfinite(d)].ravel() for d, _ in campos.values()])
    minimo = float(np.floor(np.nanpercentile(todos, 1) / intervalo) * intervalo)
    maximo = float(np.ceil(np.nanpercentile(todos, 99) / intervalo) * intervalo)
    niveles = np.arange(minimo, maximo + intervalo, intervalo)

    # Limites tomados del area y no de la extension del raster: el raster se
    # interpola sobre un rectangulo holgado, y dejar que el eje se ajuste a el
    # deja el mapa nadando en blanco.
    if anillos:
        equis = [x for anillo in anillos for x, _ in anillo]
        yes = [y for anillo in anillos for _, y in anillo]
        margen = 0.03 * max(max(equis) - min(equis), max(yes) - min(yes))
        limites = (min(equis) - margen, max(equis) + margen,
                   min(yes) - margen, max(yes) + margen)
    else:
        limites = None

    def _fondo(ax):
        for anillo in anillos:
            ax.plot([x for x, _ in anillo], [y for _, y in anillo],
                    color=graficos.GRIS_CONTEXTO, linewidth=0.9, zorder=4)
        if limites is not None:
            ax.set_xlim(limites[0], limites[1])
            ax.set_ylim(limites[2], limites[3])
        # 'box' ajusta la caja del eje y no el rango de datos: con 'datalim' el
        # eje se estira para cumplir la proporcion y el mapa queda diminuto.
        ax.set_aspect("equal", adjustable="box")
        graficos.rotular_en_miles(ax, maximo_marcas=4)
        ax.tick_params(labelsize=estilo.tamano_fuente - 3)

    # --- Comparacion de las tres fases ---------------------------------------
    with graficos.figura(
        estilo,
        titulo="Precipitación total anual multianual por fase ENSO",
        filas=1, columnas=len(orden),
        alto_cm=max(estilo.alto_cm, 11.0),
    ) as (fig, ejes):
        imagen = None
        for indice, fase in enumerate(orden):
            ax = ejes[0][indice]
            datos, extension = campos[fase]
            imagen = ax.imshow(datos, extent=extension, origin="upper",
                               cmap="YlGnBu", vmin=minimo, vmax=maximo,
                               zorder=1)
            ax.contour(datos, levels=niveles, extent=extension, origin="upper",
                       colors="#333333", linewidths=0.4, zorder=3)
            if puntos_xy.get(fase):
                ax.scatter([x for x, _ in puntos_xy[fase]],
                           [y for _, y in puntos_xy[fase]],
                           s=7.0, color="#c00000", edgecolor="white",
                           linewidth=0.3, zorder=5)
            _fondo(ax)
            ax.set_title(titulo_de.get(fase, fase),
                         fontsize=estilo.tamano_fuente, loc="left",
                         color="#333333")
            if indice:
                ax.set_yticklabels([])
        if imagen is not None:
            barra = fig.colorbar(imagen, ax=ejes.ravel().tolist(),
                                 fraction=0.03, pad=0.02)
            barra.set_label("precipitación anual (mm)",
                            fontsize=estilo.tamano_fuente - 1)
            barra.ax.tick_params(labelsize=estilo.tamano_fuente - 2)
        fig.supxlabel(f"Este (m), {crs_figuras}",
                      fontsize=estilo.tamano_fuente - 1)
        for ruta in graficos.guardar(fig, directorio / "M06_isoyetas", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Diferencia respecto de la fase neutral ------------------------------
    if "neutral" not in campos:
        logger.info("Figuras escritas en %s", rutas.relativa(directorio, base))
        return
    base_datos, base_ext = campos["neutral"]
    diferencias = [f for f in ("nino", "nina")
                   if f in campos and campos[f][0].shape == base_datos.shape]
    if not diferencias:
        logger.info("Figuras escritas en %s", rutas.relativa(directorio, base))
        return

    with graficos.figura(
        estilo,
        titulo="Cambio de la precipitación anual respecto de la fase neutral",
        filas=1, columnas=len(diferencias),
        alto_cm=max(estilo.alto_cm, 11.0),
    ) as (fig, ejes):
        with np.errstate(invalid="ignore", divide="ignore"):
            campos_pct = {f: 100.0 * (campos[f][0] - base_datos) / base_datos
                          for f in diferencias}
        tope = float(np.nanpercentile(
            np.abs(np.concatenate([c[np.isfinite(c)].ravel()
                                   for c in campos_pct.values()])), 98))
        tope = max(5.0, round(tope))
        imagen = None
        for indice, fase in enumerate(diferencias):
            ax = ejes[0][indice]
            imagen = ax.imshow(campos_pct[fase], extent=base_ext,
                               origin="upper", cmap="RdBu", vmin=-tope,
                               vmax=tope, zorder=1)
            _fondo(ax)
            ax.set_title(f"{titulo_de.get(fase, fase)} frente a neutral",
                         fontsize=estilo.tamano_fuente, loc="left",
                         color="#333333")
            if indice:
                ax.set_yticklabels([])
        if imagen is not None:
            barra = fig.colorbar(imagen, ax=ejes.ravel().tolist(),
                                 fraction=0.03, pad=0.02)
            barra.set_label("cambio (%)", fontsize=estilo.tamano_fuente - 1)
            barra.ax.tick_params(labelsize=estilo.tamano_fuente - 2)
        fig.supxlabel(f"Este (m), {crs_figuras}",
                      fontsize=estilo.tamano_fuente - 1)
        for ruta in graficos.guardar(fig, directorio / "M06_contraste_fases",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    logger.info("Figuras escritas en %s", rutas.relativa(directorio, base))


def _resumir(resultado, configuracion) -> list[Hallazgo]:
    """Informativos de sintesis y lectura de la validacion."""
    hallazgos: list[Hallazgo] = []
    if not resultado.fases:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "isoyetas.sin_fases",
            "no se produjo ninguna isoyeta. Revisar el producto del M05b y el "
            "minimo de estaciones declarado.",
        ))
        return hallazgos

    hallazgos.append(Hallazgo(
        INFORMATIVO, "isoyetas.producidas",
        f"{len(resultado.fases)} fase(s) interpoladas: "
        + ", ".join(f"{f} ({resultado.por_fase[f]['estaciones']} est.)"
                    for f in resultado.fases) + ".",
    ))

    # El gradiente altitudinal que CLAUDE.md prescribe para el M11 debe
    # comprobarse antes de aplicarlo: prescribir un metodo no garantiza que el
    # dato lo sustente.
    for fase in resultado.fases:
        gradiente = (resultado.por_fase[fase] or {}).get("gradiente_altitudinal")
        if not gradiente or "r2" not in gradiente:
            continue
        severidad = ADVERTENCIA if gradiente["r2"] < 0.30 else INFORMATIVO
        hallazgos.append(Hallazgo(
            severidad, f"isoyetas.{fase}.gradiente",
            f"gradiente altitudinal medido: {gradiente['pendiente_mm_por_m']} mm "
            f"por metro, r={gradiente['r']}, r2={gradiente['r2']}, sobre "
            f"{gradiente['n']} estacion(es) entre "
            f"{gradiente['altitud_min_m']:.0f} y "
            f"{gradiente['altitud_max_m']:.0f} m."
            + (" La altitud NO explica la precipitacion en esta red. CLAUDE.md, "
               "seccion 6, define la zonificacion pluviometrica del M11 con "
               "gradiente altitudinal: aplicarlo aqui apoyaria la zonificacion "
               "en una relacion inexistente. La causa probable es que el "
               "descarte por consistencia del M05 elimino las estaciones de "
               "cota baja y trunco el rango que mostraria el gradiente."
               if severidad == ADVERTENCIA else ""),
        ))

    for validacion in resultado.validacion:
        relativo = validacion.get("rmse_relativo_pct")
        if relativo is None:
            continue
        severidad = ADVERTENCIA if relativo > 15.0 else INFORMATIVO
        hallazgos.append(Hallazgo(
            severidad, f"isoyetas.{validacion['fase']}.validacion",
            f"validacion dejando uno fuera: RMSE {validacion['rmse_mm']} mm "
            f"({relativo}% de la media), sesgo {validacion['sesgo_mm']} mm, "
            f"Nash-Sutcliffe {validacion.get('nash_sutcliffe')}."
            + (" Un error relativo asi indica que el campo no reproduce bien lo "
               "que mediria una estacion ausente: la densidad de la red no "
               "sustenta el detalle del mapa." if severidad == ADVERTENCIA
               else ""),
        ))
    return hallazgos


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

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M06_isoyetas.json"
    reporte = {
        "modulo": MODULO,
        "fases": resultado.fases,
        "por_fase": resultado.por_fase,
        "validacion": resultado.validacion,
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
    try:
        import sig
        sig.finalizar_qgis()
    except Exception:  # noqa: BLE001
        pass
    return codigo, hallazgos


# =============================================================================
# Interfaz de linea de comandos
# =============================================================================
def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(
        prog="M06_isoyetas.py",
        description="Isoyetas de precipitacion multianual por fase ENSO.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--sin-graficas", action="store_true",
                            dest="sin_graficas")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida,
            con_graficas=not argumentos.sin_graficas,
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

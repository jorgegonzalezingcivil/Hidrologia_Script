#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M02 - DEM ALOS PALSAR, delimitación preliminar, envolvente y buffer
===================================================================
Entorno: Python de QGIS.

Encadena cuatro etapas:

1. Búsqueda y descarga de escenas ALOS PALSAR RTC sobre la subzona que entregó
   el M01, con holgura configurable.
2. Extracción del modelo de elevación de cada escena, mosaico y recorte al CRS
   de cálculo.
3. Delimitación preliminar de la cuenca desde el punto de descarga, ajustado a
   la celda de máxima acumulación de flujo.
4. Envolvente de la cuenca y área de influencia por buffer.

La delimitación es PRELIMINAR. La definitiva se hace de forma asistida en el
geoprocesamiento de HEC-HMS (M09), que es el único paso con intervención manual
obligatoria (CLAUDE.md, sección 4). Lo que produce el M02 sirve para acotar la
descarga de información y la selección de estaciones, no para el modelo.

CREDENCIALES. La descarga necesita una cuenta de Earthdata declarada en
~/.netrc. El módulo comprueba que exista la entrada y se detiene con
instrucciones si falta. Ni el módulo ni el log exponen usuario o clave.

VOLUMEN. ALOS PALSAR acumula decenas de adquisiciones sobre la misma huella
espacial y el DEM que las acompaña es el mismo. El módulo deduplica por huella y
después elige el subconjunto mínimo que cubre el área, lo que reduce la descarga
en un orden de magnitud. Antes de bajar nada reporta el volumen y se detiene si
supera los topes declarados.

Uso:
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" src/M02_dem_delimitacion.py
    ... --solo-planificar     consulta el catálogo y reporta, sin descargar
    ... --sin-descarga        usa solo las escenas ya presentes en disco

Códigos de salida:
    0  cuenca delimitada y capas escritas
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración, faltan credenciales o falló QGIS
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import red_drenaje as red  # noqa: E402
import sig  # noqa: E402
from comun import (  # noqa: E402
    asf, campos as mod_campos, entorno, esquema, registro, rutas, shapefile,
)
from comun.campos import CampoSalida  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorEntorno,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M02"
DESCRIPCION = "DEM, delimitación preliminar, envolvente y buffer"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

CAMPOS_CUENCA: tuple[CampoSalida, ...] = (
    CampoSalida("nombre", "Nombre de la cuenca", "texto", 60),
    CampoSalida("area_km2", "Área de la cuenca", "decimal", 20, 4, "km2"),
    CampoSalida("perim_km", "Perímetro de la cuenca", "decimal", 20, 4, "km"),
    CampoSalida("cota_max", "Cota máxima", "decimal", 20, 2, "m"),
    CampoSalida("cota_min", "Cota mínima", "decimal", 20, 2, "m"),
    CampoSalida("cota_med", "Cota media", "decimal", 20, 2, "m"),
    CampoSalida("x_salida", "Este del punto de salida ajustado", "decimal", 20, 3, "m"),
    CampoSalida("y_salida", "Norte del punto de salida ajustado", "decimal", 20, 3, "m"),
    CampoSalida("desp_m", "Desplazamiento aplicado al punto", "decimal", 20, 3, "m"),
    CampoSalida("res_dem_m", "Resolución del DEM", "decimal", 20, 3, "m"),
    CampoSalida("origen", "Carácter de la delimitación", "texto", 20),
)

CAMPOS_MARCO: tuple[CampoSalida, ...] = (
    CampoSalida("nombre", "Nombre de la geometría", "texto", 60),
    CampoSalida("tipo", "Tipo de geometría derivada", "texto", 30),
    CampoSalida("area_km2", "Área", "decimal", 20, 4, "km2"),
    CampoSalida("buffer_km", "Buffer aplicado", "decimal", 20, 4, "km"),
)


@dataclass
class PlanDescarga:
    """Selección de escenas y su volumen, antes de transferir nada."""

    encontradas: int = 0
    huellas: int = 0
    seleccionadas: list = field(default_factory=list)
    volumen_gb: float = 0.0
    cobertura_pct: float = 0.0
    ventana: tuple[str, str] = ("", "")

    def como_dict(self) -> dict[str, Any]:
        return {
            "ventana_adquisicion": {
                "inicio": self.ventana[0] or None,
                "fin": self.ventana[1] or None,
            },
            "escenas_encontradas": self.encontradas,
            "huellas_distintas": self.huellas,
            "escenas_seleccionadas": len(self.seleccionadas),
            "volumen_gb": round(self.volumen_gb, 3),
            "cobertura_pct": round(self.cobertura_pct, 2),
            "archivos": [e.nombre_archivo for e in self.seleccionadas],
            "fechas_escenas": sorted({
                str(e.fecha_escena)[:10] for e in self.seleccionadas
            }),
        }


@dataclass
class ResultadoM02:
    plan: PlanDescarga = field(default_factory=PlanDescarga)
    descargadas: list[str] = field(default_factory=list)
    dem: str = ""
    area_cuenca_km2: float = 0.0
    desplazamiento_m: float = 0.0
    capas: list[str] = field(default_factory=list)
    diccionarios: list[str] = field(default_factory=list)
    # Geometrías en EPSG:4326. El M03 corre en el venv, donde no hay con qué
    # reproyectar, y necesita el área de influencia en las mismas coordenadas
    # geográficas en que el catálogo de estaciones publica su ubicación.
    wkt_geografico: dict[str, str] = field(default_factory=dict)
    escenario: Any = None
    acotado: dict = field(default_factory=dict)
    cota_punto: float | None = None
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def seleccionar_cobertura(escenas: Sequence, objetivo, clase_geometria):
    """
    Elige el subconjunto mínimo de escenas que cubre el objetivo.

    Algoritmo voraz: en cada paso se toma la escena que más superficie pendiente
    cubre. Es la heurística estándar del problema de cobertura de conjuntos, que
    no garantiza el óptimo pero se acerca y es determinista si el orden de
    entrada lo es.

    Devuelve (seleccionadas, cobertura_pct).
    """
    area_objetivo = objetivo.area()
    if area_objetivo <= 0:
        return [], 0.0

    pendiente = clase_geometria(objetivo)
    disponibles = list(escenas)
    seleccionadas: list = []

    while disponibles and not pendiente.isEmpty():
        mejor_indice, mejor_area = -1, 0.0
        for indice, escena in enumerate(disponibles):
            huella = clase_geometria.fromWkt(escena.huella_wkt)
            if huella.isEmpty():
                continue
            comun = pendiente.intersection(huella)
            if comun.isEmpty():
                continue
            area = comun.area()
            if area > mejor_area:
                mejor_indice, mejor_area = indice, area

        if mejor_indice < 0 or mejor_area <= 0:
            break

        elegida = disponibles.pop(mejor_indice)
        seleccionadas.append(elegida)
        pendiente = pendiente.difference(
            clase_geometria.fromWkt(elegida.huella_wkt)
        )

    restante = pendiente.area() if not pendiente.isEmpty() else 0.0
    cobertura = max(0.0, 100.0 * (1.0 - restante / area_objetivo))
    return seleccionadas, cobertura


def extraer_dem(
    archivo_zip: Path, destino: Path, sufijo: str = ".dem.tif"
) -> list[Path]:
    """
    Extrae del producto RTC los archivos de elevación.

    Un producto ALOS PALSAR RTC contiene la imagen de radar, sus metadatos y el
    modelo de elevación empleado en la corrección de terreno. Solo interesa el
    último: extraer el resto multiplicaría el espacio en disco sin motivo.

    Excepciones
    -----------
    ErrorFormato
        Si el archivo no es un zip legible.
    """
    destino.mkdir(parents=True, exist_ok=True)
    extraidos: list[Path] = []

    try:
        with zipfile.ZipFile(archivo_zip) as comprimido:
            miembros = [
                nombre for nombre in comprimido.namelist()
                if nombre.lower().endswith(sufijo.lower())
            ]
            for miembro in miembros:
                salida = destino / Path(miembro).name
                if not salida.is_file():
                    with comprimido.open(miembro) as origen, \
                            salida.open("wb") as manejador:
                        manejador.write(origen.read())
                extraidos.append(salida)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ErrorFormato(
            f"No se pudo leer {archivo_zip.name}: {exc}"
        ) from exc

    return extraidos


def estadisticas_raster(ruta: Path, mascara=None) -> dict[str, float]:
    """
    Calcula cota mínima, máxima y media del ráster.

    Se hace con GDAL y numpy, ambos presentes en el Python de QGIS, para no
    depender de un algoritmo de Processing que puede cambiar de identificador
    entre versiones.
    """
    from osgeo import gdal
    import numpy as np

    conjunto = gdal.Open(str(ruta))
    if conjunto is None:
        raise ErrorFormato(f"GDAL no pudo abrir {ruta}")
    banda = conjunto.GetRasterBand(1)
    arreglo = banda.ReadAsArray().astype("float64")
    sin_dato = banda.GetNoDataValue()
    conjunto = None

    valido = np.isfinite(arreglo)
    if sin_dato is not None:
        valido &= arreglo != sin_dato
    if mascara is not None:
        valido &= mascara

    if not valido.any():
        return {"minimo": 0.0, "maximo": 0.0, "media": 0.0}

    datos = arreglo[valido]
    return {
        "minimo": float(datos.min()),
        "maximo": float(datos.max()),
        "media": float(datos.mean()),
    }


def toca_borde(ruta_cuenca: Path) -> bool:
    """
    Indica si la cuenca delimitada alcanza el borde del ráster.

    Una cuenca que toca el borde está truncada: el DEM no abarcó toda su
    superficie y el área resultante es menor que la real. Continuar con ella
    contaminaría todos los cálculos posteriores en silencio.
    """
    from osgeo import gdal
    import numpy as np

    conjunto = gdal.Open(str(ruta_cuenca))
    if conjunto is None:
        raise ErrorFormato(f"GDAL no pudo abrir {ruta_cuenca}")
    banda = conjunto.GetRasterBand(1)
    arreglo = banda.ReadAsArray()
    sin_dato = banda.GetNoDataValue()
    conjunto = None

    dentro = np.isfinite(arreglo.astype("float64"))
    if sin_dato is not None:
        dentro &= arreglo != sin_dato
    dentro &= arreglo > 0

    if not dentro.any():
        return False
    return bool(
        dentro[0, :].any() or dentro[-1, :].any()
        or dentro[:, 0].any() or dentro[:, -1].any()
    )


def ajustar_a_cauce(
    ruta_acumulacion: Path, este: float, norte: float, radio_m: float
) -> tuple[float, float, float]:
    """
    Desplaza el punto a la celda de mayor acumulación de flujo del entorno.

    El punto declarado por el consultor rara vez coincide con el cauce que el
    DEM modela. Sin este ajuste, una diferencia de dos o tres celdas produce una
    cuenca diminuta, y el error no se nota porque el módulo termina bien.

    Devuelve (este_ajustado, norte_ajustado, desplazamiento_m).
    """
    from osgeo import gdal
    import numpy as np

    conjunto = gdal.Open(str(ruta_acumulacion))
    if conjunto is None:
        raise ErrorFormato(f"GDAL no pudo abrir {ruta_acumulacion}")

    transformacion = conjunto.GetGeoTransform()
    banda = conjunto.GetRasterBand(1)
    arreglo = banda.ReadAsArray().astype("float64")
    conjunto = None

    origen_x, paso_x, _, origen_y, _, paso_y = transformacion
    columna = int((este - origen_x) / paso_x)
    fila = int((norte - origen_y) / paso_y)

    filas, columnas = arreglo.shape
    if not (0 <= fila < filas and 0 <= columna < columnas):
        raise ErrorFormato(
            "El punto de descarga cae fuera del ráster de acumulación. El DEM "
            "no cubre la posición declarada."
        )

    radio_celdas = max(1, int(radio_m / abs(paso_x)))
    fila_min = max(0, fila - radio_celdas)
    fila_max = min(filas, fila + radio_celdas + 1)
    col_min = max(0, columna - radio_celdas)
    col_max = min(columnas, columna + radio_celdas + 1)

    ventana = np.abs(arreglo[fila_min:fila_max, col_min:col_max])
    if not np.isfinite(ventana).any():
        return este, norte, 0.0

    plano = int(np.nanargmax(ventana))
    df, dc = np.unravel_index(plano, ventana.shape)
    fila_mejor, col_mejor = fila_min + int(df), col_min + int(dc)

    este_ajustado = origen_x + (col_mejor + 0.5) * paso_x
    norte_ajustado = origen_y + (fila_mejor + 0.5) * paso_y
    desplazamiento = (
        (este_ajustado - este) ** 2 + (norte_ajustado - norte) ** 2
    ) ** 0.5
    return este_ajustado, norte_ajustado, desplazamiento


# =============================================================================
# Etapas con QGIS
# =============================================================================
def area_de_busqueda(configuracion: Config, base: Path):
    """
    Construye el polígono de búsqueda a partir de la subzona del M01.

    Devuelve (geometria_calculo, wkt_geografico, crs_calculo).
    """
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
        QgsProject, QgsVectorLayer,
    )

    ruta_subzona = rutas.resolver(
        configuracion.obtener("subzonas_hidrograficas.salida_subzona"), base
    )
    if not ruta_subzona.is_file():
        raise ErrorFormato(
            f"No existe {ruta_subzona.name}. El M02 parte de la subzona que "
            "entrega el M01: ejecutarlo primero."
        )

    capa = QgsVectorLayer(str(ruta_subzona), "subzona", "ogr")
    if not capa.isValid():
        raise ErrorFormato(f"QGIS no pudo abrir {ruta_subzona}")

    entidades = list(capa.getFeatures())
    if not entidades:
        raise ErrorFormato(f"{ruta_subzona.name} no contiene ninguna entidad.")

    geometria = QgsGeometry(entidades[0].geometry())
    buffer_km = float(configuracion.obtener("dem.asf.buffer_busqueda_km"))
    if buffer_km > 0:
        geometria = geometria.buffer(buffer_km * 1000.0, 12)

    crs_calculo = QgsCoordinateReferenceSystem(configuracion.obtener("crs.calculo"))
    crs_geografico = QgsCoordinateReferenceSystem(
        configuracion.obtener("crs.geografico")
    )
    geografica = QgsGeometry(geometria)
    geografica.transform(QgsCoordinateTransform(
        capa.crs(), crs_geografico, QgsProject.instance().transformContext()
    ))

    return geometria, geografica.boundingBox().asWktPolygon(), crs_calculo


def planificar(
    configuracion: Config, wkt_geografico: str, logger: Any
) -> tuple[PlanDescarga, list[Hallazgo]]:
    """Consulta el catálogo, deduplica y elige la cobertura mínima."""
    from qgis.core import QgsGeometry

    hallazgos: list[Hallazgo] = []
    plan = PlanDescarga()

    fecha_inicio = configuracion.obtener("dem.asf.fecha_inicio", None)
    fecha_fin = configuracion.obtener("dem.asf.fecha_fin", None)
    if fecha_inicio or fecha_fin:
        logger.info(
            "Ventana de adquisición: %s a %s",
            fecha_inicio or "sin límite", fecha_fin or "sin límite",
        )

    escenas = asf.buscar(
        poligono_wkt=wkt_geografico,
        nivel=configuracion.obtener("dem.asf.nivel"),
        plataforma=configuracion.obtener("dem.asf.plataforma"),
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
    )
    plan.encontradas = len(escenas)
    plan.ventana = (fecha_inicio or "", fecha_fin or "")
    if not escenas:
        ventana = (f" en la ventana {fecha_inicio} a {fecha_fin}"
                   if fecha_inicio or fecha_fin else "")
        return plan, [Hallazgo(
            BLOQUEANTE, "dem.asf",
            f"el catálogo de ASF no devolvió ninguna escena para el área{ventana}. "
            "Ampliar la ventana de adquisición o revisar el área de búsqueda.",
        )]

    unicas = asf.deduplicar_por_huella(escenas)
    plan.huellas = len(unicas)

    objetivo = QgsGeometry.fromWkt(wkt_geografico)
    plan.seleccionadas, plan.cobertura_pct = seleccionar_cobertura(
        unicas, objetivo, QgsGeometry
    )
    resumen = asf.resumen_descarga(plan.seleccionadas)
    plan.volumen_gb = resumen["volumen_gb"]

    logger.info(
        "Catálogo: %d escena(s), %d huella(s) distinta(s). Selección: %d "
        "escena(s), %.2f GB, cobertura %.2f%%.",
        plan.encontradas, plan.huellas, len(plan.seleccionadas),
        plan.volumen_gb, plan.cobertura_pct,
    )
    logger.info(
        "Sin deduplicar habría sido %.2f GB.",
        asf.resumen_descarga(escenas)["volumen_gb"],
    )

    minima = float(configuracion.obtener("dem.asf.cobertura_minima_pct"))
    if plan.cobertura_pct < minima:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "dem.asf",
            f"las escenas disponibles cubren el {plan.cobertura_pct:.2f}% del "
            f"área de búsqueda, por debajo del {minima:g}% exigido. El DEM "
            "tendrá huecos.",
        ))

    tope_escenas = int(configuracion.obtener("dem.asf.max_escenas"))
    if len(plan.seleccionadas) > tope_escenas:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "dem.asf.max_escenas",
            f"la selección requiere {len(plan.seleccionadas)} escenas y el tope "
            f"es {tope_escenas}. Revisar el área o subir el tope de forma "
            "consciente.",
        ))

    tope_volumen = float(configuracion.obtener("dem.asf.max_volumen_gb"))
    if plan.volumen_gb > tope_volumen:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "dem.asf.max_volumen_gb",
            f"la descarga sumaría {plan.volumen_gb:.2f} GB y el tope es "
            f"{tope_volumen:g} GB.",
        ))

    return plan, hallazgos


def preparar_dem(
    configuracion: Config, base: Path, geometria_objetivo, crs_calculo, logger
) -> Path:
    """Mosaica los DEM extraídos, los reproyecta y los recorta."""
    import processing

    directorio = rutas.directorio("crudos_dem", base, crear=True)
    extraidos = sorted((directorio / "elevacion").glob("*.tif")) \
        if (directorio / "elevacion").is_dir() else []
    if not extraidos:
        raise ErrorFormato(
            f"No hay archivos de elevación en {directorio / 'elevacion'}. "
            "Descargar las escenas antes de mosaicar."
        )

    temporal = rutas.directorio("sig_temp", base, crear=True)
    virtual = temporal / "dem_mosaico.vrt"
    processing.run("gdal:buildvirtualraster", {
        "INPUT": [str(p) for p in extraidos],
        "RESOLUTION": 0, "SEPARATE": False, "PROJ_DIFFERENCE": False,
        "OUTPUT": str(virtual),
    })
    logger.info("Mosaico virtual con %d archivo(s): %s",
                len(extraidos), virtual.name)

    destino = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_dem"), base
    )
    destino.parent.mkdir(parents=True, exist_ok=True)

    caja = geometria_objetivo.boundingBox()
    resolucion = float(configuracion.obtener("dem.resolucion_m"))
    extension = (f"{caja.xMinimum()},{caja.xMaximum()},"
                 f"{caja.yMinimum()},{caja.yMaximum()} "
                 f"[{crs_calculo.authid()}]")

    processing.run("gdal:warpreproject", {
        "INPUT": str(virtual),
        "TARGET_CRS": crs_calculo.authid(),
        "RESAMPLING": 1,          # bilineal
        "TARGET_RESOLUTION": resolucion,
        "TARGET_EXTENT": extension,
        "TARGET_EXTENT_CRS": crs_calculo.authid(),
        "NODATA": -9999,
        "DATA_TYPE": 6,           # Float32
        "OUTPUT": str(destino),
    })
    logger.info("DEM recortado a %s m en %s: %s",
                resolucion, crs_calculo.authid(), destino.name)
    return destino


def delimitar(
    configuracion: Config, base: Path, ruta_dem: Path, este: float, norte: float,
    logger,
) -> tuple[Path, float, float, float, list[Hallazgo]]:
    """
    Ejecuta la cadena hidrológica y devuelve el ráster de cuenca.

    Devuelve (ruta_cuenca_raster, este_ajustado, norte_ajustado,
    desplazamiento_m, hallazgos).
    """
    import processing

    hallazgos: list[Hallazgo] = []
    temporal = rutas.directorio("sig_temp", base, crear=True)

    relleno = temporal / "dem_relleno.tif"
    processing.run("native:fillsinkswangliu", {
        "INPUT": str(ruta_dem), "BAND": 1, "MIN_SLOPE": 0.01,
        "OUTPUT_FILLED_DEM": str(relleno),
    })
    logger.info("Depresiones rellenadas (Wang y Liu): %s", relleno.name)

    direccion = temporal / "direccion.tif"
    acumulacion = temporal / "acumulacion.tif"
    cauces = temporal / "cauces.tif"
    processing.run("grass:r.watershed", {
        "elevation": str(relleno),
        "threshold": int(configuracion.obtener(
            "dem.delimitacion.umbral_celdas_cauce"
        )),
        "-s": True,
        "drainage": str(direccion),
        "accumulation": str(acumulacion),
        "stream": str(cauces),
    })
    logger.info("Direcciones y acumulación calculadas (r.watershed).")

    radio = float(configuracion.obtener("dem.delimitacion.radio_ajuste_m"))
    este_ajustado, norte_ajustado, desplazamiento = ajustar_a_cauce(
        acumulacion, este, norte, radio
    )
    logger.info(
        "Punto ajustado al cauce: E=%.2f N=%.2f (desplazamiento %.2f m)",
        este_ajustado, norte_ajustado, desplazamiento,
    )
    # La ventana de búsqueda es cuadrada, de modo que su alcance real llega a
    # radio por raíz de dos en las diagonales. Un desplazamiento cercano a ese
    # límite significa que el cauce elegido estaba en el extremo de la ventana y
    # que ampliar el radio podría llevar a otro cauce distinto.
    alcance = radio * (2 ** 0.5)
    if desplazamiento > radio:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "dem.delimitacion.radio_ajuste_m",
            f"el ajuste al cauce desplazó el punto {desplazamiento:.1f} m, por "
            f"encima del radio declarado ({radio:g} m) y hasta el alcance "
            f"diagonal de la ventana ({alcance:.0f} m). La celda elegida estaba "
            "en el borde de la búsqueda: verificar que sea el cauce previsto y "
            "no un afluente vecino.",
        ))
    elif desplazamiento == 0.0:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "dem.delimitacion",
            "el punto declarado ya coincidía con la celda de máxima acumulación.",
        ))

    cuenca = temporal / "cuenca.tif"
    processing.run("grass:r.water.outlet", {
        "input": str(direccion),
        "coordinates": f"{este_ajustado},{norte_ajustado}",
        "output": str(cuenca),
    })
    logger.info("Cuenca delimitada: %s", cuenca.name)

    if toca_borde(cuenca):
        severidad = (BLOQUEANTE
                     if configuracion.obtener("dem.delimitacion.detener_si_toca_borde")
                     else ADVERTENCIA)
        hallazgos.append(Hallazgo(
            severidad, "dem.delimitacion",
            "la cuenca delimitada toca el borde del DEM: está truncada y su "
            "área es menor que la real. Ampliar dem.asf.buffer_busqueda_km o "
            "revisar si la cuenca se extiende a subzonas vecinas.",
        ))

    return cuenca, este_ajustado, norte_ajustado, desplazamiento, hallazgos


def poligonizar(base: Path, ruta_cuenca: Path, crs_calculo):
    """Convierte el ráster de cuenca en un polígono único disuelto."""
    import processing
    from qgis.core import QgsGeometry, QgsVectorLayer

    temporal = rutas.directorio("sig_temp", base, crear=True)
    vectorial = temporal / "cuenca_bruta.shp"
    processing.run("gdal:polygonize", {
        "INPUT": str(ruta_cuenca), "BAND": 1, "FIELD": "valor",
        "EIGHT_CONNECTEDNESS": True, "OUTPUT": str(vectorial),
    })

    capa = QgsVectorLayer(str(vectorial), "cuenca", "ogr")
    if not capa.isValid():
        raise ErrorFormato(f"No se pudo abrir el polígono de cuenca: {vectorial}")

    partes = [
        QgsGeometry(entidad.geometry()) for entidad in capa.getFeatures()
        if entidad.geometry() and not entidad.geometry().isEmpty()
    ]
    if not partes:
        raise ErrorFormato(
            "La poligonización no produjo ninguna geometría: la cuenca quedó "
            "vacía. Revisar el ajuste del punto de descarga."
        )

    unida = QgsGeometry.unaryUnion(partes)
    return unida.makeValid() if not unida.isGeosValid() else unida



# =============================================================================
# Área de influencia según el escenario
# =============================================================================
def determinar_area(configuracion, base, logger):
    """
    Diagnostica el escenario y devuelve (geometria_area, escenario, hallazgos).

    No hay un método universal de delimitación preliminar. Se elige según dónde
    caiga el punto, y el criterio queda registrado en el reporte y en la capa.

    Para el escenario 1 el área es la subzona del M01 tal cual. Es una unidad
    hidrológica cerrada, de modo que CONTIENE la cuenca por definición de la
    zonificación: elimina de raíz el riesgo de truncamiento, que es el fallo
    crítico de este módulo. Sobredimensiona el área unas cuatro veces, y ese
    coste hoy es solo disco.
    """
    from qgis.core import QgsGeometry, QgsPointXY, QgsVectorLayer

    hallazgos = []
    directorio = Path(configuracion.obtener("referencia_nacional.directorio"))
    doble = QgsVectorLayer(
        str(directorio / configuracion.obtener("referencia_nacional.drenaje_doble")),
        "doble", "ogr")
    sencillo = QgsVectorLayer(
        str(directorio / configuracion.obtener("referencia_nacional.drenaje_sencillo")),
        "sencillo", "ogr")

    x, y = _punto_de_descarga(configuracion, base)
    punto = QgsGeometry.fromPointXY(QgsPointXY(x, y))

    escenario = red.diagnosticar_escenario(
        punto, doble, sencillo,
        float(configuracion.obtener("dem.delimitacion.umbral_drenaje_doble_m")),
        float(configuracion.obtener("dem.delimitacion.umbral_drenaje_sencillo_m")),
        configuracion.obtener("referencia_nacional.campos.nombre"),
    )
    logger.info("Escenario %d: %s. Método: %s",
                escenario.numero, escenario.descripcion, escenario.metodo)
    logger.info("  drenaje doble a %.1f m (%s) | sencillo a %.1f m (%s)",
                escenario.distancia_doble_m, escenario.nombre_doble or "sin nombre",
                escenario.distancia_sencillo_m,
                escenario.nombre_sencillo or "sin nombre")

    origen = configuracion.obtener(
        f"dem.delimitacion.origen_area.escenario_{escenario.numero}")

    if origen not in ("subzona", "red"):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "dem.delimitacion.origen_area",
            f"el escenario {escenario.numero} declara origen {origen!r}, que "
            "todavía no está implementado en este módulo. Están 'subzona' y "
            "'red'. Ver red_drenaje para el motor cartográfico.",
        ))
        return None, escenario, hallazgos

    ruta_subzona = rutas.resolver(
        configuracion.obtener("subzonas_hidrograficas.salida_subzona"), base)
    capa = QgsVectorLayer(str(ruta_subzona), "subzona", "ogr")
    if not capa.isValid():
        raise ErrorFormato(f"QGIS no pudo abrir {ruta_subzona}")
    entidades = list(capa.getFeatures())
    if not entidades:
        raise ErrorFormato(f"{ruta_subzona.name} no contiene ninguna entidad.")

    area = QgsGeometry(entidades[0].geometry())
    logger.info("Área de influencia = subzona del M01: %.1f km2",
                area.area() / 1e6)
    hallazgos.append(Hallazgo(
        INFORMATIVO, "dem.delimitacion",
        f"área de influencia tomada de la subzona hidrográfica "
        f"({area.area()/1e6:.1f} km2). Contiene la cuenca por definición de la "
        "zonificación. La delimitación definitiva es la asistida del M09.",
    ))
    return area, escenario, hallazgos


# =============================================================================
# Fase B: acotar el área con la red del M02b
# =============================================================================
def acotar_por_red(configuracion, base, logger):
    """
    Reduce el área de influencia a lo que drena al punto, más un buffer.

    SEGUNDA FASE del módulo. La primera descarga el DEM y recorta el drenaje
    acotados por la subzona; el M02b construye la red sobre esos recortes; y
    aquí se traza aguas arriba desde el punto para quedarse con la parte que
    aporta. Esa dependencia circular (el área necesita la red, la red necesita
    el DEM, el DEM necesita un área) se rompe descargando siempre por subzona:
    acotar NO ahorra descarga, acota lo que el estudio DECLARA como su área, y
    con ello la selección de estaciones del M03, la extensión de interpolación
    del M06 y el M08, y la cartografía del M16.

    La adopción es CONDICIONAL y se mide. Un trazado que apenas arrastra red no
    acota nada: acota mal. Por debajo de la fracción declarada se conserva la
    subzona y se dice por qué, en lugar de entregar una envolvente pequeña y
    plausible construida sobre un trazado que falló.

    Devuelve (geometria o None, diagnostico, hallazgos).
    """
    from qgis.core import QgsGeometry, QgsRectangle

    hallazgos: list[Hallazgo] = []
    ruta_red = rutas.resolver(
        configuracion.obtener("red_topologica.salida_red"), base)
    if not ruta_red.is_file():
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "dem.delimitacion.acotar",
            f"no existe la red del M02b en {rutas.relativa(ruta_red, base)}: no "
            "se puede acotar el área. Ejecutar el M02b y volver a lanzar la "
            "segunda fase.",
        ))
        return None, {}, hallazgos

    registros = list(shapefile.leer_registros(
        ruta_red, ["id_tramo", "receptor", "long_m", "nombre"]))
    geometrias = shapefile.leer_geometrias(ruta_red)
    afluentes: dict[int, list[int]] = {}
    longitudes: dict[int, float] = {}
    vertices: dict[int, list[tuple[float, float]]] = {}
    for registro_tramo, entidad in zip(registros, geometrias):
        identificador = int(registro_tramo["id_tramo"])
        longitudes[identificador] = float(registro_tramo["long_m"])
        vertices[identificador] = [p for parte in entidad for p in parte]
        receptor = int(registro_tramo["receptor"])
        if receptor >= 0:
            afluentes.setdefault(receptor, []).append(identificador)

    x, y = _punto_de_descarga(configuracion, base)
    diagnostico = red.enganchar_punto(
        afluentes, longitudes, vertices, x, y,
        float(configuracion.obtener("dem.delimitacion.enganche_punto_m")))

    if diagnostico.get("tramo") is None:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "dem.delimitacion.acotar",
            f"no se pudo enganchar el punto a la red: {diagnostico['motivo']}. "
            "Se conserva la subzona como área de influencia.",
        ))
        return None, diagnostico, hallazgos

    red_total_km = sum(longitudes.values()) / 1000.0
    fraccion = (diagnostico["red_arriba_km"] / red_total_km
                if red_total_km else 0.0)
    diagnostico["fraccion_de_la_red"] = round(fraccion, 5)
    minima = float(configuracion.obtener("dem.delimitacion.fraccion_minima_red"))

    logger.info("Punto enganchado al tramo %d, a %.1f m | %d tramos y %.1f km "
                "aguas arriba (%.2f %% de la red)",
                diagnostico["tramo"], diagnostico["distancia_m"],
                diagnostico["tramos_arriba"], diagnostico["red_arriba_km"],
                100 * fraccion)

    if diagnostico["descartado_el_mas_cercano"]:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "dem.delimitacion.enganche",
            f"el punto NO se engancho al tramo mas cercano. El mas cercano es "
            f"el {diagnostico['mas_cercano']}, que arrastra "
            f"{diagnostico['red_del_mas_cercano_km']:.2f} km; se adopto el "
            f"{diagnostico['tramo']}, a {diagnostico['distancia_m']:.1f} m, que "
            f"arrastra {diagnostico['red_arriba_km']:.1f} km. El eje derivado "
            "por adelgazamiento deja hebras paralelas y munones donde el cauce "
            "se ensancha, y engancharse al mas cercano puede devolver un area "
            "pequena y plausible construida sobre un trazado que no arrastra "
            "nada.",
        ))

    if fraccion < minima:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "dem.delimitacion.acotar",
            f"el trazado aguas arriba solo arrastra "
            f"{diagnostico['red_arriba_km']:.2f} km, el {100 * fraccion:.2f} % "
            f"de la red, por debajo del minimo de {100 * minima:.2f} %. NO se "
            "acota: se conserva la subzona. Un trazado asi no acota el area, la "
            "acota mal, y su envolvente tendria la misma apariencia que una "
            "buena.",
        ))
        return None, diagnostico, hallazgos

    buffer_km = float(configuracion.obtener(
        f"dem.delimitacion.buffer_red_km.escenario_1"))
    xmin, ymin, xmax, ymax = diagnostico["envolvente"]
    margen = buffer_km * 1000.0
    caja = QgsRectangle(xmin - margen, ymin - margen, xmax + margen, ymax + margen)
    area = QgsGeometry.fromRect(caja)

    diagnostico["buffer_km"] = buffer_km
    diagnostico["area_acotada_km2"] = round(area.area() / 1e6, 3)
    logger.info("Área acotada: %.1f km2 (envolvente de la red trazada mas "
                "buffer de %.1f km)", area.area() / 1e6, buffer_km)
    return area, diagnostico, hallazgos


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    solo_planificar: bool = False,
    sin_descarga: bool = False,
    fase: str = "completa",
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Encadena las cuatro etapas y emite el reporte."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)

    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    ruta_netrc = configuracion.obtener("dem.earthdata.ruta_netrc", None)
    disponibles, motivo = asf.credenciales_disponibles(ruta_declarada=ruta_netrc)
    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "subzona del M01": configuracion.obtener(
                "subzonas_hidrograficas.salida_subzona"
            ),
            "credenciales Earthdata": motivo,
            "modo": ("solo planificar" if solo_planificar
                     else "sin descarga" if sin_descarga else "completo"),
        },
        parametros=configuracion.parametros((
            "crs.calculo", "dem.fuente", "dem.resolucion_m",
            "dem.asf.nivel", "dem.asf.buffer_busqueda_km",
            "dem.asf.fecha_inicio", "dem.asf.fecha_fin",
            "dem.asf.max_escenas", "dem.asf.max_volumen_gb",
            "dem.delimitacion.umbral_celdas_cauce",
            "dem.delimitacion.radio_ajuste_m",
            "dem.area_influencia.metodo", "dem.area_influencia.buffer_km",
        )),
    )

    resultado = ResultadoM02()
    prefijo = configuracion.obtener("entornos.qgis.prefix_path")
    sig.iniciar_qgis(prefijo)
    sig.inicializar_processing(prefijo, logger)

    with registro.bloque(logger, "Área de búsqueda a partir de la subzona"):
        objetivo, wkt_geografico, crs_calculo = area_de_busqueda(configuracion, base)

    with registro.bloque(logger, "Diagnóstico del escenario y área de influencia"):
        area, escenario, hallazgos_area = determinar_area(configuracion, base, logger)
        resultado.hallazgos.extend(hallazgos_area)
        resultado.escenario = escenario
        if area is None:
            return _cerrar(logger, resultado, base, ruta_json, inicio,
                           SALIDA_BLOQUEANTE)

    # La segunda fase solo reemplaza el área: el DEM y los recortes ya están.
    solo_area = fase == "area"
    if solo_area:
        ruta_dem = rutas.resolver(
            configuracion.obtener("dem.delimitacion.salida_dem"), base)
        resultado.dem = rutas.relativa(ruta_dem, base)

    if not solo_area:
        with registro.bloque(logger, "Consulta del catálogo y selección de escenas"):
            resultado.plan, hallazgos_plan = planificar(
                configuracion, wkt_geografico, logger
            )
            resultado.hallazgos.extend(hallazgos_plan)

        if solo_planificar:
            logger.info("Modo solo planificar: no se descarga ni se procesa.")
            return _cerrar(logger, resultado, base, ruta_json, inicio,
                           SALIDA_BLOQUEANTE if esquema.hay_bloqueantes(resultado.hallazgos)
                           else SALIDA_CORRECTA)

        if esquema.hay_bloqueantes(resultado.hallazgos):
            logger.error("El plan de descarga no es admisible. Se detiene.")
            return _cerrar(logger, resultado, base, ruta_json, inicio, SALIDA_BLOQUEANTE)

        if not sin_descarga:
            if not disponibles:
                resultado.hallazgos.append(Hallazgo(
                    BLOQUEANTE, "dem.earthdata",
                    f"no hay credenciales de Earthdata utilizables: {motivo} "
                    "Crear el archivo netrc y volver a ejecutar, o usar "
                    "--sin-descarga si las escenas ya están en disco.",
                ))
                return _cerrar(logger, resultado, base, ruta_json, inicio,
                               SALIDA_BLOQUEANTE)

            with registro.bloque(logger, "Descarga de escenas"):
                resultado.descargadas = _descargar_escenas(
                    configuracion, base, resultado.plan, logger
                )

        # Si el DEM ya está y no se va a descargar nada, se reutiliza. Un
        # estudio puede heredarlo de otro que comparta subzona, y entonces no
        # tiene las escenas crudas: exigirlas obligaría a volver a descargar
        # 2,2 GB para producir un archivo idéntico al que ya existe.
        ruta_dem = rutas.resolver(
            configuracion.obtener("dem.delimitacion.salida_dem"), base)
        if sin_descarga and ruta_dem.is_file():
            resultado.dem = rutas.relativa(ruta_dem, base)
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "dem.reutilizado",
                f"se reutiliza el DEM existente en {resultado.dem} y se omiten "
                "la extracción y el mosaico. Su procedencia debe quedar "
                "declarada en el informe si viene de otro estudio.",
            ))
            logger.info("DEM reutilizado: %s", resultado.dem)
        else:
            with registro.bloque(logger, "Extracción del modelo de elevación"):
                _extraer_escenas(configuracion, base, logger)

            with registro.bloque(logger, "Mosaico, reproyección y recorte"):
                ruta_dem = preparar_dem(configuracion, base, objetivo,
                                        crs_calculo, logger)
                resultado.dem = rutas.relativa(ruta_dem, base)

    if fase != "preliminar":
        with registro.bloque(logger, "Acotado del área con la red del M02b"):
            acotada, diagnostico, hallazgos_acotar = acotar_por_red(
                configuracion, base, logger)
            resultado.hallazgos.extend(hallazgos_acotar)
            resultado.acotado = diagnostico
            if acotada is not None:
                previa = area.area() / 1e6
                area = acotada
                resultado.hallazgos.append(Hallazgo(
                    INFORMATIVO, "dem.delimitacion.acotada",
                    f"area de influencia acotada de {previa:.0f} a "
                    f"{area.area()/1e6:.0f} km2, una reduccion de "
                    f"{previa / (area.area()/1e6):.1f} veces, trazando aguas "
                    f"arriba desde el punto sobre la red del M02b: "
                    f"{diagnostico['tramos_arriba']} tramos y "
                    f"{diagnostico['red_arriba_km']:.1f} km de cauce que "
                    "aportan al punto, mas el buffer declarado. Sigue siendo "
                    "PRELIMINAR: la delimitacion definitiva es la asistida del "
                    "M09.",
                ))

    with registro.bloque(logger, "Escritura del área de influencia"):
        _escribir_area(configuracion, base, area, escenario, ruta_dem,
                       crs_calculo, resultado, logger, solo_area)

    codigo = (SALIDA_BLOQUEANTE if esquema.hay_bloqueantes(resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _punto_de_descarga(configuracion: Config, base: Path) -> tuple[float, float]:
    """Lee del producto del M01 el punto ya reproyectado al CRS de cálculo."""
    ruta = rutas.directorio("procesado", base) / "M01_punto_descarga.json"
    if not ruta.is_file():
        raise ErrorFormato(
            f"No existe {ruta.name}. El M02 usa el punto que reproyecta el M01: "
            "ejecutarlo primero."
        )
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    calculo = (datos.get("resultado") or {}).get("calculo") or {}
    if "x" not in calculo or "y" not in calculo:
        raise ErrorFormato(f"{ruta.name} no contiene el punto en el CRS de cálculo.")
    return float(calculo["x"]), float(calculo["y"])


def _descargar_escenas(configuracion: Config, base: Path, plan: PlanDescarga,
                       logger) -> list[str]:
    """Descarga las escenas seleccionadas, informando del avance."""
    directorio = rutas.directorio("crudos_dem", base, crear=True)
    verificar = bool(configuracion.obtener("dem.asf.verificar_md5"))
    reintentos = int(configuracion.obtener("dem.asf.reintentos"))
    descargadas: list[str] = []

    for indice, escena in enumerate(plan.seleccionadas, start=1):
        logger.info(
            "[%d/%d] %s (%.1f MB)", indice, len(plan.seleccionadas),
            escena.nombre_archivo, escena.tamano_mb,
        )
        destino = asf.descargar(
            escena, directorio, verificar=verificar, reintentos=reintentos,
            ruta_netrc=configuracion.obtener("dem.earthdata.ruta_netrc", None),
        )
        descargadas.append(destino.name)
    return descargadas


def _extraer_escenas(configuracion: Config, base: Path, logger) -> None:
    """Extrae el DEM de cada zip presente en el directorio de crudos."""
    directorio = rutas.directorio("crudos_dem", base, crear=True)
    sufijo = configuracion.obtener("dem.asf.patron_dem_en_zip")
    destino = directorio / "elevacion"

    comprimidos = sorted(directorio.glob("*.zip"))
    if not comprimidos:
        raise ErrorFormato(
            f"No hay archivos .zip en {directorio}. Sin escenas no hay DEM."
        )

    total = 0
    for comprimido in comprimidos:
        extraidos = extraer_dem(comprimido, destino, sufijo)
        total += len(extraidos)
    logger.info("Archivos de elevación disponibles: %d", total)


def _escribir_geometrias(configuracion, base, cuenca_raster, ruta_dem, crs_calculo,
                         este, norte, desplazamiento, resultado, logger) -> None:
    """Escribe cuenca preliminar, envolvente y área de influencia."""
    from qgis.core import QgsGeometry

    cuenca = poligonizar(base, cuenca_raster, crs_calculo)
    area_km2 = cuenca.area() / 1_000_000.0
    resultado.area_cuenca_km2 = area_km2
    logger.info("Área de la cuenca preliminar: %.4f km2", area_km2)

    cotas = estadisticas_raster(ruta_dem)
    resolucion = float(configuracion.obtener("dem.resolucion_m"))
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")

    destino_cuenca = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_cuenca"), base
    )
    sig.escribir_capa(
        destino=destino_cuenca, campos_salida=CAMPOS_CUENCA,
        geometrias=[cuenca],
        valores=[{
            "nombre": configuracion.obtener("punto_descarga.nombre"),
            "area_km2": area_km2,
            "perim_km": cuenca.length() / 1000.0,
            "cota_max": cotas["maximo"], "cota_min": cotas["minimo"],
            "cota_med": cotas["media"],
            "x_salida": este, "y_salida": norte, "desp_m": desplazamiento,
            "res_dem_m": resolucion, "origen": "preliminar",
        }],
        crs_id=crs_calculo.authid(), tipo_geometria="MultiPolygon",
    )
    _registrar_producto(resultado, base, destino_cuenca, CAMPOS_CUENCA, delimitador)

    caja = cuenca.boundingBox()
    envolvente = QgsGeometry.fromRect(caja)
    destino_envolvente = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_envolvente"), base
    )
    sig.escribir_capa(
        destino=destino_envolvente, campos_salida=CAMPOS_MARCO,
        geometrias=[envolvente],
        valores=[{
            "nombre": "Envolvente de la cuenca preliminar",
            "tipo": "envolvente",
            "area_km2": envolvente.area() / 1_000_000.0, "buffer_km": 0.0,
        }],
        crs_id=crs_calculo.authid(), tipo_geometria="Polygon",
    )
    _registrar_producto(resultado, base, destino_envolvente, CAMPOS_MARCO,
                        delimitador)

    buffer_km = float(configuracion.obtener("dem.area_influencia.buffer_km"))
    metodo = configuracion.obtener("dem.area_influencia.metodo")
    partida = envolvente if metodo == "envolvente" else cuenca
    influencia = partida.buffer(buffer_km * 1000.0, 12) if buffer_km > 0 \
        else QgsGeometry(partida)

    destino_influencia = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_area_influencia"), base
    )
    sig.escribir_capa(
        destino=destino_influencia, campos_salida=CAMPOS_MARCO,
        geometrias=[influencia],
        valores=[{
            "nombre": "Área de influencia", "tipo": metodo,
            "area_km2": influencia.area() / 1_000_000.0, "buffer_km": buffer_km,
        }],
        crs_id=crs_calculo.authid(), tipo_geometria="Polygon",
    )
    _registrar_producto(resultado, base, destino_influencia, CAMPOS_MARCO,
                        delimitador)

    # El M03 selecciona estaciones sobre el área de influencia más un buffer
    # adicional propio. Ese buffer se calcula aquí, en metros y con QGIS, porque
    # el venv no puede hacerlo sobre coordenadas geográficas sin deformarlo.
    buffer_estaciones = float(
        configuracion.obtener("estaciones.buffer_adicional_km")
    )
    seleccion = influencia.buffer(buffer_estaciones * 1000.0, 12) \
        if buffer_estaciones > 0 else QgsGeometry(influencia)

    resultado.wkt_geografico = _a_geografico(
        {"cuenca_preliminar": cuenca, "envolvente": envolvente,
         "area_influencia": influencia, "area_estaciones": seleccion},
        crs_calculo, configuracion,
    )


def _a_geografico(geometrias: dict, crs_calculo, configuracion) -> dict[str, str]:
    """
    Reproyecta las geometrías a coordenadas geográficas y las devuelve en WKT.

    Es el puente entre los dos entornos: el M03 corre en el venv y no puede
    reproyectar, de modo que recibe el área de influencia ya en el sistema en
    que el catálogo de estaciones declara latitud y longitud.
    """
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
        QgsProject,
    )

    crs_geografico = QgsCoordinateReferenceSystem(
        configuracion.obtener("crs.geografico")
    )
    transformacion = QgsCoordinateTransform(
        crs_calculo, crs_geografico, QgsProject.instance().transformContext()
    )

    salida: dict[str, str] = {}
    for nombre, geometria in geometrias.items():
        copia = QgsGeometry(geometria)
        if copia.transform(transformacion) == 0:
            salida[nombre] = copia.asWkt(8)
    return salida



def _escribir_area(configuracion, base, area, escenario, ruta_dem, crs_calculo,
                   resultado, logger, solo_area: bool = False) -> None:
    """Escribe el área de influencia, su envolvente y el área de estaciones."""
    from qgis.core import QgsGeometry

    resultado.area_cuenca_km2 = area.area() / 1e6
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")
    cotas = estadisticas_raster(ruta_dem)

    destino = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_area_influencia"), base)
    sig.escribir_capa(
        destino=destino, campos_salida=CAMPOS_MARCO, geometrias=[area],
        valores=[{
            "nombre": f"Área de influencia (escenario {escenario.numero})",
            "tipo": escenario.descripcion,
            "area_km2": resultado.area_cuenca_km2, "buffer_km": 0.0,
        }],
        crs_id=crs_calculo.authid(), tipo_geometria="MultiPolygon")
    _registrar_producto(resultado, base, destino, CAMPOS_MARCO, delimitador)

    envolvente = QgsGeometry.fromRect(area.boundingBox())
    destino_env = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_envolvente"), base)
    sig.escribir_capa(
        destino=destino_env, campos_salida=CAMPOS_MARCO, geometrias=[envolvente],
        valores=[{"nombre": "Envolvente del área de influencia",
                  "tipo": "envolvente",
                  "area_km2": envolvente.area() / 1e6, "buffer_km": 0.0}],
        crs_id=crs_calculo.authid(), tipo_geometria="Polygon")
    _registrar_producto(resultado, base, destino_env, CAMPOS_MARCO, delimitador)

    # --- Recorte del drenaje nacional ---------------------------------------
    # SOLO EN LA PRIMERA FASE. El recorte se hace a la extensión de BÚSQUEDA,
    # que es la subzona, y no al área de influencia. En la segunda fase el área
    # ya está acotada por el trazado de la red, y volver a recortar sobre ella
    # dejaría al M02b una red más pequeña, que en el siguiente acotado daría un
    # área aún menor: una realimentación que encoge el estudio en cada pasada
    # sin que nada lo señale.
    if solo_area:
        return
    # Las capas del IGAC pesan 645 MB y viven fuera del repositorio. El recorte
    # a la envolvente pesa unos cientos de kilobytes y es lo que se versiona.
    #
    # No es un producto accesorio: el M09 lo entrega a HEC-HMS para que el
    # consultor verifique que las corrientes trazadas sobre el terreno siguen la
    # red real, que es el control que atrapa el fallo del DEM sin reacondicionar.
    # El M16 lo usara ademas para la cartografia tematica.
    directorio_nacional = Path(
        configuracion.obtener("referencia_nacional.directorio"))
    campo_nombre = configuracion.obtener("referencia_nacional.campos.nombre")
    extension_recorte = envolvente.boundingBox()
    for clave_origen, clave_destino in (
        ("referencia_nacional.drenaje_sencillo",
         "referencia_nacional.salida_recorte_sencillo"),
        ("referencia_nacional.drenaje_doble",
         "referencia_nacional.salida_recorte_doble"),
        # Los embalses cortan la red: el IGAC los dibuja como poligono aparte y
        # ni las lineas ni el eje del drenaje doble los atraviesan.
        ("referencia_nacional.embalses",
         "referencia_nacional.salida_recorte_embalses"),
    ):
        origen_capa = directorio_nacional / configuracion.obtener(clave_origen)
        destino_capa = rutas.resolver(
            configuracion.obtener(clave_destino), base)
        if not origen_capa.is_file():
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, clave_destino,
                f"no se encuentra {origen_capa}: el recorte del drenaje no se "
                "escribio. El M09 lo necesita como capa de verificacion del "
                "paso manual de HEC-HMS.",
            ))
            continue
        try:
            red.recortar_capa(origen_capa, extension_recorte, destino_capa,
                              crs_calculo.authid(), campo_nombre)
        except (ErrorFormato, ErrorRutas) as exc:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, clave_destino,
                f"no se pudo recortar {origen_capa.name}: {exc}.",
            ))
            continue
        resultado.capas.append(rutas.relativa(destino_capa, base))
        logger.info("Drenaje recortado: %s", destino_capa.name)

    buffer_est = float(configuracion.obtener("estaciones.buffer_adicional_km"))
    seleccion = area.buffer(buffer_est * 1000.0, 12) if buffer_est > 0         else QgsGeometry(area)

    x, y = _punto_de_descarga(configuracion, base)
    resultado.cota_punto = _cota_en(ruta_dem, x, y)
    logger.info("Cota del punto de descarga: %s m",
                f"{resultado.cota_punto:.0f}" if resultado.cota_punto else "no disponible")
    logger.info("Área de influencia %.1f km2 | cotas %.0f a %.0f m",
                resultado.area_cuenca_km2, cotas["minimo"], cotas["maximo"])

    resultado.wkt_geografico = _a_geografico(
        {"area_influencia": area, "envolvente": envolvente,
         "area_estaciones": seleccion},
        crs_calculo, configuracion)


def _cota_en(ruta_raster, este, norte):
    """Lee la cota del DEM en una coordenada. None si cae fuera del ráster."""
    from osgeo import gdal

    conjunto = gdal.Open(str(ruta_raster))
    if conjunto is None:
        return None
    gt = conjunto.GetGeoTransform()
    columna = int((este - gt[0]) / gt[1])
    fila = int((norte - gt[3]) / gt[5])
    if not (0 <= columna < conjunto.RasterXSize and 0 <= fila < conjunto.RasterYSize):
        return None
    valor = conjunto.GetRasterBand(1).ReadAsArray(columna, fila, 1, 1)
    return float(valor[0][0]) if valor is not None else None


def _registrar_producto(resultado, base, destino, campos_salida, delimitador):
    resultado.capas.append(rutas.relativa(destino, base))
    resultado.diccionarios.append(rutas.relativa(mod_campos.escribir_diccionario(
        campos_salida, destino.with_name(f"{destino.stem}_campos.csv"),
        destino.stem, delimitador,
    ), base))


def _cerrar(logger, resultado: ResultadoM02, base: Path, ruta_json: Path | None,
            inicio: float, codigo: int) -> tuple[int, list[Hallazgo]]:
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
            emitir("  %-32s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info(
        "RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
        conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO],
    )

    productos: dict[str, Any] = {}
    if resultado.dem:
        productos["DEM recortado"] = resultado.dem
    for indice, capa in enumerate(resultado.capas, start=1):
        productos[f"capa {indice}"] = capa
    if resultado.area_cuenca_km2:
        productos["área de la cuenca"] = f"{resultado.area_cuenca_km2:.4f} km2"

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M02_delimitacion.json"

    reporte = {
        "modulo": MODULO,
        "plan_descarga": resultado.plan.como_dict(),
        "escenas_descargadas": resultado.descargadas,
        "dem": resultado.dem,
        "punto": {"cota_m": resultado.cota_punto},
        "acotado": resultado.acotado,
        "escenario": (resultado.escenario.como_dict()
                      if resultado.escenario else None),
        "area_influencia": {
            "area_km2": resultado.area_cuenca_km2,
            "caracter": "preliminar",
        },
        "capas": resultado.capas,
        "diccionarios": resultado.diccionarios,
        "geometrias_epsg4326": resultado.wkt_geografico,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(
        json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    productos["reporte JSON"] = rutas.relativa(ruta_json, base)

    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecución"] = rutas.relativa(archivo_log, base)

    estado = "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO"
    registro.registrar_cierre(
        logger, MODULO, estado,
        segundos=time.perf_counter() - inicio, productos=productos,
    )
    return codigo, hallazgos


# =============================================================================
# Interfaz de línea de comandos
# =============================================================================
def _analizar_argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        prog="M02_dem_delimitacion.py",
        description="DEM ALOS PALSAR, delimitación preliminar y área de influencia.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--solo-planificar", action="store_true",
                            dest="solo_planificar",
                            help="Consulta el catálogo y reporta, sin descargar.")
    analizador.add_argument("--sin-descarga", action="store_true",
                            dest="sin_descarga",
                            help="Usa solo las escenas ya presentes en disco.")
    analizador.add_argument(
        "--fase", choices=("preliminar", "area", "completa"),
        default="completa",
        help="preliminar: DEM y recortes acotados por la subzona. "
             "area: acota con la red del M02b. completa: las dos.")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        entorno.exigir_entorno(entorno.ENTORNO_QGIS, MODULO)
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            solo_planificar=argumentos.solo_planificar,
            sin_descarga=argumentos.sin_descarga,
            fase=argumentos.fase,
            ruta_json=argumentos.json_salida,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorEntorno, ErrorRutas, ErrorConfiguracion, ErrorFormato,
            asf.ErrorASF) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    finally:
        sig.finalizar_qgis()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M08 - Isoyetas de Pmáx 24 h por periodo de retorno
==================================================
Entorno: Python de QGIS (OSGeo4W Shell). NO importa librerías del venv.

Convierte los cuantiles que entrega el M07 en superficie y en curvas, una tanda
por periodo de retorno. Es el mismo geoproceso del M06 sobre otra magnitud, y
por eso ambos comparten src/interpolacion.py: duplicar el algoritmo garantizaría
que se corrijan por separado.

Qué interpola. Para cada estación y cada periodo de retorno, el M07 entrega el
cuantil de la distribución que adoptó para ESA estación. Eso importa al leer el
mapa: el campo de T100 puede mezclar una Gumbel en una estación con una
LogNormal en su vecina, porque cada serie eligió la suya. Es lo correcto (cada
estación se ajusta a su propio dato) y a la vez es una fuente de discontinuidad
que el informe debe declarar.

Tres advertencias que el módulo mide y no puede resolver:

    rango altitudinal   la red hereda 1850 a 3378 m del descarte del M05, de
                        modo que por debajo de esa franja el campo extrapola
    destreza            la validación dejando uno fuera dice cuánto vale la
                        superficie; en el M06 dio Nash-Sutcliffe cercano a 0,2
    extrapolación       el M07 advierte que T500 se estima sobre series de 32
                        años de mediana, y ese error viaja hasta aquí

Productos:
    data/03_SIG/raster/isoyetas/pmax_T<periodo>.tif
    data/03_SIG/vector/isoyetas/pmax_T<periodo>.shp
    data/03_SIG/vector/isoyetas/estaciones_pmax_T<periodo>.shp
    data/02_procesado/M08_isoyetas_pmax.json
    data/05_resultados/graficos/M08_*.png y .svg

Uso (desde el OSGeo4W Shell):
    python-qgis src/M08_isoyetas_pmax.py
    python-qgis src/M08_isoyetas_pmax.py --sin-graficas

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
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import interpolacion as itp  # noqa: E402
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

MODULO = "M08"
DESCRIPCION = "Isoyetas de Pmáx 24 h por periodo de retorno"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

CRS_CATALOGO = "EPSG:4686"

CAMPOS_ESTACION: tuple[CampoSalida, ...] = (
    CampoSalida("codigo", "Código de la estación", "texto", 15),
    CampoSalida("periodo", "Periodo de retorno", "decimal", 10, 2, "años"),
    CampoSalida("pmax_mm", "Pmáx 24 h del periodo", "decimal", 12, 1, "mm"),
    CampoSalida("distrib", "Distribución adoptada", "texto", 20),
    CampoSalida("metodo", "Método de ajuste", "texto", 24),
    CampoSalida("n_anios", "Años de la serie de máximos", "entero", 8),
    CampoSalida("altitud", "Altitud", "decimal", 12, 2, "m"),
)


@dataclass
class ResultadoM08:
    periodos: list[float] = field(default_factory=list)
    por_periodo: dict[str, dict[str, Any]] = field(default_factory=dict)
    validacion: list[dict[str, Any]] = field(default_factory=list)
    distribuciones: dict[str, int] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def leer_cuantiles(
    ruta: Path, delimitador: str,
) -> tuple[list[float], list[dict[str, Any]]]:
    """
    Lee el producto del M07 y devuelve los periodos y una fila por estación.

    Los periodos se deducen de las columnas 'T<valor>', de modo que añadir uno
    en config y volver a correr el M07 basta para que aparezca aquí: no hay una
    lista duplicada que pueda quedar desincronizada.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}. Ejecutar el M07 antes que este módulo."
        )
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        raise ErrorFormato(f"{ruta.name} está vacío.")

    periodos: list[float] = []
    for columna in filas[0]:
        if not columna.startswith("T"):
            continue
        try:
            periodos.append(float(columna[1:]))
        except ValueError:
            continue
    if not periodos:
        raise ErrorFormato(
            f"{ruta.name} no tiene columnas de periodo de retorno "
            f"(T<valor>). Cabecera: {list(filas[0])}."
        )
    return sorted(periodos), filas


def muestras_de_periodo(
    filas: Sequence[dict[str, Any]], periodo: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Estaciones con cuantil utilizable para ese periodo, y las que no lo tienen.

    Una estación sin cuantil no se rellena ni se sustituye por el de otro
    periodo: entraría al campo un valor que la distribución adoptada no produjo.
    """
    utiles: list[dict[str, Any]] = []
    sin_valor: list[str] = []
    clave = f"T{periodo:g}"
    for fila in filas:
        texto = (fila.get(clave) or "").strip()
        if not texto:
            sin_valor.append(fila.get("codigo", "").strip())
            continue
        try:
            valor = float(texto)
        except ValueError:
            sin_valor.append(fila.get("codigo", "").strip())
            continue
        utiles.append({
            "codigo": fila["codigo"].strip(),
            "pmax_mm": valor,
            "distrib": fila.get("distribucion", ""),
            "metodo": fila.get("metodo", ""),
            "n_anios": int(fila.get("n_anios") or 0),
        })
    return utiles, sin_valor


def nombre_periodo(periodo: float) -> str:
    """Nombre de archivo del periodo, sin punto decimal ni signos."""
    return f"T{periodo:g}".replace(".", "_")


def intervalo_de_curvas(
    valores: Sequence[float], objetivo: int = 8, minimo: float = 1.0,
) -> float:
    """
    Intervalo de curva que produce del orden de 'objetivo' líneas.

    A diferencia del M06, donde un solo intervalo sirve a las tres fases, aquí
    el rango crece con el periodo de retorno: el intervalo fijo que da ocho
    curvas en T2.33 daría dos en T500. Se redondea a un valor legible (1, 2, 5
    o 10 por potencia de diez) porque una leyenda con intervalos de 6,375 mm no
    se lee.
    """
    lista = [float(v) for v in valores]
    if len(lista) < 2:
        return minimo
    rango = max(lista) - min(lista)
    if rango <= 0:
        return minimo
    crudo = rango / max(1, objetivo)
    potencia = 10.0 ** math.floor(math.log10(crudo)) if crudo > 0 else 1.0
    for factor in (1.0, 2.0, 5.0, 10.0):
        if crudo <= factor * potencia:
            return max(minimo, factor * potencia)
    return max(minimo, 10.0 * potencia)



# =============================================================================
# Ejecución
# =============================================================================
def _reproyectar(muestras, ubicaciones, crs_calculo):
    """Lleva las estaciones al CRS de cálculo, de forma explícita."""
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
        QgsProject,
    )
    conversor = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem(CRS_CATALOGO),
        QgsCoordinateReferenceSystem(crs_calculo),
        QgsProject.instance().transformContext(),
    )
    salida, sin_ubicacion = [], []
    for fila in muestras:
        sitio = ubicaciones.get(fila["codigo"])
        if sitio is None:
            sin_ubicacion.append(fila["codigo"])
            continue
        punto = conversor.transform(QgsPointXY(sitio["lon"], sitio["lat"]))
        salida.append((punto.x(), punto.y(), fila["pmax_mm"],
                       dict(fila, altitud=sitio["altitud"])))
    return salida, sin_ubicacion


def leer_ubicaciones(ruta: Path) -> dict[str, dict[str, float]]:
    """Coordenadas geográficas y altitud de cada estación, desde el M03."""
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


def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    con_graficas: bool = True,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Interpola cada periodo de retorno, recorta, traza curvas y valida."""
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
    minimo_estaciones = int(configuracion.obtener("isoyetas.minimo_estaciones"))
    crs_figuras = (configuracion.obtener("graficos.crs_figuras")
                   or configuracion.obtener("punto_descarga.crs"))

    cuantiles = rutas.directorio("procesado_frecuencia", base) / "cuantiles.csv"
    capa_estaciones = rutas.resolver(
        configuracion.obtener("estaciones.salida_seleccionadas"), base)
    ruta_area = rutas.directorio("sig_vector", base) / "area_influencia.shp"

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"cuantiles del M07": rutas.relativa(cuantiles, base),
                 "estaciones": rutas.relativa(capa_estaciones, base),
                 "area de influencia": rutas.relativa(ruta_area, base)},
        parametros={
            "interpolacion.metodo": metodo,
            "interpolacion.idw.potencia": potencia,
            "isoyetas.resolucion_m": resolucion,
            "isoyetas.minimo_estaciones": minimo_estaciones,
        },
    )

    resultado = ResultadoM08()

    if metodo not in itp.METODOS_IMPLEMENTADOS:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "interpolacion.metodo",
            f"el metodo declarado es {metodo!r} y este modulo implementa "
            f"{list(itp.METODOS_IMPLEMENTADOS)}. Se detiene en lugar de "
            "sustituir el metodo en silencio.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    periodos, filas = leer_cuantiles(cuantiles, delimitador)
    ubicaciones = leer_ubicaciones(capa_estaciones)
    resultado.periodos = periodos

    for fila in filas:
        clave = fila.get("distribucion", "")
        if clave:
            resultado.distribuciones[clave] = (
                resultado.distribuciones.get(clave, 0) + 1)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "isoyetas.distribuciones",
        "cada estacion aporta el cuantil de la distribucion que el M07 adopto "
        "para ella: "
        + ", ".join(f"{k} ({v})"
                    for k, v in sorted(resultado.distribuciones.items(),
                                       key=lambda t: -t[1]))
        + ". Es lo correcto, porque cada serie se ajusta a su propio dato, pero "
        "significa que un mismo campo puede mezclar distribuciones vecinas y esa "
        "discontinuidad debe declararse en el informe.",
    ))

    usadas = {f["codigo"].strip() for f in filas}
    cotas = [ubicaciones[c]["altitud"] for c in usadas
             if c in ubicaciones and ubicaciones[c].get("altitud") is not None]
    if cotas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "isoyetas.rango_altitudinal",
            f"las {len(cotas)} estaciones se extienden entre {min(cotas):.0f} y "
            f"{max(cotas):.0f} m, rango heredado del descarte del M05. Todo "
            "valor que el campo produzca fuera de esa franja es EXTRAPOLACION.",
        ))

    directorio_raster = rutas.resolver(
        configuracion.obtener("isoyetas.salida_raster"), base)
    directorio_curvas = rutas.resolver(
        configuracion.obtener("isoyetas.salida_curvas"), base)
    directorio_raster.mkdir(parents=True, exist_ok=True)
    directorio_curvas.mkdir(parents=True, exist_ok=True)
    temporal = rutas.directorio("sig_temp", base, crear=True)

    extension, _ = itp.extension_de_area(ruta_area, resolucion * 10)
    rasters: dict[float, Path] = {}
    puntos_por_periodo: dict[float, list] = {}

    from qgis.core import QgsGeometry, QgsPointXY

    for periodo in periodos:
        etiqueta = nombre_periodo(periodo)
        with registro.bloque(logger, f"Periodo de retorno {periodo:g} anios"):
            crudas, sin_valor = muestras_de_periodo(filas, periodo)
            muestras, sin_ubicacion = _reproyectar(
                crudas, ubicaciones, crs_calculo)
            if sin_valor or sin_ubicacion:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"isoyetas.{etiqueta}.sin_dato",
                    f"{len(sin_valor)} estacion(es) sin cuantil y "
                    f"{len(sin_ubicacion)} sin coordenadas para {periodo:g} "
                    "anios. No se rellenan ni se sustituyen por el cuantil de "
                    "otro periodo: seria un valor que la distribucion adoptada "
                    "no produjo.",
                ))
            if len(muestras) < minimo_estaciones:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"isoyetas.{etiqueta}.pocas_estaciones",
                    f"solo {len(muestras)} estacion(es) para {periodo:g} anios, "
                    f"por debajo del minimo de {minimo_estaciones}.",
                ))
                continue

            puntos_shp = directorio_curvas / f"estaciones_pmax_{etiqueta}.shp"
            geometrias, valores = [], []
            for x, y, valor, fila in muestras:
                geometrias.append(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
                valores.append({
                    "codigo": fila["codigo"], "periodo": float(periodo),
                    "pmax_mm": valor, "distrib": fila["distrib"],
                    "metodo": fila["metodo"], "n_anios": fila["n_anios"],
                    "altitud": fila["altitud"],
                })
            sig.escribir_capa(puntos_shp, CAMPOS_ESTACION, geometrias, valores,
                              crs_calculo, "Point")
            mod_campos.escribir_diccionario(
                CAMPOS_ESTACION,
                puntos_shp.with_name(f"{puntos_shp.stem}_campos.csv"),
                puntos_shp.stem, delimitador)
            resultado.productos.append(rutas.relativa(puntos_shp, base))

            crudo = temporal / f"idw_pmax_{etiqueta}.tif"
            itp.interpolar_idw(puntos_shp, "pmax_mm", extension, resolucion,
                               potencia, crudo, crs_calculo)
            raster = directorio_raster / f"pmax_{etiqueta}.tif"
            itp.recortar_a_area(crudo, ruta_area, raster)
            resultado.productos.append(rutas.relativa(raster, base))
            rasters[periodo] = raster

            solo_xyz = [(x, y, v) for x, y, v, _ in muestras]
            intervalo = intervalo_de_curvas([v for _, _, v in solo_xyz])
            curvas = directorio_curvas / f"pmax_{etiqueta}.shp"
            itp.generar_curvas(raster, curvas, intervalo, crs_calculo,
                               campo="Pmax_mm")
            resultado.productos.append(rutas.relativa(curvas, base))

            validacion = itp.validacion_dejando_uno_fuera(
                solo_xyz, potencia, float(radio) if radio else None)
            validacion["periodo"] = periodo
            resultado.validacion.append(validacion)

            gradiente = itp.gradiente_altitudinal(
                [(f["altitud"], v) for _, _, v, f in muestras])
            primera, ultima = itp.rango_de_curvas(
                [v for _, _, v in solo_xyz], intervalo)
            resultado.por_periodo[f"{periodo:g}"] = {
                "estaciones": len(muestras),
                "intervalo_mm": intervalo,
                "min_estacion_mm": round(min(v for _, _, v in solo_xyz), 1),
                "max_estacion_mm": round(max(v for _, _, v in solo_xyz), 1),
                "primera_curva_mm": primera,
                "ultima_curva_mm": ultima,
                "raster": itp.estadisticas_raster(raster),
                "validacion": validacion,
                "gradiente_altitudinal": gradiente,
            }
            puntos_por_periodo[periodo] = [(x, y) for x, y, _, _ in muestras]
            logger.info(
                "%d estacion(es) | %.0f a %.0f mm | curva cada %g mm | "
                "RMSE %s mm (%s%%)",
                len(muestras), min(v for _, _, v in solo_xyz),
                max(v for _, _, v in solo_xyz), intervalo,
                validacion.get("rmse_mm", "?"),
                validacion.get("rmse_relativo_pct", "?"))

    if rasters and con_graficas:
        with registro.bloque(logger, "Figuras"):
            _figuras(configuracion, base, resultado, rasters,
                     puntos_por_periodo, ruta_area, crs_calculo, crs_figuras,
                     temporal, logger)

    resultado.hallazgos.extend(_resumir(resultado))
    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


# =============================================================================
# Figuras
# =============================================================================
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
        partes = (geometria.asMultiPolygon()
                  if QgsWkbTypes.isMultiType(geometria.wkbType())
                  else [geometria.asPolygon()])
        for parte in partes:
            for anillo in parte:
                anillos.append([(p.x(), p.y()) for p in anillo])
    return anillos


def _figuras(configuracion, base, resultado, rasters, puntos_por_periodo,
             ruta_area, crs_calculo, crs_figuras, temporal, logger) -> None:
    """Emite el panel comparativo y una figura por periodo de retorno."""
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
    anillos = _contorno_area(ruta_area, crs_figuras)

    campos = {}
    for periodo, ruta in rasters.items():
        reproyectado = itp.reproyectar_raster(
            ruta, temporal / f"fig_pmax_{nombre_periodo(periodo)}.tif",
            crs_figuras)
        datos, extension = itp.leer_raster(reproyectado)
        if datos is not None:
            campos[periodo] = (datos, extension)
    if not campos:
        return

    if anillos:
        equis = [x for a in anillos for x, _ in a]
        yes = [y for a in anillos for _, y in a]
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
        ax.set_aspect("equal", adjustable="box")
        graficos.rotular_en_miles(ax, maximo_marcas=3)
        ax.tick_params(labelsize=estilo.tamano_fuente - 3)

    orden = sorted(campos)
    # Escala COMUN a todos los periodos: el mapa debe mostrar que T500 llueve
    # mas que T2.33, y con escala propia cada uno saldria igual de intenso.
    todos = np.concatenate([d[np.isfinite(d)].ravel() for d, _ in campos.values()])
    minimo = float(np.floor(np.nanpercentile(todos, 1) / 10.0) * 10.0)
    maximo = float(np.ceil(np.nanpercentile(todos, 99) / 10.0) * 10.0)

    columnas = 4
    filas = (len(orden) + columnas - 1) // columnas
    with graficos.figura(
        estilo, filas=filas, columnas=columnas,
        alto_cm=max(estilo.alto_cm, 6.0 * filas),
    ) as (fig, ejes):
        imagen = None
        for indice, periodo in enumerate(orden):
            ax = ejes[indice // columnas][indice % columnas]
            datos, extension = campos[periodo]
            imagen = ax.imshow(datos, extent=extension, origin="upper",
                               cmap="YlGnBu", vmin=minimo, vmax=maximo, zorder=1)
            intervalo = resultado.por_periodo[f"{periodo:g}"]["intervalo_mm"]
            niveles = np.arange(minimo, maximo + intervalo, intervalo)
            ax.contour(datos, levels=niveles, extent=extension, origin="upper",
                       colors="#333333", linewidths=0.35, zorder=3)
            if puntos_por_periodo.get(periodo):
                pass
            _fondo(ax)
            # Solo la primera columna rotula el eje vertical: en paneles
            # contiguos las cifras de siete digitos se solapan entre si.
            if indice % columnas:
                ax.set_yticklabels([])
            ax.set_title(f"T = {periodo:g} años",
                         fontsize=estilo.tamano_fuente, loc="left",
                         color="#333333")
        for sobrante in range(len(orden), filas * columnas):
            ejes[sobrante // columnas][sobrante % columnas].axis("off")
        if imagen is not None:
            barra = fig.colorbar(imagen, ax=ejes.ravel().tolist(),
                                 fraction=0.025, pad=0.02)
            barra.set_label("Pmáx 24 h (mm)",
                            fontsize=estilo.tamano_fuente - 1)
            barra.ax.tick_params(labelsize=estilo.tamano_fuente - 2)
        fig.suptitle("Precipitación máxima en 24 horas por periodo de retorno",
                     fontsize=estilo.tamano_fuente + 2)
        for ruta in graficos.guardar(fig, directorio / "M08_isoyetas_pmax",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Una figura por periodo, para el informe -----------------------------
    if bool(configuracion.obtener("graficos.por_estacion")):
        raiz = rutas.resolver(
            configuracion.obtener("graficos.directorio_estaciones"), base)
        carpeta = graficos.directorio_tema(raiz, "isoyetas_pmax")
        individual = graficos.estilo_individual(
            estilo,
            float(configuracion.obtener("graficos.ancho_estacion_cm")),
            float(configuracion.obtener("graficos.alto_estacion_cm")))
        for periodo in orden:
            datos, extension = campos[periodo]
            intervalo = resultado.por_periodo[f"{periodo:g}"]["intervalo_mm"]
            with graficos.figura(
                individual,
                titulo=f"Pmáx 24 h, T = {periodo:g} años",
                etiqueta_x="Este (m)", etiqueta_y="Norte (m)",
                alto_cm=float(configuracion.obtener("graficos.alto_estacion_cm")),
            ) as (fig, ax):
                imagen = ax.imshow(datos, extent=extension, origin="upper",
                                   cmap="YlGnBu", zorder=1)
                niveles = np.arange(
                    np.floor(np.nanmin(datos) / intervalo) * intervalo,
                    np.nanmax(datos) + intervalo, intervalo)
                contornos = ax.contour(datos, levels=niveles, extent=extension,
                                       origin="upper", colors="#333333",
                                       linewidths=0.5, zorder=3)
                ax.clabel(contornos, inline=True,
                          fontsize=individual.tamano_fuente - 3, fmt="%.0f")
                _fondo(ax)
                barra = fig.colorbar(imagen, ax=ax, fraction=0.04, pad=0.03)
                barra.set_label("mm", fontsize=individual.tamano_fuente - 1)
                barra.ax.tick_params(labelsize=individual.tamano_fuente - 2)
                ax.annotate(f"Coordenadas {crs_figuras}", xy=(1, -0.13),
                            xycoords="axes fraction", ha="right",
                            fontsize=individual.tamano_fuente - 2,
                            color="#555555")
                fig.tight_layout()
                graficos.guardar(fig, carpeta / nombre_periodo(periodo),
                                 individual)
        resultado.productos.append(
            f"{rutas.relativa(raiz, base)} ({len(orden)} figura(s) por periodo)")

    logger.info("Figuras escritas en %s", rutas.relativa(directorio, base))


# =============================================================================
# Cierre
# =============================================================================
def _resumir(resultado) -> list[Hallazgo]:
    """Informativos de sintesis y lectura de la validacion."""
    hallazgos: list[Hallazgo] = []
    if not resultado.por_periodo:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "isoyetas.sin_periodos",
            "no se produjo ninguna isoyeta de Pmax. Revisar el producto del M07.",
        ))
        return hallazgos

    hallazgos.append(Hallazgo(
        INFORMATIVO, "isoyetas.producidas",
        f"{len(resultado.por_periodo)} periodo(s) de retorno interpolados: "
        + ", ".join(f"{k} anios ({v['estaciones']} est., "
                    f"{v['min_estacion_mm']:.0f}-{v['max_estacion_mm']:.0f} mm)"
                    for k, v in resultado.por_periodo.items()) + ".",
    ))

    for validacion in resultado.validacion:
        relativo = validacion.get("rmse_relativo_pct")
        if relativo is None:
            continue
        severidad = ADVERTENCIA if relativo > 15.0 else INFORMATIVO
        hallazgos.append(Hallazgo(
            severidad, f"isoyetas.T{validacion['periodo']:g}.validacion",
            f"validacion dejando uno fuera: RMSE {validacion['rmse_mm']} mm "
            f"({relativo}% de la media), Nash-Sutcliffe "
            f"{validacion.get('nash_sutcliffe')}."
            + (" La densidad de la red no sustenta el detalle del mapa."
               if severidad == ADVERTENCIA else ""),
        ))

    pobres = [k for k, v in resultado.por_periodo.items()
              if (v.get("gradiente_altitudinal") or {}).get("r2", 1.0) < 0.30]
    if pobres:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "isoyetas.gradiente",
            f"la altitud no explica la Pmax en {len(pobres)} de "
            f"{len(resultado.por_periodo)} periodo(s) (r2 por debajo de 0,30). "
            "El M11 define la zonificacion pluviometrica con gradiente "
            "altitudinal: aplicarlo aqui la apoyaria en una relacion "
            "inexistente. Es el mismo hallazgo que ya reporto el M06.",
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
        ruta_json = (rutas.directorio("procesado", base, crear=True)
                     / "M08_isoyetas_pmax.json")
    reporte = {
        "modulo": MODULO,
        "periodos": resultado.periodos,
        "por_periodo": resultado.por_periodo,
        "validacion": resultado.validacion,
        "distribuciones": resultado.distribuciones,
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
        prog="M08_isoyetas_pmax.py",
        description="Isoyetas de Pmax 24 h por periodo de retorno.",
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

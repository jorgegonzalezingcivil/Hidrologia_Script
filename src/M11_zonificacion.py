#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M11 - Zonificación pluviométrica y precipitación media por subcuenca
====================================================================
Entorno: venv del proyecto.

POR QUÉ NO CORRE EN QGIS, pese a lo que declara la sección 8. El M11 estaba
previsto en el entorno SIG porque se pensó que interpolaría. No interpola: el
M06 y el M08 ya dejaron los campos escritos, y lo que falta aquí es estadística
zonal, promediar un ráster dentro de unos polígonos. Eso se resuelve con el
lector de GeoTIFF de librería estándar y el barrido de 'comun.geometria', que ya
sostienen el relieve del M10. Llevarlo a QGIS obligaría a que el paso más
sencillo de la cadena dependiese de una instalación de 2 GB.

EL GRADIENTE ALTITUDINAL NO SE APLICA POR DECRETO, SE MIDE. La sección 6 define
la zonificación como diferencial porcentual, gradiente altitudinal y ponderación
por área. Los dos primeros componentes no valen lo mismo: el diferencial se
observa siempre, y el gradiente solo existe si la altitud explica la
precipitación en ESTA red. Medido en este estudio, no la explica: el M06 obtuvo
r2 entre 0,018 y 0,038 sobre las tres fases ENSO y el M08 quedó por debajo de
0,30 en los ocho periodos de retorno. Este módulo lee esas cifras del reporte de
quien las midió y, si no alcanzan el mínimo declarado, se abstiene de usar el
gradiente y lo dice. Aplicarlo igualmente apoyaría la zonificación en una
relación inexistente, con la apariencia de un método.

LO QUE ENTREGA AL M13. La precipitación media areal de cada subcuenca para cada
periodo de retorno. Es la unidad con la que HEC-HMS reparte la lluvia, y hasta
aquí los campos solo existían como superficie continua.

LO QUE NO PUEDE GARANTIZAR. Que el valor promediado sea observación y no
extrapolación. Las estaciones cubren una franja de cotas y la cuenca puede
salirse de ella: se mide qué fracción del área de cada subcuenca queda fuera y
se declara, en lugar de entregar un promedio que no distingue lo uno de lo otro.

Productos:
    data/02_procesado/precipitacion/precipitacion_por_subcuenca.csv
    data/02_procesado/precipitacion/zonificacion.csv
    data/02_procesado/precipitacion/M11_zonificacion.md
    data/02_procesado/M11_zonificacion.json

Uso:
    python src/M11_zonificacion.py

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, geometria, raster, registro, rutas, shapefile  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M11"
DESCRIPCION = "Zonificación pluviométrica y precipitación media por subcuenca"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3


@dataclass
class ResultadoM11:
    campos: list[dict[str, Any]] = field(default_factory=list)
    subcuencas: list[dict[str, Any]] = field(default_factory=list)
    zonas: list[dict[str, Any]] = field(default_factory=list)
    gradiente: dict[str, Any] = field(default_factory=dict)
    altitud: dict[str, Any] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def media_zonal(
    ruta_raster: Path, entidades, np_,
) -> list[dict[str, Any]]:
    """
    Media, extremos y número de celdas de un ráster dentro de cada polígono.

    Se recorre el ráster UNA vez y en cada fila solo se evalúan los polígonos
    cuya envolvente vertical la alcanza. La celda se resuelve por su centro,
    según el convenio compartido de 'comun.geometria'.

    Excepciones
    -----------
    ErrorRutas
        Si no está el ráster.
    ErrorHidrologia
        Si ninguna entidad se superpone con él.
    """
    info = raster.leer_info(ruta_raster)
    nodato = info.nodato

    preparadas: list[dict[str, Any] | None] = []
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

    activas = [p for p in preparadas if p]
    if not activas:
        raise ErrorHidrologia(
            f"ninguna subcuenca se superpone con {ruta_raster.name}.")

    acumulado = [{"celdas": 0, "suma": 0.0, "minimo": float("inf"),
                  "maximo": float("-inf")} for _ in preparadas]

    with raster.LectorRaster(ruta_raster) as lector:
        for fila in range(min(p["fila_ini"] for p in activas),
                          max(p["fila_fin"] for p in activas) + 1):
            candidatas = [i for i, p in enumerate(preparadas)
                          if p and p["fila_ini"] <= fila <= p["fila_fin"]]
            if not candidatas:
                continue
            valores_fila = np_.frombuffer(lector.fila(fila),
                                          dtype=info.descriptor)
            for indice in candidatas:
                mascara = np_.zeros(info.ancho, dtype=bool)
                for desde, hasta in geometria.columnas_de_fila(
                        preparadas[indice]["aristas"], info.y_de_fila(fila),
                        info.origen_x, info.tamano_x, info.ancho):
                    mascara[desde:hasta + 1] = True
                if not mascara.any():
                    continue
                if nodato is not None:
                    mascara &= valores_fila != nodato
                dentro = valores_fila[mascara]
                if not dentro.size:
                    continue
                registro_zona = acumulado[indice]
                registro_zona["celdas"] += int(dentro.size)
                registro_zona["suma"] += float(dentro.sum(dtype=np_.float64))
                registro_zona["minimo"] = min(registro_zona["minimo"],
                                              float(dentro.min()))
                registro_zona["maximo"] = max(registro_zona["maximo"],
                                              float(dentro.max()))

    salida = []
    for registro_zona in acumulado:
        if not registro_zona["celdas"]:
            salida.append({"media": None, "minimo": None, "maximo": None,
                           "celdas": 0})
            continue
        salida.append({
            "media": registro_zona["suma"] / registro_zona["celdas"],
            "minimo": registro_zona["minimo"],
            "maximo": registro_zona["maximo"],
            "celdas": registro_zona["celdas"],
        })
    return salida


def fraccion_fuera_del_rango(
    ruta_dem: Path, entidades, cota_min: float, cota_max: float, np_,
) -> list[dict[str, Any]]:
    """
    Fracción del área de cada subcuenca fuera del rango de cotas de las estaciones.

    Es la medida que separa observación de extrapolación. Un campo interpolado
    produce valores en toda su extensión, también donde ninguna estación
    informa: el promedio de una subcuenca cuya mitad está por encima de la
    estación más alta no vale lo mismo que el de una que cae dentro de la franja
    medida, y el informe no puede tratarlos igual.
    """
    info = raster.leer_info(ruta_dem)
    nodato = info.nodato
    salida = []

    with raster.LectorRaster(ruta_dem) as lector:
        for anillos in entidades:
            poligono = [list(anillo) for anillo in anillos]
            aristas = geometria.aristas_de([poligono])
            if not aristas:
                salida.append({"fraccion_fuera_pct": None, "celdas": 0})
                continue
            _, ymin, _, ymax = geometria.envolvente([poligono])
            dentro = fuera = 0
            for fila in range(max(0, info.fila_de(ymax)),
                              min(info.alto - 1, info.fila_de(ymin)) + 1):
                mascara = np_.zeros(info.ancho, dtype=bool)
                for desde, hasta in geometria.columnas_de_fila(
                        aristas, info.y_de_fila(fila), info.origen_x,
                        info.tamano_x, info.ancho):
                    mascara[desde:hasta + 1] = True
                if not mascara.any():
                    continue
                cotas = np_.frombuffer(lector.fila(fila),
                                       dtype=info.descriptor)
                if nodato is not None:
                    mascara &= cotas != nodato
                valores = cotas[mascara]
                if not valores.size:
                    continue
                dentro += int(valores.size)
                fuera += int(((valores < cota_min) | (valores > cota_max)).sum())
            salida.append({
                "fraccion_fuera_pct": round(100.0 * fuera / dentro, 2)
                if dentro else None,
                "celdas": dentro,
            })
    return salida


def zonificar(
    subcuencas: Sequence[dict[str, Any]],
    clave_valor: str,
    diferencia_maxima_pct: float,
) -> list[dict[str, Any]]:
    """
    Agrupa subcuencas cuya precipitación no difiere más del umbral declarado.

    Se ordenan por su valor y se abre una zona nueva cuando la diferencia
    porcentual con el primer miembro del grupo supera el umbral. Es un
    agrupamiento por intervalos y no un clasificador estadístico: el criterio lo
    fija el consultor en la configuración, y el resultado tiene que poder
    explicarse en una frase ante interventoría.

    LA ZONA NO ES CONTIGUA POR CONSTRUCCIÓN. Dos subcuencas de la misma zona
    pueden estar en extremos opuestos de la cuenca: comparten régimen de lluvia,
    no vecindad. Quien dibuje el mapa debe saberlo.
    """
    con_valor = [s for s in subcuencas if s.get(clave_valor) is not None]
    if not con_valor:
        return []

    ordenadas = sorted(con_valor, key=lambda s: s[clave_valor])
    zonas: list[dict[str, Any]] = []
    grupo: list[dict[str, Any]] = []
    referencia = None

    for subcuenca in ordenadas:
        valor = subcuenca[clave_valor]
        if referencia is None:
            referencia = valor
        elif referencia > 0 and 100.0 * (valor - referencia) / referencia > \
                diferencia_maxima_pct:
            zonas.append(_resumir_zona(len(zonas) + 1, grupo, clave_valor))
            grupo, referencia = [], valor
        grupo.append(subcuenca)

    if grupo:
        zonas.append(_resumir_zona(len(zonas) + 1, grupo, clave_valor))
    return zonas


def _resumir_zona(numero: int, grupo, clave_valor: str) -> dict[str, Any]:
    """Resumen de una zona: extensión, precipitación y quiénes la componen."""
    area = sum(s.get("area_km2", 0.0) for s in grupo)
    valores = [s[clave_valor] for s in grupo]
    # Ponderación POR ÁREA (CLAUDE.md, sección 6): una subcuenca de diez
    # kilómetros cuadrados no puede pesar lo mismo que una de una hectárea.
    if area > 0:
        media = sum(s[clave_valor] * s.get("area_km2", 0.0)
                    for s in grupo) / area
    else:
        media = statistics.fmean(valores)
    return {
        "zona": numero,
        "subcuencas": len(grupo),
        "area_km2": round(area, 3),
        "precipitacion_media_mm": round(media, 2),
        "minimo_mm": round(min(valores), 2),
        "maximo_mm": round(max(valores), 2),
        "miembros": [s["subcuenca"] for s in grupo],
    }


def leer_gradiente_medido(ruta_reporte: Path) -> dict[str, Any]:
    """
    Recupera del reporte del M08 el ajuste altitud-precipitación que midió.

    No se vuelve a calcular aquí. El módulo que interpola es el que dispone de
    las estaciones con su cota, y duplicar el ajuste abriría la puerta a que dos
    partes del estudio declarasen gradientes distintos.
    """
    if not ruta_reporte.is_file():
        return {}
    try:
        datos = json.loads(ruta_reporte.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    ajustes = []
    cotas: list[float] = []
    for periodo, campo in (datos.get("por_periodo") or {}).items():
        gradiente = (campo or {}).get("gradiente_altitudinal") or {}
        if gradiente.get("r2") is None:
            continue
        ajustes.append({
            "campo": periodo,
            "r2": float(gradiente["r2"]),
            "mm_por_m": gradiente.get("pendiente_mm_por_m"),
        })
        for clave in ("altitud_min_m", "altitud_max_m"):
            if gradiente.get(clave) is not None:
                cotas.append(float(gradiente[clave]))
    if not ajustes:
        return {}
    medido = {
        "ajustes": ajustes,
        "r2_maximo": max(a["r2"] for a in ajustes),
        "r2_mediano": statistics.median(a["r2"] for a in ajustes),
    }
    if cotas:
        # El rango de cotas sale de la MISMA medicion que el gradiente, y no de
        # la configuracion: es el de las estaciones que efectivamente entraron
        # en la interpolacion, que no tiene por que coincidir con el de las
        # seleccionadas al principio de la cadena.
        medido["altitud_min_m"] = min(cotas)
        medido["altitud_max_m"] = max(cotas)
    return medido


# =============================================================================
# Ejecución
# =============================================================================
def _rasteres_de_periodo(directorio: Path) -> list[tuple[str, Path]]:
    """Rásteres de Pmáx por periodo de retorno, ordenados por periodo."""
    encontrados = []
    for ruta in sorted(directorio.glob("pmax_T*.tif")):
        etiqueta = ruta.stem.replace("pmax_T", "").replace("_", ".")
        try:
            periodo = float(etiqueta)
        except ValueError:
            continue
        encontrados.append((periodo, etiqueta, ruta))
    return [(e, r) for _, e, r in sorted(encontrados)]


def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Promedia los campos de precipitación por subcuenca y zonifica."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM11()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M11_zonificacion.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "subcuencas": "data/03_SIG/vector/subcuencas.shp",
            "campos de Pmax": configuracion.obtener("isoyetas.salida_raster"),
            "DEM": configuracion.obtener("dem.delimitacion.salida_dem"),
        },
        parametros=configuracion.parametros("zonificacion_pluviometrica"))

    import numpy as np

    ruta_subcuencas = rutas.directorio("sig_vector", base) / "subcuencas.shp"
    directorio_pmax = rutas.resolver(
        configuracion.obtener("isoyetas.salida_raster"), base)
    ruta_dem = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_dem"), base)

    if not ruta_subcuencas.is_file():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "subcuencas.ausentes",
            f"no se encuentra {rutas.relativa(ruta_subcuencas, base)}. La "
            "precipitacion se promedia por subcuenca, que es la unidad con la "
            "que HEC-HMS reparte la lluvia: ejecutar antes el M09 --importar.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    entidades = shapefile.leer_geometrias(ruta_subcuencas)
    registros = list(shapefile.leer_registros(ruta_subcuencas, ["name"]))
    areas = shapefile.areas_poligonos(ruta_subcuencas)
    nombres = [str(registros[i].get("name", "")).strip() or f"S{i + 1}"
               for i in range(len(entidades))]
    resultado.subcuencas = [
        {"subcuenca": nombres[i], "area_km2": round(areas[i] / 1e6, 4)}
        for i in range(len(entidades))]

    campos = _rasteres_de_periodo(directorio_pmax)
    if not campos:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "campos.ausentes",
            f"no hay ningun raster pmax_T*.tif en "
            f"{rutas.relativa(directorio_pmax, base)}: ejecutar antes el M08.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    # --- Precipitacion media por subcuenca -----------------------------------
    with registro.bloque(logger, "Precipitacion media por subcuenca"):
        for etiqueta, ruta in campos:
            try:
                zonal = media_zonal(ruta, entidades, np)
            except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"campo.T{etiqueta}",
                    f"no se pudo promediar {ruta.name}: {error}"))
                continue
            for subcuenca, valores in zip(resultado.subcuencas, zonal):
                subcuenca[f"p_T{etiqueta}_mm"] = (
                    round(valores["media"], 2)
                    if valores["media"] is not None else None)
            con_dato = [v["media"] for v in zonal if v["media"] is not None]
            area_total = sum(s["area_km2"] for s in resultado.subcuencas)
            ponderada = sum(
                s[f"p_T{etiqueta}_mm"] * s["area_km2"]
                for s in resultado.subcuencas
                if s.get(f"p_T{etiqueta}_mm") is not None) / area_total \
                if area_total else None
            resultado.campos.append({
                "periodo_retorno": etiqueta,
                "raster": rutas.relativa(ruta, base),
                "subcuencas_con_dato": len(con_dato),
                "media_ponderada_mm": round(ponderada, 2) if ponderada else None,
                "minimo_mm": round(min(con_dato), 2) if con_dato else None,
                "maximo_mm": round(max(con_dato), 2) if con_dato else None,
            })
            logger.info("T %s anios: %d subcuencas, media ponderada %.1f mm",
                        etiqueta, len(con_dato), ponderada or 0.0)

        sin_dato = [s["subcuenca"] for s in resultado.subcuencas
                    if all(s.get(f"p_T{e}_mm") is None for e, _ in campos)]
        if sin_dato:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "subcuencas.sin_precipitacion",
                f"{len(sin_dato)} subcuenca(s) sin ninguna celda de los campos "
                f"de precipitacion: {sin_dato[:6]}. Suelen ser las diminutas, "
                "mas pequenas que la celda del raster interpolado. Sin lluvia "
                "asignada no pueden entrar en el modelo.",
            ))

    # --- Gradiente altitudinal: se lee, no se decreta ------------------------
    with registro.bloque(logger, "Gradiente altitudinal"):
        _resolver_gradiente(configuracion, base, resultado, logger)

    # --- Extrapolacion por cota ----------------------------------------------
    with registro.bloque(logger, "Rango altitudinal de las estaciones"):
        _resolver_extrapolacion(configuracion, base, ruta_dem, entidades,
                                resultado, np, logger)

    # --- Zonificacion --------------------------------------------------------
    with registro.bloque(logger, "Zonificacion"):
        referencia = configuracion.obtener(
            "zonificacion_pluviometrica.periodo_referencia")
        clave = f"p_T{referencia}_mm"
        if not any(s.get(clave) is not None for s in resultado.subcuencas):
            clave = f"p_T{campos[0][0]}_mm"
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "zonificacion.periodo",
                f"el periodo de referencia declarado ({referencia} anios) no "
                f"tiene campo: se zonifica con {clave}.",
            ))
        resultado.zonas = zonificar(
            resultado.subcuencas, clave,
            float(configuracion.obtener(
                "zonificacion_pluviometrica.diferencia_maxima_pct")))
        _resolver_zonas(resultado, clave, logger)

    _escribir_productos(configuracion, base, resultado, logger)
    return _cerrar(logger, resultado, base, ruta_json, inicio, SALIDA_CORRECTA)


def _resolver_gradiente(configuracion, base, resultado, logger) -> None:
    """Decide si el gradiente altitudinal se puede usar, con la cifra medida."""
    declarado = bool(configuracion.obtener(
        "zonificacion_pluviometrica.considerar_gradiente_altitudinal"))
    minimo = float(configuracion.obtener(
        "zonificacion_pluviometrica.r2_minimo_gradiente"))
    medido = leer_gradiente_medido(
        rutas.directorio("procesado", base) / "M08_isoyetas_pmax.json")
    resultado.gradiente = {"declarado": declarado, "r2_minimo": minimo,
                           **medido}

    if not declarado:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "gradiente.desactivado",
            "el gradiente altitudinal esta desactivado en la configuracion: la "
            "zonificacion usa el diferencial porcentual y la ponderacion por "
            "area, los otros dos componentes de la seccion 6.",
        ))
        return

    if not medido:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "gradiente.sin_medir",
            "el gradiente altitudinal esta activado pero no se encontro la "
            "medicion del M08. NO se aplica: un gradiente sin r2 conocido es "
            "una hipotesis, no un dato.",
        ))
        resultado.gradiente["aplicado"] = False
        return

    aplicable = medido["r2_maximo"] >= minimo
    resultado.gradiente["aplicado"] = aplicable
    logger.info("Gradiente: r2 maximo %.3f, mediano %.3f (minimo %.2f) -> %s",
                medido["r2_maximo"], medido["r2_mediano"], minimo,
                "aplicable" if aplicable else "NO aplicable")
    if aplicable:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "gradiente.aplicado",
            f"el gradiente altitudinal se aplica: r2 maximo {medido['r2_maximo']:.3f} "
            f"sobre {len(medido['ajustes'])} campo(s), por encima del minimo "
            f"de {minimo:.2f}.",
        ))
        return

    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "gradiente.no_aplicable",
        f"el gradiente altitudinal NO se aplica. La altitud no explica la "
        f"precipitacion en esta red: r2 maximo {medido['r2_maximo']:.3f} y "
        f"mediano {medido['r2_mediano']:.3f} sobre {len(medido['ajustes'])} "
        f"campo(s), frente al minimo de {minimo:.2f}. La seccion 6 define la "
        "zonificacion con tres componentes y aqui se sostiene con dos, el "
        "diferencial porcentual y la ponderacion por area. Es una EXCEPCION "
        "DECLARADA, no una omision: aplicar un gradiente de ese r2 daria a la "
        "zonificacion la apariencia de un metodo apoyandola en una relacion "
        "que se midio y no esta. La causa probable es el descarte por "
        "consistencia del M05, que trunco el rango de cotas de la red.",
    ))


def _resolver_extrapolacion(configuracion, base, ruta_dem, entidades,
                            resultado, np_, logger) -> None:
    """Mide qué parte de cada subcuenca queda fuera del rango de las estaciones."""
    medido = resultado.gradiente
    rango = None
    if medido.get("altitud_min_m") is not None:
        rango = (medido["altitud_min_m"], medido["altitud_max_m"])
    if not rango or not ruta_dem.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "altitud.sin_rango",
            "no se pudo comprobar si el promedio de cada subcuenca es "
            "observacion o extrapolacion: falta el rango de cotas de las "
            "estaciones o el DEM. El informe no podra distinguirlos.",
        ))
        return

    cota_min, cota_max = float(rango[0]), float(rango[1])
    try:
        fuera = fraccion_fuera_del_rango(ruta_dem, entidades, cota_min,
                                         cota_max, np_)
    except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "altitud.sin_rango",
            f"no se pudo medir la extrapolacion por cota: {error}"))
        return

    for subcuenca, medida in zip(resultado.subcuencas, fuera):
        subcuenca["area_fuera_del_rango_pct"] = medida["fraccion_fuera_pct"]

    con_medida = [s for s in resultado.subcuencas
                  if s.get("area_fuera_del_rango_pct") is not None]
    if not con_medida:
        return
    area_total = sum(s["area_km2"] for s in con_medida)
    ponderada = sum(s["area_fuera_del_rango_pct"] * s["area_km2"]
                    for s in con_medida) / area_total if area_total else 0.0
    afectadas = sorted(
        (s for s in con_medida if s["area_fuera_del_rango_pct"] > 50.0),
        key=lambda s: -s["area_fuera_del_rango_pct"])
    resultado.altitud = {
        "cota_min_estaciones": cota_min,
        "cota_max_estaciones": cota_max,
        "area_fuera_ponderada_pct": round(ponderada, 2),
        "subcuencas_mayoritariamente_fuera": len(afectadas),
    }
    logger.info("Fuera del rango %.0f-%.0f m: %.1f %% del area",
                cota_min, cota_max, ponderada)

    severidad = ADVERTENCIA if ponderada > 5.0 else INFORMATIVO
    resultado.hallazgos.append(Hallazgo(
        severidad, "altitud.extrapolacion",
        f"el {ponderada:.1f} % del area de la cuenca esta fuera de la franja de "
        f"{cota_min:.0f} a {cota_max:.0f} m que cubren las estaciones, de modo "
        "que ahi el campo interpolado EXTRAPOLA y su promedio no es "
        f"observacion. {len(afectadas)} subcuenca(s) tienen mas de la mitad de "
        "su area fuera"
        + (f": {[s['subcuenca'] for s in afectadas[:6]]}" if afectadas else "")
        + ". Debe declararse junto a la tabla de precipitacion, no en una nota "
        "al pie: es el limite de lo que la red mide.",
    ))


def _resolver_zonas(resultado, clave, logger) -> None:
    """Registra el resultado de la zonificación."""
    if not resultado.zonas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "zonificacion.sin_zonas",
            "no se pudo zonificar: ninguna subcuenca tiene precipitacion "
            "asignada."))
        return

    for zona in resultado.zonas:
        for subcuenca in resultado.subcuencas:
            if subcuenca["subcuenca"] in zona["miembros"]:
                subcuenca["zona"] = zona["zona"]

    logger.info("%d zona(s) sobre %s", len(resultado.zonas), clave)
    detalle = "; ".join(
        f"zona {z['zona']}: {z['subcuencas']} subcuenca(s), "
        f"{z['area_km2']:.1f} km2, {z['precipitacion_media_mm']:.1f} mm"
        for z in resultado.zonas)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "zonificacion.zonas",
        f"{len(resultado.zonas)} zona(s) pluviometrica(s) sobre {clave}, "
        f"agrupando subcuencas que no difieren mas del umbral declarado y "
        f"ponderando por area. {detalle}. Las zonas NO son contiguas por "
        "construccion: agrupan regimen de lluvia, no vecindad, y el mapa debe "
        "dibujarlas como tal.",
    ))


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Escribe una tabla, con las columnas de la primera fila."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    filas = list(filas)
    if not filas:
        destino.write_text("", encoding="utf-8-sig")
        return
    columnas: list[str] = []
    for fila in filas:
        for clave in fila:
            if clave not in columnas:
                columnas.append(clave)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=columnas,
                                  delimiter=delimitador, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(filas)


def _escribir_productos(configuracion, base, resultado, logger) -> None:
    """Escribe las tablas y el informe del módulo."""
    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    directorio = rutas.directorio("procesado", base, crear=True) / "precipitacion"
    directorio.mkdir(parents=True, exist_ok=True)

    for nombre, contenido in (
        ("precipitacion_por_subcuenca.csv", resultado.subcuencas),
        ("zonificacion.csv", [{c: v for c, v in z.items() if c != "miembros"}
                              for z in resultado.zonas]),
        ("campos_promediados.csv", resultado.campos),
    ):
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))

    informe = directorio / "M11_zonificacion.md"
    _escribir_informe(informe, resultado)
    resultado.productos.append(rutas.relativa(informe, base))
    logger.info("%d subcuenca(s) con precipitacion, %d zona(s)",
                len(resultado.subcuencas), len(resultado.zonas))


def _escribir_informe(destino: Path, resultado) -> None:
    """Informe en Markdown, con lo que el M15 necesita citar."""
    lineas = ["# M11 - Zonificación pluviométrica y precipitación por subcuenca",
              ""]

    if resultado.gradiente:
        lineas += ["## Gradiente altitudinal", ""]
        if resultado.gradiente.get("aplicado"):
            lineas.append("Aplicado. r2 máximo "
                          f"{resultado.gradiente['r2_maximo']:.3f}.")
        else:
            lineas.append(
                "**NO aplicado.** La altitud no explica la precipitación en "
                "esta red: r2 máximo "
                f"{resultado.gradiente.get('r2_maximo', float('nan')):.3f}, "
                f"mínimo exigido {resultado.gradiente['r2_minimo']:.2f}. La "
                "zonificación se sostiene con el diferencial porcentual y la "
                "ponderación por área. Excepción declarada a la sección 6.")
        lineas.append("")

    if resultado.altitud:
        lineas += [
            "## Rango de las estaciones", "",
            f"Las estaciones cubren de {resultado.altitud['cota_min_estaciones']:.0f} "
            f"a {resultado.altitud['cota_max_estaciones']:.0f} m. El "
            f"{resultado.altitud['area_fuera_ponderada_pct']:.1f} % del área "
            "queda fuera de esa franja, y allí el campo extrapola.", ""]

    if resultado.campos:
        lineas += ["## Precipitación media areal por periodo de retorno", "",
                   "| T (años) | Media ponderada (mm) | Mínimo | Máximo |",
                   "|---|---|---|---|"]
        for campo in resultado.campos:
            lineas.append(
                f"| {campo['periodo_retorno']} | "
                f"{campo['media_ponderada_mm']} | {campo['minimo_mm']} | "
                f"{campo['maximo_mm']} |")
        lineas.append("")

    if resultado.zonas:
        lineas += ["## Zonas pluviométricas", "",
                   "| Zona | Subcuencas | Área (km²) | P media (mm) |",
                   "|---|---|---|---|"]
        for zona in resultado.zonas:
            lineas.append(
                f"| {zona['zona']} | {zona['subcuencas']} | "
                f"{zona['area_km2']} | {zona['precipitacion_media_mm']} |")
        lineas.append("")

    destino.write_text("\n".join(lineas), encoding="utf-8")


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
        "campos": resultado.campos,
        "subcuencas": resultado.subcuencas,
        "zonas": resultado.zonas,
        "gradiente": resultado.gradiente,
        "altitud": resultado.altitud,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(
        json.dumps(reporte, ensure_ascii=False, indent=1), encoding="utf-8")

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

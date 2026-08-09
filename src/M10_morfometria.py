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

LO QUE ESTE MÓDULO NO CALCULA. Los parámetros de RELIEVE (cota máxima, mínima,
media, curva hipsométrica, pendiente de la cuenca) exigen leer el DEM celda a
celda, y el venv no tiene GDAL. Se declaran como pendientes en lugar de
inventarlos: corresponden al entorno SIG.

Productos:
    data/02_procesado/morfometria/parametros.csv
    data/02_procesado/morfometria/tiempo_concentracion.csv
    data/02_procesado/morfometria/M10_morfometria.md
    data/02_procesado/M10_morfometria.json

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

from comun import esquema, geometria, registro, rutas, shapefile  # noqa: E402
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

# Parámetros que exigen el DEM y no se pueden calcular en el venv. Se declaran
# para que el informe sepa que faltan, en lugar de que su ausencia pase
# inadvertida.
PARAMETROS_DE_RELIEVE = (
    "cota_max", "cota_min", "cota_media", "desnivel_altitudinal",
    "curva_hipsometrica", "pendiente_media_cuenca",
)


@dataclass
class ResultadoM10:
    modo: str = ""
    unidades: list[dict[str, Any]] = field(default_factory=list)
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
    """
    area_m2 = float(shapefile.area_poligonos(ruta))
    perimetro_m = float(shapefile.perimetro_poligonos(ruta))
    axial_m = float(shapefile.distancia_maxima(ruta))
    if area_m2 <= 0:
        raise ErrorFormato(f"{ruta.name} no encierra área positiva.")

    area_km2 = area_m2 / 1e6
    perimetro_km = perimetro_m / 1000.0
    axial_km = axial_m / 1000.0
    return {
        "area_km2": round(area_km2, 3),
        "perimetro_km": round(perimetro_km, 3),
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
    Longitud de cauce dentro de la cuenca, densidad y frecuencia.

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
    for entidad in shapefile.leer_geometrias(ruta_drenaje):
        dentro_alguno = False
        for parte in entidad:
            for uno, otro in zip(parte, parte[1:]):
                centro = _centro((uno, otro))
                if not geometria.punto_en_alguno(centro[0], centro[1], poligonos):
                    continue
                longitud_m += math.hypot(otro[0] - uno[0], otro[1] - uno[1])
                dentro_alguno = True
        if dentro_alguno:
            tramos += 1
    return {"long_cauces_km": round(longitud_m / 1000.0, 2),
            "tramos_dentro": tramos}


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
) -> list[dict[str, Any]]:
    """Aplica la matriz y devuelve una fila por fórmula, con su veredicto."""
    salida: list[dict[str, Any]] = []
    for fila in matriz:
        aplicable, motivo = es_aplicable(fila, area_km2, pendiente)
        salida.append({
            "formula": fila.get("formula", ""),
            "nombre": fila.get("nombre", ""),
            "origen": fila.get("origen", ""),
            "area_min_km2": fila.get("area_min_km2"),
            "area_max_km2": fila.get("area_max_km2"),
            "tipo_cuenca": fila.get("tipo_cuenca", ""),
            "aplicable": aplicable,
            "motivo": motivo,
        })
    return salida


def resumir_adopcion(
    evaluadas: Sequence[dict[str, Any]],
    minimo_formulas: int,
) -> dict[str, Any]:
    """
    Decide si procede adoptar un valor, según la cautela de la sección 7.

    Con menos fórmulas aplicables que el mínimo declarado NO se adopta la
    mediana: se advierte y la decisión queda para el consultor. Adoptarla de
    todos modos daría una cifra con la misma apariencia que una bien sustentada.
    """
    aplicables = [e for e in evaluadas if e["aplicable"]]
    return {
        "formulas_evaluadas": len(evaluadas),
        "formulas_aplicables": len(aplicables),
        "minimo_exigido": minimo_formulas,
        "procede_adoptar": len(aplicables) >= minimo_formulas,
        "aplicables": [e["formula"] for e in aplicables],
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
        if parametros["coef_compacidad"] > 1.5:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "morfometria.forma",
                f"coeficiente de compacidad {parametros['coef_compacidad']:.2f}: "
                "la cuenca es alargada. Eso amortigua el hidrograma, porque el "
                "agua de las cabeceras llega desfasada respecto a la cercana al "
                "cierre.",
            ))

    # --- Drenaje -------------------------------------------------------------
    with registro.bloque(logger, "Parametros de drenaje"):
        wkt = None
        reporte_m02 = rutas.directorio("procesado", base) / "M02_delimitacion.json"
        poligonos = []
        try:
            datos = json.loads(reporte_m02.read_text(encoding="utf-8"))
            wkt = (datos.get("geometrias_epsg4326") or {}).get("area_influencia")
        except (OSError, json.JSONDecodeError):
            wkt = None
        if wkt:
            try:
                from pyproj import Transformer
                conversor = Transformer.from_crs(
                    "EPSG:4326", configuracion.obtener("crs.calculo"),
                    always_xy=True)
                poligonos = [
                    [[conversor.transform(float(x), float(y)) for x, y in anillo]
                     for anillo in poli]
                    for poli in geometria.poligonos_de_wkt(wkt)
                ]
            except (ImportError, ErrorFormato):
                poligonos = []

        drenaje = rutas.resolver(
            configuracion.obtener("referencia_nacional.salida_recorte_sencillo"),
            base)
        if poligonos and drenaje.is_file():
            red = parametros_de_drenaje(drenaje, poligonos)
            parametros.update(red)
            if parametros["area_km2"]:
                parametros["densidad_drenaje_km_km2"] = round(
                    red["long_cauces_km"] / parametros["area_km2"], 4)
            logger.info("%.0f km de cauce dentro | densidad %.3f km/km2",
                        red["long_cauces_km"],
                        parametros.get("densidad_drenaje_km_km2", 0.0))
        else:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "morfometria.drenaje",
                "no se pudo calcular la densidad de drenaje: falta el recorte "
                "del drenaje o la geometria del area del M02.",
            ))

    resultado.unidades.append(parametros)

    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "morfometria.relieve",
        "los parametros de RELIEVE no se calcularon: "
        + ", ".join(PARAMETROS_DE_RELIEVE)
        + ". Exigen leer el DEM celda a celda y el venv no tiene GDAL. "
        "Corresponden al entorno SIG y quedan pendientes; sin ellos no hay "
        "pendiente media de cuenca, que varias formulas de Tc necesitan.",
    ))

    # --- Tiempo de concentracion ---------------------------------------------
    with registro.bloque(logger, "Tiempo de concentracion"):
        matriz = leer_matriz_aplicabilidad(
            rutas.resolver(
                configuracion.obtener("tiempo_concentracion.tabla_aplicabilidad"),
                base),
            delimitador)
        # Sin DEM no hay pendiente media: la matriz se aplica solo por area, y
        # se declara, porque filtrar tambien por pendiente descartaria mas.
        resultado.tiempos = evaluar_aplicabilidad(
            matriz, parametros["area_km2"], None)
        minimo = int(configuracion.obtener(
            "tiempo_concentracion.min_formulas_aplicables"))
        resultado.adoptados = resumir_adopcion(resultado.tiempos, minimo)

        aplicables = resultado.adoptados["formulas_aplicables"]
        logger.info("%d de %d formula(s) aplicables (minimo exigido %d)",
                    aplicables, resultado.adoptados["formulas_evaluadas"],
                    minimo)

        if not resultado.adoptados["procede_adoptar"]:
            fuera = [e for e in resultado.tiempos if not e["aplicable"]][:4]
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA if aplicables else BLOQUEANTE,
                "tiempo_concentracion.no_adoptable",
                f"solo {aplicables} formula(s) aplican a una cuenca de "
                f"{parametros['area_km2']:.0f} km2, por debajo del minimo de "
                f"{minimo}. NO se adopta la mediana, como exige CLAUDE.md, "
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
        else:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "tiempo_concentracion.aplicables",
                f"{aplicables} formula(s) aplicables: "
                f"{resultado.adoptados['aplicables']}. El valor se adopta por "
                f"{configuracion.obtener('tiempo_concentracion.valor_adoptado')}"
                " del subconjunto.",
            ))

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

    for nombre, contenido in (("parametros.csv", resultado.unidades),
                              ("tiempo_concentracion.csv", resultado.tiempos)):
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))

    informe = directorio / "M10_morfometria.md"
    _escribir_informe(informe, resultado, configuracion)
    resultado.productos.append(rutas.relativa(informe, base))
    logger.info("%d unidad(es) caracterizada(s)", len(resultado.unidades))


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
    lineas += [
        "",
        "## Parametros de relieve",
        "",
        "NO se calcularon: " + ", ".join(PARAMETROS_DE_RELIEVE) + ".",
        "",
        "Exigen leer el DEM celda a celda y este modulo corre en el venv, que",
        "no tiene GDAL. Corresponden al entorno SIG y quedan pendientes. Sin",
        "ellos no hay pendiente media de cuenca, que varias formulas de tiempo",
        "de concentracion necesitan.",
        "",
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
        ["formula", "nombre", "area_min_km2", "area_max_km2", "aplicable",
         "motivo"])
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

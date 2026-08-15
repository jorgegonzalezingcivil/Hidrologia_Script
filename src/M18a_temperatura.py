#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M18a - Temperatura por gradiente altitudinal
============================================
Entorno: venv del proyecto.

PRIMER PARÁMETRO DEL BALANCE HÍDRICO. Toma las series diarias de temperatura
máxima y mínima que el M04 consolidó, las agrega, ajusta el campo térmico contra
la elevación y lo lleva a cada subcuenca. La evapotranspiración se apoya en este
resultado.

NO SE INTERPOLA POR DISTANCIA. En montaña la elevación explica la temperatura
mucho mejor que la vecindad horizontal: dos estaciones separadas cinco
kilómetros pero con seiscientos metros de desnivel no se parecen. Se ajusta por
tanto una regresión T = a + b·h sobre las estaciones, y se evalúa sobre la
elevación media de cada subcuenca.

EVALUAR EN LA COTA MEDIA ES EXACTO, NO UNA APROXIMACIÓN. Con una relación lineal,
la media de T sobre un área es a + b por la media de h sobre esa área. La cota
media por subcuenca la calculó el M10 sobre el DEM celda a celda, de modo que no
hace falta volver a recorrerlo ni construir un ráster intermedio.

EL AJUSTE ES DE ESTE ESTUDIO, SIEMPRE. Los coeficientes se recalculan con las
estaciones del caso; ninguno se hereda. Un gradiente ajustado en otra cuenca
describe esa cuenca, y aplicado aquí produce un campo verosímil y falso.

LO QUE EL MÓDULO VIGILA Y POR QUÉ. Un R² alto no basta para dar por bueno un
gradiente. Se reportan además el intervalo de confianza de la pendiente, el
contraste contra los gradientes de referencia y la fracción de área que queda
por encima de la estación más alta. Ese último dato es el que decide si el campo
sobre las partes altas está medido o extrapolado.

Productos:
    data/02_procesado/temperatura/temperatura_mensual.csv
    data/02_procesado/temperatura/temperatura_por_estacion.csv
    data/02_procesado/temperatura/gradiente.csv
    data/02_procesado/temperatura/temperatura_por_subcuenca.csv
    data/05_resultados/graficos/M18a_*.png y .svg
    data/02_procesado/M18a_temperatura.json

Uso:
    python src/M18a_temperatura.py

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import calendar
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

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M18a"
DESCRIPCION = "Temperatura por gradiente altitudinal"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Fase con que se etiqueta el ajuste que usa TODOS los meses, sin separar por
# ENSO. Es el que gobierna el balance; los de fase sirven para el contraste.
FASE_COMPUESTA = "compuesto"


@dataclass
class ResultadoM18a:
    mensual: list[dict[str, Any]] = field(default_factory=list)
    estaciones: list[dict[str, Any]] = field(default_factory=list)
    gradientes: list[dict[str, Any]] = field(default_factory=list)
    subcuencas: list[dict[str, Any]] = field(default_factory=list)
    cobertura: dict[str, Any] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def agregar_mensual(
    registros: Sequence[dict[str, Any]], completitud_min: float,
) -> tuple[list[dict[str, Any]], int]:
    """
    Lleva la serie diaria a media mensual, con control de completitud.

    UN MES INCOMPLETO NO SE PROMEDIA COMO SI LO ESTUVIERA. Con temperatura el
    riesgo es distinto al de la lluvia: no subestima el total, sesga la media
    hacia la estación del año que quedó representada. Un mes con solo los diez
    primeros días no dice lo mismo que el mes entero, y promediar los dos juntos
    mezcla dos cosas.

    Se exige la fracción declarada de los días que el mes tiene realmente, no de
    treinta: febrero no se juzga con la misma vara que julio.

    Devuelve las medias mensuales y cuántos meses se descartaron.
    """
    por_mes: dict[tuple[str, str, int, int], list[float]] = {}
    for fila in registros:
        try:
            anio, mes = int(fila["fecha"][:4]), int(fila["fecha"][5:7])
            valor = float(fila["valor"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        clave = (fila["codigo"], fila["etiqueta"], anio, mes)
        por_mes.setdefault(clave, []).append(valor)

    salida: list[dict[str, Any]] = []
    descartados = 0
    for (codigo, etiqueta, anio, mes), valores in sorted(por_mes.items()):
        dias = calendar.monthrange(anio, mes)[1]
        completitud = len(valores) / dias
        if completitud < completitud_min:
            descartados += 1
            continue
        salida.append({
            "codigo": codigo, "etiqueta": etiqueta, "anio": anio, "mes": mes,
            "media_c": round(sum(valores) / len(valores), 3),
            "dias_con_dato": len(valores), "dias_del_mes": dias,
            "completitud": round(completitud, 3),
        })
    return salida, descartados


def combinar_maxima_y_minima(
    mensual: Sequence[dict[str, Any]], etiqueta_max: str, etiqueta_min: str,
) -> list[dict[str, Any]]:
    """
    Temperatura media mensual como semisuma de la máxima y la mínima.

    SE EXIGEN LAS DOS EN EL MISMO MES. Un mes con máxima pero sin mínima no
    produce media: usar solo una de las dos daría una serie que parece
    temperatura media y es otra cosa, varios grados por encima o por debajo.
    """
    por_clave: dict[tuple[str, int, int], dict[str, float]] = {}
    for fila in mensual:
        clave = (fila["codigo"], fila["anio"], fila["mes"])
        por_clave.setdefault(clave, {})[fila["etiqueta"]] = fila["media_c"]

    salida: list[dict[str, Any]] = []
    for (codigo, anio, mes), valores in sorted(por_clave.items()):
        maxima, minima = valores.get(etiqueta_max), valores.get(etiqueta_min)
        if maxima is None or minima is None:
            continue
        salida.append({
            "codigo": codigo, "anio": anio, "mes": mes,
            "t_max_c": round(maxima, 3), "t_min_c": round(minima, 3),
            "t_media_c": round((maxima + minima) / 2.0, 3),
            "amplitud_c": round(maxima - minima, 3),
        })
    return salida


def ajustar_gradiente(
    alturas: Sequence[float], temperaturas: Sequence[float],
) -> dict[str, Any]:
    """
    Ajusta T = a + b·h por mínimos cuadrados y describe la calidad del ajuste.

    SE DEVUELVE EL INTERVALO DE CONFIANZA DE LA PENDIENTE, no solo el R². Con
    pocas estaciones y un rango de elevación estrecho, el R² puede salir alto y
    la pendiente estar muy mal determinada: el coeficiente de determinación mide
    cuánto de la varianza explica la recta, no cuán segura es su inclinación. El
    gradiente es lo que se extrapola sobre las partes altas de la cuenca, así
    que lo que importa es su incertidumbre.

    El gradiente se expresa además en grados por kilómetro y con signo positivo
    de enfriamiento, que es como se compara con los valores de referencia.

    Excepciones
    -----------
    ErrorHidrologia
        Con menos de tres puntos, o si todas las estaciones están a la misma
        cota: sin rango de elevación no hay pendiente que ajustar.
    """
    if len(alturas) != len(temperaturas):
        raise ErrorHidrologia(
            f"hay {len(alturas)} altura(s) y {len(temperaturas)} temperatura(s).")
    if len(alturas) < 3:
        raise ErrorHidrologia(
            f"se necesitan al menos 3 estaciones para ajustar un gradiente y "
            f"hay {len(alturas)}.")

    n = len(alturas)
    media_h = sum(alturas) / n
    media_t = sum(temperaturas) / n
    sxx = sum((h - media_h) ** 2 for h in alturas)
    if sxx <= 0:
        raise ErrorHidrologia(
            "todas las estaciones estan a la misma cota: sin rango de elevacion "
            "no hay gradiente que ajustar.")
    sxy = sum((h - media_h) * (t - media_t)
              for h, t in zip(alturas, temperaturas))
    pendiente = sxy / sxx
    intercepto = media_t - pendiente * media_h

    residuos = [t - (intercepto + pendiente * h)
                for h, t in zip(alturas, temperaturas)]
    ss_res = sum(r * r for r in residuos)
    ss_tot = sum((t - media_t) ** 2 for t in temperaturas)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    error_pendiente = (math.sqrt(ss_res / (n - 2)) / math.sqrt(sxx)
                       if n > 2 else float("inf"))
    # 1,96 es el cuantil normal al 95 %. Con muestras pequeñas lo correcto es la
    # t de Student, más ancha; se usa el normal por no depender de scipy en un
    # módulo que no lo necesita para nada más, y el intervalo queda del lado
    # OPTIMISTA, lo que se declara para que nadie lo lea como holgado.
    margen = 1.96 * error_pendiente

    return {
        "n": n,
        "intercepto_c": round(intercepto, 4),
        "pendiente_c_por_m": round(pendiente, 7),
        "gradiente_c_por_km": round(-pendiente * 1000.0, 3),
        "gradiente_min_c_por_km": round(-(pendiente + margen) * 1000.0, 3),
        "gradiente_max_c_por_km": round(-(pendiente - margen) * 1000.0, 3),
        "error_pendiente_c_por_km": round(error_pendiente * 1000.0, 3),
        "r2": round(r2, 4),
        "cota_min_m": round(min(alturas), 1),
        "cota_max_m": round(max(alturas), 1),
        "rango_cota_m": round(max(alturas) - min(alturas), 1),
        "residuo_max_c": round(max(abs(r) for r in residuos), 3),
    }


def evaluar(ajuste: dict[str, Any], altura: float) -> float:
    """Temperatura que el ajuste asigna a una elevación."""
    return ajuste["intercepto_c"] + ajuste["pendiente_c_por_m"] * float(altura)


def cobertura_altitudinal(
    cota_min_estaciones: float, cota_max_estaciones: float,
    franjas: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """
    Fracción del área que queda FUERA del rango de elevación de las estaciones.

    ES EL DATO QUE DECIDE SI EL CAMPO ESTÁ MEDIDO O EXTRAPOLADO. Una regresión
    ajustada entre dos cotas describe lo que hay entre ellas; por encima de la
    estación más alta la recta se prolonga sin nada que la sujete, y en una
    cuenca de montaña esa parte suele ser la de mayor rendimiento hídrico, la
    que más pesa en el balance.

    Las franjas son las de la distribución altimétrica del M10, con 'cota_inf',
    'cota_sup' y 'area_km2'. El reparto dentro de una franja partida por el
    límite se hace proporcional a su espesor, que es lo que la propia
    distribución supone.
    """
    total = encima = debajo = 0.0
    for franja in franjas:
        try:
            inferior = float(franja["cota_inf"])
            superior = float(franja["cota_sup"])
            area = float(franja["area_km2"])
        except (KeyError, TypeError, ValueError):
            continue
        total += area
        espesor = superior - inferior
        if espesor <= 0:
            continue
        # Solapamiento de la franja con cada tramo extrapolado, en metros, y de
        # ahi la fraccion de su area que le corresponde.
        sobre = max(0.0, superior - max(inferior, cota_max_estaciones))
        bajo = max(0.0, min(superior, cota_min_estaciones) - inferior)
        encima += area * min(sobre, espesor) / espesor
        debajo += area * min(bajo, espesor) / espesor

    return {
        "area_total_km2": round(total, 3),
        "area_sobre_estaciones_km2": round(encima, 3),
        "area_bajo_estaciones_km2": round(debajo, 3),
        "pct_extrapolado": round(100.0 * (encima + debajo) / total, 2)
        if total > 0 else 0.0,
        "cota_min_estaciones_m": round(cota_min_estaciones, 1),
        "cota_max_estaciones_m": round(cota_max_estaciones, 1),
    }


def contrastar_con_referencia(
    ajuste: dict[str, Any], referencias: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Compara el gradiente ajustado con los de referencia de la doctrina.

    NO SUSTITUYE AL AJUSTE, LO JUZGA. El gradiente que gobierna es siempre el de
    las estaciones del estudio; esta comparación solo dice si ese valor es
    compatible con lo que la física del aire admite, y señala cuando no lo es.
    """
    salida: list[dict[str, Any]] = []
    ajustado = ajuste["gradiente_c_por_km"]
    minimo = ajuste["gradiente_min_c_por_km"]
    maximo = ajuste["gradiente_max_c_por_km"]
    for referencia in referencias:
        try:
            valor = float(referencia["gradiente_c_por_km"])
            tolerancia = float(referencia.get("tolerancia_c_por_km", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        salida.append({
            "criterio": referencia.get("criterio", ""),
            "referencia_c_por_km": valor,
            "diferencia_c_por_km": round(ajustado - valor, 3),
            "diferencia_pct": round(100.0 * (ajustado - valor) / valor, 1)
            if valor else None,
            "dentro_de_tolerancia": abs(ajustado - valor) <= tolerancia,
            "contenido_en_el_intervalo": minimo <= valor <= maximo,
            "fuente": referencia.get("fuente", ""),
        })
    return salida


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Ajusta el campo térmico de la cuenca y lo lleva a cada subcuenca."""
    inicio_reloj = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM18a()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M18a_temperatura.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"series": "data/02_procesado/series/series_ideam.csv"},
        parametros=configuracion.parametros("temperatura"))

    etiqueta_max = str(configuracion.obtener("temperatura.etiqueta_maxima"))
    etiqueta_min = str(configuracion.obtener("temperatura.etiqueta_minima"))
    completitud = float(configuracion.obtener(
        "temperatura.completitud_mensual_min"))
    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))

    with registro.bloque(logger, "Series diarias"):
        try:
            registros, alturas, nombres = _leer_series(
                rutas.directorio("procesado_series", base) / "series_ideam.csv",
                delimitador, (etiqueta_max, etiqueta_min))
        except (ErrorRutas, ErrorFormato) as error:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "temperatura.series", str(error)))
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)
        if not registros:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "temperatura.sin_registros",
                f"la serie consolidada no trae ningun registro de "
                f"{etiqueta_max!r} ni {etiqueta_min!r}. El M04 es quien los "
                "ingesta: revisar que se hayan descargado.",
            ))
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)
        logger.info("%d registro(s) diarios de %d estacion(es)",
                    len(registros), len(alturas))

    with registro.bloque(logger, "Agregacion mensual"):
        mensual, descartados = agregar_mensual(registros, completitud)
        resultado.mensual = combinar_maxima_y_minima(
            mensual, etiqueta_max, etiqueta_min)
        logger.info("%d mes(es) con dato, %d descartado(s) por completitud",
                    len(resultado.mensual), descartados)
        if descartados:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "temperatura.meses_descartados",
                f"{descartados} mes(es) se descartaron por no alcanzar el "
                f"{completitud:.0%} de dias con dato. Con temperatura un mes "
                "incompleto no subestima el total como en la lluvia: sesga la "
                "media hacia los dias que quedaron.",
            ))

    with registro.bloque(logger, "Ajuste del gradiente"):
        if not _resolver_gradiente(configuracion, base, alturas, nombres,
                                   resultado, logger, delimitador):
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Temperatura por subcuenca"):
        _resolver_subcuencas(base, delimitador, resultado, logger)

    with registro.bloque(logger, "Figuras"):
        _escribir_figuras(configuracion, base, resultado, logger)

    _escribir_tablas(configuracion, base, resultado, delimitador, logger)
    resultado.productos = [str(p) for p in resultado.productos]
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _leer_series(ruta, delimitador, etiquetas):
    """Lee de la serie consolidada solo las etiquetas de temperatura."""
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}: la serie consolidada la escribe el M04.")
    registros, alturas, nombres = [], {}, {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        lector = csv.DictReader(manejador, delimiter=delimitador)
        faltan = [c for c in ("codigo", "etiqueta", "fecha", "valor", "altitud")
                  if c not in (lector.fieldnames or [])]
        if faltan:
            raise ErrorFormato(f"{ruta.name} no trae las columnas {faltan}.")
        for fila in lector:
            if fila["etiqueta"] not in etiquetas:
                continue
            registros.append(fila)
            try:
                alturas[fila["codigo"]] = float(fila["altitud"])
            except (TypeError, ValueError):
                pass
            nombres[fila["codigo"]] = fila.get("nombre", "")
    return registros, alturas, nombres


def _resolver_gradiente(configuracion, base, alturas, nombres, resultado,
                        logger, delimitador) -> bool:
    """Resume por estación, ajusta la recta y la contrasta con la doctrina."""
    anios_min = int(configuracion.obtener("temperatura.anios_min"))
    meses_min = int(configuracion.obtener("temperatura.meses_min_por_anio"))

    por_estacion: dict[str, list[dict]] = {}
    for fila in resultado.mensual:
        por_estacion.setdefault(fila["codigo"], []).append(fila)

    for codigo, filas in sorted(por_estacion.items()):
        por_anio: dict[int, int] = {}
        for fila in filas:
            por_anio[fila["anio"]] = por_anio.get(fila["anio"], 0) + 1
        completos = sum(1 for n in por_anio.values() if n >= meses_min)
        medias = [f["t_media_c"] for f in filas]
        resultado.estaciones.append({
            "codigo": codigo, "nombre": nombres.get(codigo, ""),
            "altitud_m": alturas.get(codigo),
            "t_media_c": round(sum(medias) / len(medias), 3),
            "t_max_media_c": round(
                sum(f["t_max_c"] for f in filas) / len(filas), 3),
            "t_min_media_c": round(
                sum(f["t_min_c"] for f in filas) / len(filas), 3),
            "meses": len(filas), "anios_con_dato": len(por_anio),
            "anios_completos": completos,
            "suficiente": completos >= anios_min,
        })

    # SE AJUSTA CON TODAS LAS ESTACIONES, por decision declarada del consultor,
    # y el sesgo se declara. Se calcula ademas el ajuste del subconjunto de
    # series largas, no para sustituirlo sino para que el informe muestre
    # cuanto cambia el gradiente segun a quien se le pregunte.
    utiles = [e for e in resultado.estaciones if e["altitud_m"] is not None]
    largas = [e for e in utiles if e["suficiente"]]
    for etiqueta, grupo in (("todas", utiles), ("series_largas", largas)):
        if len(grupo) < 3:
            continue
        try:
            ajuste = ajustar_gradiente([e["altitud_m"] for e in grupo],
                                       [e["t_media_c"] for e in grupo])
        except ErrorHidrologia as error:
            if etiqueta == "todas":
                resultado.hallazgos.append(Hallazgo(
                    BLOQUEANTE, "temperatura.gradiente", str(error)))
                return False
            continue
        ajuste["conjunto"] = etiqueta
        ajuste["fase"] = FASE_COMPUESTA
        ajuste["adoptado"] = etiqueta == "todas"
        resultado.gradientes.append(ajuste)

    if not any(g["adoptado"] for g in resultado.gradientes):
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "temperatura.sin_gradiente",
            f"solo hay {len(utiles)} estacion(es) con altitud y temperatura, y "
            "hacen falta al menos 3 para ajustar una recta.",
        ))
        return False

    adoptado = next(g for g in resultado.gradientes if g["adoptado"])
    logger.info(
        "Gradiente adoptado: %.2f C/km (IC 95%% %.2f a %.2f), R2=%.3f, n=%d",
        adoptado["gradiente_c_por_km"], adoptado["gradiente_min_c_por_km"],
        adoptado["gradiente_max_c_por_km"], adoptado["r2"], adoptado["n"])

    contraste = contrastar_con_referencia(
        adoptado, _leer_referencias(configuracion, base, delimitador))
    resultado.cobertura["contraste"] = contraste
    _hallazgos_del_gradiente(resultado, adoptado, largas, utiles, contraste,
                             anios_min)
    return True


def _leer_referencias(configuracion, base, delimitador):
    """Gradientes de referencia de la tabla de doctrina."""
    ruta = rutas.resolver(
        configuracion.obtener("temperatura.tabla_gradiente"), base)
    if not ruta.is_file():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        return list(csv.DictReader(manejador, delimiter=delimitador))


def _hallazgos_del_gradiente(resultado, adoptado, largas, utiles, contraste,
                             anios_min) -> None:
    """Convierte el ajuste y su contraste en hallazgos del reporte."""
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "temperatura.gradiente_ajustado",
        f"gradiente ajustado con las {adoptado['n']} estaciones del estudio: "
        f"{adoptado['gradiente_c_por_km']:.2f} C/km, intervalo de confianza al "
        f"95 por ciento de {adoptado['gradiente_min_c_por_km']:.2f} a "
        f"{adoptado['gradiente_max_c_por_km']:.2f}, R2 de {adoptado['r2']:.3f}. "
        f"La recta es T = {adoptado['intercepto_c']:.2f} "
        f"{adoptado['pendiente_c_por_m']:+.5f}*h, ajustada entre "
        f"{adoptado['cota_min_m']:.0f} y {adoptado['cota_max_m']:.0f} m. Los "
        "coeficientes son de ESTE estudio: ninguno se hereda de otro.",
    ))

    fuera = [c for c in contraste
             if c["criterio"] == "adiabatico_ambiental"
             and not c["contenido_en_el_intervalo"]]
    if fuera:
        caso = fuera[0]
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "temperatura.gradiente_fuera_de_referencia",
            f"el gradiente ajustado se aparta un {caso['diferencia_pct']:+.0f} "
            f"por ciento del adiabatico ambiental de "
            f"{caso['referencia_c_por_km']:.1f} C/km, y el intervalo de "
            "confianza NO lo contiene. Eso no es ruido del ajuste: apunta a un "
            "sesgo en la muestra de estaciones. La causa habitual es que las "
            "bajas sean urbanas y las altas rurales, de modo que la isla de "
            "calor empina la recta y enfria de mas las partes altas. SE ADOPTA "
            "IGUALMENTE, por decision declarada del consultor, y el informe "
            "debe recoger la salvedad.",
        ))

    limite = next((c for c in contraste
                   if c["criterio"] == "adiabatico_seco"), None)
    if limite and limite["diferencia_c_por_km"] > 0:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "temperatura.gradiente_imposible",
            f"el gradiente ajustado supera el adiabatico seco "
            f"({limite['referencia_c_por_km']:.1f} C/km), que es el limite "
            "fisico de una parcela de aire. Un campo termico no puede "
            "enfriarse mas rapido con la altura: revisar las altitudes de las "
            "estaciones antes de seguir.",
        ))

    largo = next((g for g in resultado.gradientes
                  if g["conjunto"] == "series_largas"), None)
    if largo:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "temperatura.contraste_series_largas",
            f"con las {largo['n']} estacion(es) de {anios_min} anos o mas el "
            f"gradiente seria {largo['gradiente_c_por_km']:.2f} C/km con R2 de "
            f"{largo['r2']:.3f}, frente a {adoptado['gradiente_c_por_km']:.2f} "
            f"con las {adoptado['n']} de todas. Se adopta el de todas por "
            "decision declarada; la diferencia mide cuanto depende el "
            "resultado de a que estaciones se le pregunte.",
        ))
    if len(largas) < len(utiles) / 2:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "temperatura.series_cortas",
            f"solo {len(largas)} de {len(utiles)} estaciones alcanzan "
            f"{anios_min} anos con registro suficiente. El gradiente se apoya "
            "en buena parte en series cortas, cuya media multianual es menos "
            "representativa del clima que la de una serie larga.",
        ))


def _resolver_subcuencas(base, delimitador, resultado, logger) -> None:
    """
    Evalúa el gradiente en la cota media de cada subcuenca.

    ES EXACTO Y NO UNA APROXIMACIÓN: con una relación lineal, la media de la
    temperatura sobre un área es la recta evaluada en la media de la elevación
    sobre esa área. La cota media la calculó el M10 celda a celda sobre el DEM.
    """
    adoptado = next((g for g in resultado.gradientes if g["adoptado"]), None)
    if adoptado is None:
        return

    ruta = base / "data/02_procesado/morfometria/subcuencas.csv"
    if not ruta.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "temperatura.sin_subcuencas",
            f"no se encuentra {ruta.name}: el campo termico queda sin llevar a "
            "las subcuencas. Ejecutar antes el M10."))
        return
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        subcuencas = list(csv.DictReader(manejador, delimiter=delimitador))

    for fila in subcuencas:
        try:
            cota = float(fila["cota_media"])
            area = float(fila["area_km2"])
        except (KeyError, TypeError, ValueError):
            continue
        resultado.subcuencas.append({
            "subcuenca": fila.get("subcuenca", ""),
            "area_km2": round(area, 4),
            "cota_media_m": round(cota, 1),
            "t_media_c": round(evaluar(adoptado, cota), 2),
            "extrapolada": not (adoptado["cota_min_m"] <= cota
                                <= adoptado["cota_max_m"]),
        })

    franjas = _leer_franjas(base, delimitador)
    if franjas:
        resultado.cobertura.update(cobertura_altitudinal(
            adoptado["cota_min_m"], adoptado["cota_max_m"], franjas))

    area_total = sum(s["area_km2"] for s in resultado.subcuencas)
    if area_total > 0:
        resultado.cobertura["t_media_cuenca_c"] = round(sum(
            s["t_media_c"] * s["area_km2"]
            for s in resultado.subcuencas) / area_total, 2)
    extrapoladas = [s for s in resultado.subcuencas if s["extrapolada"]]
    logger.info("%d subcuenca(s) con temperatura; media de cuenca %.2f C",
                len(resultado.subcuencas),
                resultado.cobertura.get("t_media_cuenca_c", float("nan")))

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "temperatura.por_subcuenca",
        f"{len(resultado.subcuencas)} subcuenca(s) reciben su temperatura media "
        f"evaluando la recta en su cota media, y la media de la cuenca es de "
        f"{resultado.cobertura.get('t_media_cuenca_c')} C. Evaluar en la cota "
        "media es exacto y no una aproximacion: con una relacion lineal, la "
        "media de T sobre un area es la recta evaluada en la media de h.",
    ))

    pct = resultado.cobertura.get("pct_extrapolado")
    if pct:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA if pct >= 5 else INFORMATIVO,
            "temperatura.extrapolacion",
            f"el {pct:.1f} por ciento del area de la cuenca queda FUERA del "
            f"rango de elevacion de las estaciones "
            f"({adoptado['cota_min_m']:.0f} a {adoptado['cota_max_m']:.0f} m), "
            f"de los cuales "
            f"{resultado.cobertura.get('area_sobre_estaciones_km2', 0):.1f} km2 "
            f"por encima de la mas alta, y {len(extrapoladas)} subcuenca(s) "
            "quedan enteramente ahi. En esa parte el campo termico no esta "
            "medido sino prolongado por la recta, y en una cuenca de montana "
            "suele ser la de mayor rendimiento hidrico, la que mas pesa en el "
            "balance.",
        ))


def _leer_franjas(base, delimitador):
    """Distribución altimétrica del M10, para medir la extrapolación."""
    ruta = base / "data/02_procesado/morfometria/distribucion_altimetrica.csv"
    if not ruta.is_file():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        return list(csv.DictReader(manejador, delimiter=delimitador))


def _escribir_tablas(configuracion, base, resultado, delimitador, logger) -> None:
    """Escribe las cuatro tablas del módulo."""
    destino = rutas.resolver(configuracion.obtener("temperatura.salida"), base)
    destino.mkdir(parents=True, exist_ok=True)
    for nombre, filas in (("temperatura_mensual", resultado.mensual),
                          ("temperatura_por_estacion", resultado.estaciones),
                          ("gradiente", resultado.gradientes),
                          ("temperatura_por_subcuenca", resultado.subcuencas)):
        ruta = destino / f"{nombre}.csv"
        _escribir_csv(ruta, filas, delimitador)
        resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("Tablas escritas en %s", rutas.relativa(destino, base))


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Escribe una tabla con la union de las columnas de todas sus filas."""
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


def _escribir_figuras(configuracion, base, resultado, logger) -> None:
    """Las cuatro figuras del capítulo de temperatura del informe."""
    if not resultado.estaciones:
        return
    try:
        import graficos
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron dibujar las figuras: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    adoptado = next((g for g in resultado.gradientes if g["adoptado"]), None)
    escritas = 0

    # 1. La figura que sostiene el metodo: temperatura contra elevacion.
    if adoptado:
        utiles = [e for e in resultado.estaciones if e["altitud_m"] is not None]
        largas = [e for e in utiles if e["suficiente"]]
        cortas = [e for e in utiles if not e["suficiente"]]
        with graficos.figura(
                estilo, titulo="Temperatura media contra elevación",
                etiqueta_x="Elevación (m s. n. m.)",
                etiqueta_y="Temperatura media (°C)") as (fig, ax):
            for grupo, etiqueta, relleno in (
                    (largas, "series largas", True), (cortas, "series cortas", False)):
                if not grupo:
                    continue
                ax.scatter([e["altitud_m"] for e in grupo],
                           [e["t_media_c"] for e in grupo],
                           s=34, color=estilo.color(0 if relleno else 1),
                           facecolors=None if relleno else "none",
                           edgecolors=estilo.color(0 if relleno else 1),
                           label=etiqueta, zorder=3)
            cotas = [min(e["altitud_m"] for e in utiles),
                     max(e["altitud_m"] for e in utiles)]
            ax.plot(cotas, [evaluar(adoptado, c) for c in cotas],
                    color="#b03a2e", linewidth=1.6,
                    label=f"ajuste, {adoptado['gradiente_c_por_km']:.2f} °C/km")
            # El tramo extrapolado se dibuja PARTIDO: la recta no esta sujeta
            # por ningun dato ahi, y con trazo continuo se lee como si lo
            # estuviera.
            techo = max((s["cota_media_m"] for s in resultado.subcuencas),
                        default=cotas[1])
            if techo > cotas[1]:
                ax.plot([cotas[1], techo],
                        [evaluar(adoptado, cotas[1]), evaluar(adoptado, techo)],
                        color="#b03a2e", linewidth=1.4, linestyle="--",
                        label="extrapolación sobre la cuenca")
            ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
            fig.text(0.01, -0.04,
                     f"Ajuste con las {adoptado['n']} estaciones del estudio. "
                     f"R² = {adoptado['r2']:.3f}. Intervalo de confianza al "
                     f"95 % del gradiente: {adoptado['gradiente_min_c_por_km']:.2f} "
                     f"a {adoptado['gradiente_max_c_por_km']:.2f} °C/km.",
                     fontsize=estilo.tamano_fuente - 2, color="#555555")
            for ruta in graficos.guardar(
                    fig, directorio / "M18a_gradiente_altitudinal", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
            escritas += 1

    # 2. Ciclo anual de la temperatura media, maxima y minima.
    por_mes: dict[int, list[dict]] = {}
    for fila in resultado.mensual:
        por_mes.setdefault(fila["mes"], []).append(fila)
    if por_mes:
        meses = sorted(por_mes)
        series = {}
        for etiqueta, columna in (("máxima", "t_max_c"), ("media", "t_media_c"),
                                  ("mínima", "t_min_c")):
            series[etiqueta] = (
                meses, [sum(f[columna] for f in por_mes[m]) / len(por_mes[m])
                        for m in meses])
        with graficos.figura(
                estilo, titulo="Ciclo anual de la temperatura",
                etiqueta_x="Mes", etiqueta_y="Temperatura (°C)") as (fig, ax):
            graficos.lineas(ax, series, estilo)
            ax.set_xticks(meses)
            fig.text(0.01, -0.04,
                     f"Media de las {len({f['codigo'] for f in resultado.mensual})} "
                     "estaciones con registro.",
                     fontsize=estilo.tamano_fuente - 2, color="#555555")
            for ruta in graficos.guardar(
                    fig, directorio / "M18a_ciclo_anual", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
            escritas += 1

    # 3. Cuantos anios aporta cada estacion, ordenadas por elevacion.
    utiles = sorted((e for e in resultado.estaciones if e["altitud_m"] is not None),
                    key=lambda e: e["altitud_m"])
    if utiles:
        with graficos.figura(
                estilo, titulo="Años con registro por estación",
                etiqueta_x="Años completos", etiqueta_y="",
                alto_cm=graficos.alto_para_filas(len(utiles), estilo)) as (fig, ax):
            posiciones = range(len(utiles))
            ax.barh(list(posiciones), [e["anios_completos"] for e in utiles],
                    color=[estilo.color(0) if e["suficiente"] else "#b0b0b0"
                           for e in utiles])
            ax.set_yticks(list(posiciones))
            ax.set_yticklabels([f"{e['codigo']} ({e['altitud_m']:.0f} m)"
                                for e in utiles],
                               fontsize=estilo.tamano_fuente - 2)
            ax.invert_yaxis()
            for ruta in graficos.guardar(
                    fig, directorio / "M18a_anios_por_estacion", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
            escritas += 1

    # 4. Representacion geografica del campo termico por subcuenca.
    if resultado.subcuencas:
        escritas += _mapa_de_temperatura(configuracion, base, resultado,
                                         estilo, directorio, graficos)
    logger.info("%d figura(s) escritas", escritas)


def _mapa_de_temperatura(configuracion, base, resultado, estilo, directorio,
                         graficos) -> int:
    """Coropleta de la temperatura media por subcuenca, si hay geometría."""
    from comun import shapefile

    ruta = rutas.resolver(configuracion.obtener(
        "hec_hms.intercambio.subcuencas", ""), base) if configuracion.obtener(
            "hec_hms.intercambio.subcuencas", "") else None
    if ruta is None or not Path(ruta).is_file():
        candidatas = sorted((base / "data/03_SIG/vector").glob("*ubCuenca*.shp"))
        ruta = candidatas[0] if candidatas else None
    if ruta is None or not Path(ruta).is_file():
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "temperatura.sin_geometria",
            "no se encontro el shapefile de subcuencas: el campo termico queda "
            "en tabla, sin mapa."))
        return 0

    try:
        entidades = shapefile.leer_geometrias(Path(ruta))
    except Exception as error:  # noqa: BLE001 - depende del insumo del usuario
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "temperatura.mapa_no_dibujado",
            f"no se pudo leer la geometria de subcuencas: {error}"))
        return 0

    # EL ORDEN ES EL DEL ARCHIVO, y es el mismo con que el M10 escribio
    # subcuencas.csv. Se comprueba el conteo antes de dibujar: si no coincide,
    # el mapa pintaria cada subcuenca con la temperatura de otra, y saldria un
    # campo termico verosimil y cruzado.
    if len(entidades) != len(resultado.subcuencas):
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "temperatura.mapa_descuadrado",
            f"el shapefile trae {len(entidades)} entidad(es) y la tabla "
            f"{len(resultado.subcuencas)} subcuenca(s). No se dibuja el mapa: "
            "con los conteos desiguales cada poligono recibiria el valor de "
            "otro y el resultado pareceria correcto."))
        return 0
    valores = [s["t_media_c"] for s in resultado.subcuencas]

    with graficos.figura(
            estilo, titulo="Temperatura media por subcuenca",
            etiqueta_x="Este (m)", etiqueta_y="Norte (m)") as (fig, ax):
        mapeador = graficos.coropleta(ax, entidades, valores, estilo)
        graficos.barra_de_color(fig, ax, mapeador, estilo, "Temperatura (°C)")
        for ruta_figura in graficos.guardar(
                fig, directorio / "M18a_mapa_temperatura", estilo):
            resultado.productos.append(rutas.relativa(ruta_figura, base))
    return 1


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
    if conteo[BLOQUEANTE] and codigo == SALIDA_CORRECTA:
        codigo = SALIDA_BLOQUEANTE
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    reporte = {
        "modulo": MODULO,
        "estaciones": len(resultado.estaciones),
        "meses": len(resultado.mensual),
        "gradientes": resultado.gradientes,
        "cobertura": resultado.cobertura,
        "subcuencas": len(resultado.subcuencas),
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
    raise SystemExit(main())

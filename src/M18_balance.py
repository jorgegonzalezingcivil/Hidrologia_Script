#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M18 - Precipitación del balance y balance hídrico
=================================================
Entorno: venv del proyecto.

DOS ESCALAS CON ALCANCES DISTINTOS, por decisión del consultor:

    multianual, por SUBCUENCA   Budyko, Dekop y Turc, las tres, para contraste
    mensual,    por CUENCA      Budyko, que es lo que el informe propio usa

Las dos se comparan entre sí: el promedio de la serie mensual debe parecerse al
caudal de largo plazo, y si no lo hace es que algo falla en una de ellas.

LA PRECIPITACIÓN SE INTERPOLA POR PROXIMIDAD Y NO POR ELEVACIÓN, y eso está
MEDIDO sobre este estudio, no supuesto: el total anual contra la altura da un R²
de 0,026, frente al 0,773 del campo térmico. La lluvia y la temperatura no se
comportan igual, y por eso cada una lleva su método (CLAUDE.md, sección 6).

NO SE CONSTRUYE NINGÚN RÁSTER. El IDW se evalúa DIRECTAMENTE en el destino, el
centroide de cada subcuenca, en lugar de rellenar una malla y hacer estadística
zonal después. Es la misma matemática sin el paso intermedio, y sin introducir
una resolución de celda que no aporta nada.

SIN ALMACENAMIENTO ENTRE MESES, por decisión declarada. El uso es obras de
protección y definición de niveles, no abastecimiento. Budyko supone cambio de
almacenamiento despreciable, cierto sobre años y no sobre un mes: la serie
mensual subestima la variabilidad y el informe debe declararlo.

Productos:
    data/02_procesado/precipitacion/precipitacion_anual_por_subcuenca.csv
    data/02_procesado/precipitacion/precipitacion_mensual_cuenca.csv
    data/02_procesado/balance/balance_multianual.csv
    data/02_procesado/balance/balance_mensual.csv
    data/05_resultados/excel/M18_balance.xlsx
    data/05_resultados/graficos/M18_*.png y .svg
    data/02_procesado/M18_balance.json

Uso:
    python src/M18_balance.py

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

from comun import esquema, geometria, registro, rutas, shapefile  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M18"
DESCRIPCION = "Precipitación del balance y balance hídrico"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

DIAS_DEL_ANIO = 365.25


@dataclass
class ResultadoM18:
    estaciones: list[dict[str, Any]] = field(default_factory=list)
    por_subcuenca: list[dict[str, Any]] = field(default_factory=list)
    mensual_cuenca: list[dict[str, Any]] = field(default_factory=list)
    multianual: list[dict[str, Any]] = field(default_factory=list)
    mensual: list[dict[str, Any]] = field(default_factory=list)
    contraste: dict[str, Any] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def idw(
    destino: tuple[float, float],
    fuentes: Sequence[tuple[float, float, float]],
    exponente: float = 2.0,
    radio_max_m: float = 0.0,
    minimo_estaciones: int = 1,
) -> dict[str, Any]:
    """
    Interpolación por distancia inversa, evaluada en UN punto.

    Cada fuente es (x, y, valor). El peso de cada una es 1/d^exponente, de modo
    que las cercanas mandan sobre las lejanas.

    SE EVALÚA EN EL DESTINO Y NO SOBRE UNA MALLA. Rellenar un ráster para luego
    promediarlo sobre la subcuenca da lo mismo con más pasos, y añade una
    resolución de celda que no procede de ningún dato.

    UN DESTINO QUE COINCIDE CON UNA FUENTE devuelve su valor sin más: la
    distancia es cero y el peso sería infinito.

    IDW NO EXTRAPOLA. El resultado queda siempre entre el mínimo y el máximo de
    las fuentes usadas, y por eso se devuelven ambos: en una cuenca cuya parte
    alta no tiene estaciones, eso significa que la lámina de esa zona está
    acotada por lo que se midió más abajo, y quien llame debe advertirlo.

    Excepciones
    -----------
    ErrorHidrologia
        Si no hay fuentes suficientes dentro del radio.
    """
    candidatas = []
    for x, y, valor in fuentes:
        distancia = math.hypot(destino[0] - x, destino[1] - y)
        if radio_max_m > 0 and distancia > radio_max_m:
            continue
        candidatas.append((distancia, valor))

    if len(candidatas) < max(1, minimo_estaciones):
        raise ErrorHidrologia(
            f"solo {len(candidatas)} estacion(es) dentro del radio de "
            f"{radio_max_m:.0f} m y se exigen {minimo_estaciones}.")

    exacta = [v for d, v in candidatas if d <= 1e-6]
    if exacta:
        return {"valor": round(exacta[0], 3), "estaciones": len(candidatas),
                "distancia_min_m": 0.0, "minimo": round(min(
                    v for _, v in candidatas), 3),
                "maximo": round(max(v for _, v in candidatas), 3)}

    pesos = [1.0 / d ** exponente for d, _ in candidatas]
    total = sum(pesos)
    valor = sum(p * v for p, (_, v) in zip(pesos, candidatas)) / total
    return {
        "valor": round(valor, 3),
        "estaciones": len(candidatas),
        "distancia_min_m": round(min(d for d, _ in candidatas), 1),
        "minimo": round(min(v for _, v in candidatas), 3),
        "maximo": round(max(v for _, v in candidatas), 3),
    }


def balance(precipitacion_mm: float, etp_mm: float, area_km2: float,
            dias: float, temperatura_c: float | None = None) -> dict[str, Any]:
    """
    Cierra el balance con las tres formulaciones y devuelve caudal por cada una.

    P = ETR + E, con E la escorrentía. Turc solo se calcula si se recibe
    temperatura Y la escala es anual, cosa que garantiza quien llama: su
    polinomio está calibrado con valores anuales.
    """
    import M18a_temperatura as etr

    salida: dict[str, Any] = {
        "precipitacion_mm": round(precipitacion_mm, 2),
        "etp_mm": round(etp_mm, 2),
        "area_km2": round(area_km2, 4),
        "dias": dias,
    }
    for nombre, valor in (
            ("budyko", etr.etr_budyko(precipitacion_mm, etp_mm)),
            ("dekop", etr.etr_dekop(precipitacion_mm, etp_mm))):
        escurrida = etr.escorrentia(precipitacion_mm, valor)
        salida[f"etr_{nombre}_mm"] = round(valor, 2)
        salida[f"escorrentia_{nombre}_mm"] = round(escurrida, 2)
        salida[f"caudal_{nombre}_m3s"] = round(
            etr.caudal_medio(escurrida, area_km2, dias), 4)
    if temperatura_c is not None:
        valor = etr.etr_turc(precipitacion_mm, temperatura_c)
        escurrida = etr.escorrentia(precipitacion_mm, valor)
        salida["etr_turc_mm"] = round(valor, 2)
        salida["escorrentia_turc_mm"] = round(escurrida, 2)
        salida["caudal_turc_m3s"] = round(
            etr.caudal_medio(escurrida, area_km2, dias), 4)
    return salida


def contrastar_escalas(caudal_multianual: float,
                       caudales_mensuales: Sequence[float]) -> dict[str, Any]:
    """
    Compara el caudal de largo plazo con el promedio de la serie mensual.

    ES LA VERIFICACIÓN MUTUA DE LAS DOS ESCALAS. Parten de la misma lluvia y la
    misma evapotranspiración; si su promedio no coincide, algo falla en una de
    las dos y el informe no puede presentarlas juntas sin explicarlo.

    No tienen por qué salir idénticas: la multianual se calcula por subcuenca y
    se agrega, mientras que la mensual se calcula sobre la cuenca entera, y
    Budyko no es lineal. La diferencia mide ese efecto y no un error.
    """
    if not caudales_mensuales or caudal_multianual <= 0:
        return {}
    promedio = sum(caudales_mensuales) / len(caudales_mensuales)
    return {
        "caudal_multianual_m3s": round(caudal_multianual, 4),
        "promedio_mensual_m3s": round(promedio, 4),
        "diferencia_pct": round(
            100.0 * (promedio - caudal_multianual) / caudal_multianual, 2),
        "minimo_mensual_m3s": round(min(caudales_mensuales), 4),
        "maximo_mensual_m3s": round(max(caudales_mensuales), 4),
        "razon_max_min": round(
            max(caudales_mensuales) / min(caudales_mensuales), 2)
        if min(caudales_mensuales) > 0 else None,
    }


def cobertura_de_estaciones(
    cotas_estaciones: Sequence[float], franjas: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """
    Área de la cuenca por encima de la estación más alta, y por debajo de la más baja.

    IDW NO EXTRAPOLA: la lámina de una zona sin estaciones queda acotada por lo
    que se midió en otra parte. En montaña tropical la precipitación crece con
    la altura hasta la franja de condensación máxima, de modo que una cuenca sin
    estaciones en su parte alta tiende a salir con la oferta subestimada
    justamente donde más aporta.
    """
    import M18a_temperatura as m18a

    if not cotas_estaciones:
        return {}
    return m18a.cobertura_altitudinal(
        min(cotas_estaciones), max(cotas_estaciones), franjas)


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Interpola la precipitación del balance y lo cierra en las dos escalas."""
    inicio_reloj = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM18()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M18_balance.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"temperatura": "data/02_procesado/temperatura"},
        parametros=configuracion.parametros("balance_hidrico"))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))

    with registro.bloque(logger, "Estaciones y precipitacion anual"):
        try:
            resultado.estaciones = _leer_estaciones(base, delimitador,
                                                    configuracion)
        except (ErrorRutas, ErrorFormato, ErrorHidrologia) as error:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "balance.estaciones", str(error)))
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)
        logger.info("%d estacion(es) con total anual y coordenada",
                    len(resultado.estaciones))

    with registro.bloque(logger, "Precipitacion por subcuenca"):
        if not _interpolar(configuracion, base, delimitador, resultado, logger):
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Balance multianual por subcuenca"):
        _balance_multianual(base, delimitador, resultado, logger)

    with registro.bloque(logger, "Balance mensual de la cuenca"):
        _balance_mensual(base, delimitador, resultado, logger)

    with registro.bloque(logger, "Tablas y figuras"):
        _escribir(configuracion, base, delimitador, resultado, logger)

    resultado.productos = [str(p) for p in resultado.productos]
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _leer_estaciones(base, delimitador, configuracion):
    """
    Estaciones con total anual multianual, coordenada y altitud.

    EL TOTAL SALE DE LA FASE NEUTRAL O DE LA MEDIA DE FASES, no de sumar la
    serie sin más: el M05b ya resolvió qué años están completos y cuáles no, y
    volver a agregarlo aquí duplicaría ese criterio con otro resultado.
    """
    ruta_fases = rutas.directorio("procesado_enso", base) / "precipitacion_por_fase.csv"
    ruta_inv = (rutas.directorio("procesado_estaciones", base)
                / "inventario_estaciones.csv")
    for ruta, quien in ((ruta_fases, "M05b"), (ruta_inv, "M03")):
        if not ruta.is_file():
            raise ErrorRutas(
                f"no se encuentra {ruta.name}: lo escribe el {quien}.")

    totales: dict[str, list[float]] = {}
    with ruta_fases.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            if str(fila.get("completo", "")).strip().lower() != "true":
                continue
            try:
                totales.setdefault(fila["codigo"], []).append(
                    float(fila["total_anual_mm"]))
            except (KeyError, TypeError, ValueError):
                continue

    fichas: dict[str, dict] = {}
    with ruta_inv.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            codigo = str(fila.get("Código de la estación", "")).strip()
            try:
                fichas[codigo] = {
                    "nombre": fila.get("Nombre de la estación", ""),
                    "latitud": float(str(fila["Latitud"]).replace(",", ".")),
                    "longitud": float(str(fila["Longitud"]).replace(",", ".")),
                    "altitud": float(str(fila["Altitud"]).replace(",", ".")),
                }
            except (KeyError, TypeError, ValueError):
                continue

    salida = []
    for codigo, valores in sorted(totales.items()):
        ficha = fichas.get(codigo)
        if ficha is None:
            continue
        salida.append({
            "codigo": codigo, **ficha,
            "p_anual_mm": round(sum(valores) / len(valores), 1),
            "fases": len(valores),
        })
    if len(salida) < 3:
        raise ErrorHidrologia(
            f"solo {len(salida)} estacion(es) con total anual y coordenada: no "
            "alcanzan para interpolar.")
    return salida


def _proyectar(estaciones, crs_origen, crs_destino, logger):
    """
    Lleva las estaciones al CRS de cálculo, que es donde están las subcuencas.

    LA DISTANCIA DEL IDW TIENE QUE SER MÉTRICA. Calcularla sobre grados mezcla
    unidades y, peor, deforma: un grado de longitud no mide lo mismo que uno de
    latitud, de modo que las estaciones al este pesarían distinto que las del
    norte a la misma distancia real.
    """
    from pyproj import Transformer

    transformador = Transformer.from_crs(crs_origen, crs_destino,
                                         always_xy=True)
    for estacion in estaciones:
        x, y = transformador.transform(estacion["longitud"], estacion["latitud"])
        estacion["x"] = round(x, 2)
        estacion["y"] = round(y, 2)
    logger.info("Estaciones reproyectadas de %s a %s", crs_origen, crs_destino)
    return estaciones


def _interpolar(configuracion, base, delimitador, resultado, logger) -> bool:
    """Interpola el total anual a cada subcuenca y comprueba la cobertura."""
    try:
        _proyectar(resultado.estaciones,
                   str(configuracion.obtener("crs.geografico")),
                   str(configuracion.obtener("crs.calculo")), logger)
    except Exception as error:  # noqa: BLE001 - depende de pyproj
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "balance.reproyeccion",
            f"no se pudieron reproyectar las estaciones: {error}"))
        return False

    ruta = _ruta_subcuencas(base)
    if ruta is None:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "balance.sin_geometria",
            "no se encontro el shapefile de subcuencas: sin el no hay donde "
            "interpolar."))
        return False

    entidades = shapefile.leer_geometrias(ruta)
    temperatura = _leer_temperatura(base, delimitador)
    if len(entidades) != len(temperatura):
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "balance.descuadre",
            f"el shapefile trae {len(entidades)} entidad(es) y la tabla de "
            f"temperatura {len(temperatura)}. Con los conteos desiguales cada "
            "subcuenca recibiria la lluvia de otra."))
        return False

    exponente = float(configuracion.obtener("balance_hidrico.idw.exponente"))
    radio = float(configuracion.obtener("balance_hidrico.idw.radio_max_km")) * 1000.0
    minimo = int(configuracion.obtener("balance_hidrico.idw.estaciones_min"))
    fuentes = [(e["x"], e["y"], e["p_anual_mm"]) for e in resultado.estaciones]

    sin_dato = []
    for entidad, ficha in zip(entidades, temperatura):
        # La entidad ES el poligono, una lista de anillos, y 'centroide' ya
        # descuenta los huecos ponderando por area con signo. Pasarle un solo
        # anillo lo hace iterar vertices como si fueran anillos.
        centro = geometria.centroide(entidad)
        try:
            interpolado = idw(centro, fuentes, exponente, radio, minimo)
        except ErrorHidrologia as error:
            sin_dato.append(f"{ficha['subcuenca']}: {error}")
            continue
        resultado.por_subcuenca.append({
            "subcuenca": ficha["subcuenca"],
            "area_km2": ficha["area_km2"],
            "cota_media_m": ficha["cota_media_m"],
            "x": round(centro[0], 1), "y": round(centro[1], 1),
            "p_anual_mm": interpolado["valor"],
            "estaciones_usadas": interpolado["estaciones"],
            "distancia_min_m": interpolado["distancia_min_m"],
            "etp_mm_anio": ficha.get("etp_cenicafe_mm_anio"),
            "t_media_c": ficha.get("t_media_c"),
        })

    if not resultado.por_subcuenca:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "balance.sin_interpolacion",
            f"ninguna subcuenca recibio lluvia: {sin_dato[:3]}"))
        return False

    laminas = [s["p_anual_mm"] for s in resultado.por_subcuenca]
    area = sum(s["area_km2"] for s in resultado.por_subcuenca)
    media = sum(s["p_anual_mm"] * s["area_km2"]
                for s in resultado.por_subcuenca) / area
    logger.info("Precipitacion de %.0f a %.0f mm/ano, media ponderada %.0f",
                min(laminas), max(laminas), media)

    resultado.contraste["p_media_cuenca_mm"] = round(media, 1)
    resultado.contraste["area_km2"] = round(area, 3)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "balance.precipitacion",
        f"{len(resultado.por_subcuenca)} subcuenca(s) reciben su lamina anual "
        f"por IDW con exponente {exponente:g}, de {min(laminas):.0f} a "
        f"{max(laminas):.0f} mm/ano y media ponderada por area de {media:.0f}. "
        "Se interpola por PROXIMIDAD y no por elevacion porque asi lo dice el "
        "dato de este estudio: el total anual contra la altura da un R2 de "
        "0,026, frente al 0,773 del campo termico. Se evalua en el centroide de "
        "cada subcuenca, sin construir ningun raster.",
    ))

    franjas = _leer_franjas(base, delimitador)
    cobertura = cobertura_de_estaciones(
        [e["altitud"] for e in resultado.estaciones], franjas)
    if cobertura:
        resultado.contraste["cobertura"] = cobertura
        if cobertura.get("pct_extrapolado", 0) > 0:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "balance.cobertura",
                f"el {cobertura['pct_extrapolado']:.1f} por ciento del area "
                f"queda fuera del rango de las estaciones "
                f"({cobertura['cota_min_estaciones_m']:.0f} a "
                f"{cobertura['cota_max_estaciones_m']:.0f} m), y "
                f"{cobertura['area_sobre_estaciones_km2']:.1f} km2 por encima "
                "de la mas alta. IDW NO EXTRAPOLA: la lamina de esa zona queda "
                "acotada por lo que se midio mas abajo. En montana tropical la "
                "lluvia crece con la altura hasta la franja de condensacion "
                "maxima, de modo que la oferta de la parte alta sale "
                "probablemente subestimada, y es la que mas aporta.",
            ))
    if sin_dato:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "balance.subcuencas_sin_lluvia",
            f"{len(sin_dato)} subcuenca(s) sin lamina: {sin_dato[:4]}."))
    return True


def _ruta_subcuencas(base):
    candidatas = sorted((base / "data/03_SIG/vector").glob("*ubCuenca*.shp"))
    return candidatas[0] if candidatas else None


def _leer_temperatura(base, delimitador):
    ruta = base / "data/02_procesado/temperatura/temperatura_por_subcuenca.csv"
    if not ruta.is_file():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    for fila in filas:
        for clave in ("area_km2", "cota_media_m", "t_media_c",
                      "etp_cenicafe_mm_anio"):
            try:
                fila[clave] = float(fila[clave])
            except (KeyError, TypeError, ValueError):
                fila[clave] = None
    return filas


def _leer_franjas(base, delimitador):
    ruta = base / "data/02_procesado/morfometria/distribucion_altimetrica.csv"
    if not ruta.is_file():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        return list(csv.DictReader(manejador, delimiter=delimitador))


def _balance_multianual(base, delimitador, resultado, logger) -> None:
    """Cierra el balance de largo plazo en cada subcuenca, con las tres vías."""
    sin_etp = []
    for fila in resultado.por_subcuenca:
        if not fila.get("etp_mm_anio"):
            sin_etp.append(fila["subcuenca"])
            continue
        cerrado = balance(fila["p_anual_mm"], fila["etp_mm_anio"],
                          fila["area_km2"], DIAS_DEL_ANIO,
                          temperatura_c=fila.get("t_media_c"))
        cerrado["subcuenca"] = fila["subcuenca"]
        cerrado["cota_media_m"] = fila["cota_media_m"]
        resultado.multianual.append(cerrado)

    if sin_etp:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "balance.sin_etp",
            f"{len(sin_etp)} subcuenca(s) sin ETP: {sin_etp[:4]}. La calcula el "
            "M18a; ejecutarlo antes."))
    if not resultado.multianual:
        return

    for metodo in ("budyko", "dekop", "turc"):
        clave = f"caudal_{metodo}_m3s"
        if clave not in resultado.multianual[0]:
            continue
        resultado.contraste[f"caudal_{metodo}_m3s"] = round(
            sum(f[clave] for f in resultado.multianual), 4)
    caudal = resultado.contraste.get("caudal_budyko_m3s", 0.0)
    area = resultado.contraste.get("area_km2", 0.0)
    logger.info("Caudal multianual de la cuenca: %.3f m3/s (Budyko)", caudal)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "balance.multianual",
        f"caudal medio de largo plazo en el cierre: "
        + ", ".join(f"{m} {resultado.contraste[f'caudal_{m}_m3s']:.3f}"
                    for m in ("budyko", "dekop", "turc")
                    if f"caudal_{m}_m3s" in resultado.contraste)
        + f" m3/s, sumando las {len(resultado.multianual)} subcuencas. "
        f"Rendimiento de {1000.0 * caudal / area:.1f} l/s por km2 con Budyko. "
        "Las tres se presentan a proposito: la eleccion de formulacion mueve el "
        "caudal de forma apreciable y adoptar una en silencio no es defendible.",
    ))


def _balance_mensual(base, delimitador, resultado, logger) -> None:
    """
    Cierra el balance mes a mes sobre la cuenca completa.

    LA LLUVIA MENSUAL SE LLEVA A LA CUENCA POR EL MISMO IDW, evaluado en su
    centroide de area. La ETP mensual la aporta el M18a, ya ajustada al nivel
    multianual.
    """
    ruta = base / "data/02_procesado/temperatura/temperatura_mensual_cuenca.csv"
    if not ruta.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "balance.sin_etp_mensual",
            "no esta la serie mensual del M18a: no se cierra el balance mensual."))
        return
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        etp_mensual = list(csv.DictReader(manejador, delimiter=delimitador))

    mensual = _leer_precipitacion_mensual(base, delimitador, resultado)
    if not mensual:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "balance.sin_lluvia_mensual",
            "no se pudo construir la serie mensual de la cuenca: el balance "
            "mensual queda sin calcular."))
        return
    resultado.mensual_cuenca = mensual

    area = resultado.contraste.get("area_km2", 0.0)
    por_mes = {int(f["mes"]): f for f in etp_mensual}
    for fila in mensual:
        ficha = por_mes.get(fila["mes"])
        if ficha is None:
            continue
        try:
            etp = float(ficha.get("etp_ajustada_mm")
                        or ficha.get("etp_thornthwaite_mm"))
        except (TypeError, ValueError):
            continue
        dias = calendar.monthrange(2001, fila["mes"])[1]
        cerrado = balance(fila["p_mm"], etp, area, dias)
        cerrado["mes"] = fila["mes"]
        cerrado["t_media_c"] = ficha.get("t_media_cuenca_c")
        resultado.mensual.append(cerrado)

    if not resultado.mensual:
        return
    caudales = [f["caudal_budyko_m3s"] for f in resultado.mensual]
    resultado.contraste.update(contrastar_escalas(
        resultado.contraste.get("caudal_budyko_m3s", 0.0), caudales))
    logger.info("Caudal mensual de %.3f a %.3f m3/s", min(caudales),
                max(caudales))

    diferencia = resultado.contraste.get("diferencia_pct")
    if diferencia is not None:
        severidad = ADVERTENCIA if abs(diferencia) > 15 else INFORMATIVO
        resultado.hallazgos.append(Hallazgo(
            severidad, "balance.contraste_de_escalas",
            f"el promedio de la serie mensual es "
            f"{resultado.contraste['promedio_mensual_m3s']:.3f} m3/s y el "
            f"caudal de largo plazo {resultado.contraste['caudal_multianual_m3s']:.3f}: "
            f"difieren un {diferencia:+.1f} por ciento. No tienen por que "
            "coincidir, porque la multianual se calcula por subcuenca y se "
            "agrega mientras la mensual va sobre la cuenca entera, y Budyko no "
            "es lineal; pero una diferencia grande apunta a un fallo en alguna "
            f"de las dos. La serie va de {min(caudales):.3f} a "
            f"{max(caudales):.3f} m3/s, razon "
            f"{resultado.contraste.get('razon_max_min')}. SIN ALMACENAMIENTO "
            "entre meses, por decision declarada: esa razon es menor que la "
            "real y el informe debe recogerlo.",
        ))


def _leer_precipitacion_mensual(base, delimitador, resultado):
    """
    Serie mensual multianual de la cuenca, por IDW sobre el centroide de area.

    Se parte de la serie complementada del M05, que trae una columna por
    estacion. El ciclo se promedia sobre todos los anios con dato.
    """
    ruta = (rutas.directorio("procesado_series", base)
            / "precipitacion_mensual_complementada.csv")
    if not ruta.is_file():
        return []
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        return []

    por_estacion: dict[str, dict[int, list[float]]] = {}
    for fila in filas:
        try:
            mes = int(fila["mes"])
        except (KeyError, TypeError, ValueError):
            continue
        for codigo, valor in fila.items():
            if codigo in ("anio", "mes") or not valor:
                continue
            try:
                por_estacion.setdefault(codigo, {}).setdefault(
                    mes, []).append(float(valor))
            except (TypeError, ValueError):
                continue

    ubicadas = {e["codigo"]: e for e in resultado.estaciones}
    area = sum(s["area_km2"] for s in resultado.por_subcuenca)
    centro = (sum(s["x"] * s["area_km2"] for s in resultado.por_subcuenca) / area,
              sum(s["y"] * s["area_km2"] for s in resultado.por_subcuenca) / area)

    salida = []
    for mes in range(1, 13):
        fuentes = []
        for codigo, ficha in ubicadas.items():
            valores = por_estacion.get(codigo, {}).get(mes, [])
            if valores:
                fuentes.append((ficha["x"], ficha["y"],
                                sum(valores) / len(valores)))
        if len(fuentes) < 3:
            continue
        try:
            interpolado = idw(centro, fuentes)
        except ErrorHidrologia:
            continue
        salida.append({"mes": mes, "p_mm": interpolado["valor"],
                       "estaciones": interpolado["estaciones"]})
    return salida


def _escribir(configuracion, base, delimitador, resultado, logger) -> None:
    """Tablas, libro de Excel y figuras del balance."""
    destino_p = rutas.directorio("procesado", base, crear=True) / "precipitacion"
    destino_b = rutas.directorio("procesado", base, crear=True) / "balance"
    tablas = (
        ("precipitacion_anual_por_subcuenca", resultado.por_subcuenca, destino_p),
        ("precipitacion_mensual_cuenca", resultado.mensual_cuenca, destino_p),
        ("estaciones_del_balance", resultado.estaciones, destino_p),
        ("balance_multianual", resultado.multianual, destino_b),
        ("balance_mensual", resultado.mensual, destino_b),
    )
    for nombre, filas, carpeta in tablas:
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta = carpeta / f"{nombre}.csv"
        _escribir_csv(ruta, filas, delimitador)
        resultado.productos.append(rutas.relativa(ruta, base))

    try:
        import excel
        detalle = excel.escribir_libro(
            rutas.directorio("resultados_excel", base, crear=True)
            / "M18_balance.xlsx",
            [(n, f) for n, f, _ in tablas if f])
        resultado.productos.append(
            rutas.relativa(Path(detalle["archivo"]), base))
        logger.info("Libro con %d hoja(s)", len(detalle["hojas"]))
    except Exception as error:  # noqa: BLE001 - depende del entorno
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "balance.excel", f"no se escribio el libro: {error}"))

    _figuras(configuracion, base, resultado, logger)


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


def _figuras(configuracion, base, resultado, logger) -> None:
    """Las cuatro figuras del capítulo de balance."""
    try:
        import graficos
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente", f"sin figuras: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    escritas = 0

    # 1. El argumento del metodo: la lluvia NO depende de la elevacion.
    if resultado.estaciones:
        with graficos.figura(
                estilo, titulo="Precipitación anual contra elevación",
                etiqueta_x="Elevación (m s. n. m.)",
                etiqueta_y="Precipitación (mm/año)") as (fig, ax):
            ax.scatter([e["altitud"] for e in resultado.estaciones],
                       [e["p_anual_mm"] for e in resultado.estaciones],
                       s=40, color=estilo.color(0), zorder=3)
            fig.text(0.01, -0.04,
                     "La elevación explica el 2,6 % de la variación del total "
                     "anual, frente al 77 % del campo térmico. Por eso la "
                     "lluvia se interpola por proximidad y la temperatura por "
                     "regresión con la altura.",
                     fontsize=estilo.tamano_fuente - 2, color="#555555")
            for ruta in graficos.guardar(
                    fig, directorio / "M18_lluvia_contra_elevacion", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
            escritas += 1

    # 2. Serie mensual de la cuenca: lluvia, ETP y escorrentia.
    if resultado.mensual:
        meses = [f["mes"] for f in resultado.mensual]
        with graficos.figura(
                estilo, titulo="Balance hídrico mensual de la cuenca",
                etiqueta_x="Mes", etiqueta_y="Lámina (mm/mes)") as (fig, ax):
            ax.bar(meses, [f["precipitacion_mm"] for f in resultado.mensual],
                   color=estilo.color(0), label="precipitación", width=0.7)
            ax.plot(meses, [f["etp_mm"] for f in resultado.mensual],
                    color="#b03a2e", linewidth=1.6, marker="o", markersize=4,
                    label="ETP")
            ax.plot(meses, [f["etr_budyko_mm"] for f in resultado.mensual],
                    color="#7d3c98", linewidth=1.6, marker="s", markersize=4,
                    label="ETR, Budyko")
            ax.set_xticks(meses)
            ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
            for ruta in graficos.guardar(
                    fig, directorio / "M18_balance_mensual", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
            escritas += 1

    # 3. Caudal mensual por las tres vias, y el de largo plazo.
    if resultado.mensual:
        meses = [f["mes"] for f in resultado.mensual]
        series = {}
        for metodo, etiqueta in (("budyko", "Budyko"), ("dekop", "Dekop")):
            clave = f"caudal_{metodo}_m3s"
            if clave in resultado.mensual[0]:
                series[etiqueta] = (meses, [f[clave] for f in resultado.mensual])
        if series:
            with graficos.figura(
                    estilo, titulo="Caudal medio mensual de la cuenca",
                    etiqueta_x="Mes", etiqueta_y="Caudal (m³/s)") as (fig, ax):
                graficos.lineas(ax, series, estilo)
                largo = resultado.contraste.get("caudal_budyko_m3s")
                if largo:
                    ax.axhline(largo, color="#555555", linestyle=":",
                               linewidth=1.3,
                               label="largo plazo, por subcuenca")
                    ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
                ax.set_xticks(meses)
                ax.set_ylim(bottom=0)
                fig.text(0.01, -0.04,
                         "Sin almacenamiento entre meses, por decisión "
                         "declarada: la variabilidad real es mayor que la que "
                         "muestra esta serie.",
                         fontsize=estilo.tamano_fuente - 2, color="#555555")
                for ruta in graficos.guardar(
                        fig, directorio / "M18_caudal_mensual", estilo):
                    resultado.productos.append(rutas.relativa(ruta, base))
                escritas += 1

    # 4. Isoyetas: la lluvia interpolada sobre las subcuencas.
    if resultado.por_subcuenca:
        ruta_shp = _ruta_subcuencas(base)
        if ruta_shp is not None:
            entidades = shapefile.leer_geometrias(ruta_shp)
            if len(entidades) == len(resultado.por_subcuenca):
                with graficos.figura(
                        estilo, titulo="Precipitación media anual por subcuenca",
                        etiqueta_x="Este (m)",
                        etiqueta_y="Norte (m)") as (fig, ax):
                    mapeador = graficos.coropleta(
                        ax, entidades,
                        [s["p_anual_mm"] for s in resultado.por_subcuenca],
                        estilo)
                    graficos.barra_de_color(fig, ax, mapeador, estilo,
                                            "Precipitación (mm/año)")
                    for ruta in graficos.guardar(
                            fig, directorio / "M18_mapa_precipitacion", estilo):
                        resultado.productos.append(rutas.relativa(ruta, base))
                    escritas += 1
    logger.info("%d figura(s) escritas", escritas)


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
        "subcuencas": len(resultado.por_subcuenca),
        "contraste": resultado.contraste,
        "meses": len(resultado.mensual),
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

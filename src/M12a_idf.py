#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M12a - Curvas IDF y factores de cambio climático
================================================
Entorno: venv del proyecto.

DOS METODOLOGÍAS EN PARALELO (CLAUDE.md, sección 6): INVIAS, es decir la
regionalización de Vargas y Díaz-Granados, y Silva. Se calculan las dos y se
comparan. Ninguna se adopta sola: una IDF regionalizada es una estimación a
partir de un mapa de coeficientes, no una medida de esta cuenca, y dos
estimaciones que discrepan dicen más que una que no tiene con qué compararse.

LA VERIFICACIÓN QUE IMPORTA NO ES ENTRE MÉTODOS, ES CONTRA EL PROPIO DATO. A
1.440 minutos la curva describe un aguacero de 24 horas, y de ese aguacero este
estudio SÍ tiene medida propia: la Pmáx24h que el M07 ajustó sobre las series
del IDEAM. Si la IDF y el análisis de frecuencia no coinciden ahí, una de las
dos no describe esta cuenca, y esa comparación es la única del módulo que se
apoya en datos locales. Es además la que atrapa un coeficiente mal transcrito,
que ninguna comparación entre metodologías detectaría.

LA DESAGREGACIÓN DE 24 h A LA DURACIÓN DE DISEÑO se calcula por las TRES
hipótesis de la sección 6 y no se adopta ninguna: 'h1_directa' toma P24h entera,
'h2_idf' integra la curva sobre la duración y 'h3_factor' aplica un coeficiente
documentado. Se entregan las tres con su cociente para que el consultor compare
y decida, que es lo que declara 'tormenta.hipotesis_adoptada'.

CAMBIO CLIMÁTICO, REGLA CONDICIONAL. El factor se aplica SOLO si es de
incremento. Si la proyección es a la baja no se afecta el hietograma y se
documenta: una reducción proyectada no es un margen que se pueda gastar, porque
la incertidumbre del modelo climático es mayor que la reducción que anuncia.

Productos:
    data/02_procesado/tormenta/idf.csv
    data/02_procesado/tormenta/desagregacion.csv
    data/02_procesado/tormenta/cambio_climatico.csv
    data/05_resultados/graficos/M12a_curvas_idf.png y .svg
    data/02_procesado/M12a_idf.json

Uso:
    python src/M12a_idf.py

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

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M12a"
DESCRIPCION = "Curvas IDF y factores de cambio climático"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

MINUTOS_EN_24H = 1440.0


@dataclass
class ResultadoM12a:
    region: str = ""
    curvas: list[dict[str, Any]] = field(default_factory=list)
    verificacion: list[dict[str, Any]] = field(default_factory=list)
    desagregacion: list[dict[str, Any]] = field(default_factory=list)
    cambio_climatico: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def leer_coeficientes(ruta: Path, delimitador: str) -> dict[str, dict[str, Any]]:
    """
    Coeficientes regionales de la IDF, por región del país.

    Es doctrina y vive en data/referencia. La columna 'validado' viaja al
    reporte: unos coeficientes transcritos y no contrastados contra el manual
    no valen lo mismo que unos verificados, y el informe debe poder decirlo.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si a una región le falta algún coeficiente.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la tabla de coeficientes en {ruta}.")
    tabla: dict[str, dict[str, Any]] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            region = str(fila.get("region", "")).strip().lower()
            if not region:
                continue
            try:
                coeficientes = {clave: float(fila[clave])
                                for clave in ("a", "b", "c", "d")}
            except (KeyError, TypeError, ValueError) as exc:
                raise ErrorFormato(
                    f"la region {region!r} de {ruta.name} no trae los cuatro "
                    f"coeficientes legibles: {exc}.") from exc
            # LA UNIDAD DE LA DURACION SE DECLARA, no se supone. La misma
            # ecuacion aparece publicada con t en horas y con t en minutos, y
            # confundirlas cambia la intensidad en un factor de 60^c, que para
            # c = 0,66 son catorce veces. No da error en ninguna parte.
            coeficientes["unidad_duracion"] = str(
                fila.get("unidad_duracion", "horas")).strip().lower()
            if coeficientes["unidad_duracion"] not in ("horas", "minutos"):
                raise ErrorFormato(
                    f"la region {region!r} de {ruta.name} declara la unidad de "
                    f"duracion {coeficientes['unidad_duracion']!r}; se admite "
                    "'horas' o 'minutos'.")
            coeficientes["origen"] = str(fila.get("origen", "")).strip()
            coeficientes["descripcion"] = str(fila.get("descripcion", "")).strip()
            coeficientes["validado"] = str(
                fila.get("validado", "")).strip().lower() in ("si", "sí", "true")
            tabla[region] = coeficientes
    if not tabla:
        raise ErrorFormato(f"{ruta.name} no contiene ninguna region.")
    return tabla


def intensidad_invias(
    duracion_min: float, periodo_retorno: float, media_pmax24_mm: float,
    coeficientes: dict[str, Any],
) -> float:
    """
    Intensidad por la regionalización de Vargas y Díaz-Granados.

        i = a * T^b * M^d / t^c

    con i en mm/h, T en años, M la media de la serie de máximos diarios anuales
    en mm, y t en la unidad que DECLARE la tabla. M es lo que ancla la curva
    regional a esta cuenca: los cuatro coeficientes describen la forma y M el
    nivel.

    LA UNIDAD DE t ES HORAS, y se declara en la tabla porque el propio manual
    induce a error: su lista de variables dice "Duración de la lluvia (min)",
    pero la tabla de resultados solo se reproduce con horas. Verificado contra
    la Tabla 58 del informe de referencia, numeral 5.5.1, treinta valores entre
    10 y 90 minutos y entre 2,33 y 100 años: con horas la desviación máxima es
    del 0,008 %, con minutos llega al 92 %. Confundirlas multiplica la
    intensidad por 60^c, catorce veces con el c = 0,66 de la región Andina, y no
    produce ningún error: solo una curva desplazada.

    Excepciones
    -----------
    ErrorHidrologia
        Si alguna magnitud no es positiva. Una duración nula daría división por
        cero y una media nula anularía la curva entera.
    """
    if duracion_min <= 0 or periodo_retorno <= 0 or media_pmax24_mm <= 0:
        raise ErrorHidrologia(
            f"duración ({duracion_min} min), periodo de retorno "
            f"({periodo_retorno} años) y media de Pmáx24h ({media_pmax24_mm} "
            "mm) deben ser positivos.")
    duracion = duracion_min
    if str(coeficientes.get("unidad_duracion", "horas")) == "horas":
        duracion = duracion_min / 60.0
    return (coeficientes["a"] * periodo_retorno ** coeficientes["b"]
            * media_pmax24_mm ** coeficientes["d"]
            / duracion ** coeficientes["c"])


def intensidad_silva(
    duracion_min: float, pmax24_mm: float, coeficiente_1h: float,
    b_min: float, n: float,
) -> float:
    """
    Intensidad por el método de Silva (1998), en la forma que publica su fuente.

        I = K / (d + b)^n

    con d en MINUTOS, b un tiempo característico de la zona y n el exponente de
    decaimiento. K se obtiene anclando la curva en la intensidad de UNA HORA:

        P1h = coeficiente_1h * P24h
        K   = P1h * (60 + b)^n

    NO ES UNA LEY POTENCIAL. La primera versión de este módulo la implementó
    como P(t) = P24h * (t/1440)^0,25, que es una regla de desagregación
    corriente pero no es Silva. La diferencia no es de matiz: con la forma de
    Talbot la curva decae con exponente n = 0,6 y con la potencial lo hacía con
    0,75, de modo que quedaba SIEMPRE por debajo de INVIAS y la separación
    crecía con la duración. En el informe de referencia las dos curvas SE
    CRUZAN, con INVIAS arriba en los primeros minutos y Silva arriba a partir
    de la media hora, y ese cruce es la firma de que los exponentes son 0,66 y
    0,6 y no 0,66 y 0,75.

    Los tres parámetros se declaran en la configuración. 'b' está entre 5 y 20
    minutos y 'n' entre 0,5 y 0,6, siendo 0,6 el asociado a lluvias más
    intensas. El coeficiente de paso de 24 h a 1 h es específico del estudio y
    exige fuente escrita: gobierna el nivel entero de la curva.

    Excepciones
    -----------
    ErrorHidrologia
        Si alguna magnitud no es positiva.
    """
    if duracion_min <= 0 or pmax24_mm <= 0 or coeficiente_1h <= 0:
        raise ErrorHidrologia(
            f"duración ({duracion_min} min), Pmáx24h ({pmax24_mm} mm) y "
            f"coeficiente de paso a 1 h ({coeficiente_1h}) deben ser positivos.")
    if b_min < 0 or n <= 0:
        raise ErrorHidrologia(
            f"b ({b_min} min) no puede ser negativo y n ({n}) debe ser positivo.")
    intensidad_1h = coeficiente_1h * pmax24_mm
    k = intensidad_1h * (60.0 + b_min) ** n
    return k / (duracion_min + b_min) ** n


def lamina_de_intensidad(intensidad_mm_h: float, duracion_min: float) -> float:
    """Lámina acumulada en la duración, a partir de la intensidad media."""
    return intensidad_mm_h * duracion_min / 60.0


def desagregar(
    pmax24_mm: float, duracion_min: float, intensidad_idf_mm_h: float | None,
    coeficiente: float | None,
) -> dict[str, Any]:
    """
    Las tres hipótesis de paso de P24h a la duración de diseño, en paralelo.

    Ninguna se adopta. Se entregan las tres con su cociente sobre P24h para que
    el consultor vea de un vistazo cuánto separa a una de otra: sobre esa
    diferencia se decide, y la decisión debe quedar escrita.

    'h1_directa' asigna la lámina de 24 horas a la duración de diseño. Es la más
    conservadora con diferencia y rara vez defendible, pero se calcula porque
    marca la cota superior de las otras dos.
    """
    hipotesis: dict[str, Any] = {
        "duracion_min": duracion_min,
        "pmax24_mm": round(pmax24_mm, 2),
        "h1_directa_mm": round(pmax24_mm, 2),
    }
    if intensidad_idf_mm_h is not None:
        hipotesis["h2_idf_mm"] = round(
            lamina_de_intensidad(intensidad_idf_mm_h, duracion_min), 2)
    if coeficiente is not None:
        hipotesis["h3_factor_mm"] = round(pmax24_mm * coeficiente, 2)

    for clave in ("h1_directa", "h2_idf", "h3_factor"):
        valor = hipotesis.get(f"{clave}_mm")
        if valor is not None and pmax24_mm > 0:
            hipotesis[f"{clave}_sobre_p24"] = round(valor / pmax24_mm, 4)
    return hipotesis


def factor_de_cambio_climatico(
    proyectado_pct: float, solo_si_incremento: bool,
) -> dict[str, Any]:
    """
    Convierte un cambio proyectado en porcentaje en un factor aplicable.

    REGLA CONDICIONAL (CLAUDE.md, sección 6): si la proyección es a la baja NO
    se afecta el hietograma. Una reducción proyectada no es margen que se pueda
    gastar: la incertidumbre entre modelos climáticos es mayor que la reducción
    que anuncian, de modo que descontarla apostaría el diseño a la parte menos
    firme de la proyección. El factor se registra igual, con su motivo.
    """
    factor = 1.0 + proyectado_pct / 100.0
    if solo_si_incremento and proyectado_pct <= 0:
        return {
            "cambio_pct": proyectado_pct,
            "factor_proyectado": round(factor, 4),
            "factor_aplicado": 1.0,
            "aplicado": False,
            "motivo": "proyeccion a la baja: no se afecta el hietograma",
        }
    return {
        "cambio_pct": proyectado_pct,
        "factor_proyectado": round(factor, 4),
        "factor_aplicado": round(factor, 4),
        "aplicado": True,
        "motivo": "",
    }


def leer_cuantiles(ruta: Path, delimitador: str) -> dict[float, float]:
    """
    Pmáx24h por periodo de retorno, de la tabla que dejó el M07.

    Se toma la media de las estaciones para cada periodo: la IDF regional
    describe una zona, no un punto, y anclarla a una sola estación trasladaría
    a toda la cuenca la particularidad de esa estación.

    Excepciones
    -----------
    ErrorRutas
        Si no está el archivo.
    ErrorFormato
        Si no se pudo leer ningún cuantil.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}: ejecutar antes el M07, que es quien "
            "ajusta las distribuciones de Pmáx24h.")

    # El M07 escribe en formato ANCHO: una fila por estacion y una columna por
    # periodo, 'T2.33', 'T5', 'T10'. Leerlo asi y no en formato largo evita
    # tener que reescribir su salida solo para este modulo.
    por_periodo: dict[float, list[float]] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            for columna, bruto in fila.items():
                nombre = str(columna or "").strip()
                if not nombre.upper().startswith("T"):
                    continue
                try:
                    periodo = float(nombre[1:])
                    valor = float(bruto)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(valor) and valor > 0:
                    por_periodo.setdefault(periodo, []).append(valor)

    if not por_periodo:
        raise ErrorFormato(
            f"{ruta.name} no trae ningun cuantil legible: se esperan columnas "
            "por periodo de retorno con el formato 'T2.33', 'T5', 'T10'.")
    return {periodo: statistics.fmean(valores)
            for periodo, valores in sorted(por_periodo.items())}


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Calcula las curvas IDF, las verifica y desagrega la lluvia de diseño."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM12a()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M12a_idf.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "cuantiles de Pmax24h":
                "data/02_procesado/frecuencia/cuantiles.csv",
            "coeficientes IDF": configuracion.obtener("idf.coeficientes_invias"),
        },
        parametros=configuracion.parametros("idf"))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    duraciones = [float(d) for d in configuracion.obtener("idf.duraciones_min")]
    duracion_diseno_min = float(
        configuracion.obtener("tormenta.duracion_h")) * 60.0

    try:
        cuantiles = leer_cuantiles(
            rutas.directorio("procesado_frecuencia", base) / "cuantiles.csv",
            delimitador)
        coeficientes = leer_coeficientes(
            rutas.resolver(configuracion.obtener("idf.coeficientes_invias"),
                           base), delimitador)
    except (ErrorFormato, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "idf.insumos", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    region = str(configuracion.obtener("idf.region", "andina")).strip().lower()
    if region not in coeficientes:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "idf.region",
            f"la region declarada {region!r} no esta en la tabla de "
            f"coeficientes, que trae {sorted(coeficientes)}.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)
    resultado.region = region
    del coeficientes  # se vuelve a leer dentro, ya validada la region

    _resolver_curvas(configuracion, base, resultado, cuantiles, duraciones,
                     delimitador, logger)
    _resolver_verificacion(resultado, cuantiles, logger)
    _resolver_desagregacion(configuracion, resultado, cuantiles,
                            duracion_diseno_min, logger)
    _resolver_cambio_climatico(configuracion, base, resultado, delimitador,
                               logger)
    _escribir_figura(configuracion, base, resultado, logger)
    _escribir_productos(base, resultado, delimitador, logger)
    return _cerrar(logger, resultado, base, ruta_json, inicio, SALIDA_CORRECTA)


def _resolver_curvas(configuracion, base, resultado, cuantiles, duraciones,
                     delimitador, logger) -> None:
    """Calcula las dos metodologías para cada duración y periodo."""
    with registro.bloque(logger, "Curvas IDF"):
        tabla = leer_coeficientes(
            rutas.resolver(configuracion.obtener("idf.coeficientes_invias"),
                           base), delimitador)
        region = tabla[resultado.region]
        metodologias = [str(m).strip().lower()
                        for m in configuracion.obtener("idf.metodologias")]
        b_silva = float(configuracion.obtener("idf.silva.b_min"))
        n_silva = float(configuracion.obtener("idf.silva.n"))
        coeficiente_1h = float(configuracion.obtener(
            "idf.silva.coeficiente_24h_a_1h"))
        fuente_1h = str(configuracion.obtener(
            "idf.silva.fuente_coeficiente", "") or "").strip()
        # La media de la serie ancla la curva regional a esta cuenca. Se toma
        # el cuantil de 2,33 anios, que es la media de una Gumbel.
        media = cuantiles.get(2.33) or statistics.fmean(cuantiles.values())

        for periodo, pmax in cuantiles.items():
            for duracion in duraciones:
                fila = {"periodo_retorno": periodo, "duracion_min": duracion}
                if "invias" in metodologias:
                    fila["i_invias_mm_h"] = round(intensidad_invias(
                        duracion, periodo, media, region), 3)
                if "silva" in metodologias:
                    fila["i_silva_mm_h"] = round(intensidad_silva(
                        duracion, pmax, coeficiente_1h, b_silva, n_silva), 3)
                uno = fila.get("i_invias_mm_h")
                otro = fila.get("i_silva_mm_h")
                if uno and otro:
                    fila["diferencia_pct"] = round(
                        100.0 * (uno - otro) / otro, 1)
                resultado.curvas.append(fila)

        logger.info("%d punto(s) de curva | region %s | media de anclaje "
                    "%.1f mm", len(resultado.curvas), resultado.region, media)

        if "silva" in metodologias and not fuente_1h:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "idf.silva_sin_fuente",
                f"el coeficiente de paso de 24 h a 1 h ({coeficiente_1h}) no "
                "tiene fuente declarada. Gobierna el NIVEL entero de la curva "
                "de Silva: multiplicarlo por dos duplica toda la intensidad. Es "
                "especifico del estudio y no puede heredarse de otro sin "
                "escribir de donde sale.",
            ))

        if not region["validado"]:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "idf.coeficientes_sin_validar",
                f"los coeficientes de la region {resultado.region!r} estan "
                "declarados como NO validados contra la fuente. Gobiernan toda "
                "la intensidad de diseno, de modo que un digito mal transcrito "
                "se propaga a cada caudal del estudio sin dejar rastro. "
                f"Contrastarlos con: {region['origen']}.",
            ))

        discrepantes = [f for f in resultado.curvas
                        if abs(f.get("diferencia_pct") or 0.0) > 50.0]
        if discrepantes:
            peor = max(discrepantes, key=lambda f: abs(f["diferencia_pct"]))
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "idf.discrepancia_entre_metodos",
                f"{len(discrepantes)} de {len(resultado.curvas)} punto(s) con "
                "mas del 50 % de diferencia entre INVIAS y Silva. El peor: "
                f"{peor['duracion_min']:.0f} min y T {peor['periodo_retorno']} "
                f"anios, {peor['i_invias_mm_h']:.1f} frente a "
                f"{peor['i_silva_mm_h']:.1f} mm/h "
                f"({peor['diferencia_pct']:+.0f} %). Son dos estimaciones de lo "
                "mismo: donde se separan tanto, al menos una no describe esta "
                "cuenca, y adoptar cualquiera de las dos sin mirar seria "
                "arbitrario.",
            ))


def _resolver_verificacion(resultado, cuantiles, logger) -> None:
    """Contrasta la IDF a 24 h con la Pmáx24h del análisis de frecuencia."""
    with registro.bloque(logger, "Verificacion contra el analisis de frecuencia"):
        for periodo, pmax in cuantiles.items():
            fila = next((f for f in resultado.curvas
                         if f["periodo_retorno"] == periodo
                         and f["duracion_min"] == MINUTOS_EN_24H), None)
            if fila is None or not fila.get("i_invias_mm_h"):
                continue
            lamina = lamina_de_intensidad(fila["i_invias_mm_h"], MINUTOS_EN_24H)
            resultado.verificacion.append({
                "periodo_retorno": periodo,
                "pmax24_frecuencia_mm": round(pmax, 2),
                "pmax24_idf_mm": round(lamina, 2),
                "diferencia_pct": round(100.0 * (lamina - pmax) / pmax, 1),
            })

        if not resultado.verificacion:
            return
        peor = max(resultado.verificacion,
                   key=lambda v: abs(v["diferencia_pct"]))
        mediana = statistics.median(
            abs(v["diferencia_pct"]) for v in resultado.verificacion)
        logger.info("Verificacion a 24 h: diferencia mediana %.1f %%, peor "
                    "%.1f %% en T %s", mediana, peor["diferencia_pct"],
                    peor["periodo_retorno"])

        # El umbral es ancho A PROPOSITO. La regionalizacion se ajusto sobre
        # duraciones de minutos a pocas horas, y en 24 h esta extrapolando: en
        # el propio informe de referencia, con sus coeficientes de Orinoquia y
        # su M de 126,69 mm, la curva da 160 mm en 24 h frente a una media de
        # maximos de 127, un 27 % por encima. La diferencia aqui NO delata un
        # coeficiente mal transcrito, sino el limite del metodo, y por eso lo
        # que se compara es el orden de magnitud.
        severidad = ADVERTENCIA if mediana > 60.0 else INFORMATIVO
        resultado.hallazgos.append(Hallazgo(
            severidad, "idf.verificacion_24h",
            f"a 1.440 minutos la IDF de INVIAS da una lamina que difiere un "
            f"{mediana:.1f} % (mediana) de la Pmax24h que el M07 ajusto sobre "
            f"las series del IDEAM; la peor, {peor['diferencia_pct']:+.1f} % en "
            f"T {peor['periodo_retorno']} anios "
            f"({peor['pmax24_idf_mm']:.1f} frente a "
            f"{peor['pmax24_frecuencia_mm']:.1f} mm). Es la unica comprobacion "
            "del modulo apoyada en datos de esta cuenca: la curva regional sale "
            "de un mapa de coeficientes y aqui se enfrenta a lo que midieron "
            "las estaciones. La regionalizacion se ajusto sobre duraciones de "
            "minutos a pocas horas, de modo que en 24 h esta EXTRAPOLANDO y se "
            "espera que sobrestime: en el propio informe de referencia la curva "
            "da un 27 % por encima de su media de maximos. Lo que se contrasta "
            "aqui es el orden de magnitud, no la coincidencia."
            + (" Una diferencia de esta magnitud excede lo atribuible a la "
               "extrapolacion: revisar la region declarada y la media de "
               "anclaje antes de usar la curva."
               if severidad == ADVERTENCIA else ""),
        ))


def _resolver_desagregacion(configuracion, resultado, cuantiles,
                            duracion_min, logger) -> None:
    """Calcula las tres hipótesis de paso de P24h a la duración de diseño."""
    with registro.bloque(logger, "Desagregacion a la duracion de diseno"):
        coeficiente = configuracion.obtener(
            "tormenta.coeficiente_desagregacion.valor", None)
        fuente = str(configuracion.obtener(
            "tormenta.coeficiente_desagregacion.fuente", "") or "").strip()
        coeficiente = float(coeficiente) if coeficiente is not None else None

        for periodo, pmax in cuantiles.items():
            fila = next((f for f in resultado.curvas
                         if f["periodo_retorno"] == periodo
                         and f["duracion_min"] == duracion_min), None)
            intensidad = fila.get("i_invias_mm_h") if fila else None
            hipotesis = desagregar(pmax, duracion_min, intensidad, coeficiente)
            hipotesis["periodo_retorno"] = periodo
            resultado.desagregacion.append(hipotesis)

        adoptada = configuracion.obtener("tormenta.hipotesis_adoptada", None)
        if resultado.desagregacion:
            muestra = resultado.desagregacion[0]
            cocientes = {c: muestra.get(f"{c}_sobre_p24")
                         for c in ("h1_directa", "h2_idf", "h3_factor")
                         if muestra.get(f"{c}_sobre_p24") is not None}
            logger.info("Cocientes sobre P24h en %.0f min: %s",
                        duracion_min,
                        ", ".join(f"{c} {v:.3f}" for c, v in cocientes.items()))

        if coeficiente is None:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "desagregacion.sin_h3",
                "la hipotesis 'h3_factor' no se calculo: "
                "tormenta.coeficiente_desagregacion.valor esta sin declarar. Un "
                "coeficiente sin fuente escrita no es una hipotesis, es un "
                "numero.",
            ))
        elif not fuente:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "desagregacion.h3_sin_fuente",
                f"'h3_factor' usa un coeficiente de {coeficiente} sin fuente "
                "declarada. La configuracion la exige, y sin ella el informe no "
                "puede sostener de donde sale.",
            ))

        if not adoptada:
            muestra = resultado.desagregacion[0] if resultado.desagregacion else {}
            detalle = "; ".join(
                f"{c}: {muestra[f'{c}_mm']:.1f} mm "
                f"({muestra[f'{c}_sobre_p24']:.0%} de P24h)"
                for c in ("h1_directa", "h2_idf", "h3_factor")
                if muestra.get(f"{c}_mm") is not None)
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "desagregacion.sin_adoptar",
                "NO se adopto ninguna hipotesis de desagregacion: "
                "tormenta.hipotesis_adoptada esta en null. Las tres se "
                f"calcularon y para T {resultado.desagregacion[0]['periodo_retorno']} "
                f"anios dan {detalle}. La diferencia entre ellas se traslada "
                "entera al caudal de diseno, de modo que es la decision con mas "
                "peso que queda abierta en la cadena de lluvia. La toma el "
                "consultor y debe quedar escrita.",
            ))


def _resolver_cambio_climatico(configuracion, base, resultado, delimitador,
                               logger) -> None:
    """Lee las proyecciones y aplica la regla condicional de la sección 6."""
    with registro.bloque(logger, "Cambio climatico"):
        if not bool(configuracion.obtener("cambio_climatico.aplicar")):
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "cambio_climatico.desactivado",
                "el cambio climatico esta desactivado en la configuracion: el "
                "hietograma no se afecta y el informe debe declararlo.",
            ))
            return

        ruta = rutas.resolver(configuracion.obtener("cambio_climatico.fuente"),
                              base)
        if not ruta.is_file():
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "cambio_climatico.sin_fuente",
                f"no se encuentra {rutas.relativa(ruta, base)}: no se calculo "
                "ningun factor de cambio climatico. La tabla debe traer, por "
                "comunicacion nacional, escenario y horizonte, el cambio "
                "proyectado en precipitacion para la zona del estudio.",
            ))
            return

        comunicacion = str(configuracion.obtener(
            "cambio_climatico.comunicacion")).strip().lower()
        escenarios = [str(e).strip().lower()
                      for e in configuracion.obtener("cambio_climatico.escenarios")]
        horizontes = [str(h).strip()
                      for h in configuracion.obtener("cambio_climatico.horizontes")]
        solo_incremento = bool(
            configuracion.obtener("cambio_climatico.solo_si_incremento"))

        with ruta.open(encoding="utf-8-sig", newline="") as manejador:
            filas = list(csv.DictReader(manejador, delimiter=delimitador))
        for fila in filas:
            if str(fila.get("comunicacion", "")).strip().lower() != comunicacion:
                continue
            escenario = str(fila.get("escenario", "")).strip().lower()
            horizonte = str(fila.get("horizonte", "")).strip()
            if escenario not in escenarios or horizonte not in horizontes:
                continue
            try:
                cambio = float(fila["cambio_precipitacion_pct"])
            except (KeyError, TypeError, ValueError):
                continue
            registro_cc = factor_de_cambio_climatico(cambio, solo_incremento)
            registro_cc.update({"comunicacion": comunicacion,
                                "escenario": escenario, "horizonte": horizonte,
                                "origen": str(fila.get("origen", "")).strip()})
            resultado.cambio_climatico.append(registro_cc)

        if not resultado.cambio_climatico:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "cambio_climatico.sin_coincidencias",
                f"la tabla no trae ninguna fila para la comunicacion "
                f"{comunicacion!r} con los escenarios {escenarios} y los "
                f"horizontes {horizontes}. No se calculo ningun factor.",
            ))
            return

        aplicados = [c for c in resultado.cambio_climatico if c["aplicado"]]
        descartados = [c for c in resultado.cambio_climatico if not c["aplicado"]]
        logger.info("%d factor(es) de cambio climatico, %d aplicable(s)",
                    len(resultado.cambio_climatico), len(aplicados))

        if aplicados:
            mayor = max(aplicados, key=lambda c: c["factor_aplicado"])
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "cambio_climatico.factores",
                f"{len(aplicados)} de {len(resultado.cambio_climatico)} "
                f"proyeccion(es) son de incremento y dan factor aplicable. El "
                f"mayor: {mayor['escenario']} en {mayor['horizonte']}, "
                f"{mayor['cambio_pct']:+.1f} %, factor "
                f"{mayor['factor_aplicado']:.3f}. El M12b decide cual usa; el "
                "informe debe declarar escenario y horizonte junto al caudal, "
                "porque un caudal de diseno sin ellos no es comparable.",
            ))
        if descartados:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "cambio_climatico.a_la_baja",
                f"{len(descartados)} proyeccion(es) son a la baja y NO se "
                "aplican, por la regla condicional de la seccion 6: "
                + "; ".join(f"{c['escenario']} {c['horizonte']} "
                            f"({c['cambio_pct']:+.1f} %)" for c in descartados)
                + ". Una reduccion proyectada no es margen que se pueda gastar, "
                "porque la incertidumbre entre modelos climaticos es mayor que "
                "la reduccion que anuncian. Queda documentado como margen de "
                "seguridad, no como omision.",
            ))


def _escribir_figura(configuracion, base, resultado, logger) -> None:
    """Dibuja las curvas IDF de las dos metodologías, por periodo de retorno."""
    if not resultado.curvas:
        return
    try:
        import graficos
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron dibujar las curvas IDF: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    periodos = sorted({f["periodo_retorno"] for f in resultado.curvas})
    colores = graficos.rampa(len(periodos), estilo)
    duracion_diseno = float(configuracion.obtener("tormenta.duracion_h")) * 60.0
    # EJES LINEALES Y ACOTADOS A LA DURACION DE DISENO. Una IDF se lee por su
    # forma, la caida abrupta de los primeros minutos y el aplanamiento
    # posterior, y eso solo se ve en lineal: en log-log la ley potencial es una
    # recta y la figura deja de parecerse a lo que un revisor espera. Llevarla
    # hasta 1.440 minutos aplastaria contra el eje justo el tramo que interesa.
    # Asi la presenta el informe de referencia, numeral 5.5, hasta 180 minutos.
    limite = duracion_diseno

    with graficos.figura(
            estilo,
            titulo=f"Curvas IDF, región {resultado.region}",
            etiqueta_x="Duración (min)",
            etiqueta_y="Intensidad (mm/h)") as (fig, ax):
        for color, periodo in zip(colores, periodos):
            de_ese = sorted((f for f in resultado.curvas
                             if f["periodo_retorno"] == periodo
                             and f["duracion_min"] <= limite),
                            key=lambda f: f["duracion_min"])
            equis = [f["duracion_min"] for f in de_ese]
            ax.plot(equis, [f.get("i_invias_mm_h") for f in de_ese],
                    color=color, linewidth=1.3, label=f"T {periodo:g}",
                    zorder=2)
            ax.plot(equis, [f.get("i_silva_mm_h") for f in de_ese],
                    color=color, linewidth=1.0, linestyle=":", zorder=2)

        ax.set_xlim(0, limite * 1.05)
        ax.set_ylim(bottom=0)
        ax.axvline(duracion_diseno, color="#b03a2e", linewidth=1.0,
                   linestyle="--", zorder=3)
        # El rotulo va abajo: arriba se solapaba con la leyenda, que ocupa la
        # esquina donde arrancan las curvas de periodo alto.
        ax.annotate(f"diseño {duracion_diseno:.0f} min",
                    xy=(duracion_diseno, 0),
                    xytext=(-5, 6), textcoords="offset points",
                    color="#b03a2e", fontsize=estilo.tamano_fuente - 1,
                    ha="right", va="bottom", zorder=4)
        ax.legend(title="Periodo de retorno", loc="upper right", frameon=False,
                  fontsize=estilo.tamano_fuente - 2, ncols=2)
        fig.text(0.01, -0.02,
                 "Línea continua: INVIAS (Vargas y Díaz-Granados). "
                 "Punteada: Silva (1998). La tabla llega a 1.440 min; la "
                 "figura se acota a la duración de diseño.",
                 fontsize=estilo.tamano_fuente - 2, color="#555555")

        for ruta in graficos.guardar(fig, directorio / "M12a_curvas_idf",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("Curvas IDF dibujadas para %d periodo(s)", len(periodos))


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Escribe una tabla con las columnas de todas sus filas."""
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


def _escribir_productos(base, resultado, delimitador, logger) -> None:
    """Escribe las tablas del módulo."""
    directorio = rutas.directorio("procesado_tormenta", base, crear=True)
    for nombre, contenido in (
        ("idf.csv", resultado.curvas),
        ("verificacion_idf_24h.csv", resultado.verificacion),
        ("desagregacion.csv", resultado.desagregacion),
        ("cambio_climatico.csv", resultado.cambio_climatico),
    ):
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))
    logger.info("Tablas escritas en %s", rutas.relativa(directorio, base))


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
        "region": resultado.region,
        "curvas": resultado.curvas,
        "verificacion": resultado.verificacion,
        "desagregacion": resultado.desagregacion,
        "cambio_climatico": resultado.cambio_climatico,
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
    sys.exit(main())

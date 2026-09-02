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

from comun import esquema, geometria, raster, registro, rutas, shapefile  # noqa: E402
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
    anclaje: dict = field(default_factory=dict)
    curvas: list[dict[str, Any]] = field(default_factory=list)
    verificacion: list[dict[str, Any]] = field(default_factory=list)
    desagregacion: list[dict[str, Any]] = field(default_factory=list)
    cambio_climatico: list[dict[str, Any]] = field(default_factory=list)
    silva: dict[str, Any] = field(default_factory=dict)
    hoja_silva: list[dict[str, Any]] = field(default_factory=list)
    adoptado: dict[str, Any] = field(default_factory=dict)
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


def hoja_de_silva(
    serie: Sequence[tuple[int, float]], coeficiente_1h: float,
    b_min: float, n: float,
) -> list[dict[str, Any]]:
    """
    Hoja de cálculo año por año con que el informe presenta la IDF de Silva.

    Ordena la serie de máximos de mayor a menor, le asigna posición de graficación
    de Weibull y deriva el periodo de retorno empírico, la máxima en una hora y
    la constante K de la curva:

        m    orden, 1 el mayor
        W    m / (n + 1), posición de graficación de Weibull
        P    probabilidad de excedencia, W en por ciento
        Tr   1 / W, en años
        P1h  coeficiente_1h · Pmáx24h
        Imáx P1h dividido por una hora, que numéricamente es P1h
        K    Imáx · (60 + b)^n, la constante que ancla la curva de ese año

    ES LA VIA EMPIRICA Y NO LA QUE LA CADENA ADOPTA. El estudio ancla la curva
    en los cuantiles ajustados en el sitio de proyecto, que es un método
    posterior y mejor: esta hoja reproduce el cálculo que el informe de
    referencia presenta, y las dos deben aparecer diciendo cuál es cuál. El Tr
    de esta tabla es empírico y no sale de la distribución ajustada.

    Excepciones
    -----------
    ErrorHidrologia
        Si la serie está vacía o alguna magnitud no es positiva.
    """
    if not serie:
        raise ErrorHidrologia(
            "la serie de maximos anuales esta vacia: sin ella no hay hoja de "
            "calculo que presentar.")
    if coeficiente_1h <= 0 or b_min < 0 or n <= 0:
        raise ErrorHidrologia(
            f"coeficiente de paso a 1 h ({coeficiente_1h}), b ({b_min} min) y "
            f"n ({n}) deben ser positivos.")

    total = len(serie)
    ordenada = sorted(serie, key=lambda par: (-par[1], par[0]))
    filas: list[dict[str, Any]] = []
    for orden, (anio, pmax24) in enumerate(ordenada, start=1):
        weibull = orden / (total + 1.0)
        p1h = coeficiente_1h * pmax24
        filas.append({
            "anio": anio,
            "pmax24_mm": round(pmax24, 1),
            "m": orden,
            "weibull": round(weibull, 4),
            "prob_excedencia_pct": round(100.0 * weibull, 2),
            "tr_anios": round(1.0 / weibull, 2),
            "pmax_1h_mm": round(p1h, 2),
            "imax_mm_h": round(p1h, 2),
            "k": round(p1h * (60.0 + b_min) ** n, 2),
        })
    return filas



def _resolver_hoja_silva(configuracion, base, resultado, coeficiente_1h,
                         b_min, n, delimitador, logger) -> None:
    """
    Arma la hoja de calculo ano por ano que el informe presenta para Silva.

    LA ESTACION ES UNA DECISION CON MARGEN. La hoja es de UNA serie y el
    estudio tiene doce; el sitio de proyecto no tiene serie propia, porque su
    valor sale de los campos interpolados. Si el consultor no declara cual,
    se toma la mas larga y se DICE cual se tomo: elegirla en silencio dejaria
    una tabla del informe sin poder explicar de donde salio.
    """
    ruta = (rutas.directorio("procesado_frecuencia", base)
            / "pmax24h_serie.csv")
    if not ruta.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "idf.silva_sin_serie",
            f"no esta {rutas.relativa(ruta, base)}: la hoja de calculo de la "
            "IDF por Silva queda sin escribir. La produce el M07.",
        ))
        return

    series: dict[str, list[tuple[int, float]]] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            try:
                series.setdefault(str(fila["codigo"]).strip(), []).append(
                    (int(fila["anio"]), float(fila["pmax24_mm"])))
            except (KeyError, TypeError, ValueError):
                continue
    if not series:
        return

    pedida = str(configuracion.obtener("idf.silva.estacion_hoja", "") or "").strip()
    if pedida and pedida in series:
        codigo, criterio = pedida, "declarada en idf.silva.estacion_hoja"
    else:
        codigo = max(series, key=lambda c: (len(series[c]), c))
        criterio = "la serie mas larga, por no haberse declarado ninguna"
        if pedida:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "idf.silva_estacion_ausente",
                f"idf.silva.estacion_hoja pide la estacion {pedida} y no tiene "
                f"serie de maximos en este estudio. Se uso {codigo}.",
            ))

    try:
        resultado.hoja_silva = hoja_de_silva(
            series[codigo], coeficiente_1h, b_min, n)
    except ErrorHidrologia as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "idf.silva_hoja", str(error)))
        return

    resultado.silva["estacion_hoja"] = codigo
    resultado.silva["criterio_estacion"] = criterio
    logger.info("Hoja de Silva sobre %s: %d anio(s) (%s)", codigo,
                len(resultado.hoja_silva), criterio)
    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA if not pedida else INFORMATIVO, "idf.silva_estacion",
        f"la hoja de calculo de la IDF por Silva se armo sobre la estacion "
        f"{codigo} ({len(resultado.hoja_silva)} anios), {criterio}. LA HOJA ES "
        f"LA VIA EMPIRICA y su Tr no sale de la distribucion ajustada: el "
        f"estudio ancla la curva en los cuantiles del sitio de proyecto, que "
        f"es metodo posterior y mejor. Las dos pueden ir en el informe "
        f"diciendo cual es cual. Para fijar otra estacion, declarar "
        f"idf.silva.estacion_hoja.",
    ))


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


def calibrar_coeficiente_1h(b_min: float, n: float) -> dict[str, Any]:
    """
    Coeficiente de paso de 24 h a 1 h que hace la curva consistente con su dato.

    El criterio es el del numeral 5.5.2 del informe de referencia: la lámina que
    la curva acumula en 24 horas debe ser la Pmáx24h del propio estudio. Con la
    forma de Talbot eso se resuelve solo, sin iterar y sin depender del valor de
    la lámina, que se cancela:

        coef = 1 / (24 * ((60 + b) / (1440 + b))^n)

    El coeficiente ES ESPECÍFICO DEL ESTUDIO y por eso no se hereda. El informe
    de referencia adoptó 0,369 para su serie de 126,78 mm, calibrado en un anexo
    de memoria de cálculo que no acompaña al documento: con b = 10 y n = 0,6 ese
    valor hace que la curva acumule 1,437 veces la Pmáx24h en 24 horas, de modo
    que no es el que sale de este criterio y no puede trasladarse sin más.

    Se calibra y se DECLARA, en lugar de arrastrar el número de otro proyecto.
    Quien prefiera un valor propio lo escribe en la configuración con su fuente,
    y entonces este cálculo no se usa.
    """
    if b_min < 0 or n <= 0:
        raise ErrorHidrologia(
            f"b ({b_min} min) no puede ser negativo y n ({n}) debe ser "
            "positivo.")
    razon = ((60.0 + b_min) / (MINUTOS_EN_24H + b_min)) ** n
    coeficiente = 1.0 / (24.0 * razon)
    return {
        "coeficiente_24h_a_1h": round(coeficiente, 4),
        "b_min": b_min,
        "n": n,
        "criterio": "la curva acumula exactamente la Pmax24h en 1.440 min",
        "lamina_en_24h_sobre_p24": round(coeficiente * razon * 24.0, 4),
    }


def lamina_de_intensidad(intensidad_mm_h: float, duracion_min: float) -> float:
    """Lámina acumulada en la duración, a partir de la intensidad media."""
    return intensidad_mm_h * duracion_min / 60.0


def coeficiente_de_escala(duracion_min: float, exponente: float) -> float:
    """
    Factor de escala temporal P(d) / P(24h) por invarianza de escala.

        P_d = P_24h * (d / 1440)^H

    CON H = 0,25 ES LA RELACIÓN DE DYCK Y PESCHKE, de uso corriente en la
    práctica colombiana para discretizar la lámina de 24 horas. Es el caso
    particular del escalamiento simple que Menabde, Seed y Pegram (1999)
    formalizaron: la serie de máximos anuales de intensidad media cumple esa
    propiedad entre 30 min y 24 h. La literatura sitúa H entre 0,20 y 0,35 para
    lluvia extrema.

    EL COEFICIENTE SE DERIVA DE LA DURACIÓN Y NO SE DECLARA FIJO. Un número
    escrito a mano vale para la duración con que se calculó y calla si la
    tormenta de diseño cambia: con 3 horas vale 0,595 y con 6 vale 0,707, y
    nada avisaría de que el estudio quedó con el de la duración anterior.

    Excepciones
    -----------
    ErrorHidrologia
        Si la duración no es positiva, si supera las 24 horas (no es una
        desagregación) o si el exponente sale del intervalo (0, 1).
    """
    if duracion_min <= 0:
        raise ErrorHidrologia(
            f"la duracion {duracion_min} min no es positiva.")
    if duracion_min > MINUTOS_EN_24H:
        raise ErrorHidrologia(
            f"la duracion {duracion_min:.0f} min supera las 24 h: la relacion "
            "de escala desagrega la lamina de 24 h, no la extrapola.")
    if not 0.0 < exponente < 1.0:
        raise ErrorHidrologia(
            f"el exponente de escala {exponente:g} esta fuera de (0, 1). La "
            "literatura lo situa entre 0,20 y 0,35 para lluvia extrema.")
    return (duracion_min / MINUTOS_EN_24H) ** exponente


def razon_interna_de_idf(
    intensidad_duracion: float | None, intensidad_24h: float | None,
    duracion_min: float,
) -> float | None:
    """
    Razón P(duración) / P(24 h) calculada DENTRO de la misma curva IDF.

    POR QUÉ NO SE DIVIDE ENTRE LA P24h DEL ANÁLISIS DE FRECUENCIA. Son dos
    estimaciones con niveles distintos: medido en este estudio, la curva del
    INVIAS extrapolada a 24 h supera en un 72 % la Pmáx24h de las estaciones.
    Dividir la lámina de una entre la de la otra no da una escala temporal, da
    esa discrepancia disfrazada de factor. Se notaba en que el cociente variaba
    con el periodo de retorno (0,80 a 0,90) cuando una relación de escala no
    debe hacerlo, y en que el exponente implícito salía 0,079, muy por debajo
    de cualquier valor publicado.

    Tomando la razón dentro de la curva se usa su FORMA y no su NIVEL, que es
    lo único que la regionalización resuelve con fiabilidad.
    """
    if not intensidad_duracion or not intensidad_24h or duracion_min <= 0:
        return None
    lamina = lamina_de_intensidad(intensidad_duracion, duracion_min)
    lamina_24h = lamina_de_intensidad(intensidad_24h, MINUTOS_EN_24H)
    if lamina_24h <= 0:
        return None
    return lamina / lamina_24h


def desagregar(
    pmax24_mm: float,
    duracion_min: float,
    intensidades: dict[str, float | None] | None,
    coeficiente: float | None,
    metodologia_adoptada: str = "",
    intensidades_24h: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """
    Las tres hipótesis de paso de P24h a la duración de diseño, en paralelo.

    Ninguna se adopta. Se entregan las tres con su cociente sobre P24h para que
    el consultor vea de un vistazo cuánto separa a una de otra: sobre esa
    diferencia se decide, y la decisión debe quedar escrita.

    'h1_directa' asigna la lámina de 24 horas a la duración de diseño. Es la más
    conservadora con diferencia y rara vez defendible, pero se calcula porque
    marca la cota superior de las otras dos.

    'h2_idf' SE CALCULA POR CADA METODOLOGÍA, porque integrar la curva de INVIAS
    o la de Silva no da lo mismo: son dos estimaciones distintas de la misma
    intensidad y la hipótesis hereda la diferencia entera. Mientras no se declare
    cuál se adopta, se entregan las dos por separado y ninguna ocupa la columna
    'h2_idf_mm', que es la que consumiría el M12b.
    """
    hipotesis: dict[str, Any] = {
        "duracion_min": duracion_min,
        "pmax24_mm": round(pmax24_mm, 2),
        "h1_directa_mm": round(pmax24_mm, 2),
    }

    for metodo, intensidad in (intensidades or {}).items():
        if intensidad is None:
            continue
        lamina = round(lamina_de_intensidad(intensidad, duracion_min), 2)
        hipotesis[f"h2_idf_{metodo}_mm"] = lamina
        if pmax24_mm > 0:
            # Cociente MEZCLADO, contra la P24h del analisis de frecuencia. Se
            # conserva porque hace visible la discrepancia entre las dos
            # fuentes, pero NO es una escala temporal y el M12b no lo consume.
            hipotesis[f"h2_idf_{metodo}_sobre_p24"] = round(
                lamina / pmax24_mm, 4)
        razon = razon_interna_de_idf(
            intensidad, (intensidades_24h or {}).get(metodo), duracion_min)
        if razon is not None:
            hipotesis[f"h2_idf_{metodo}_razon_interna"] = round(razon, 4)
            # La lamina que de verdad consume el M12b: el NIVEL lo pone el
            # analisis de frecuencia y la FORMA la curva.
            hipotesis[f"h2_idf_{metodo}_escalada_mm"] = round(
                pmax24_mm * razon, 2)

    adoptada = (metodologia_adoptada or "").strip().lower()
    if adoptada and hipotesis.get(f"h2_idf_{adoptada}_mm") is not None:
        hipotesis["h2_idf_mm"] = hipotesis[f"h2_idf_{adoptada}_mm"]
        hipotesis["h2_idf_metodologia"] = adoptada

    if coeficiente is not None:
        hipotesis["h3_factor_mm"] = round(pmax24_mm * coeficiente, 2)
        # EL FACTOR SE PUBLICA, no se deja recuperar dividiendo. La lámina va
        # redondeada a dos decimales y el cociente heredaba ese redondeo: sobre
        # este estudio salía entre 0,5945 y 0,5947, y quien comprobase que una
        # escala no varía con el periodo de retorno lo leería como que sí varía.
        hipotesis["h3_factor_escala"] = round(coeficiente, 6)

    for clave in ("h1_directa", "h2_idf", "h3_factor"):
        valor = hipotesis.get(f"{clave}_mm")
        if valor is not None and pmax24_mm > 0:
            hipotesis[f"{clave}_sobre_p24"] = round(valor / pmax24_mm, 4)
    return hipotesis


def ruta_de_escenario(
    directorio: Path, patron: str, departamento: str, variable: str,
    magnitud: str, escenario: str, horizonte: str,
) -> Path:
    """
    Arma la ruta del ráster de un escenario y horizonte.

    EL PATRÓN SE DECLARA, no se codifica. La Cuarta Comunicación reparte los
    rásteres en carpetas por departamento, variable y escenario, y el nombre del
    archivo repite escenario, horizonte y departamento. Además la carpeta de la
    variable lleva tilde ('PRECIPITACIÓN') y el archivo no ('Precipitacion'),
    de modo que hacen falta las dos formas. Si el IDEAM cambia la nomenclatura
    en una entrega futura se ajusta una línea de configuración.
    """
    relativa = patron.format(
        departamento=departamento, variable=variable, magnitud=magnitud,
        escenario=escenario.upper(), horizonte=horizonte)
    return directorio / relativa


def cambio_medio_en_la_cuenca(
    ruta_raster: Path, subcuencas, areas_km2, crs_calculo: str,
) -> dict[str, Any]:
    """
    Cambio proyectado medio sobre la cuenca, ponderado por área.

    Se muestrea el ráster en el CENTROIDE de cada subcuenca y se pondera por su
    área. La malla del IDEAM tiene celda de 0,1 grados, unos once kilómetros,
    y una cuenca de doscientos veinte kilómetros cuadrados cabe en unas pocas
    celdas: promediar por centroides sobre las ciento veinticinco subcuencas
    describe ese reparto sin fingir un detalle que la malla no tiene.

    SE DEVUELVE TAMBIÉN EL RANGO. Si el mínimo y el máximo entre subcuencas se
    separan, la cuenca cae sobre celdas distintas y el promedio esconde esa
    diferencia; si coinciden, la cuenca entera está en una sola celda y hay que
    decirlo, porque entonces el factor no distingue una parte de la cuenca de
    otra.

    Excepciones
    -----------
    ErrorRutas
        Si no está el ráster.
    ErrorHidrologia
        Si ninguna subcuenca cae dentro de su extensión.
    """
    import struct

    from pyproj import Transformer

    info = raster.leer_info(ruta_raster)
    # El raster viene sin .prj legible y en grados: se declara el geografico.
    conversor = Transformer.from_crs(crs_calculo, info.crs_epsg or "EPSG:4326",
                                     always_xy=True)
    formato = {"<f4": "f", "<f8": "d", "<i2": "h", "<u2": "H"}.get(
        info.descriptor)
    if formato is None:
        raise ErrorFormato(
            f"{ruta_raster.name}: tipo {info.descriptor} no muestreable.")

    valores: list[float] = []
    pesos: list[float] = []
    fuera = 0
    with raster.LectorRaster(ruta_raster) as lector:
        for anillos, area in zip(subcuencas, areas_km2):
            try:
                x, y = geometria.centroide([list(a) for a in anillos])
            except ErrorFormato:
                continue
            gx, gy = conversor.transform(x, y)
            if not info.contiene(gx, gy, gx, gy):
                fuera += 1
                continue
            fila, columna = info.fila_de(gy), info.columna_de(gx)
            if not (0 <= fila < info.alto and 0 <= columna < info.ancho):
                fuera += 1
                continue
            bruto = struct.unpack_from(
                "<" + formato, lector.fila(fila),
                columna * info.bytes_por_muestra)[0]
            valor = float(bruto)
            if info.nodato is not None and valor == float(info.nodato):
                fuera += 1
                continue
            if not math.isfinite(valor):
                fuera += 1
                continue
            valores.append(valor)
            pesos.append(float(area))

    if not valores:
        raise ErrorHidrologia(
            f"ninguna subcuenca cae sobre {ruta_raster.name}: revisar el "
            "departamento declarado.")

    total = sum(pesos) or float(len(valores))
    medio = sum(v * p for v, p in zip(valores, pesos)) / total
    return {
        "cambio_pct": round(medio, 3),
        "minimo_pct": round(min(valores), 3),
        "maximo_pct": round(max(valores), 3),
        "subcuencas_muestreadas": len(valores),
        "subcuencas_fuera": fuera,
        "celda_grados": round(info.tamano_x, 4),
        "una_sola_celda": bool(max(valores) == min(valores)),
    }


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


def cuantiles_en_el_punto(
    directorio_raster: Path, x: float, y: float, crs_punto: str,
    crs_raster: str,
) -> dict[float, float]:
    """
    Pmáx24h por periodo de retorno EN EL PUNTO, leída de los campos del M08.

    LA IDF ES UNA CURVA DE PUNTO. Tanto la regionalización de Vargas y
    Díaz-Granados como la de Silva estan formuladas para un sitio, y el paso de
    lluvia puntual a lluvia sobre el área es justo lo que hace el factor de
    reducción por área que el M12b aplica después. Anclar la curva en un
    promedio espacial y aplicarle luego ese factor reduce dos veces por el mismo
    motivo.

    Se lee del campo interpolado y no de la estación más próxima: el campo ya
    reúne todas las estaciones y da el valor del sitio, mientras que la más
    próxima trasladaría al punto la particularidad de una sola serie.

    Devuelve un diccionario vacío si no hay campos que leer, y quien llama
    decide si cae a la media de estaciones.
    """
    import struct

    from pyproj import Transformer

    directorio_raster = Path(directorio_raster)
    if not directorio_raster.is_dir():
        return {}

    conversor = Transformer.from_crs(crs_punto, crs_raster, always_xy=True)
    px, py = conversor.transform(x, y)

    cuantiles: dict[float, float] = {}
    for ruta in sorted(directorio_raster.glob("pmax_T*.tif")):
        etiqueta = ruta.stem[len("pmax_T"):].replace("_", ".")
        try:
            periodo = float(etiqueta)
        except ValueError:
            continue
        try:
            info = raster.leer_info(ruta)
        except (ErrorFormato, ErrorRutas):
            continue
        formato = {"<f4": "f", "<f8": "d"}.get(info.descriptor)
        if formato is None or not info.contiene(px, py, px, py):
            continue
        fila, columna = info.fila_de(py), info.columna_de(px)
        if not (0 <= fila < info.alto and 0 <= columna < info.ancho):
            continue
        try:
            with raster.LectorRaster(ruta) as lector:
                valor = float(struct.unpack_from(
                    "<" + formato, lector.fila(fila),
                    columna * info.bytes_por_muestra)[0])
        except (ErrorFormato, ErrorRutas, IndexError, struct.error):
            continue
        if info.nodato is not None and valor == float(info.nodato):
            continue
        if math.isfinite(valor) and valor > 0:
            cuantiles[periodo] = valor
    return dict(sorted(cuantiles.items()))


def _anclar_cuantiles(configuracion, base, medias, resultado, logger):
    """
    Elige el anclaje de la curva y devuelve (cuantiles, detalle del anclaje).

    LA IDF ES UNA CURVA DE PUNTO y el sitio de proyecto es el punto que interesa.
    Anclarla en la media de las estaciones describe la zona, no el sitio, y
    ademas choca con el factor de reduccion por area que el M12b aplica despues:
    ese factor existe para pasar de lluvia puntual a lluvia sobre el area, de
    modo que partir de un promedio espacial reduce dos veces por el mismo
    motivo.

    Si el criterio declarado es el punto y los campos del M08 no estan, se cae a
    la media y se advierte: es preferible una curva declarada como regional a
    ninguna curva.
    """
    criterio = str(configuracion.obtener("idf.anclaje", "punto")).strip().lower()
    detalle = {"criterio": criterio, "aplicado": "media_estaciones",
               "estaciones": len(medias)}
    if criterio != "punto":
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "idf.anclaje_regional",
            "la curva se ancla en la MEDIA de las estaciones y no en el sitio "
            "de proyecto, por declaracion de idf.anclaje. Describe la zona y no "
            "el punto, y el factor de reduccion por area que el M12b aplica "
            "despues supone una curva puntual: partir de un promedio espacial "
            "reduce dos veces.",
        ))
        return medias, detalle

    en_punto = cuantiles_en_el_punto(
        rutas.directorio("sig_raster", base) / "isoyetas",
        float(configuracion.obtener("punto_descarga.x")),
        float(configuracion.obtener("punto_descarga.y")),
        str(configuracion.obtener("punto_descarga.crs")),
        str(configuracion.obtener("crs.calculo")))

    if not en_punto:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "idf.anclaje_sin_campos",
            "se pidio anclar la IDF en el sitio de proyecto y no hay campos de "
            "Pmax24h que muestrear: ejecutar el M08 antes que este modulo. La "
            "curva se ancla en la media de las estaciones y queda declarada "
            "como regional.",
        ))
        return medias, detalle

    comunes = sorted(set(medias) & set(en_punto))
    if comunes:
        diferencias = [(p, medias[p], en_punto[p]) for p in comunes]
        peor = max(diferencias,
                   key=lambda d: abs(d[2] - d[1]) / d[1] if d[1] else 0.0)
        detalle["diferencia_maxima_pct"] = round(
            100.0 * (peor[2] - peor[1]) / peor[1], 1) if peor[1] else None
        detalle["periodo_de_la_diferencia"] = peor[0]
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "idf.anclaje_en_el_punto",
            f"la curva se ancla en el SITIO DE PROYECTO, leyendo los campos de "
            f"Pmax24h del M08 en el punto de descarga: {len(en_punto)} periodo(s). "
            f"Frente a la media de las {len(medias)} estaciones, la mayor "
            f"diferencia es de {detalle['diferencia_maxima_pct']:+.1f} % en "
            f"T = {peor[0]:g} anios ({peor[1]:.1f} contra {peor[2]:.1f} mm). "
            "La media describe la zona; el punto es lo que la IDF pide y lo que "
            "hace consistente el factor de reduccion por area que se aplica "
            "despues.",
        ))

    detalle["aplicado"] = "punto_de_descarga"
    detalle["periodos"] = len(en_punto)
    logger.info("IDF anclada en el punto de descarga: %d periodo(s)",
                len(en_punto))
    return en_punto, detalle


def leer_cuantiles(ruta: Path, delimitador: str) -> dict[float, float]:
    """
    Pmáx24h por periodo de retorno, de la tabla que dejó el M07.

    Se promedia sobre las estaciones. ES EL RESPALDO, no el anclaje primario:
    la IDF es una curva de punto y lo que corresponde es el valor en el sitio de
    proyecto, que 'cuantiles_en_el_punto' lee de los campos del M08. Esta media
    se usa cuando esos campos no existen todavia, y entonces la curva queda
    declarada como regional.

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
        cuantiles, resultado.anclaje = _anclar_cuantiles(
            configuracion, base, cuantiles, resultado, logger)
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
    _figura_cambio_climatico(configuracion, base, resultado, logger)
    _figura_cambio_departamental(configuracion, base, resultado, logger)
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
        declarado = configuracion.obtener("idf.silva.coeficiente_24h_a_1h", None)
        fuente_1h = str(configuracion.obtener(
            "idf.silva.fuente_coeficiente", "") or "").strip()
        if declarado is None:
            calibracion = calibrar_coeficiente_1h(b_silva, n_silva)
            coeficiente_1h = calibracion["coeficiente_24h_a_1h"]
            resultado.silva = calibracion
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "idf.silva_calibrado",
                f"el coeficiente de paso de 24 h a 1 h se CALIBRO en "
                f"{coeficiente_1h:.4f} con el criterio del numeral 5.5.2: la "
                "curva acumula exactamente la Pmax24h de este estudio en 1.440 "
                f"minutos, con b = {b_silva:.0f} min y n = {n_silva}. No se "
                "heredo de otro proyecto: el 0,369 del informe de referencia "
                "corresponde a su propia serie y con estos b y n haria que la "
                "curva acumulase 1,44 veces la Pmax24h.",
            ))
        else:
            coeficiente_1h = float(declarado)
            resultado.silva = {"coeficiente_24h_a_1h": coeficiente_1h,
                               "b_min": b_silva, "n": n_silva,
                               "criterio": "declarado en la configuracion",
                               "fuente": fuente_1h}
        # EL ANCLA DE LA CURVA, ya resuelto en _anclar_cuantiles: el valor del
        # sitio de proyecto si hay campos que muestrear, o la media de las
        # estaciones si no. Se toma el cuantil de 2,33 anios, que es la media de
        # una Gumbel y es la M que la formulacion de Vargas y Diaz-Granados
        # pide.
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

        logger.info("%d punto(s) de curva | region %s | anclaje %s, M = "
                    "%.1f mm", len(resultado.curvas), resultado.region,
                    resultado.anclaje.get("aplicado", "?"), media)

        if "silva" in metodologias:
            _resolver_hoja_silva(configuracion, base, resultado,
                                 coeficiente_1h, b_silva, n_silva,
                                 delimitador, logger)

        if "silva" in metodologias and declarado is not None and not fuente_1h:
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
        criterio = str(configuracion.obtener(
            "tormenta.coeficiente_desagregacion.criterio",
            "declarado")).strip().lower()
        fuente = str(configuracion.obtener(
            "tormenta.coeficiente_desagregacion.fuente", "") or "").strip()
        if criterio == "escalamiento":
            exponente = float(configuracion.obtener(
                "tormenta.coeficiente_desagregacion.exponente_escala"))
            coeficiente = coeficiente_de_escala(duracion_min, exponente)
            logger.info(
                "Coeficiente de h3_factor derivado de la duracion: "
                "(%.0f/1440)^%.3f = %.4f", duracion_min, exponente, coeficiente)
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "desagregacion.escala_temporal",
                f"'h3_factor' usa un factor de escala temporal de "
                f"{coeficiente:.4f} para {duracion_min:.0f} min, derivado de "
                f"P_d = P24h*(d/1440)^{exponente:g}. Con H = 0,25 es la "
                f"relacion de Dyck y Peschke; la literatura de escalamiento "
                f"simple situa H entre 0,20 y 0,35 para lluvia extrema. EL "
                f"COEFICIENTE SE DERIVA DE LA DURACION y cambia con ella, de "
                f"modo que una tormenta de otra duracion no hereda el de esta. "
                f"Fuente declarada: {fuente or 'sin declarar'}.",
            ))
        else:
            valor = configuracion.obtener(
                "tormenta.coeficiente_desagregacion.valor", None)
            coeficiente = float(valor) if valor is not None else None

        adoptada = str(configuracion.obtener(
            "idf.metodologia_adoptada", "") or "").strip().lower()
        for periodo, pmax in cuantiles.items():
            def curva_en(duracion):
                return next((f for f in resultado.curvas
                             if f["periodo_retorno"] == periodo
                             and f["duracion_min"] == duracion), None) or {}

            fila = curva_en(duracion_min)
            de_24h = curva_en(MINUTOS_EN_24H)
            hipotesis = desagregar(
                pmax, duracion_min,
                {"invias": fila.get("i_invias_mm_h"),
                 "silva": fila.get("i_silva_mm_h")},
                coeficiente, adoptada,
                {"invias": de_24h.get("i_invias_mm_h"),
                 "silva": de_24h.get("i_silva_mm_h")})
            hipotesis["periodo_retorno"] = periodo
            resultado.desagregacion.append(hipotesis)

        hipotesis = str(configuracion.obtener(
            "tormenta.hipotesis_adoptada", "") or "").strip().lower()
        # La eleccion de metodologia solo gobierna si la hipotesis adoptada es
        # la que integra la curva. Con h1 o h3 no entra en el resultado, y
        # advertir por ella seria ruido que tapa lo que si importa.
        if not adoptada and hipotesis in ("h1_directa", "h3_factor"):
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "desagregacion.metodologia_no_gobierna",
                f"idf.metodologia_adoptada esta sin declarar, pero la hipotesis "
                f"adoptada es {hipotesis!r} y no integra la curva IDF: la "
                "eleccion entre INVIAS y Silva no afecta a la lamina de diseno. "
                "Las dos curvas siguen calculadas y en las figuras, que es lo "
                "que el informe necesita para justificar por que no se usaron.",
            ))
        elif not adoptada:
            muestra = resultado.desagregacion[0] if resultado.desagregacion else {}
            por_metodo = "; ".join(
                f"{m}: {muestra[f'h2_idf_{m}_mm']:.1f} mm "
                f"({muestra[f'h2_idf_{m}_sobre_p24']:.0%} de P24h)"
                for m in ("invias", "silva")
                if muestra.get(f"h2_idf_{m}_mm") is not None)
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "desagregacion.sin_metodologia",
                "'h2_idf' se calculo por CADA metodologia y ninguna se adopto: "
                "idf.metodologia_adoptada esta sin declarar. Integrar la curva "
                "de INVIAS o la de Silva no da lo mismo, y la hipotesis hereda "
                f"la diferencia entera. Para T {muestra.get('periodo_retorno')} "
                f"anios: {por_metodo}. Mientras no se declare, la columna "
                "'h2_idf_mm' queda vacia y el M12b no tiene de donde tomarla.",
            ))
        elif adoptada not in ("invias", "silva"):
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "desagregacion.metodologia_desconocida",
                f"idf.metodologia_adoptada declara {adoptada!r}, que no es "
                "ninguna de las calculadas ('invias', 'silva').",
            ))
        else:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "desagregacion.metodologia",
                f"'h2_idf' integra la curva de {adoptada.upper()}, declarada en "
                "idf.metodologia_adoptada. Es la que el M12b usara si se adopta "
                "esa hipotesis.",
            ))

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

        if hipotesis:
            _declarar_hipotesis(configuracion, resultado, hipotesis, logger)

        if not hipotesis:
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


def _declarar_hipotesis(configuracion, resultado, hipotesis, logger) -> None:
    """
    Registra la hipótesis adoptada y CUÁNTO se aparta de las otras.

    Declararla no basta: el informe tiene que poder decir qué margen introduce.
    'h1_directa' asume que toda la lámina de 24 horas cae en la duración de
    diseño, de modo que la intensidad implícita queda por encima de la que dan
    las curvas IDF a esa misma duración, y esa distancia es exactamente el
    margen de seguridad adoptado. Medirlo es lo que permite defenderlo como
    criterio y no como descuido.
    """
    clave = f"{hipotesis}_mm"
    con_valor = [d for d in resultado.desagregacion if d.get(clave) is not None]
    if not con_valor:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "desagregacion.adoptada_sin_valor",
            f"se adopto la hipotesis {hipotesis!r} pero no se calculo para "
            "ningun periodo de retorno: revisar la configuracion que la "
            "sostiene.",
        ))
        return

    motivo = str(configuracion.obtener(
        "tormenta.motivo_hipotesis", "") or "").strip()
    duracion_h = float(configuracion.obtener("tormenta.duracion_h"))
    for fila in resultado.desagregacion:
        fila["hipotesis_adoptada"] = hipotesis
        fila["lamina_adoptada_mm"] = fila.get(clave)

    razones = []
    for metodo in ("invias", "silva"):
        pares = [(d[clave], d[f"h2_idf_{metodo}_mm"])
                 for d in con_valor if d.get(f"h2_idf_{metodo}_mm")]
        if pares:
            razones.append(
                f"{metodo.upper()} {statistics.fmean(a / b for a, b in pares):.2f}")

    muestra = max(con_valor, key=lambda d: d["periodo_retorno"])
    intensidad = muestra[clave] / duracion_h
    logger.info("Hipotesis adoptada %s | T %s: %.1f mm en %.0f h (%.1f mm/h)",
                hipotesis, muestra["periodo_retorno"], muestra[clave],
                duracion_h, intensidad)
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "desagregacion.adoptada",
        f"hipotesis adoptada: {hipotesis}. En T "
        f"{muestra['periodo_retorno']} anios da {muestra[clave]:.1f} mm en "
        f"{duracion_h:.0f} h, es decir {intensidad:.1f} mm/h sostenidos."
        + (f" Frente a integrar la curva IDF, la razon media es {', '.join(razones)}."
           if razones else "")
        + (f" Motivo declarado: {motivo}" if motivo else
           " SIN MOTIVO DECLARADO: la seccion 7 exige que el criterio adoptado "
           "quede registrado, y tormenta.motivo_hipotesis esta vacio."),
    ))
    if not motivo:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "desagregacion.sin_motivo",
            "la hipotesis esta adoptada pero tormenta.motivo_hipotesis esta "
            "vacio. Un estudio que no explica por que eligio una de tres "
            "hipotesis no puede defenderla ante interventoria.",
        ))


def _resolver_cambio_climatico(configuracion, base, resultado, delimitador,
                               logger) -> None:
    """Lee los rásteres departamentales y aplica la regla condicional."""
    with registro.bloque(logger, "Cambio climatico"):
        if not bool(configuracion.obtener("cambio_climatico.aplicar")):
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "cambio_climatico.desactivado",
                "el cambio climatico esta desactivado en la configuracion: el "
                "hietograma no se afecta y el informe debe declararlo.",
            ))
            return

        directorio = (Path(configuracion.obtener("referencia_nacional.directorio"))
                      / str(configuracion.obtener("cambio_climatico.directorio")))
        patron = str(configuracion.obtener("cambio_climatico.patron"))
        departamento = str(configuracion.obtener(
            "cambio_climatico.departamento"))
        variable = str(configuracion.obtener("cambio_climatico.variable"))
        magnitud = str(configuracion.obtener("cambio_climatico.magnitud"))
        escenarios = [str(e).strip()
                      for e in configuracion.obtener("cambio_climatico.escenarios")]
        horizontes = [str(h).strip()
                      for h in configuracion.obtener("cambio_climatico.horizontes")]
        solo_incremento = bool(
            configuracion.obtener("cambio_climatico.solo_si_incremento"))
        crs_calculo = str(configuracion.obtener("crs.calculo"))

        ruta_subcuencas = rutas.directorio("sig_vector", base) / "subcuencas.shp"
        if not ruta_subcuencas.is_file():
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "cambio_climatico.sin_cuenca",
                "no se encuentra subcuencas.shp: sin geometria no se puede "
                "promediar el cambio sobre la cuenca.",
            ))
            return
        entidades = shapefile.leer_geometrias(ruta_subcuencas)
        areas = [a / 1e6 for a in shapefile.areas_poligonos(ruta_subcuencas)]

        faltan = []
        for escenario in escenarios:
            for horizonte in horizontes:
                ruta = ruta_de_escenario(directorio, patron, departamento,
                                         variable, magnitud, escenario,
                                         horizonte)
                if not ruta.is_file():
                    faltan.append(f"{escenario} {horizonte}")
                    continue
                try:
                    medida = cambio_medio_en_la_cuenca(
                        ruta, entidades, areas, crs_calculo)
                except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
                    resultado.hallazgos.append(Hallazgo(
                        ADVERTENCIA, "cambio_climatico.lectura",
                        f"no se pudo leer {ruta.name}: {error}"))
                    continue
                registro_cc = factor_de_cambio_climatico(
                    medida["cambio_pct"], solo_incremento)
                registro_cc.update({
                    "escenario": escenario, "horizonte": horizonte,
                    "variable": magnitud, "departamento": departamento,
                    "raster": ruta.name,
                    "minimo_pct": medida["minimo_pct"],
                    "maximo_pct": medida["maximo_pct"],
                    "subcuencas_muestreadas": medida["subcuencas_muestreadas"],
                    "celda_grados": medida["celda_grados"],
                    "una_sola_celda": medida["una_sola_celda"],
                })
                resultado.cambio_climatico.append(registro_cc)
                logger.info("%-8s %-10s cambio %+6.2f %% -> factor %.4f%s",
                            escenario, horizonte, medida["cambio_pct"],
                            registro_cc["factor_aplicado"],
                            "" if registro_cc["aplicado"] else " (no se aplica)")

        if faltan:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "cambio_climatico.faltan_rasteres",
                f"no se encontraron los rasteres de: {faltan}. Se buscan bajo "
                f"{directorio} con el patron declarado. Descargarlos del "
                "portal del IDEAM y dejarlos ahi, o quitar de la configuracion "
                "los escenarios y horizontes que no se vayan a usar.",
            ))

        if not resultado.cambio_climatico:
            return

        _adoptar_escenario(configuracion, resultado, logger)

        aplicados = [c for c in resultado.cambio_climatico if c["aplicado"]]
        descartados = [c for c in resultado.cambio_climatico
                       if not c["aplicado"]]

        en_una_celda = [c for c in resultado.cambio_climatico
                        if c.get("una_sola_celda")]
        if en_una_celda:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "cambio_climatico.resolucion",
                f"en {len(en_una_celda)} de {len(resultado.cambio_climatico)} "
                "combinacion(es) la cuenca entera cae en una sola celda de la "
                f"malla, que tiene {resultado.cambio_climatico[0]['celda_grados']} "
                "grados de lado, unos once kilometros. El factor no distingue "
                "una parte de la cuenca de otra, y eso es del dato y no del "
                "metodo.",
            ))

        if aplicados:
            mayor = max(aplicados, key=lambda c: c["factor_aplicado"])
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "cambio_climatico.factores",
                f"{len(aplicados)} de {len(resultado.cambio_climatico)} "
                "proyeccion(es) son de incremento y dan factor aplicable, "
                f"leidas de los rasteres departamentales de {departamento}. El "
                f"mayor: {mayor['escenario']} en {mayor['horizonte']}, "
                f"{mayor['cambio_pct']:+.1f} % (entre {mayor['minimo_pct']:+.1f} "
                f"y {mayor['maximo_pct']:+.1f} % dentro de la cuenca), factor "
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

        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "cambio_climatico.variable",
            "el factor sale del cambio proyectado en la precipitacion MEDIA "
            "ANUAL, y el hietograma de diseno es un evento extremo de tres "
            "horas. No son la misma variable, y la literatura es consistente en "
            "que los extremos cambian mas que las medias. Es la aproximacion de "
            "uso corriente, pero el informe debe declarar con que variable se "
            "calculo el factor y no solo que escenario y horizonte se uso.",
        ))


def _adoptar_escenario(configuracion, resultado, logger) -> None:
    """
    Marca la combinación adoptada, según el criterio declarado.

    CRITERIO 'maximo': se adopta la de mayor factor entre las aplicables. Es el
    lado seguro y es una decisión, no un descuido: con cuatro proyecciones que
    difieren, quedarse con la mayor evita que el diseño dependa de cuál de los
    modelos climáticos se prefiera. El informe debe relacionar LAS CUATRO, y por
    eso todas viajan a la tabla y a la figura; adoptar una no borra las demás.

    CRITERIO 'declarado': se adopta la que nombren escenario_adoptado y
    horizonte_adoptado, para cuando el contrato fija cuál usar.
    """
    criterio = str(configuracion.obtener(
        "cambio_climatico.criterio_adopcion")).strip().lower()
    aplicables = [c for c in resultado.cambio_climatico if c["aplicado"]]
    for registro_cc in resultado.cambio_climatico:
        registro_cc["adoptado"] = False

    if not aplicables:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "cambio_climatico.sin_adoptar",
            "ninguna proyeccion es de incremento, de modo que no hay factor "
            "que adoptar y el hietograma no se afecta.",
        ))
        return

    if criterio == "declarado":
        escenario = str(configuracion.obtener(
            "cambio_climatico.escenario_adoptado", "") or "").strip().lower()
        horizonte = str(configuracion.obtener(
            "cambio_climatico.horizonte_adoptado", "") or "").strip()
        elegida = next((c for c in aplicables
                        if c["escenario"].lower() == escenario
                        and c["horizonte"] == horizonte), None)
        if elegida is None:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "cambio_climatico.declarado_ausente",
                f"se declaro adoptar {escenario!r} en {horizonte!r}, que no "
                "esta entre las combinaciones aplicables calculadas.",
            ))
            return
    else:
        elegida = max(aplicables, key=lambda c: c["factor_aplicado"])

    elegida["adoptado"] = True
    resultado.adoptado = dict(elegida, criterio=criterio)
    logger.info("Adoptado %s %s: factor %.4f (criterio %s)",
                elegida["escenario"], elegida["horizonte"],
                elegida["factor_aplicado"], criterio)
    otras = "; ".join(
        f"{c['escenario']} {c['horizonte']} {c['factor_aplicado']:.3f}"
        for c in resultado.cambio_climatico if not c["adoptado"])
    resultado.hallazgos.extend(_avisar_inversion_de_escenarios(
        resultado.cambio_climatico, elegida))
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "cambio_climatico.adoptado",
        f"se adopta {elegida['escenario'].upper()} en {elegida['horizonte']}, "
        f"con un cambio de {elegida['cambio_pct']:+.1f} % y factor "
        f"{elegida['factor_aplicado']:.3f}, por el criterio {criterio!r}. "
        f"Las demas quedan calculadas y en la figura: {otras}. Adoptar la mayor "
        "es el lado seguro y evita que el diseno dependa de cual de los "
        "modelos climaticos se prefiera, pero el informe debe relacionar las "
        "cuatro: un caudal con factor de cambio climatico y sin escenario "
        "declarado no es comparable con ningun otro estudio.",
    ))


def _malla_departamental(ruta_raster: Path):
    """
    Ráster departamental completo como matriz, con su extensión geográfica.

    Devuelve (matriz, extensión) o (None, None). Se lee entero a propósito: son
    19 por 22 celdas de una décima de grado, unas cuatrocientas en total, y
    remuestrear un campo tan grueso solo añadiría interpolación donde el dato no
    la tiene.
    """
    import struct

    import numpy as np

    ruta_raster = Path(ruta_raster)
    if not ruta_raster.is_file():
        return None, None
    try:
        info = raster.leer_info(ruta_raster)
    except (ErrorFormato, ErrorRutas):
        return None, None
    formato = {"<f4": "f", "<f8": "d", "<i2": "h", "<u2": "H"}.get(
        info.descriptor)
    if formato is None:
        return None, None

    matriz = np.full((info.alto, info.ancho), np.nan)
    with raster.LectorRaster(ruta_raster) as lector:
        for fila in range(info.alto):
            try:
                contenido = lector.fila(fila)
            except (ErrorFormato, ErrorRutas, IndexError):
                continue
            for columna in range(info.ancho):
                desplazamiento = columna * info.bytes_por_muestra
                if desplazamiento + info.bytes_por_muestra > len(contenido):
                    continue
                valor = float(struct.unpack_from("<" + formato, contenido,
                                                 desplazamiento)[0])
                if info.nodato is not None and valor == float(info.nodato):
                    continue
                # El IDEAM rellena el fondo con un valor centinela muy negativo
                # fuera del límite departamental; sin descartarlo, la escala de
                # color se estira hasta él y el campo real sale plano.
                if valor < -1e30:
                    continue
                matriz[fila, columna] = valor

    # LA EXTENSION LA DA EL ADAPTADOR. Recalcularla aqui obliga a acertar el
    # signo de tamano_y, y en este raster es una magnitud POSITIVA con origen_y
    # en el borde norte: sumarla situaba el departamento dos grados al norte,
    # sobre Santander, con la cuenca fuera del campo.
    xmin, ymin, xmax, ymax = info.extension
    return matriz, (xmin, xmax, ymin, ymax)


def _contorno_en_geograficas(base: Path, crs_calculo: str, ruta_area: Path):
    """
    Contorno de la cuenca en grados, para situarla sobre el campo del IDEAM.

    'ruta_area' es la reserva si aun no hay subcuencas, y llega resuelta desde
    fuera: esta funcion no recibe la configuracion, y fijar aqui el nombre del
    archivo lo desligaria de la ruta que el estudio declara.
    """
    from pyproj import Transformer

    ruta = rutas.directorio("sig_vector", base) / "subcuencas.shp"
    if not ruta.is_file():
        ruta = Path(ruta_area)
    if not ruta.is_file():
        return []
    try:
        poligonos = shapefile.leer_geometrias(ruta)
    except (ErrorFormato, ErrorRutas):
        return []
    conversor = Transformer.from_crs(crs_calculo, "EPSG:4326", always_xy=True)
    salida = []
    for anillos in poligonos:
        for anillo in anillos:
            equis, griegas = conversor.transform([p[0] for p in anillo],
                                                 [p[1] for p in anillo])
            salida.append(list(zip(equis, griegas)))
    return salida


def _celdas_tocadas(campos, contorno) -> int:
    """
    Cuántas celdas del campo departamental cubre la cuenca.

    Es la cifra que sostiene el rango: si cayera en una sola, el cambio seria un
    valor unico y el rango dentro de la cuenca no existiria. Se cuenta sobre la
    envolvente del contorno, que es una cota superior barata y suficiente para
    una nota al pie.
    """
    if not campos or not contorno:
        return 0
    _proyeccion, matriz, extension = campos[0]
    xmin, xmax, ymin, ymax = extension
    alto, ancho = matriz.shape
    paso_x = (xmax - xmin) / ancho if ancho else 0.0
    paso_y = (ymax - ymin) / alto if alto else 0.0
    if not paso_x or not paso_y:
        return 0
    # SE CUENTAN LAS CELDAS QUE EL CONTORNO PISA, no el producto del rango de
    # filas por el de columnas: ese producto es una cota superior y cuenta
    # celdas de la esquina del rectangulo que la cuenca no toca. Sobre este
    # estudio daba seis donde el muestreo encuentra cinco.
    celdas = {(int((ymax - p[1]) // paso_y), int((p[0] - xmin) // paso_x))
              for anillo in contorno for p in anillo}
    return len(celdas)


def _figura_cambio_departamental(configuracion, base, resultado,
                                 logger) -> None:
    """
    El campo departamental del IDEAM, con la cuenca situada dentro.

    LA FUENTE ES DEPARTAMENTAL Y EL ESTUDIO TOMA DE ELLA UN VALOR. El capítulo
    tiene que mostrar de dónde sale ese número: sin el campo completo, un factor
    de 1,104 parece una propiedad de la cuenca cuando es una muestra de una
    superficie que abarca todo el departamento en celdas de una décima de grado,
    unos once kilómetros. Con el campo delante se ve por qué el rango dentro de
    la cuenca va de 1,7 a 13,1 por ciento: la cuenca cruza varias celdas.

    LA ESCALA DE COLOR ES COMUN A LOS CUATRO PANELES. Con escala propia, un
    escenario de cambio pequeño saldría tan intenso como uno grande, que es
    justo la comparación que la figura debe permitir.
    """
    if not resultado.cambio_climatico:
        return
    try:
        import graficos
        import numpy as np
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudo dibujar el campo departamental: {error}"))
        return

    raiz = configuracion.obtener("referencia_nacional.directorio", "")
    if not raiz:
        return
    directorio_cc = (Path(raiz)
                     / str(configuracion.obtener("cambio_climatico.directorio")))
    patron = str(configuracion.obtener("cambio_climatico.patron"))
    departamento = str(configuracion.obtener("cambio_climatico.departamento"))
    variable = str(configuracion.obtener("cambio_climatico.variable"))
    magnitud = str(configuracion.obtener("cambio_climatico.magnitud"))

    datos = sorted(resultado.cambio_climatico,
                   key=lambda c: (c["escenario"], c["horizonte"]))
    campos = []
    for proyeccion in datos:
        ruta = directorio_cc / patron.format(
            departamento=departamento, variable=variable, magnitud=magnitud,
            escenario=str(proyeccion["escenario"]).upper(),
            horizonte=proyeccion["horizonte"])
        matriz, extension = _malla_departamental(ruta)
        if matriz is not None and np.isfinite(matriz).any():
            campos.append((proyeccion, matriz, extension))
    if not campos:
        logger.info("no se encontraron los rasteres departamentales: se omite "
                    "la figura de contexto")
        return

    todos = np.concatenate([m[np.isfinite(m)].ravel() for _, m, _ in campos])
    minimo = float(np.floor(todos.min()))
    maximo = float(np.ceil(todos.max()))
    # DIVERGENTE Y CENTRADA EN CERO, pero sin simetrizar los extremos. El signo
    # del cambio es la primera lectura de la figura y una rampa secuencial lo
    # esconde; simetrizarla, en cambio, desperdiciaria media barra cuando casi
    # todo el campo es positivo, como aqui.
    from matplotlib.colors import TwoSlopeNorm
    norma = TwoSlopeNorm(vmin=min(minimo, -0.5), vcenter=0.0,
                         vmax=max(maximo, 0.5))

    contorno = _contorno_en_geograficas(
        base, configuracion.obtener("crs.calculo"),
        rutas.resolver(configuracion.obtener(
            "hec_hms.intercambio.salida_area_influencia"), base))
    adoptado = next((c for c in datos if c.get("adoptado")), None)

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    columnas = 2
    filas = (len(campos) + columnas - 1) // columnas

    with graficos.figura(
            estilo, filas=filas, columnas=columnas,
            alto_cm=max(estilo.alto_cm, 7.5 * filas)) as (fig, ejes):
        imagen = None
        for indice, (proyeccion, matriz, extension) in enumerate(campos):
            ax = ejes[indice // columnas][indice % columnas]
            imagen = ax.imshow(matriz, extent=extension, origin="upper",
                               cmap="BrBG", norm=norma,
                               interpolation="nearest", zorder=1)
            for anillo in contorno:
                ax.plot([p[0] for p in anillo], [p[1] for p in anillo],
                        color="#111111", linewidth=0.5, zorder=3)
            es_adoptado = (adoptado is not None
                           and proyeccion["escenario"] == adoptado["escenario"]
                           and proyeccion["horizonte"] == adoptado["horizonte"])
            titulo = (f"{str(proyeccion['escenario']).upper()}  "
                      f"{proyeccion['horizonte']}   "
                      f"{proyeccion['cambio_pct']:.1f} %")
            ax.set_title(titulo + ("   [adoptado]" if es_adoptado else ""),
                         fontsize=estilo.tamano_fuente,
                         color="#b03a2e" if es_adoptado else "#333333",
                         loc="left")
            ax.set_aspect("equal", adjustable="box")
            ax.tick_params(labelsize=estilo.tamano_fuente - 3)
            if indice % columnas:
                ax.set_yticklabels([])
            if indice // columnas < filas - 1:
                ax.set_xticklabels([])
        for sobrante in range(len(campos), filas * columnas):
            ejes[sobrante // columnas][sobrante % columnas].axis("off")
        if imagen is not None:
            barra = fig.colorbar(imagen, ax=ejes.ravel().tolist(),
                                 fraction=0.035, pad=0.02)
            barra.set_label("Cambio en la precipitación media anual (%)",
                            fontsize=estilo.tamano_fuente - 1)
        celdas = _celdas_tocadas(campos, contorno)
        detalle_celdas = (f"cae sobre {celdas} celdas" if celdas > 1
                          else "cae dentro de una sola celda")
        fig.text(0.01, 0.005,
                 "Cuarta Comunicación Nacional, IDEAM. Campo del departamento "
                 f"de {departamento.title()} en celdas de 0,1 grados, unos "
                 f"11 km de lado. El trazo negro es la cuenca de estudio: "
                 f"{detalle_celdas}, y de ahí que el cambio no sea un valor "
                 "único sino un rango.",
                 fontsize=estilo.tamano_fuente - 2, color="#555555")
        for ruta in graficos.guardar(
                fig, directorio / "M12a_cambio_departamental", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("figura del campo departamental escrita")


# Orden de forzamiento radiativo de las trayectorias del IPCC, de menor a
# mayor. SSP126 es mitigacion fuerte y SSP585 el de emisiones altas.
_FORZAMIENTO = {"ssp119": 1, "ssp126": 2, "ssp245": 3, "ssp370": 4,
                "ssp434": 5, "ssp460": 6, "ssp585": 7}


def _avisar_inversion_de_escenarios(proyecciones, elegida) -> list[Hallazgo]:
    """
    Avisa si el aumento proyectado NO crece con el forzamiento del escenario.

    NO ES UN ERROR NI UNA RAREZA DEL DATO. La precipitacion regional no responde
    de forma monotona al forzamiento: mas calentamiento desplaza la circulacion
    y puede reducir la lluvia en una zona mientras la aumenta en otra. Pero es
    lo primero que una interventoria va a preguntar cuando vea que el factor de
    diseno sale del escenario de MITIGACION, y el informe tiene que llegar con
    la respuesta escrita en lugar de improvisarla.

    Se comprueba dentro de cada horizonte por separado: comparar el SSP126 de
    2041-2060 con el SSP585 de 2021-2040 mezclaria el efecto del escenario con
    el del tiempo transcurrido.
    """
    if not proyecciones:
        return []

    por_horizonte: dict[str, list] = {}
    for proyeccion in proyecciones:
        por_horizonte.setdefault(str(proyeccion["horizonte"]), []).append(
            proyeccion)

    invertidos = []
    for horizonte, grupo in sorted(por_horizonte.items()):
        ordenado = sorted(
            (p for p in grupo
             if str(p["escenario"]).lower() in _FORZAMIENTO),
            key=lambda p: _FORZAMIENTO[str(p["escenario"]).lower()])
        if len(ordenado) < 2:
            continue
        cambios = [float(p["cambio_pct"]) for p in ordenado]
        if cambios[0] > cambios[-1]:
            invertidos.append(
                f"{horizonte}: "
                + " > ".join(f"{str(p['escenario']).upper()} "
                             f"{float(p['cambio_pct']):+.1f} %"
                             for p in ordenado))

    if not invertidos:
        return []

    escenario_elegido = str(elegida["escenario"]).lower()
    mayor_forzamiento = max(
        (str(p["escenario"]).lower() for p in proyecciones
         if str(p["escenario"]).lower() in _FORZAMIENTO),
        key=lambda e: _FORZAMIENTO[e], default="")
    aviso = ""
    if (escenario_elegido in _FORZAMIENTO and mayor_forzamiento
            and _FORZAMIENTO[escenario_elegido]
            < _FORZAMIENTO[mayor_forzamiento]):
        aviso = (f" El factor adoptado sale de {escenario_elegido.upper()}, que "
                 f"NO es el de mayor forzamiento ({mayor_forzamiento.upper()}): "
                 "conviene decirlo en el informe antes de que lo pregunten.")

    return [Hallazgo(
        ADVERTENCIA, "cambio_climatico.inversion_de_escenarios",
        "el aumento proyectado DISMINUYE al crecer el forzamiento del "
        "escenario, en " + ("todos los horizontes" if len(invertidos) > 1
                            else "un horizonte") + ": "
        + "; ".join(invertidos)
        + ". No es un error del dato: la precipitacion regional no responde de "
          "forma monotona al forzamiento, porque mas calentamiento desplaza la "
          "circulacion y puede reducir la lluvia en una zona mientras la "
          "aumenta en otra." + aviso)]


def _figura_cambio_climatico(configuracion, base, resultado, logger) -> None:
    """
    Las cuatro proyecciones, con su rango dentro de la cuenca y la adoptada.

    La tabla dice el número; la figura dice cuánto se separan entre sí y cuánto
    varía cada una dentro de la propia cuenca. Las dos cosas hacen falta para
    sostener por qué se adoptó una: si el rango interno de una proyección se
    solapa con el de otra, la diferencia entre escenarios no es la que parece.
    """
    if not resultado.cambio_climatico:
        return
    try:
        import graficos
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudo dibujar el cambio climatico: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    datos = sorted(resultado.cambio_climatico,
                   key=lambda c: (c["escenario"], c["horizonte"]))

    with graficos.figura(
            estilo,
            titulo="Cambio proyectado en la precipitación media anual",
            etiqueta_x="",
            etiqueta_y="Cambio (%)") as (fig, ax):
        posiciones = range(len(datos))
        etiquetas = [f"{c['escenario'].upper()}\n{c['horizonte']}"
                     for c in datos]
        colores = ["#b03a2e" if c.get("adoptado") else estilo.color(0)
                   for c in datos]
        alturas = [c["cambio_pct"] for c in datos]
        ax.bar(posiciones, alturas, color=colores, width=0.6)

        # El rango dentro de la cuenca, como barra de error: es lo que la
        # media sola no dice.
        inferiores = [c["cambio_pct"] - c["minimo_pct"] for c in datos]
        superiores = [c["maximo_pct"] - c["cambio_pct"] for c in datos]
        ax.errorbar(posiciones, alturas, yerr=[inferiores, superiores],
                    fmt="none", ecolor="#555555", capsize=4, linewidth=1.0,
                    label="rango dentro de la cuenca")

        ax.axhline(0.0, color=graficos.GRIS_CONTEXTO, linewidth=0.8)
        for posicion, dato in zip(posiciones, datos):
            ax.annotate(f"{dato['factor_aplicado']:.3f}",
                        xy=(posicion, dato["maximo_pct"]),
                        xytext=(0, 4), textcoords="offset points",
                        ha="center", fontsize=estilo.tamano_fuente - 1,
                        color="#b03a2e" if dato.get("adoptado") else "#555555")
        ax.set_xticks(list(posiciones))
        ax.set_xticklabels(etiquetas, fontsize=estilo.tamano_fuente - 1)
        # Holgura arriba: el rotulo del factor y la leyenda comparten esquina y
        # sin ella se pisaban.
        techo = max(c["maximo_pct"] for c in datos)
        ax.set_ylim(min(0.0, min(c["minimo_pct"] for c in datos) * 1.1),
                    techo * 1.35)
        adoptada = next((c for c in datos if c.get("adoptado")), None)
        if adoptada is not None:
            ax.plot([], [], marker="s", linestyle="none", color="#b03a2e",
                    markersize=8,
                    label=f"adoptado: factor {adoptada['factor_aplicado']:.3f}")
        ax.legend(loc="upper left", frameon=False,
                  fontsize=estilo.tamano_fuente - 1)
        fig.text(0.01, -0.09,
                 "Rotulo sobre cada barra: factor aplicable. El cambio es de la "
                 "precipitación MEDIA ANUAL, no del evento extremo.",
                 fontsize=estilo.tamano_fuente - 2, color="#555555")
        for ruta in graficos.guardar(fig, directorio / "M12a_cambio_climatico",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("Figura de cambio climatico escrita")


# Periodos que lleva la figura de COMPARACION. Con ocho curvas por metodologia
# la figura se vuelve ilegible; el informe de referencia usa tres, que bastan
# para ver si los metodos se cruzan y donde.
PERIODOS_DE_COMPARACION = (2.33, 25.0, 100.0)


def _dibujar_idf(graficos, estilo, resultado, periodos, limite, duracion_diseno,
                 series, titulo, pie):
    """
    Arma una figura de curvas IDF, en ejes lineales.

    UNA IDF SE LEE POR SU FORMA: la caída abrupta de los primeros minutos y el
    aplanamiento posterior. En log-log una ley potencial es una recta y la
    figura deja de parecerse a lo que es, aunque los valores sean los mismos.
    Se acota además a la duración de diseño, porque llevarla a 1.440 minutos
    aplasta contra el eje justo el tramo que se usa.
    """
    colores = graficos.rampa(len(periodos), estilo)
    with graficos.figura(estilo, titulo=titulo, etiqueta_x="Duración (min)",
                         etiqueta_y="Intensidad (mm/h)") as (fig, ax):
        for color, periodo in zip(colores, periodos):
            de_ese = sorted((f for f in resultado.curvas
                             if f["periodo_retorno"] == periodo
                             and f["duracion_min"] <= limite),
                            key=lambda f: f["duracion_min"])
            equis = [f["duracion_min"] for f in de_ese]
            for columna, estilo_linea, sufijo in series:
                valores = [f.get(columna) for f in de_ese]
                if not any(v is not None for v in valores):
                    continue
                ax.plot(equis, valores, color=color, linewidth=1.3,
                        linestyle=estilo_linea, zorder=2,
                        label=f"T {periodo:g}{sufijo}")

        ax.set_xlim(0, limite * 1.05)
        ax.set_ylim(bottom=0)
        ax.axvline(duracion_diseno, color="#b03a2e", linewidth=1.0,
                   linestyle="--", zorder=3)
        ax.annotate(f"diseño {duracion_diseno:.0f} min",
                    xy=(duracion_diseno, 0), xytext=(-5, 6),
                    textcoords="offset points", color="#b03a2e",
                    fontsize=estilo.tamano_fuente - 1, ha="right",
                    va="bottom", zorder=4)
        ax.legend(title="Periodo de retorno", loc="upper right", frameon=False,
                  fontsize=estilo.tamano_fuente - 2,
                  ncols=2 if len(periodos) > 4 else 1)
        if pie:
            fig.text(0.01, -0.02, pie,
                     fontsize=estilo.tamano_fuente - 2, color="#555555")
        return fig


def _escribir_figura(configuracion, base, resultado, logger) -> None:
    """
    Tres figuras: cada metodología por separado y la comparación.

    Separarlas no es cosmético. Con dos metodologías y ocho periodos, una sola
    figura lleva dieciséis curvas y no se lee ninguna. El informe las presenta
    así: una por método con todos sus periodos, y una de contraste con tres.
    """
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
    duracion_diseno = float(configuracion.obtener("tormenta.duracion_h")) * 60.0
    limite = duracion_diseno

    figuras = [
        ("M12a_idf_invias", periodos,
         [("i_invias_mm_h", "-", "")],
         f"Curvas IDF, método INVIAS, región {resultado.region}",
         "Vargas y Díaz-Granados, regionalización del INVIAS. La tabla llega a "
         "1.440 min; la figura se acota a la duración de diseño."),
        ("M12a_idf_silva", periodos,
         [("i_silva_mm_h", "-", "")],
         "Curvas IDF, método Silva",
         "Silva (1998), anclada en la Pmáx24h de las estaciones del estudio."),
        ("M12a_idf_comparacion",
         [p for p in periodos if p in PERIODOS_DE_COMPARACION] or periodos[:3],
         [("i_invias_mm_h", "-", " INVIAS"), ("i_silva_mm_h", "--", " Silva")],
         "Curvas IDF, comparación de metodologías",
         "Línea continua: INVIAS. Discontinua: Silva. Donde se separan, al "
         "menos una no describe esta cuenca."),
    ]

    for nombre, sus_periodos, series, titulo, pie in figuras:
        columnas = [c for c, _, _ in series]
        if not any(f.get(c) is not None
                   for f in resultado.curvas for c in columnas):
            continue
        figura = _dibujar_idf(graficos, estilo, resultado, sus_periodos, limite,
                              duracion_diseno, series, titulo, pie)
        for ruta in graficos.guardar(figura, directorio / nombre, estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("Figuras de IDF escritas en %s",
                rutas.relativa(directorio, base))


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
        ("idf_silva_hoja.csv", resultado.hoja_silva),
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
        "anclaje": resultado.anclaje,
        "curvas": resultado.curvas,
        "verificacion": resultado.verificacion,
        "desagregacion": resultado.desagregacion,
        "cambio_climatico": resultado.cambio_climatico,
        "silva": resultado.silva,
        "cambio_climatico_adoptado": resultado.adoptado,
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

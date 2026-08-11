# -*- coding: utf-8 -*-
"""
tiempo_concentracion
====================
Las trece fórmulas de tiempo de concentración de la matriz del estudio.

REPARTO CON data/referencia/tc_aplicabilidad.csv. La matriz guarda lo que
cambia y se discute: el rango de calibración de cada fórmula, su procedencia y
el tipo de cuenca al que corresponde. Aquí vive el álgebra, que no cambia. Un
CSV con expresiones ejecutables sería doctrina aparente y código real, con el
inconveniente añadido de tener que evaluar texto de un archivo.

La correspondencia entre las dos mitades se comprueba en las pruebas: cada
fórmula de la matriz tiene implementación y cada implementación está en la
matriz. Sin ese control, añadir una fila al CSV daría una fórmula que se
declara aplicable y no se calcula nunca.

UNIDADES. Cada fórmula se publicó en las suyas y mezclarlas es el error más
frecuente al programarlas. Aquí todas reciben los mismos argumentos en unidades
del SI declaradas y todas devuelven HORAS. La conversión va dentro de cada
función, junto a la expresión que la necesita.

Argumentos comunes:

    area_km2      área de la cuenca
    longitud_km   longitud del cauce principal
    pendiente     pendiente del cauce, en m/m
    desnivel_m    desnivel entre los extremos del cauce
    cota_media_m  cota media de la cuenca sobre la de salida
    cn            número de curva, solo para scs_lag

Ninguna acepta pendiente nula o negativa: casi todas la llevan en el
denominador y el resultado sería infinito. Se devuelve None, que el módulo
consumidor reporta como no calculable, en lugar de propagar un infinito que
más adelante parecería un número.
"""

from __future__ import annotations

import math
from typing import Any, Callable

__all__ = [
    "FORMULAS",
    "REQUISITOS",
    "calcular",
    "calcular_todas",
    "estadisticos",
]


def _positivo(*valores) -> bool:
    """Todas las magnitudes deben ser positivas y finitas."""
    for valor in valores:
        if valor is None:
            return False
        try:
            numero = float(valor)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(numero) or numero <= 0.0:
            return False
    return True


# =============================================================================
# Las fórmulas
# =============================================================================
def kirpich(area_km2=None, longitud_km=None, pendiente=None, **_) -> float | None:
    """
    Kirpich (1940). Publicada con L en pies y en minutos; aquí en SI.

    Tc = 0,0195 * L^0,77 * S^-0,385, con L en metros y el resultado en minutos.
    """
    if not _positivo(longitud_km, pendiente):
        return None
    longitud_m = longitud_km * 1000.0
    return 0.0195 * (longitud_m ** 0.77) * (pendiente ** -0.385) / 60.0


def california(area_km2=None, longitud_km=None, desnivel_m=None,
               **_) -> float | None:
    """
    California Culverts Practice (1942). Variante de Kirpich para montaña.

    Tc = 0,0195 * (L^3 / H)^0,385, con L en metros, H en metros, minutos.
    """
    if not _positivo(longitud_km, desnivel_m):
        return None
    longitud_m = longitud_km * 1000.0
    return 0.0195 * ((longitud_m ** 3) / desnivel_m) ** 0.385 / 60.0


def temez(area_km2=None, longitud_km=None, pendiente=None, **_) -> float | None:
    """Témez (1978). Tc = 0,3 * (L / S^0,25)^0,76, L en km, horas."""
    if not _positivo(longitud_km, pendiente):
        return None
    return 0.3 * (longitud_km / (pendiente ** 0.25)) ** 0.76


def giandotti(area_km2=None, longitud_km=None, cota_media_m=None,
              **_) -> float | None:
    """
    Giandotti (1934). Exige el desnivel MEDIO respecto de la cota de salida.

    Tc = (4*raiz(A) + 1,5*L) / (0,8*raiz(Hm)), A en km2, L en km, Hm en m.

    Confundir Hm con el desnivel total es un error frecuente y sesga el
    resultado a la baja, porque el desnivel total siempre es mayor.
    """
    if not _positivo(area_km2, longitud_km, cota_media_m):
        return None
    return ((4.0 * math.sqrt(area_km2) + 1.5 * longitud_km)
            / (0.8 * math.sqrt(cota_media_m)))


def ventura(area_km2=None, pendiente=None, **_) -> float | None:
    """Ventura-Heras. Tc = 0,05 * raiz(A/S), A en km2, horas."""
    if not _positivo(area_km2, pendiente):
        return None
    return 0.05 * math.sqrt(area_km2 / pendiente)


def passini(area_km2=None, longitud_km=None, pendiente=None,
            **_) -> float | None:
    """Passini. Tc = 0,108 * (A*L)^(1/3) / raiz(S), A en km2, L en km, horas."""
    if not _positivo(area_km2, longitud_km, pendiente):
        return None
    return 0.108 * ((area_km2 * longitud_km) ** (1.0 / 3.0)) / math.sqrt(pendiente)


def bransby(area_km2=None, longitud_km=None, pendiente=None,
            **_) -> float | None:
    """Bransby Williams. Tc = 0,2433*L / (A^0,1 * S^0,2), L km, A km2, horas."""
    if not _positivo(area_km2, longitud_km, pendiente):
        return None
    return 0.2433 * longitud_km / ((area_km2 ** 0.1) * (pendiente ** 0.2))


def johnstone(area_km2=None, longitud_km=None, pendiente=None,
              **_) -> float | None:
    """
    Johnstone y Cross (1949). Tc = 0,4623 * L^0,5 * S^-0,25, L km, horas.

    Es la única de la matriz calibrada en cuencas grandes y de baja pendiente,
    de 65 a 4.200 km2.
    """
    if not _positivo(longitud_km, pendiente):
        return None
    return 0.4623 * math.sqrt(longitud_km) * (pendiente ** -0.25)


def scs_lag(area_km2=None, longitud_km=None, pendiente=None, cn=None,
            **_) -> float | None:
    """
    SCS Lag (NRCS). Es la coherente con el método SCS de transformación.

    Tlag = L^0,8 * (S+1)^0,7 / (1900 * Y^0,5), en HORAS, con L en pies,
    S = 1000/CN - 10 la retención potencial en pulgadas e Y la pendiente en
    porcentaje. Tc = Tlag / 0,6.

    Es la única de la matriz que necesita el número de curva, de modo que sin
    CN no se puede calcular. Devolver None y decirlo es preferible a suponer un
    CN, que gobernaría el resultado.

    Está definida para cuencas menores de 800 ha. Fuera de ahí la matriz ya la
    descarta.
    """
    if not _positivo(longitud_km, pendiente, cn) or cn >= 100.0:
        return None
    longitud_pies = longitud_km * 1000.0 / 0.3048
    retencion = 1000.0 / cn - 10.0
    if retencion <= 0.0:
        return None
    pendiente_pct = pendiente * 100.0
    rezago = ((longitud_pies ** 0.8) * ((retencion + 1.0) ** 0.7)
              / (1900.0 * math.sqrt(pendiente_pct)))
    return rezago / 0.6


def clark(area_km2=None, pendiente=None, **_) -> float | None:
    """Clark. Tc = 0,335 * (A / raiz(S))^0,593, A en km2, horas."""
    if not _positivo(area_km2, pendiente):
        return None
    return 0.335 * (area_km2 / math.sqrt(pendiente)) ** 0.593


def pilgrim(area_km2=None, **_) -> float | None:
    """
    Pilgrim y McDermott. Tc = 0,76 * A^0,38, A en km2, horas.

    Solo depende del área. Útil como contraste, no como valor adoptado: una
    fórmula que ignora la pendiente y la longitud del cauce da el mismo tiempo
    a una cuenca de montaña y a una de llanura de igual tamaño.
    """
    if not _positivo(area_km2):
        return None
    return 0.76 * (area_km2 ** 0.38)


def valencia(area_km2=None, longitud_km=None, pendiente=None,
             **_) -> float | None:
    """
    Valencia y Zuluaga. Calibrada en la cordillera colombiana.

    Tc = 1,7694 * A^0,325 * L^-0,096 * S^-0,290, A en km2, L en km, horas.
    """
    if not _positivo(area_km2, longitud_km, pendiente):
        return None
    return (1.7694 * (area_km2 ** 0.325) * (longitud_km ** -0.096)
            * (pendiente ** -0.290))


def v_te_chow(area_km2=None, longitud_km=None, pendiente=None,
              **_) -> float | None:
    """V. T. Chow. Tc = 0,1602 * (L / raiz(S))^0,64, L en km, horas."""
    if not _positivo(longitud_km, pendiente):
        return None
    return 0.1602 * (longitud_km / math.sqrt(pendiente)) ** 0.64


# Clave de la matriz -> implementación. El nombre debe coincidir con la
# columna 'formula' de data/referencia/tc_aplicabilidad.csv.
FORMULAS: dict[str, Callable[..., float | None]] = {
    "kirpich": kirpich,
    "california": california,
    "temez": temez,
    "giandotti": giandotti,
    "ventura": ventura,
    "passini": passini,
    "bransby": bransby,
    "johnstone": johnstone,
    "scs_lag": scs_lag,
    "clark": clark,
    "pilgrim": pilgrim,
    "valencia": valencia,
    "v_te_chow": v_te_chow,
}

# Qué magnitud necesita cada una, para poder explicar por qué no se calculó.
REQUISITOS: dict[str, tuple[str, ...]] = {
    "kirpich": ("longitud_km", "pendiente"),
    "california": ("longitud_km", "desnivel_m"),
    "temez": ("longitud_km", "pendiente"),
    "giandotti": ("area_km2", "longitud_km", "cota_media_m"),
    "ventura": ("area_km2", "pendiente"),
    "passini": ("area_km2", "longitud_km", "pendiente"),
    "bransby": ("area_km2", "longitud_km", "pendiente"),
    "johnstone": ("longitud_km", "pendiente"),
    "scs_lag": ("longitud_km", "pendiente", "cn"),
    "clark": ("area_km2", "pendiente"),
    "pilgrim": ("area_km2",),
    "valencia": ("area_km2", "longitud_km", "pendiente"),
    "v_te_chow": ("longitud_km", "pendiente"),
}


# =============================================================================
# Cálculo
# =============================================================================
def calcular(nombre: str, **magnitudes) -> tuple[float | None, str]:
    """
    Calcula una fórmula y devuelve (horas, motivo si no se pudo).

    El motivo nombra la magnitud que falta. Un informe que dice "no se calculó"
    sin decir que faltaba la cota media no permite corregir el insumo.
    """
    funcion = FORMULAS.get(nombre)
    if funcion is None:
        return None, f"no hay implementación de {nombre!r}"

    faltan = [requisito for requisito in REQUISITOS.get(nombre, ())
              if not _positivo(magnitudes.get(requisito))]
    if faltan:
        return None, "falta " + ", ".join(faltan)

    valor = funcion(**magnitudes)
    if valor is None or not math.isfinite(valor) or valor <= 0.0:
        return None, "la fórmula no devuelve un valor positivo finito"
    return valor, ""


def calcular_todas(**magnitudes) -> dict[str, dict[str, Any]]:
    """Aplica todas las fórmulas y devuelve, por clave, su horas y su motivo."""
    salida: dict[str, dict[str, Any]] = {}
    for nombre in FORMULAS:
        horas, motivo = calcular(nombre, **magnitudes)
        salida[nombre] = {
            "horas": round(horas, 4) if horas is not None else None,
            "minutos": round(horas * 60.0, 2) if horas is not None else None,
            "motivo": motivo,
        }
    return salida


def estadisticos(valores) -> dict[str, Any]:
    """
    Resumen del subconjunto adoptable: mediana, media, extremos y dispersión.

    Se reporta el coeficiente de variación porque la sección 7 de CLAUDE.md
    condiciona la adopción a que la dispersión no sea alta. Con fórmulas que se
    calibraron en cuencas distintas, una dispersión grande no es ruido: significa
    que la cuenca no se parece a ninguna de ellas.
    """
    limpios = sorted(float(v) for v in valores
                     if v is not None and math.isfinite(float(v)) and v > 0)
    if not limpios:
        return {"n": 0, "mediana": None, "media": None, "minimo": None,
                "maximo": None, "desviacion": None, "cv": None,
                "razon_extremos": None}

    cuantos = len(limpios)
    medio = cuantos // 2
    mediana = (limpios[medio] if cuantos % 2
               else 0.5 * (limpios[medio - 1] + limpios[medio]))
    media = sum(limpios) / cuantos
    varianza = (sum((v - media) ** 2 for v in limpios) / (cuantos - 1)
                if cuantos > 1 else 0.0)
    desviacion = math.sqrt(varianza)
    return {
        "n": cuantos,
        "mediana": round(mediana, 4),
        "media": round(media, 4),
        "minimo": round(limpios[0], 4),
        "maximo": round(limpios[-1], 4),
        "desviacion": round(desviacion, 4),
        "cv": round(desviacion / media, 4) if media else None,
        "razon_extremos": (round(limpios[-1] / limpios[0], 3)
                           if limpios[0] else None),
    }

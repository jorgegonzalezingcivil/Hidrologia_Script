#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Análisis de frecuencia de extremos hidrológicos
===============================================
Entorno: venv del proyecto. Depende de numpy y scipy.

Sigue la separación de entornos ya establecida: no puede importarse desde
src/comun, que comparte el Python de QGIS.

CLAUDE.md, sección 6, cierra la lista: "Normal, LogNormal 2/3P, Gumbel, GEV,
Pearson III, Log-Pearson III, Exponencial, Weibull, Gamma. Momentos, momentos-L
y MV. Pruebas KS, Anderson-Darling, chi-cuadrado, AIC/BIC". Este módulo
implementa esa matriz y devuelve resultados comparables entre sí; NO elige la
distribución, que es decisión del consultor sobre la evidencia.

Sobre los tres métodos de ajuste. No son intercambiables y por eso se calculan
los tres:

    momentos      simple y reproducible, pero muy sensible al valor extremo,
                  que en una serie de máximos es justamente el dato de diseño
    momentos-L    robusto frente al extremo y recomendado por Hosking para
                  muestras cortas, que es el caso habitual en hidrología
    verosimilitud eficiente cuando la muestra es larga y la distribución es la
                  correcta; inestable cuando no lo es

Sobre los valores atípicos. CLAUDE.md, sección 7, es explícito: no se aplica IQR
a la serie de máximos, porque truncaría el dato de diseño. Lo que sí procede es
la prueba de Grubbs-Beck del Bulletin 17C, que busca atípicos BAJOS: un año
anormalmente seco distorsiona la cola alta al forzar el ajuste hacia abajo. Los
altos se conservan siempre.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
from scipy import optimize, stats

__all__ = [
    "Ajuste",
    "DISTRIBUCIONES",
    "METODOS",
    "momentos_l",
    "ajustar",
    "cuantiles",
    "bondad_de_ajuste",
    "grubbs_beck",
    "densidad",
    "posicion_grafica",
    "FORMA_GEV_FIJA",
    "ErrorFrecuencia",
]


class ErrorFrecuencia(ValueError):
    """Serie o combinación que no admite el ajuste pedido."""


# Nombre declarado en config -> distribución de scipy. El nombre 'gumbel_max'
# alude al máximo, que es lo que se ajusta: gumbel_r en scipy.
DISTRIBUCIONES: dict[str, str] = {
    "normal": "norm",
    "lognormal2": "lognorm2",
    "lognormal3": "lognorm3",
    "gumbel_max": "gumbel_r",
    "gev": "genextreme",
    "pearson3": "pearson3",
    "logpearson3": "logpearson3",
    "exponencial": "expon",
    "weibull": "weibull_min",
    "gamma": "gamma",
    # Añadidas para cubrir el repertorio de Hydrognomon, que CLAUDE.md, sección
    # 4, declara reemplazado por este análisis: el reemplazo debe ser al menos
    # tan completo como lo reemplazado.
    #
    # EV2-Max es el valor extremo tipo II, de cola pesada: en scipy es la
    # Fréchet, que se expone como invweibull. Importa porque una serie con cola
    # más pesada que la Gumbel queda mal descrita por ella justo en el periodo
    # de retorno alto, que es el de diseño.
    "ev2_max": "invweibull",
    # GEV con el parámetro de forma FIJADO, no ajustado. Se usa cuando el valor
    # procede de un análisis regional y no de la muestra propia, que con
    # treinta años estima la forma con mucha incertidumbre.
    "gev_k_fijo": "genextreme_k",
    # Pareto generalizada. Su uso propio es series sobre umbral y no máximos
    # anuales; se ofrece para contraste, como hace Hydrognomon.
    "pareto": "genpareto",
}

# Las de MÍNIMOS de Hydrognomon (EV1-Min, EV3-Min como mínimo, GEV-Min) no se
# incluyen: no describen una serie de máximos. Corresponden al M19, donde se
# analizan caudales de estiaje.

METODOS = ("momentos", "momentos_l", "maxima_verosimilitud")

# Parámetro de forma de la GEV cuando se fija en lugar de ajustarse. Quien
# orquesta lo sobrescribe con el valor declarado en config: no es una constante
# universal sino una elección regional que el consultor debe justificar.
FORMA_GEV_FIJA = 0.15


@dataclass
class Ajuste:
    """Resultado de ajustar una distribución por un método."""

    distribucion: str
    metodo: str
    parametros: tuple[float, ...] = ()
    n: int = 0
    error: str = ""
    bondad: dict[str, Any] = field(default_factory=dict)
    cuantiles: dict[float, float] = field(default_factory=dict)

    @property
    def valido(self) -> bool:
        return not self.error and bool(self.parametros)

    def como_dict(self) -> dict[str, Any]:
        return {
            "distribucion": self.distribucion,
            "metodo": self.metodo,
            "parametros": [round(float(p), 6) for p in self.parametros],
            "n": self.n,
            "error": self.error,
            **{k: v for k, v in self.bondad.items()},
        }


def _limpiar(datos: Iterable[float]) -> np.ndarray:
    arreglo = np.asarray(list(datos), dtype=float)
    arreglo = arreglo[np.isfinite(arreglo)]
    return np.sort(arreglo)


# =============================================================================
# Momentos-L (Hosking)
# =============================================================================
def momentos_l(datos: Iterable[float]) -> dict[str, float]:
    """
    Los cuatro primeros momentos-L y sus razones.

    Se calculan por los estimadores insesgados de Hosking, a partir de los
    momentos ponderados por probabilidad. Su virtud frente a los momentos
    ordinarios es que no elevan las desviaciones al cuadrado ni al cubo, de modo
    que un único valor extremo no domina el resultado. En una serie de máximos
    anuales, donde el extremo es el dato de diseño y no un error, esa robustez
    es exactamente lo que se busca.
    """
    x = _limpiar(datos)
    n = x.size
    if n < 4:
        raise ErrorFrecuencia(
            f"los momentos-L necesitan al menos cuatro datos y se recibieron {n}."
        )
    # Momentos ponderados por probabilidad b0..b3.
    indices = np.arange(1, n + 1)
    b0 = float(np.mean(x))
    b1 = float(np.sum((indices - 1) / (n - 1) * x) / n)
    b2 = float(np.sum((indices - 1) * (indices - 2)
                      / ((n - 1) * (n - 2)) * x) / n)
    b3 = float(np.sum((indices - 1) * (indices - 2) * (indices - 3)
                      / ((n - 1) * (n - 2) * (n - 3)) * x) / n)

    l1 = b0
    l2 = 2 * b1 - b0
    l3 = 6 * b2 - 6 * b1 + b0
    l4 = 20 * b3 - 30 * b2 + 12 * b1 - b0
    return {
        "l1": l1, "l2": l2, "l3": l3, "l4": l4,
        "t": l2 / l1 if l1 else float("nan"),
        "t3": l3 / l2 if l2 else float("nan"),
        "t4": l4 / l2 if l2 else float("nan"),
    }


def _ajuste_momentos_l(nombre: str, x: np.ndarray) -> tuple[float, ...]:
    """
    Parámetros por momentos-L, con las relaciones de Hosking.

    Se implementan las distribuciones para las que existe una relación cerrada.
    Las demás declaran su ausencia en lugar de recurrir en silencio a otro
    método, que produciría un resultado etiquetado como momentos-L sin serlo.
    """
    lm = momentos_l(x)
    l1, l2, t3 = lm["l1"], lm["l2"], lm["t3"]

    if nombre == "norm":
        return (l1, l2 * math.sqrt(math.pi))
    if nombre == "expon":
        return (l1 - 2 * l2, 2 * l2)
    if nombre == "gumbel_r":
        escala = l2 / math.log(2.0)
        return (l1 - 0.5772156649 * escala, escala)
    if nombre == "genextreme":
        # Hosking, Wallis y Wood (1985).
        c = 2.0 / (3.0 + t3) - math.log(2.0) / math.log(3.0)
        k = 7.8590 * c + 2.9554 * c * c
        if abs(k) < 1e-8:
            escala = l2 / math.log(2.0)
            return (0.0, l1 - 0.5772156649 * escala, escala)
        gamma_k = math.gamma(1.0 + k)
        escala = l2 * k / ((1.0 - 2.0 ** -k) * gamma_k)
        ubicacion = l1 - escala * (1.0 - gamma_k) / k
        # scipy usa el signo contrario en la forma.
        return (k, ubicacion, escala)
    if nombre == "pearson3":
        # Hosking y Wallis (1997), aproximación por t3.
        absoluto = abs(t3)
        if absoluto < 1e-6:
            return (0.0, l1, l2 * math.sqrt(math.pi))
        if absoluto < 1.0 / 3.0:
            z = 3.0 * math.pi * t3 * t3
            alfa = (1.0 + 0.2906 * z) / (z + 0.1882 * z * z + 0.0442 * z ** 3)
        else:
            z = 1.0 - absoluto
            alfa = (0.36067 * z - 0.59567 * z * z + 0.25361 * z ** 3) / \
                   (1.0 - 2.78861 * z + 2.56096 * z * z - 0.77045 * z ** 3)
        if not math.isfinite(alfa) or alfa <= 0:
            raise ErrorFrecuencia(
                f"la razón de momentos-L (t3={t3:.4f}) no da un parámetro de "
                "forma utilizable."
            )
        sesgo = 2.0 / math.sqrt(alfa) * (1.0 if t3 > 0 else -1.0)
        # La razón de gammas se evalúa por diferencia de logaritmos: con sesgo
        # cercano a cero, alfa se dispara y gamma(alfa) desborda mucho antes de
        # que la división pueda cancelarlo. En logaritmos la cancelación ocurre
        # primero y el resultado tiende a l2 por raíz de pi, que es el límite
        # normal correcto.
        log_desviacion = (math.log(l2) + 0.5 * math.log(math.pi * alfa)
                          + math.lgamma(alfa) - math.lgamma(alfa + 0.5))
        return (sesgo, l1, math.exp(log_desviacion))
    raise ErrorFrecuencia(
        f"no hay relación cerrada de momentos-L para {nombre!r}."
    )


# =============================================================================
# Ajuste
# =============================================================================
def _log_positivo(x: np.ndarray) -> np.ndarray:
    if np.any(x <= 0):
        raise ErrorFrecuencia(
            "la transformación logarítmica exige valores estrictamente "
            "positivos y la serie contiene ceros o negativos."
        )
    return np.log(x)


def ajustar(datos: Iterable[float], distribucion: str, metodo: str) -> Ajuste:
    """
    Ajusta una distribución por el método pedido.

    Devuelve el resultado con su error en lugar de propagar la excepción: la
    matriz de diez distribuciones por tres métodos tiene combinaciones que no
    existen o no convergen, y el módulo debe poder reportarlas todas en lugar
    de detenerse en la primera.
    """
    x = _limpiar(datos)
    resultado = Ajuste(distribucion=distribucion, metodo=metodo, n=int(x.size))
    if x.size < 5:
        resultado.error = f"solo {x.size} dato(s)"
        return resultado
    nombre = DISTRIBUCIONES.get(distribucion)
    if nombre is None:
        resultado.error = f"distribución no reconocida: {distribucion!r}"
        return resultado

    try:
        resultado.parametros = _ajustar_nucleo(nombre, x, metodo)
    except (ErrorFrecuencia, ValueError, RuntimeError, FloatingPointError,
            OverflowError, ZeroDivisionError, TypeError) as exc:
        resultado.error = f"{type(exc).__name__}: {exc}"
    return resultado


def _ajustar_nucleo(nombre: str, x: np.ndarray, metodo: str) -> tuple[float, ...]:
    """Parámetros crudos, en la convención de scipy para cada distribución."""
    # Las variantes logarítmicas se ajustan sobre el logaritmo y conservan esa
    # convención: quien evalúa debe deshacerla, y por eso viven aparte.
    if nombre == "lognorm2":
        y = _log_positivo(x)
        return _ajustar_nucleo("norm", y, metodo)
    if nombre == "lognorm3":
        # Umbral por el estimador de Stedinger, que hace simétrico el logaritmo.
        umbral = _umbral_lognormal3(x)
        y = _log_positivo(x - umbral)
        parametros = _ajustar_nucleo("norm", y, metodo)
        return (umbral,) + tuple(parametros)
    if nombre == "logpearson3":
        y = _log_positivo(x)
        return _ajustar_nucleo("pearson3", y, metodo)

    if nombre == "genextreme_k":
        # La forma se fija y solo se estiman ubicación y escala.
        parametros = stats.genextreme.fit(x, f0=FORMA_GEV_FIJA)
        return tuple(float(p) for p in parametros)

    if metodo == "momentos_l":
        return _ajuste_momentos_l(nombre, x)

    if metodo == "momentos":
        return _ajuste_momentos(nombre, x)

    if metodo == "maxima_verosimilitud":
        distribucion = getattr(stats, nombre)
        with np.errstate(all="ignore"):
            if nombre in ("expon", "weibull_min", "gamma", "genpareto",
                          "invweibull"):
                # El soporte empieza en el origen; fijarlo evita que el ajuste
                # coloque el umbral por encima del mínimo observado y deje datos
                # con verosimilitud nula.
                parametros = distribucion.fit(x, floc=0.0)
            else:
                parametros = distribucion.fit(x)
        return tuple(float(p) for p in parametros)

    raise ErrorFrecuencia(f"método no reconocido: {metodo!r}")


def _ajuste_momentos(nombre: str, x: np.ndarray) -> tuple[float, ...]:
    """Parámetros por igualación de momentos ordinarios."""
    media = float(np.mean(x))
    desviacion = float(np.std(x, ddof=1))
    if desviacion <= 0:
        raise ErrorFrecuencia("la serie es constante.")

    if nombre == "norm":
        return (media, desviacion)
    if nombre == "expon":
        return (media - desviacion, desviacion)
    if nombre == "gumbel_r":
        escala = desviacion * math.sqrt(6.0) / math.pi
        return (media - 0.5772156649 * escala, escala)
    if nombre == "gamma":
        forma = (media / desviacion) ** 2
        return (forma, 0.0, desviacion ** 2 / media)
    if nombre == "pearson3":
        sesgo = float(stats.skew(x, bias=False))
        return (sesgo, media, desviacion)
    if nombre == "genextreme":
        # Sin relación cerrada estable: se parte de momentos-L y se refina.
        return _ajuste_momentos_l(nombre, x)
    if nombre == "weibull_min":
        def objetivo(forma):
            if forma <= 0:
                return 1e6
            uno = math.gamma(1.0 + 1.0 / forma)
            dos = math.gamma(1.0 + 2.0 / forma)
            return math.sqrt(dos - uno * uno) / uno - desviacion / media
        solucion = optimize.brentq(objetivo, 0.1, 50.0)
        escala = media / math.gamma(1.0 + 1.0 / solucion)
        return (solucion, 0.0, escala)
    raise ErrorFrecuencia(f"no hay ajuste por momentos para {nombre!r}.")


def _umbral_lognormal3(x: np.ndarray) -> float:
    """
    Umbral inferior de la lognormal de tres parámetros.

    Se usa el estimador de Stedinger, que iguala el sesgo del logaritmo a cero.
    Queda por debajo del mínimo observado por construcción, de modo que el
    logaritmo siempre está definido.
    """
    minimo = float(np.min(x))
    mediana = float(np.median(x))
    maximo = float(np.max(x))
    denominador = minimo + maximo - 2.0 * mediana
    if abs(denominador) < 1e-12:
        return minimo - 1e-6 * max(1.0, abs(minimo))
    umbral = (minimo * maximo - mediana ** 2) / denominador
    if umbral >= minimo:
        umbral = minimo - 1e-6 * max(1.0, abs(minimo))
    return float(umbral)


# =============================================================================
# Cuantiles y bondad de ajuste
# =============================================================================
def _congelada(distribucion: str, parametros: Sequence[float]):
    """Distribución de scipy lista para evaluar, en el espacio de los datos."""
    nombre = DISTRIBUCIONES[distribucion]
    if nombre in ("lognorm2", "lognorm3", "logpearson3"):
        return None
    if nombre == "genextreme_k":
        return stats.genextreme(*parametros)
    return getattr(stats, nombre)(*parametros)


def cuantiles(
    ajuste: Ajuste, periodos_retorno: Sequence[float],
) -> dict[float, float]:
    """
    Cuantil asociado a cada periodo de retorno.

    La probabilidad de no excedencia es 1 - 1/T, que es la convención para
    series de máximos anuales. Las variantes logarítmicas se evalúan en el
    espacio del logaritmo y se deshace la transformación al final.
    """
    if not ajuste.valido:
        return {}
    nombre = DISTRIBUCIONES[ajuste.distribucion]
    salida: dict[float, float] = {}
    for periodo in periodos_retorno:
        periodo = float(periodo)
        if periodo <= 1.0:
            continue
        probabilidad = 1.0 - 1.0 / periodo
        try:
            if nombre == "lognorm2":
                valor = math.exp(
                    stats.norm(*ajuste.parametros).ppf(probabilidad))
            elif nombre == "lognorm3":
                umbral = ajuste.parametros[0]
                valor = umbral + math.exp(
                    stats.norm(*ajuste.parametros[1:]).ppf(probabilidad))
            elif nombre == "logpearson3":
                valor = math.exp(
                    stats.pearson3(*ajuste.parametros).ppf(probabilidad))
            else:
                valor = float(_congelada(
                    ajuste.distribucion, ajuste.parametros).ppf(probabilidad))
        except (ValueError, OverflowError, ZeroDivisionError):
            continue
        if math.isfinite(valor):
            salida[periodo] = float(valor)
    return salida


def _cdf(ajuste: Ajuste, x: np.ndarray) -> np.ndarray | None:
    """Función de distribución acumulada evaluada en los datos."""
    nombre = DISTRIBUCIONES[ajuste.distribucion]
    try:
        if nombre == "lognorm2":
            return stats.norm(*ajuste.parametros).cdf(np.log(x))
        if nombre == "lognorm3":
            umbral = ajuste.parametros[0]
            desplazado = x - umbral
            if np.any(desplazado <= 0):
                return None
            return stats.norm(*ajuste.parametros[1:]).cdf(np.log(desplazado))
        if nombre == "logpearson3":
            return stats.pearson3(*ajuste.parametros).cdf(np.log(x))
        return _congelada(ajuste.distribucion, ajuste.parametros).cdf(x)
    except (ValueError, OverflowError):
        return None


def bondad_de_ajuste(
    ajuste: Ajuste, datos: Iterable[float], clases: int = 0,
) -> dict[str, Any]:
    """
    Pruebas de bondad y criterios de información.

    KS mide la mayor separación entre la acumulada empírica y la teórica;
    Anderson-Darling pesa más las colas, que es donde vive el periodo de
    retorno alto, y por eso ordena mejor para diseño; chi-cuadrado compara
    frecuencias por clases. AIC y BIC penalizan el número de parámetros, de modo
    que una distribución de tres parámetros no gana solo por tener más grados de
    libertad.

    El valor p de KS es aproximado: los parámetros se estimaron de la misma
    muestra, y eso lo hace optimista. Sirve para ordenar candidatas, no para
    aceptar una en términos absolutos.
    """
    x = _limpiar(datos)
    n = x.size
    if not ajuste.valido or n < 5:
        return {}
    acumulada = _cdf(ajuste, x)
    if acumulada is None or not np.all(np.isfinite(acumulada)):
        return {}
    acumulada = np.clip(acumulada, 1e-12, 1 - 1e-12)

    empirica = np.arange(1, n + 1) / n
    anterior = np.arange(0, n) / n
    ks = float(np.max(np.maximum(empirica - acumulada, acumulada - anterior)))

    indices = np.arange(1, n + 1)
    suma = np.sum((2 * indices - 1) *
                  (np.log(acumulada) + np.log(1 - acumulada[::-1])))
    ad = float(-n - suma / n)

    k = len(ajuste.parametros)
    verosimilitud = _log_verosimilitud(ajuste, x)
    salida: dict[str, Any] = {
        "ks": round(ks, 5),
        "ks_p": round(float(stats.kstwo.sf(ks, n)), 5),
        "anderson_darling": round(ad, 4),
        "n_parametros": k,
    }
    if verosimilitud is not None and math.isfinite(verosimilitud):
        salida["log_verosimilitud"] = round(verosimilitud, 3)
        salida["aic"] = round(2 * k - 2 * verosimilitud, 3)
        salida["bic"] = round(k * math.log(n) - 2 * verosimilitud, 3)

    clases = clases or max(4, int(round(1 + 3.322 * math.log10(n))))
    chi = _chi_cuadrado(acumulada, n, clases, k)
    if chi:
        salida.update(chi)
    return salida


def _log_verosimilitud(ajuste: Ajuste, x: np.ndarray) -> float | None:
    """Log-verosimilitud, con el jacobiano de la transformación logarítmica."""
    nombre = DISTRIBUCIONES[ajuste.distribucion]
    try:
        if nombre in ("lognorm2", "logpearson3"):
            y = np.log(x)
            base = (stats.norm if nombre == "lognorm2" else stats.pearson3)
            densidad = base(*ajuste.parametros).logpdf(y) - y
        elif nombre == "lognorm3":
            desplazado = x - ajuste.parametros[0]
            if np.any(desplazado <= 0):
                return None
            y = np.log(desplazado)
            densidad = stats.norm(*ajuste.parametros[1:]).logpdf(y) - y
        else:
            densidad = _congelada(
                ajuste.distribucion, ajuste.parametros).logpdf(x)
    except (ValueError, OverflowError):
        return None
    densidad = np.asarray(densidad, dtype=float)
    if not np.all(np.isfinite(densidad)):
        return None
    return float(np.sum(densidad))


def _chi_cuadrado(
    acumulada: np.ndarray, n: int, clases: int, parametros: int,
) -> dict[str, Any]:
    """Chi-cuadrado sobre clases equiprobables de la acumulada teórica."""
    bordes = np.linspace(0.0, 1.0, clases + 1)
    observado, _ = np.histogram(acumulada, bins=bordes)
    esperado = np.full(clases, n / clases)
    if np.any(esperado < 5):
        return {"chi2": None,
                "chi2_nota": f"clases con menos de 5 esperados (n={n})"}
    estadistico = float(np.sum((observado - esperado) ** 2 / esperado))
    grados = clases - 1 - parametros
    if grados < 1:
        return {"chi2": round(estadistico, 4),
                "chi2_nota": "sin grados de libertad suficientes"}
    return {
        "chi2": round(estadistico, 4),
        "chi2_p": round(float(stats.chi2.sf(estadistico, grados)), 5),
        "chi2_gl": grados,
    }


# =============================================================================
# Atípicos y posición gráfica
# =============================================================================
def grubbs_beck(datos: Iterable[float], alfa: float = 0.10) -> dict[str, Any]:
    """
    Prueba de Grubbs-Beck para atípicos BAJOS (Bulletin 17C).

    CLAUDE.md, sección 7, prohíbe aplicar IQR a la serie de máximos porque
    truncaría el dato de diseño. Lo que sí procede es esta prueba, que busca en
    la cola opuesta: un año anormalmente seco fuerza el ajuste hacia abajo y
    distorsiona precisamente el periodo de retorno alto.

    Los atípicos ALTOS no se tocan nunca: son el dato de diseño.

    Se aplica sobre el logaritmo, como el Bulletin, y solo se REPORTA. Quitarlos
    del ajuste es decisión del consultor, porque un año seco real y un error de
    registro se ven igual desde la estadística.
    """
    x = _limpiar(datos)
    n = x.size
    if n < 10:
        return {"n": n, "error": "menos de diez datos"}
    if np.any(x <= 0):
        return {"n": n, "error": "la prueba exige valores positivos"}
    y = np.log(x)
    media = float(np.mean(y))
    desviacion = float(np.std(y, ddof=1))
    if desviacion <= 0:
        return {"n": n, "error": "serie constante"}
    # Aproximación de Pilon para el estadístico K_N al 10%.
    k = (-0.9043 + 3.345 * math.sqrt(math.log10(n))
         - 0.4046 * math.log10(n))
    umbral = math.exp(media - k * desviacion)
    bajos = [float(v) for v in x if v < umbral]
    return {
        "n": n,
        "umbral_bajo": round(umbral, 2),
        "k": round(k, 4),
        "alfa": alfa,
        "atipicos_bajos": bajos,
        "cuantos": len(bajos),
    }


def densidad(ajuste: Ajuste, x: Sequence[float]):
    """
    Función de densidad evaluada en los puntos dados.

    La necesita la figura de histograma con todas las distribuciones
    superpuestas, que es la que permite ver de un vistazo cuál describe la forma
    de la muestra y no solo su cola. Las variantes logarítmicas incorporan el
    jacobiano de la transformación: sin él, la curva no integraría uno sobre el
    eje de los datos y quedaría por debajo de las demás sin que eso signifique
    peor ajuste.
    """
    if not ajuste.valido:
        return None
    puntos = np.asarray(list(x), dtype=float)
    nombre = DISTRIBUCIONES[ajuste.distribucion]
    try:
        if nombre in ("lognorm2", "logpearson3"):
            validos = puntos > 0
            salida = np.full(puntos.shape, np.nan)
            base = stats.norm if nombre == "lognorm2" else stats.pearson3
            salida[validos] = base(*ajuste.parametros).pdf(
                np.log(puntos[validos])) / puntos[validos]
            return salida
        if nombre == "lognorm3":
            umbral = ajuste.parametros[0]
            desplazado = puntos - umbral
            validos = desplazado > 0
            salida = np.full(puntos.shape, np.nan)
            salida[validos] = stats.norm(*ajuste.parametros[1:]).pdf(
                np.log(desplazado[validos])) / desplazado[validos]
            return salida
        congelada = _congelada(ajuste.distribucion, ajuste.parametros)
        return np.asarray(congelada.pdf(puntos), dtype=float)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def posicion_grafica(n: int, formula: str = "weibull") -> np.ndarray:
    """
    Probabilidad empírica de no excedencia de cada dato ordenado.

    Weibull es la posición insesgada de la frecuencia y la más usada en
    hidrología colombiana; Gringorten y Cunnane se conservan porque el consultor
    puede pedirlas para contrastar el papel de probabilidad.
    """
    i = np.arange(1, n + 1)
    nombre = formula.strip().lower()
    if nombre == "weibull":
        return i / (n + 1.0)
    if nombre == "gringorten":
        return (i - 0.44) / (n + 0.12)
    if nombre == "cunnane":
        return (i - 0.40) / (n + 0.20)
    if nombre == "hazen":
        return (i - 0.5) / n
    raise ErrorFrecuencia(f"posición gráfica no reconocida: {formula!r}")

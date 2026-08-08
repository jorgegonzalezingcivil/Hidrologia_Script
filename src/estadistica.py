#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pruebas estadísticas de series hidrológicas, compartidas por los módulos de análisis
====================================================================================
Entorno: venv del proyecto.

Sigue el mismo criterio de separación que src/graficos.py:

    src/comun/         solo librería estándar y PyYAML   ambos entornos
    src/sig.py         depende de QGIS                    módulos SIG
    src/graficos.py    depende de matplotlib              módulos de análisis
    src/estadistica.py depende de numpy y scipy           módulos de análisis

No puede importarse desde src/comun: el Python de QGIS comparte ese paquete.

Qué vive aquí. Las pruebas que el M05 usa para juzgar consistencia y que otros
módulos volverán a necesitar (el M19 para tendencias, el M18 para el balance).
Todas son funciones puras: reciben arreglos y devuelven un resultado con su
estadístico, su valor p y su lectura, sin escribir archivos ni consultar
configuración.

Sobre los valores p. Pettitt y SNHT no tienen distribución nula cerrada; se usan
las aproximaciones habituales en la literatura y se declara cuál en cada
función. Un valor p aproximado sirve para ordenar sospechas, no para cerrar una
conclusión: el M05 los reporta y el consultor decide, que es lo que CLAUDE.md,
sección 7, exige para toda decisión con margen.

Las rutinas heredadas Outlier.py e Impute.py aportaron la lógica de negocio.
Se corrigen sus defectos documentados en CLAUDE.md, sección 9:

    Outlier.py  cuartiles fuera de norma (0.08 y 0.95 en lugar de 0.25 y 0.75)
                y límite inferior que producía precipitación negativa
    Impute.py   sin validación cruzada, de modo que ningún método podía
                compararse con otro
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
from scipy import stats

__all__ = [
    "Resultado",
    "LimitesAnomalos",
    "limites_iqr",
    "limites_er",
    "marcar_anomalos",
    "pettitt",
    "snht",
    "mann_kendall",
    "rachas",
    "curva_doble_masa",
    "quiebre_doble_masa",
    "correlacion_pareada",
    "ErrorEstadistica",
]


class ErrorEstadistica(ValueError):
    """Entrada que no permite calcular la prueba pedida."""


@dataclass(frozen=True)
class Resultado:
    """
    Salida de una prueba de hipótesis.

    'hay_indicio' no afirma que exista el fenómeno: afirma que la prueba lo
    señala al nivel de significancia pedido. La distinción importa porque estas
    pruebas se aplican a decenas de estaciones y algunos rechazos serán falsos
    positivos por puro número de ensayos.
    """

    prueba: str
    estadistico: float
    valor_p: float
    hay_indicio: bool
    n: int
    detalle: dict[str, Any] = field(default_factory=dict)

    def como_dict(self) -> dict[str, Any]:
        return {
            "prueba": self.prueba,
            "estadistico": round(float(self.estadistico), 6),
            "valor_p": round(float(self.valor_p), 6),
            "hay_indicio": bool(self.hay_indicio),
            "n": int(self.n),
            **{k: v for k, v in self.detalle.items()},
        }


@dataclass(frozen=True)
class LimitesAnomalos:
    """Límites de aceptación y el criterio con que se obtuvieron."""

    metodo: str
    inferior: float
    superior: float
    recortado_en_minimo: bool = False


def _limpiar(valores: Iterable[float]) -> np.ndarray:
    """Arreglo de flotantes sin nulos, listo para calcular."""
    arreglo = np.asarray(list(valores), dtype=float)
    return arreglo[np.isfinite(arreglo)]


# =============================================================================
# Detección de anómalos
# =============================================================================
def limites_iqr(
    valores: Iterable[float],
    q1: float = 0.25,
    q3: float = 0.75,
    factor: float = 1.5,
    valor_minimo: float | None = None,
) -> LimitesAnomalos:
    """
    Límites por rango intercuartílico.

    Los cuartiles por defecto son los normativos, 0.25 y 0.75. La rutina
    heredada usaba 0.08 y 0.95, que ensanchan tanto el rango que casi nada
    resulta anómalo: con ellos la prueba deja de filtrar.

    'valor_minimo' recorta el límite inferior. En precipitación el límite
    calculado suele ser negativo, y un valor negativo no marca nada porque
    ninguna lluvia lo alcanza; peor aún, si el tratamiento es 'cap', la rutina
    heredada escribía ese negativo en la serie. Recortarlo a cero deja el
    criterio en lo único que tiene sentido físico.
    """
    datos = _limpiar(valores)
    if datos.size < 4:
        raise ErrorEstadistica(
            f"el rango intercuartílico necesita al menos cuatro datos y se "
            f"recibieron {datos.size}."
        )
    primero, tercero = np.quantile(datos, [q1, q3])
    rango = tercero - primero
    inferior = float(primero - factor * rango)
    superior = float(tercero + factor * rango)
    recortado = valor_minimo is not None and inferior < valor_minimo
    if recortado:
        inferior = float(valor_minimo)
    return LimitesAnomalos("IQR", inferior, superior, recortado)


def limites_er(
    valores: Iterable[float],
    k: float = 3.0,
    valor_minimo: float | None = None,
) -> LimitesAnomalos:
    """
    Límites por regla empírica, media más o menos k desviaciones.

    Supone simetría, que la precipitación mensual no tiene: su distribución es
    asimétrica a la derecha. Se conserva porque el consultor puede pedirlo y
    porque sirve de contraste, pero el IQR es el criterio por defecto y la razón
    está en esa asimetría.
    """
    datos = _limpiar(valores)
    if datos.size < 2:
        raise ErrorEstadistica(
            f"la regla empírica necesita al menos dos datos y se recibieron "
            f"{datos.size}."
        )
    media = float(np.mean(datos))
    desviacion = float(np.std(datos, ddof=1))
    inferior = media - k * desviacion
    superior = media + k * desviacion
    recortado = valor_minimo is not None and inferior < valor_minimo
    if recortado:
        inferior = float(valor_minimo)
    return LimitesAnomalos("ER", inferior, superior, recortado)


def limites_zscore(
    valores: Iterable[float],
    umbral: float = 3.0,
    valor_minimo: float | None = None,
) -> LimitesAnomalos:
    """
    Límites por puntuación z.

    Con el mismo factor, coincide exactamente con la regla empírica: ambas son
    media más o menos k desviaciones. Se mantiene separada porque el consultor
    declara umbrales distintos para cada una.
    """
    limites = limites_er(valores, k=umbral, valor_minimo=valor_minimo)
    return LimitesAnomalos("ZSCORE", limites.inferior, limites.superior,
                           limites.recortado_en_minimo)


def marcar_anomalos(
    valores: Sequence[float], limites: LimitesAnomalos,
) -> np.ndarray:
    """
    Máscara de los valores fuera de los límites. Los nulos nunca se marcan.

    Marcar y no eliminar es lo predeterminado (config: anomalos.tratamiento).
    Un dato anómalo puede ser un error de transcripción o una tormenta real, y
    la diferencia no la resuelve la estadística: la resuelve el consultor
    mirando el registro.
    """
    arreglo = np.asarray(valores, dtype=float)
    fuera = (arreglo < limites.inferior) | (arreglo > limites.superior)
    return fuera & np.isfinite(arreglo)


# =============================================================================
# Homogeneidad y tendencia
# =============================================================================
def pettitt(valores: Iterable[float], alfa: float = 0.05) -> Resultado:
    """
    Prueba de Pettitt: busca UN cambio abrupto en la mediana.

    No paramétrica, basada en el estadístico de Mann-Whitney acumulado. Detecta
    el punto donde la serie se parte mejor en dos poblaciones distintas, que en
    una serie de precipitación suele corresponder a un traslado de la estación o
    a un cambio de observador.

    El valor p es la aproximación de Pettitt (1979), válida para n moderado y
    conservadora en las colas. Se declara porque no es exacta.
    """
    datos = _limpiar(valores)
    n = datos.size
    if n < 10:
        raise ErrorEstadistica(
            f"la prueba de Pettitt necesita al menos diez datos y se recibieron {n}."
        )
    # U_k acumulado a partir del signo de todas las parejas.
    signos = np.sign(datos[:, None] - datos[None, :])
    acumulado = np.cumsum(signos.sum(axis=1))
    absolutos = np.abs(acumulado)
    posicion = int(np.argmax(absolutos))
    k = float(absolutos[posicion])
    valor_p = float(min(1.0, 2.0 * np.exp(-6.0 * k ** 2 / (n ** 3 + n ** 2))))
    return Resultado(
        "pettitt", k, valor_p, valor_p < alfa, n,
        {"indice_quiebre": posicion,
         "media_antes": float(np.mean(datos[:posicion + 1])),
         "media_despues": float(np.mean(datos[posicion + 1:]))
         if posicion + 1 < n else float("nan")},
    )


def snht(valores: Iterable[float], alfa: float = 0.05) -> Resultado:
    """
    Prueba normal estándar de homogeneidad (Alexandersson, 1986).

    Compara la media de los primeros k datos, tipificados, con la del resto.
    Es más sensible que Pettitt cerca de los extremos de la serie y menos en el
    centro, de modo que ambas se reportan juntas: coincidir refuerza el indicio.

    El valor crítico se interpola de la tabla de Khaliq y Ouarda (2007), que es
    la referencia habitual. No hay valor p cerrado: se devuelve el nivel
    alcanzado de forma aproximada a partir de los críticos tabulados, y por eso
    la lectura de esta prueba debe apoyarse en el estadístico, no en el p.
    """
    datos = _limpiar(valores)
    n = datos.size
    if n < 10:
        raise ErrorEstadistica(
            f"la prueba SNHT necesita al menos diez datos y se recibieron {n}."
        )
    desviacion = float(np.std(datos, ddof=1))
    if desviacion == 0:
        raise ErrorEstadistica(
            "la serie es constante: la prueba SNHT no está definida."
        )
    tipificada = (datos - float(np.mean(datos))) / desviacion
    k = np.arange(1, n)
    suma_izquierda = np.cumsum(tipificada)[:-1]
    z1 = suma_izquierda / k
    z2 = (np.sum(tipificada) - suma_izquierda) / (n - k)
    serie_t = k * z1 ** 2 + (n - k) * z2 ** 2
    posicion = int(np.argmax(serie_t))
    estadistico = float(serie_t[posicion])
    critico = _critico_snht(n, alfa)
    return Resultado(
        "snht", estadistico, float("nan"), estadistico > critico, n,
        {"indice_quiebre": posicion + 1, "critico": round(critico, 3),
         "alfa": alfa},
    )


# Valores críticos de SNHT al 5% (Khaliq y Ouarda, 2007). Fuera de rango se
# interpola linealmente y por encima del mayor tamaño se usa el último valor,
# que es conservador porque el crítico crece muy despacio con n.
_CRITICOS_SNHT_5 = {
    10: 5.05, 12: 5.70, 14: 6.09, 16: 6.35, 18: 6.66, 20: 6.95, 30: 7.65,
    40: 8.10, 50: 8.45, 70: 8.80, 100: 9.15, 150: 9.55, 200: 9.75, 500: 10.55,
}
_CRITICOS_SNHT_1 = {
    10: 7.60, 12: 8.10, 14: 8.45, 16: 8.80, 18: 9.10, 20: 9.35, 30: 10.45,
    40: 11.01, 50: 11.38, 70: 11.89, 100: 12.32, 150: 12.66, 200: 12.90,
    500: 13.60,
}


def _critico_snht(n: int, alfa: float) -> float:
    """Interpola el valor crítico tabulado para el tamaño de muestra."""
    tabla = _CRITICOS_SNHT_1 if alfa <= 0.01 else _CRITICOS_SNHT_5
    tamanos = sorted(tabla)
    if n <= tamanos[0]:
        return tabla[tamanos[0]]
    if n >= tamanos[-1]:
        return tabla[tamanos[-1]]
    return float(np.interp(n, tamanos, [tabla[t] for t in tamanos]))


def mann_kendall(valores: Iterable[float], alfa: float = 0.05) -> Resultado:
    """
    Prueba de Mann-Kendall: tendencia monótona, con corrección por empates.

    No paramétrica y sin supuesto de distribución, que es lo que la hace
    apropiada para precipitación. Se acompaña de la pendiente de Sen, que es el
    tamaño del efecto: una tendencia significativa de 0,2 mm por año no cambia
    ningún diseño, y sin la pendiente el valor p solo no lo diría.
    """
    datos = _limpiar(valores)
    n = datos.size
    if n < 8:
        raise ErrorEstadistica(
            f"Mann-Kendall necesita al menos ocho datos y se recibieron {n}."
        )
    signos = np.sign(datos[None, :] - datos[:, None])
    s = float(np.sum(np.triu(signos, k=1)))

    _, repeticiones = np.unique(datos, return_counts=True)
    empates = repeticiones[repeticiones > 1]
    correccion = float(np.sum(empates * (empates - 1) * (2 * empates + 5)))
    varianza = (n * (n - 1) * (2 * n + 5) - correccion) / 18.0
    if varianza <= 0:
        raise ErrorEstadistica("varianza nula: la serie no admite Mann-Kendall.")

    if s > 0:
        z = (s - 1) / np.sqrt(varianza)
    elif s < 0:
        z = (s + 1) / np.sqrt(varianza)
    else:
        z = 0.0
    valor_p = float(2 * (1 - stats.norm.cdf(abs(z))))

    return Resultado(
        "mann_kendall", float(z), valor_p, valor_p < alfa, n,
        {"s": s, "pendiente_sen": _pendiente_sen(datos),
         "sentido": "creciente" if z > 0 else ("decreciente" if z < 0 else "sin tendencia")},
    )


def _pendiente_sen(datos: np.ndarray) -> float:
    """Mediana de las pendientes entre todas las parejas de puntos."""
    n = datos.size
    indices = np.arange(n)
    numerador = datos[None, :] - datos[:, None]
    denominador = indices[None, :] - indices[:, None]
    superior = np.triu_indices(n, k=1)
    pendientes = numerador[superior] / denominador[superior]
    return float(np.median(pendientes))


def rachas(valores: Iterable[float], alfa: float = 0.05) -> Resultado:
    """
    Prueba de rachas de Wald-Wolfowitz sobre la mediana: aleatoriedad.

    Detecta persistencia (pocas rachas, valores agrupados por encima y por
    debajo) o alternancia excesiva. En precipitación mensual la persistencia
    suele venir de ciclos climáticos y no de un defecto del dato, de modo que un
    rechazo aquí NO es motivo de descarte por sí solo: se reporta junto a las
    otras pruebas.

    Los valores iguales a la mediana se descartan, que es la convención de la
    prueba: no pertenecen a ninguno de los dos grupos.
    """
    datos = _limpiar(valores)
    if datos.size < 10:
        raise ErrorEstadistica(
            f"la prueba de rachas necesita al menos diez datos y se recibieron "
            f"{datos.size}."
        )
    mediana = float(np.median(datos))
    signos = datos[datos != mediana] > mediana
    n = signos.size
    arriba = int(np.sum(signos))
    abajo = n - arriba
    if arriba == 0 or abajo == 0:
        raise ErrorEstadistica(
            "todos los datos quedan al mismo lado de la mediana: la prueba de "
            "rachas no está definida."
        )
    observadas = int(1 + np.sum(signos[1:] != signos[:-1]))
    esperadas = 1 + 2.0 * arriba * abajo / n
    varianza = (2.0 * arriba * abajo * (2.0 * arriba * abajo - n)) / \
               (n ** 2 * (n - 1))
    if varianza <= 0:
        raise ErrorEstadistica("varianza nula: la prueba de rachas no aplica.")
    z = (observadas - esperadas) / np.sqrt(varianza)
    valor_p = float(2 * (1 - stats.norm.cdf(abs(z))))
    return Resultado(
        "rachas", float(z), valor_p, valor_p < alfa, n,
        {"rachas_observadas": observadas,
         "rachas_esperadas": round(float(esperadas), 3),
         "sobre_mediana": arriba, "bajo_mediana": abajo,
         "lectura": "persistencia" if z < 0 else "alternancia"},
    )


# =============================================================================
# Consistencia entre estaciones
# =============================================================================
def curva_doble_masa(
    estacion: Sequence[float], patron: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    """
    Acumulados de la estación y del patrón, sobre los periodos comunes.

    El patrón es el promedio de las vecinas. Solo se acumulan los periodos en
    que ambos tienen dato: acumular con huecos desplaza la curva y produce
    quiebres que no existen, que es el error más común al aplicar este método.
    """
    a = np.asarray(estacion, dtype=float)
    b = np.asarray(patron, dtype=float)
    if a.size != b.size:
        raise ErrorEstadistica(
            f"la estación tiene {a.size} periodos y el patrón {b.size}."
        )
    comunes = np.isfinite(a) & np.isfinite(b)
    return np.cumsum(a[comunes]), np.cumsum(b[comunes])


def quiebre_doble_masa(
    acumulado_estacion: Sequence[float],
    acumulado_patron: Sequence[float],
    minimo_por_tramo: int = 5,
) -> dict[str, Any]:
    """
    Punto donde la curva de doble masa cambia de pendiente.

    Se prueba cada corte posible y se elige el que minimiza el error cuadrático
    de dos rectas ajustadas por separado. La razón entre pendientes es el factor
    de corrección que el consultor aplicaría si decidiera homogeneizar: mayor
    que uno significa que la estación registró más que el patrón después del
    quiebre.

    Devuelve razón 1.0 y sin quiebre cuando no hay datos suficientes para
    partir la curva, en lugar de inventar un corte.
    """
    x = np.asarray(acumulado_patron, dtype=float)
    y = np.asarray(acumulado_estacion, dtype=float)
    n = x.size
    if n < 2 * minimo_por_tramo:
        return {"hay_quiebre": False, "indice": None, "razon_pendientes": 1.0,
                "motivo": f"solo {n} periodo(s) comunes"}

    mejor = None
    for corte in range(minimo_por_tramo, n - minimo_por_tramo + 1):
        error = 0.0
        pendientes = []
        for xs, ys in ((x[:corte], y[:corte]), (x[corte:], y[corte:])):
            pendiente, intercepto = np.polyfit(xs, ys, 1)
            error += float(np.sum((ys - (pendiente * xs + intercepto)) ** 2))
            pendientes.append(float(pendiente))
        if mejor is None or error < mejor[0]:
            mejor = (error, corte, pendientes)

    _, corte, (antes, despues) = mejor
    razon = float(despues / antes) if antes else float("nan")
    pendiente_global, _ = np.polyfit(x, y, 1)
    error_global = float(np.sum(
        (y - np.polyval(np.polyfit(x, y, 1), x)) ** 2))
    mejora = 1.0 - (mejor[0] / error_global) if error_global > 0 else 0.0
    return {
        "hay_quiebre": bool(abs(razon - 1.0) > 0.10 and mejora > 0.05),
        "indice": corte,
        "pendiente_antes": round(antes, 6),
        "pendiente_despues": round(despues, 6),
        "razon_pendientes": round(razon, 4),
        "pendiente_global": round(float(pendiente_global), 6),
        "mejora_ajuste": round(float(mejora), 4),
    }


def correlacion_pareada(
    a: Sequence[float], b: Sequence[float], minimo_comun: int = 12,
) -> tuple[float, int]:
    """
    Correlación de Pearson sobre los periodos con dato en ambas series.

    Devuelve también cuántos periodos la sustentan. Una correlación de 0,95
    calculada sobre seis meses no dice lo mismo que la misma cifra sobre veinte
    años, y el conteo es lo único que permite distinguirlas.
    """
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    comunes = np.isfinite(x) & np.isfinite(y)
    cuantos = int(np.sum(comunes))
    if cuantos < minimo_comun:
        return float("nan"), cuantos
    if np.std(x[comunes]) == 0 or np.std(y[comunes]) == 0:
        return float("nan"), cuantos
    return float(np.corrcoef(x[comunes], y[comunes])[0, 1]), cuantos

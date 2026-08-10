# -*- coding: utf-8 -*-
"""
comun.geometria
===============
Operaciones geométricas mínimas con librería estándar.

Doctrina (CLAUDE.md, sección 3): los módulos de análisis corren en el venv, que
no tiene librerías geoespaciales. Cuando uno de ellos necesita una operación
espacial elemental, la alternativa a este archivo sería arrastrar la pila SIG al
segundo entorno o mover el módulo al de QGIS. Ninguna de las dos compensa para
una prueba de punto en polígono.

Alcance deliberadamente limitado: lectura de polígonos en WKT y punto en
polígono. No hay reproyección, ni intersecciones, ni buffers. Todo eso pertenece
al entorno de QGIS, y el polígono debe llegar aquí ya en el sistema de
referencia en que están los puntos a evaluar.
"""

from __future__ import annotations

import re
from typing import Sequence

from .errores import ErrorFormato

__all__ = [
    "Anillo",
    "Poligono",
    "poligonos_de_wkt",
    "punto_en_poligono",
    "punto_en_alguno",
    "envolvente",
]

# Un anillo es una secuencia de vértices; un polígono, su anillo exterior
# seguido de los interiores (huecos).
Anillo = list[tuple[float, float]]
Poligono = list[Anillo]

_NUMERO = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def _vertices(texto: str) -> Anillo:
    """Interpreta la lista de vértices de un anillo en WKT."""
    pares = re.findall(rf"({_NUMERO})\s+({_NUMERO})", texto)
    if len(pares) < 3:
        raise ErrorFormato(
            f"Un anillo necesita al menos tres vértices y se leyeron {len(pares)}."
        )
    return [(float(x), float(y)) for x, y in pares]


def poligonos_de_wkt(wkt: str) -> list[Poligono]:
    """
    Interpreta un WKT de tipo POLYGON o MULTIPOLYGON.

    Devuelve una lista de polígonos, cada uno con su anillo exterior en primera
    posición y sus huecos a continuación. Ignora la dimensión Z o M si aparece,
    quedándose con las dos primeras coordenadas de cada vértice.

    Excepciones
    -----------
    ErrorFormato
        Si el texto no es un POLYGON ni un MULTIPOLYGON reconocible.
    """
    if not isinstance(wkt, str) or not wkt.strip():
        raise ErrorFormato("El WKT está vacío.")

    texto = wkt.strip()
    tipo = texto.split("(", 1)[0].strip().upper().replace(" Z", "").replace(" M", "")

    if tipo not in ("POLYGON", "MULTIPOLYGON"):
        raise ErrorFormato(
            f"Se esperaba POLYGON o MULTIPOLYGON y se recibió {tipo!r}."
        )

    # Los anillos son el contenido de cada pareja de paréntesis más interna.
    anillos_crudos = re.findall(r"\(([^()]*)\)", texto)
    if not anillos_crudos:
        raise ErrorFormato("El WKT no contiene ningún anillo.")

    anillos = [_vertices(crudo) for crudo in anillos_crudos]

    if tipo == "POLYGON":
        return [anillos]

    # En un MULTIPOLYGON hay que repartir los anillos entre polígonos. Se usa la
    # estructura de paréntesis: cada '((' abre un polígono nuevo.
    poligonos: list[Poligono] = []
    indice = 0
    for bloque in re.findall(r"\(\((?:[^()]|\([^()]*\))*\)\)", texto):
        cuantos = len(re.findall(r"\(([^()]*)\)", bloque))
        poligonos.append(anillos[indice:indice + cuantos])
        indice += cuantos

    if not poligonos:  # WKT con una forma inesperada pero anillos legibles
        return [[anillo] for anillo in anillos]
    return poligonos


def punto_en_poligono(x: float, y: float, poligono: Poligono) -> bool:
    """
    Indica si un punto cae dentro de un polígono, descontando sus huecos.

    Usa el algoritmo de cruces (lanzamiento de rayo). Un punto exactamente sobre
    el borde puede resolverse a un lado u otro según el redondeo en coma
    flotante; para seleccionar estaciones esa ambigüedad es irrelevante, y quien
    necesite una respuesta estable en el borde debe aplicar una tolerancia
    explícita antes de llamar.
    """
    if not poligono:
        return False

    if not _dentro_del_anillo(x, y, poligono[0]):
        return False
    return not any(_dentro_del_anillo(x, y, hueco) for hueco in poligono[1:])


def _dentro_del_anillo(x: float, y: float, anillo: Anillo) -> bool:
    dentro = False
    total = len(anillo)
    for indice in range(total):
        x1, y1 = anillo[indice]
        x2, y2 = anillo[(indice + 1) % total]
        if (y1 > y) != (y2 > y):
            if y2 != y1:
                corte = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                if x < corte:
                    dentro = not dentro
    return dentro


def punto_en_alguno(x: float, y: float, poligonos: Sequence[Poligono]) -> bool:
    """Indica si el punto cae en alguno de los polígonos."""
    return any(punto_en_poligono(x, y, poligono) for poligono in poligonos)


def envolvente(poligonos: Sequence[Poligono]) -> tuple[float, float, float, float]:
    """
    Devuelve (x_min, y_min, x_max, y_max) del conjunto.

    Sirve como filtro previo barato: comprobar la envolvente antes del punto en
    polígono descarta de inmediato la mayoría de los candidatos cuando el área
    es pequeña frente al catálogo.
    """
    xs: list[float] = []
    ys: list[float] = []
    for poligono in poligonos:
        for anillo in poligono:
            for x, y in anillo:
                xs.append(x)
                ys.append(y)
    if not xs:
        raise ErrorFormato("No hay vértices de los que obtener una envolvente.")
    return (min(xs), min(ys), max(xs), max(ys))


# =============================================================================
# Rasterización por barrido
# =============================================================================
def aristas_de(poligonos: Sequence[Poligono]) -> list[tuple[float, float, float, float]]:
    """
    Reduce un conjunto de polígonos a la lista de aristas que cruza un barrido.

    Cada arista se devuelve como (y_inferior, y_superior, x_en_y_inferior,
    pendiente_dx_dy), ya normalizada de abajo arriba y sin las horizontales,
    que no aportan cruces.

    Se prepara una sola vez y se reutiliza en todas las filas del ráster. Sin
    ese paso previo, cruzar un DEM de nueve mil filas con una cuenca de decenas
    de miles de vértices recorrería la geometría completa nueve mil veces.

    Los huecos NO se tratan aparte. Con la regla de paridad, un anillo interior
    invierte el estado de dentro y fuera por sí solo, con independencia de su
    sentido de giro, que es justo lo que hace falta y lo que ahorra distinguir
    anillo exterior de isla.
    """
    aristas: list[tuple[float, float, float, float]] = []
    for poligono in poligonos:
        for anillo in poligono:
            total = len(anillo)
            if total < 2:
                continue
            for indice in range(total):
                x1, y1 = anillo[indice]
                x2, y2 = anillo[(indice + 1) % total]
                if y1 == y2:
                    continue
                if y1 > y2:
                    x1, y1, x2, y2 = x2, y2, x1, y1
                aristas.append((y1, y2, x1, (x2 - x1) / (y2 - y1)))
    return aristas


class IndicePoligonos:
    """
    Índice de aristas por banda horizontal, para consultar muchos puntos.

    'punto_en_alguno' recorre todos los vértices en cada consulta. Con una
    cuenca de tres mil vértices y una red de drenaje de cien mil segmentos eso
    son cientos de millones de operaciones, y el módulo tardaba minutos en un
    cálculo que es de segundos.

    El índice reparte las aristas en bandas de igual altura y consulta solo la
    banda del punto. La respuesta es idéntica a la de 'punto_en_alguno': misma
    regla de paridad, mismos huecos descontados.
    """

    def __init__(self, poligonos: Sequence[Poligono], bandas: int = 512) -> None:
        self._aristas = aristas_de(poligonos)
        if not self._aristas:
            raise ErrorFormato("no hay aristas con las que construir el índice.")
        self.y_min = min(a[0] for a in self._aristas)
        self.y_max = max(a[1] for a in self._aristas)
        self.x_min = min(min(a[2], a[2] + a[3] * (a[1] - a[0]))
                         for a in self._aristas)
        self.x_max = max(max(a[2], a[2] + a[3] * (a[1] - a[0]))
                         for a in self._aristas)
        self._bandas = max(1, int(bandas))
        altura = self.y_max - self.y_min
        self._alto_banda = altura / self._bandas if altura > 0 else 1.0
        self._por_banda: list[list[tuple[float, float, float, float]]] = [
            [] for _ in range(self._bandas)]
        for arista in self._aristas:
            desde = self._banda(arista[0])
            hasta = self._banda(arista[1])
            for indice in range(desde, hasta + 1):
                self._por_banda[indice].append(arista)

    def _banda(self, y: float) -> int:
        indice = int((y - self.y_min) / self._alto_banda)
        return min(max(indice, 0), self._bandas - 1)

    def contiene(self, x: float, y: float) -> bool:
        """Indica si el punto cae dentro del conjunto, huecos descontados."""
        if not (self.y_min <= y < self.y_max
                and self.x_min <= x <= self.x_max):
            return False
        cruces = 0
        for y1, y2, x1, pendiente in self._por_banda[self._banda(y)]:
            if y1 <= y < y2 and x < x1 + (y - y1) * pendiente:
                cruces += 1
        return cruces % 2 == 1


def tramos_de_barrido(
    aristas: Sequence[tuple[float, float, float, float]], y: float
) -> list[tuple[float, float]]:
    """
    Devuelve los tramos [x_inicio, x_fin) que la ordenada 'y' recorre dentro.

    La convención de mitad abierta en el eje vertical (y_inferior <= y <
    y_superior) es la que impide contar dos veces un vértice compartido por dos
    aristas: sin ella, una cuenca con un pico exacto sobre el centro de una
    fila abriría o cerraría un tramo de más y dejaría una banda espuria de
    celdas de ancho arbitrario.
    """
    cortes = sorted(
        x1 + (y - y1) * pendiente
        for y1, y2, x1, pendiente in aristas
        if y1 <= y < y2
    )
    return [(cortes[i], cortes[i + 1]) for i in range(0, len(cortes) - 1, 2)]

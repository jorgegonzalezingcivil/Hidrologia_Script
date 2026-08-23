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

import math
import re
from typing import Any, Sequence

from .errores import ErrorFormato

__all__ = [
    "Anillo",
    "Poligono",
    "poligonos_de_wkt",
    "punto_en_poligono",
    "punto_en_alguno",
    "envolvente",
    "segmentos_de_frontera",
    "cadenas_de_frontera",
    "perimetro_exterior",
    "centroide",
    "columnas_de_fila",
    "IndiceEtiquetado",
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


def centroide(poligono: Poligono) -> tuple[float, float]:
    """
    Centroide de área de un polígono, descontando sus huecos.

    Se obtiene con la fórmula de Gauss sobre cada anillo, ponderando por el área
    con signo: un anillo interior aporta área negativa y desplaza el centroide
    lejos del hueco, que es lo correcto.

    NO es el centro de la envolvente. Sobre una forma alargada o curvada los dos
    difieren, y el de la envolvente puede caer fuera del propio polígono.

    Si el polígono degenera y encierra área nula, se devuelve la media de sus
    vértices: es lo único que queda, y sigue estando dentro de su extensión.

    Excepciones
    -----------
    ErrorFormato
        Si no hay ningún vértice.
    """
    x_total = y_total = area_total = 0.0
    vertices: list[tuple[float, float]] = []

    for anillo in poligono:
        vertices.extend(anillo)
        area = x_parcial = y_parcial = 0.0
        for uno, otro in zip(anillo, list(anillo[1:]) + [anillo[0]]):
            cruz = uno[0] * otro[1] - otro[0] * uno[1]
            area += cruz
            x_parcial += (uno[0] + otro[0]) * cruz
            y_parcial += (uno[1] + otro[1]) * cruz
        if area:
            x_total += x_parcial / 6.0
            y_total += y_parcial / 6.0
            area_total += area / 2.0

    if not vertices:
        raise ErrorFormato("el polígono no tiene ningún vértice.")
    if area_total == 0.0:
        return (sum(v[0] for v in vertices) / len(vertices),
                sum(v[1] for v in vertices) / len(vertices))
    return (x_total / area_total, y_total / area_total)


def columnas_de_fila(
    aristas: Sequence[tuple[float, float, float, float]],
    y: float,
    origen_x: float,
    tamano_x: float,
    ancho: int,
) -> list[tuple[int, int]]:
    """
    Rangos de columnas de una fila de ráster que caen dentro de la geometría.

    Devuelve pares (desde, hasta), ambos incluidos.

    LA CELDA SE RESUELVE POR SU CENTRO, que es la convención de estadística
    zonal de GDAL y de QGIS. El criterio alternativo, celda tocada, inflaría el
    área en un borde de media celda alrededor de todo el contorno. Vive aquí y
    no en cada módulo porque es un convenio, y un convenio duplicado es un
    convenio que acaba divergiendo: dos módulos que midieran la misma cuenca con
    criterios distintos darían áreas distintas sin que nada lo señalara.
    """
    rangos: list[tuple[int, int]] = []
    for x_inicio, x_fin in tramos_de_barrido(aristas, y):
        desde = math.ceil((x_inicio - origen_x) / tamano_x - 0.5)
        hasta = math.ceil((x_fin - origen_x) / tamano_x - 0.5) - 1
        desde = max(int(desde), 0)
        hasta = min(int(hasta), ancho - 1)
        if hasta >= desde:
            rangos.append((desde, hasta))
    return rangos


class IndiceEtiquetado:
    """
    Índice que responde QUÉ polígono contiene un punto, no solo si alguno lo hace.

    'IndicePoligonos' funde todas las aristas en un único conjunto y contesta sí
    o no. Para cruzar una capa de coberturas con una malla de muestreo eso no
    basta: hace falta saber cuál de las nueve mil entidades es, para leer su
    clase.

    Se indexa por REJILLA sobre la envolvente de cada polígono, y no por bandas
    horizontales. Las coberturas Corine son miles de polígonos pequeños
    repartidos por el área; con bandas, cada consulta recorrería las aristas de
    toda una franja del mapa, mientras que con rejilla solo se prueban los pocos
    cuya envolvente toca la celda del punto.
    """

    def __init__(self, poligonos: Sequence[Poligono], celdas: int = 256) -> None:
        self._poligonos = [list(p) for p in poligonos]
        if not self._poligonos:
            raise ErrorFormato("no hay polígonos con los que construir el índice.")

        self._cajas: list[tuple[float, float, float, float]] = []
        for poligono in self._poligonos:
            self._cajas.append(envolvente([poligono]))
        self.x_min = min(c[0] for c in self._cajas)
        self.y_min = min(c[1] for c in self._cajas)
        self.x_max = max(c[2] for c in self._cajas)
        self.y_max = max(c[3] for c in self._cajas)

        self._n = max(1, int(celdas))
        self._ancho = max((self.x_max - self.x_min) / self._n, 1e-9)
        self._alto = max((self.y_max - self.y_min) / self._n, 1e-9)
        self._rejilla: dict[tuple[int, int], list[int]] = {}
        for indice, (xmin, ymin, xmax, ymax) in enumerate(self._cajas):
            for columna in range(self._columna(xmin), self._columna(xmax) + 1):
                for fila in range(self._fila(ymin), self._fila(ymax) + 1):
                    self._rejilla.setdefault((columna, fila), []).append(indice)

    def _columna(self, x: float) -> int:
        return min(max(int((x - self.x_min) / self._ancho), 0), self._n - 1)

    def _fila(self, y: float) -> int:
        return min(max(int((y - self.y_min) / self._alto), 0), self._n - 1)

    def indice_en(self, x: float, y: float) -> int | None:
        """
        Posición del polígono que contiene el punto, o None si ninguno lo hace.

        Con solapes devuelve el primero en el orden de la capa. Una cobertura
        bien construida no los tiene, y resolverlos por área exigiría intersecar
        geometría, que no pertenece a este entorno.
        """
        if not (self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max):
            return None
        for indice in self._rejilla.get((self._columna(x), self._fila(y)), ()):
            xmin, ymin, xmax, ymax = self._cajas[indice]
            if not (xmin <= x <= xmax and ymin <= y <= ymax):
                continue
            if punto_en_poligono(x, y, self._poligonos[indice]):
                return indice
        return None


def segmentos_de_frontera(
    poligonos: Sequence[Poligono], tolerancia_m: float = 0.01,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """
    Aristas que pertenecen a una sola pieza del mosaico.

    En una cobertura sin huecos ni solapes cada linde interior aparece dos
    veces, una por cada pieza que lo comparte, y cada tramo del contorno una
    sola. Se devuelven las que aparecen una vez, con sus coordenadas.
    """
    escala = 1.0 / tolerancia_m if tolerancia_m > 0 else 1.0
    cuenta: dict = {}
    coordenadas: dict = {}
    for poligono in poligonos:
        for anillo in poligono:
            for uno, otro in zip(anillo, anillo[1:]):
                izquierda = (round(uno[0] * escala), round(uno[1] * escala))
                derecha = (round(otro[0] * escala), round(otro[1] * escala))
                if izquierda == derecha:
                    continue
                clave = ((izquierda, derecha) if izquierda < derecha
                         else (derecha, izquierda))
                cuenta[clave] = cuenta.get(clave, 0) + 1
                coordenadas.setdefault(clave, (uno, otro))
    return [coordenadas[clave] for clave, veces in cuenta.items()
            if veces == 1]


def cadenas_de_frontera(
    segmentos: Sequence[tuple[tuple[float, float], tuple[float, float]]],
    tolerancia_m: float = 0.01,
) -> list[Anillo]:
    """
    Encadena aristas sueltas en recorridos, de mayor a menor longitud.

    Se recorre por ARISTAS y no por nodos: en los nodos donde concurren tres,
    marcar el nodo como visto cortaria las dos cadenas restantes.
    """
    escala = 1.0 / tolerancia_m if tolerancia_m > 0 else 1.0

    def clave(punto):
        return (round(punto[0] * escala), round(punto[1] * escala))

    adyacencia: dict = {}
    for indice, (uno, otro) in enumerate(segmentos):
        adyacencia.setdefault(clave(uno), []).append((indice, otro))
        adyacencia.setdefault(clave(otro), []).append((indice, uno))

    usadas: set[int] = set()
    cadenas: list[Anillo] = []
    for indice, (uno, _otro) in enumerate(segmentos):
        if indice in usadas:
            continue
        cadena = [uno]
        nodo = clave(uno)
        while True:
            siguiente = next((t for t in adyacencia.get(nodo, [])
                              if t[0] not in usadas), None)
            if siguiente is None:
                break
            usadas.add(siguiente[0])
            cadena.append(siguiente[1])
            nodo = clave(siguiente[1])
        if len(cadena) > 2:
            cadenas.append(cadena)

    cadenas.sort(key=_longitud_de_cadena, reverse=True)
    return cadenas


def _longitud_de_cadena(cadena: Anillo) -> float:
    """Longitud recorrida por una polilínea."""
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(cadena, cadena[1:]))


def _area_con_signo(cadena: Anillo) -> float:
    """Área con signo del recorrido, cerrándolo si hace falta."""
    puntos = list(cadena)
    if puntos[0] != puntos[-1]:
        puntos.append(puntos[0])
    total = 0.0
    for uno, otro in zip(puntos, puntos[1:]):
        total += uno[0] * otro[1] - otro[0] * uno[1]
    return total / 2.0


def perimetro_exterior(
    poligonos: Sequence[Poligono], tolerancia_m: float = 0.01,
) -> dict[str, Any]:
    """
    Perímetro del contorno de un mosaico de polígonos contiguos.

    Sumar el perímetro de cada pieza NO da el perímetro del conjunto: cada linde
    interior se cuenta dos veces. Sobre las 125 subcuencas de este estudio la
    suma da 1.002,6 km y el contorno real 119,5 km.

    El método es de conteo, no de geometría: en un mosaico sin huecos ni solapes
    cada linde interior aparece exactamente DOS veces y cada tramo del contorno
    una sola.

    CONTAR LAS ARISTAS NO BASTA. Dos piezas vecinas pueden describir el MISMO
    linde con distinto número de vértices: una lo da como un segmento y la otra
    lo parte con un vértice colineal. Entonces las tres mitades aparecen una vez
    cada una y el conteo las toma por contorno. Medido sobre este estudio, ese
    solo defecto añadía 25,77 km a un perímetro de 119,52 km, y con él subía el
    coeficiente de compacidad de 2,27 a 2,76 sin que nada lo señalara.

    Por eso las aristas se ENCADENAN y se descarta lo que no encierra
    superficie: un recorrido que va y vuelve sobre sí mismo tiene área nula y no
    es un contorno. Se conservan todas las cadenas que sí encierran área, porque
    un estudio con dos piezas separadas tiene dos contornos legítimos.

    Se devuelve el recuento, porque es el que dice si el resultado vale. Un
    mosaico con aristas que aparecen tres veces o más no es una cobertura
    limpia, y ahí el conteo deja de significar lo que se supone: quien llame
    debe mirar 'cobertura_limpia' antes de usar el perímetro.
    """
    escala = 1.0 / tolerancia_m if tolerancia_m > 0 else 1.0
    cuenta: dict = {}
    for poligono in poligonos:
        for anillo in poligono:
            for uno, otro in zip(anillo, anillo[1:]):
                izquierda = (round(uno[0] * escala), round(uno[1] * escala))
                derecha = (round(otro[0] * escala), round(otro[1] * escala))
                if izquierda == derecha:
                    continue
                clave = ((izquierda, derecha) if izquierda < derecha
                         else (derecha, izquierda))
                cuenta[clave] = cuenta.get(clave, 0) + 1

    frontera = sum(1 for veces in cuenta.values() if veces == 1)
    compartidas = sum(1 for veces in cuenta.values() if veces == 2)
    repetidas = sum(1 for veces in cuenta.values() if veces > 2)

    cadenas = cadenas_de_frontera(
        segmentos_de_frontera(poligonos, tolerancia_m), tolerancia_m)

    perimetro = 0.0
    descartada = 0.0
    contornos = 0
    for cadena in cadenas:
        largo = _longitud_de_cadena(cadena)
        # UNA CADENA QUE NO ENCIERRA SUPERFICIE NO ES UN CONTORNO. El umbral es
        # la superficie de una banda del ancho de la tolerancia a lo largo del
        # recorrido: nada más delgado que eso puede ser un polígono real.
        if abs(_area_con_signo(cadena)) <= max(tolerancia_m, 1e-9) * largo:
            descartada += largo
            continue
        perimetro += largo
        contornos += 1

    return {
        "perimetro_m": perimetro,
        "aristas_frontera": frontera,
        "aristas_compartidas": compartidas,
        "aristas_repetidas": repetidas,
        "contornos": contornos,
        "cadenas_degeneradas": len(cadenas) - contornos,
        "longitud_degenerada_m": descartada,
        "cobertura_limpia": repetidas == 0 and frontera > 0 and contornos > 0,
    }


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

# -*- coding: utf-8 -*-
"""
red_drenaje
===========
Motor de análisis de la red de drenaje cartográfica del IGAC, para el entorno
de QGIS.

Sustituye al análisis de terreno del M02. La delimitación preliminar pasa a
derivarse de la cartografía 1:100.000 en lugar del modelo de elevación, por dos
razones medidas sobre este proyecto:

1. En terreno plano, el DEM de radar encamina el flujo por donde le indica su
   ruido vertical y no por el cauce. Sobre el Río Bogotá produjo una cuenca de
   6,59 km2 correspondiente a un afluente menor, con código de salida correcto y
   sin ninguna señal de error.
2. La delimitación definitiva ya estaba asignada al M09, la asistida de HEC-HMS
   (CLAUDE.md, sección 4). Lo que el M02 necesita es acotar la descarga del DEM
   y la selección de estaciones, y para eso basta una aproximación cartográfica
   declarada como tal.

Cuatro hechos medidos sobre las capas del IGAC que condicionan el diseño:

- El sentido de digitalización coincide con el de flujo. Solo el 3,17% de los
  nodos lo incumple en la zona del estudio. Se valida en cada ejecución.
- La red NO es topológica: 1.313 tramos con 2.626 extremos producen 2.506 nodos
  distintos. Los afluentes terminan sobre el trazado del receptor, no en un
  vértice compartido. La adyacencia se resuelve por geometría con tolerancia.
- El 93% de las desembocaduras que alcanzan un drenaje doble lo tocan
  exactamente. Cinco metros de tolerancia bastan.
- El drenaje sencillo no incluye el eje de los dobles. El Río Bogotá solo existe
  como polígono, de modo que la red queda cortada justo en el cauce del estudio
  y hay que reponer ese eje.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from comun.errores import ErrorFormato

__all__ = [
    "Tramo",
    "ReporteSentido",
    "recortar_capa",
    "leer_tramos",
    "validar_sentido",
    "construir_adyacencia",
    "trazar_aguas_arriba",
    "eje_de_poligonos",
    "orientar_eje",
    "empalmar_eje",
    "Escenario",
    "diagnosticar_escenario",
    "medir_conectividad",
]


@dataclass
class Tramo:
    """Un tramo de drenaje con su geometría y su sentido de digitalización."""

    identificador: int
    nombre: str
    geometria: Any                     # QgsGeometry
    inicio: tuple[float, float]        # vértice de aguas arriba
    fin: tuple[float, float]           # vértice de aguas abajo
    origen: str = "sencillo"           # sencillo | eje_doble
    longitud_m: float = 0.0


@dataclass
class ReporteSentido:
    """Medida de cuánto se cumple la convención de sentido de digitalización."""

    tramos: int = 0
    nodos: int = 0
    confluencias: int = 0
    bifurcaciones: int = 0
    incumplimiento_pct: float = 0.0
    aceptable: bool = True

    def como_dict(self) -> dict[str, Any]:
        return {
            "tramos": self.tramos,
            "nodos": self.nodos,
            "confluencias": self.confluencias,
            "bifurcaciones": self.bifurcaciones,
            "incumplimiento_pct": round(self.incumplimiento_pct, 3),
            "aceptable": self.aceptable,
        }


# =============================================================================
# Lectura y recorte
# =============================================================================
def recortar_capa(
    ruta_origen: Path,
    extension,
    destino: Path,
    crs_id: str,
    nombre_campo: str = "NOMBRE_GEO",
) -> Path:
    """
    Recorta una capa nacional a la extensión de trabajo y la escribe.

    Es lo que permite que el repositorio versione un recorte de unos cientos de
    kilobytes en lugar de los 645 MB de las capas nacionales, que viven fuera
    del árbol del proyecto.

    Excepciones
    -----------
    ErrorFormato
        Si la capa de origen no se puede abrir o el recorte no se puede escribir.
    """
    from qgis.core import (
        QgsCoordinateTransformContext, QgsFeature, QgsFeatureRequest,
        QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes,
    )

    capa = QgsVectorLayer(str(ruta_origen), ruta_origen.stem, "ogr")
    if not capa.isValid():
        raise ErrorFormato(f"QGIS no pudo abrir {ruta_origen}")

    tipo = QgsWkbTypes.displayString(capa.wkbType())
    memoria = QgsVectorLayer(f"{tipo}?crs={crs_id}", "recorte", "memory")
    memoria.dataProvider().addAttributes(capa.fields())
    memoria.updateFields()

    entidades = []
    for entidad in capa.getFeatures(QgsFeatureRequest().setFilterRect(extension)):
        geometria = entidad.geometry()
        if geometria is None or geometria.isEmpty():
            continue
        copia = QgsFeature(memoria.fields())
        copia.setGeometry(geometria)
        copia.setAttributes(entidad.attributes())
        entidades.append(copia)

    memoria.dataProvider().addFeatures(entidades)
    memoria.updateExtents()

    destino.parent.mkdir(parents=True, exist_ok=True)
    for extension_archivo in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"):
        destino.with_suffix(extension_archivo).unlink(missing_ok=True)

    opciones = QgsVectorFileWriter.SaveVectorOptions()
    opciones.driverName = "ESRI Shapefile"
    opciones.fileEncoding = "UTF-8"
    error, mensaje = QgsVectorFileWriter.writeAsVectorFormatV3(
        memoria, str(destino), QgsCoordinateTransformContext(), opciones
    )[:2]
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise ErrorFormato(f"No se pudo escribir {destino}: {mensaje or error}")

    return destino


def leer_tramos(
    ruta: Path, nombre_campo: str = "NOMBRE_GEO", origen: str = "sencillo"
) -> list[Tramo]:
    """
    Lee una capa de líneas como lista de tramos con su sentido.

    Cada parte de una geometría multilínea se trata como un tramo propio: en la
    cartografía del IGAC un curso de agua puede venir partido en varias piezas.
    """
    from qgis.core import QgsGeometry, QgsVectorLayer

    capa = QgsVectorLayer(str(ruta), ruta.stem, "ogr")
    if not capa.isValid():
        raise ErrorFormato(f"QGIS no pudo abrir {ruta}")

    tiene_campo = capa.fields().indexOf(nombre_campo) >= 0
    tramos: list[Tramo] = []
    contador = 0

    for entidad in capa.getFeatures():
        geometria = entidad.geometry()
        if geometria is None or geometria.isEmpty():
            continue
        nombre = (str(entidad[nombre_campo]).strip()
                  if tiene_campo and entidad[nombre_campo] else "")
        partes = (geometria.asMultiPolyline() if geometria.isMultipart()
                  else [geometria.asPolyline()])
        for linea in partes:
            if len(linea) < 2:
                continue
            geometria_parte = QgsGeometry.fromPolylineXY(linea)
            tramos.append(Tramo(
                identificador=contador,
                nombre=nombre,
                geometria=geometria_parte,
                inicio=(linea[0].x(), linea[0].y()),
                fin=(linea[-1].x(), linea[-1].y()),
                origen=origen,
                longitud_m=geometria_parte.length(),
            ))
            contador += 1

    return tramos


# =============================================================================
# Validación del sentido de digitalización
# =============================================================================
def validar_sentido(
    tramos: Sequence[Tramo],
    maximo_incumplimiento_pct: float = 10.0,
    rejilla_m: float = 0.5,
) -> ReporteSentido:
    """
    Mide cuánto se cumple la convención de que el tramo va de aguas arriba a
    aguas abajo.

    En una red bien dirigida, de un nodo arranca como máximo un tramo: el agua
    baja por un solo camino. Los nodos con dos o más tramos arrancando son
    bifurcaciones, que existen en deltas y brazos pero deben ser escasas. Una
    proporción alta significa que la capa no respeta la convención y que todo lo
    que se derive de ella sería inventado.
    """
    def nodo(punto: tuple[float, float]) -> tuple[int, int]:
        return (round(punto[0] / rejilla_m), round(punto[1] / rejilla_m))

    arrancan: dict = defaultdict(int)
    terminan: dict = defaultdict(int)
    for tramo in tramos:
        arrancan[nodo(tramo.inicio)] += 1
        terminan[nodo(tramo.fin)] += 1

    nodos = set(arrancan) | set(terminan)
    confluencias = sum(1 for n in nodos if terminan[n] >= 2)
    bifurcaciones = sum(1 for n in nodos if arrancan[n] >= 2)

    denominador = confluencias + bifurcaciones
    porcentaje = (100.0 * bifurcaciones / denominador) if denominador else 0.0

    return ReporteSentido(
        tramos=len(tramos), nodos=len(nodos), confluencias=confluencias,
        bifurcaciones=bifurcaciones, incumplimiento_pct=porcentaje,
        aceptable=porcentaje <= maximo_incumplimiento_pct,
    )


# =============================================================================
# Adyacencia geométrica
# =============================================================================
def construir_adyacencia(
    tramos: Sequence[Tramo], tolerancia_m: float = 5.0
) -> dict[int, list[int]]:
    """
    Devuelve, para cada tramo, la lista de tramos que desembocan en él.

    No se usan vértices compartidos porque la red del IGAC no es topológica: el
    afluente termina sobre el *trazado* del receptor, no en uno de sus vértices.
    Se busca, para el vértice final de cada tramo, qué otros tramos pasan a
    menos de la tolerancia.

    El índice espacial es imprescindible: sin él la comparación sería de todos
    contra todos.
    """
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsSpatialIndex

    if not tramos:
        return {}

    indice = QgsSpatialIndex()
    por_identificador: dict[int, Tramo] = {}
    for tramo in tramos:
        entidad = QgsFeature(tramo.identificador)
        entidad.setGeometry(tramo.geometria)
        indice.addFeature(entidad)
        por_identificador[tramo.identificador] = tramo

    afluentes: dict[int, list[int]] = defaultdict(list)

    for tramo in tramos:
        punto_fin = QgsGeometry.fromPointXY(QgsPointXY(*tramo.fin))
        caja = punto_fin.boundingBox()
        caja.grow(tolerancia_m)

        mejor, mejor_distancia = None, float("inf")
        for candidato_id in indice.intersects(caja):
            if candidato_id == tramo.identificador:
                continue
            candidato = por_identificador[candidato_id]
            distancia = candidato.geometria.distance(punto_fin)
            if distancia > tolerancia_m:
                continue
            # El receptor es el tramo más cercano cuyo propio final no coincida
            # con este punto: si dos tramos terminan juntos, ninguno desemboca
            # en el otro, ambos desembocan en un tercero.
            if _misma_posicion(candidato.fin, tramo.fin, tolerancia_m):
                continue
            if distancia < mejor_distancia:
                mejor, mejor_distancia = candidato_id, distancia

        if mejor is not None:
            afluentes[mejor].append(tramo.identificador)

    return dict(afluentes)


def _misma_posicion(a: tuple[float, float], b: tuple[float, float],
                    tolerancia: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerancia


def trazar_aguas_arriba(
    afluentes: dict[int, list[int]], identificador_inicial: int
) -> set[int]:
    """
    Devuelve el conjunto de tramos situados aguas arriba del tramo indicado.

    Recorrido en anchura sobre la relación de afluencia. El conjunto incluye el
    tramo de partida: la cuenca de un punto contiene el cauce en el que está.
    """
    visitados = {identificador_inicial}
    cola = deque([identificador_inicial])
    while cola:
        actual = cola.popleft()
        for tributario in afluentes.get(actual, ()):
            if tributario not in visitados:
                visitados.add(tributario)
                cola.append(tributario)
    return visitados


# =============================================================================
# Jerarquía de la red
# =============================================================================
# Estas funciones trabajan sobre la relación de afluencia y NO usan QGIS. Se
# pueden ejecutar y probar desde el venv, aunque el módulo que las alimenta
# corra en el entorno SIG.
def orden_strahler(
    afluentes: dict[int, list[int]], identificadores: Sequence[int]
) -> tuple[dict[int, int], list[int]]:
    """
    Asigna a cada tramo su orden de Strahler.

    Un tramo sin tributarios es de orden 1. Al confluir, el orden sube solo si
    se juntan DOS o más del mismo orden máximo; si llega uno solo, el receptor
    hereda ese orden. Esa es la definición de Strahler, y la diferencia con la
    de Shreve, que suma, importa: la razón de bifurcación y la frecuencia de
    corrientes que se reportan después están definidas sobre Strahler.

    Devuelve (orden por tramo, tramos en ciclo). Lo segundo no es un detalle
    defensivo: la adyacencia se resuelve por proximidad geométrica y no por
    topología declarada, de modo que dos tramos pueden quedar señalándose el
    uno al otro. Ahí el orden es indefinido, y decirlo es preferible a
    entregar un número que parece bueno.

    El recorrido es iterativo. Con miles de tramos encadenados, una
    implementación recursiva agota la pila del intérprete.
    """
    tributarios = {int(k): list(v) for k, v in afluentes.items()}
    todos = list(dict.fromkeys(int(i) for i in identificadores))

    orden: dict[int, int] = {}
    estado: dict[int, int] = {}  # 0 sin visitar, 1 en proceso, 2 resuelto
    en_ciclo: set[int] = set()

    for raiz in todos:
        if estado.get(raiz, 0) == 2:
            continue
        pila = [(raiz, False)]
        while pila:
            actual, expandido = pila.pop()
            if expandido:
                ordenes = [orden[t] for t in tributarios.get(actual, ())
                           if t in orden]
                if not ordenes:
                    orden[actual] = 1
                else:
                    maximo = max(ordenes)
                    orden[actual] = (maximo + 1 if ordenes.count(maximo) >= 2
                                     else maximo)
                estado[actual] = 2
                continue
            if estado.get(actual, 0) == 2:
                continue
            if estado.get(actual, 0) == 1:
                # Se ha vuelto a un tramo que sigue en proceso: hay ciclo.
                en_ciclo.add(actual)
                continue
            estado[actual] = 1
            pila.append((actual, True))
            for tributario in tributarios.get(actual, ()):
                if estado.get(tributario, 0) != 2:
                    pila.append((tributario, False))

    for identificador in todos:
        orden.setdefault(identificador, 1)

    return orden, sorted(en_ciclo)


def contar_corrientes(
    orden: dict[int, int], afluentes: dict[int, list[int]]
) -> dict[int, int]:
    """
    Cuenta CORRIENTES por orden, que no es lo mismo que contar tramos.

    Una corriente de orden n es la cadena completa de tramos consecutivos de
    ese orden, desde donde nace hasta donde el orden sube. La cartografía parte
    un mismo río en decenas de tramos por razones de dibujo, de modo que contar
    tramos multiplicaría el resultado por un factor arbitrario y arruinaría
    tanto la razón de bifurcación como la frecuencia de corrientes.

    Se cuenta la CABECERA de cada cadena: un tramo cuyo orden no le llega ya
    hecho desde aguas arriba.
    """
    cuenta: dict[int, int] = {}
    for identificador, propio in orden.items():
        hereda = any(orden.get(t) == propio
                     for t in afluentes.get(identificador, ()))
        if not hereda:
            cuenta[propio] = cuenta.get(propio, 0) + 1
    return dict(sorted(cuenta.items()))


def razon_bifurcacion(corrientes: dict[int, int]) -> dict[str, Any]:
    """
    Razón de bifurcación por par de órdenes consecutivos y su media.

    Horton observó que el cociente entre el número de corrientes de un orden y
    el del siguiente es aproximadamente constante en una cuenca, y que en redes
    naturales cae entre 3 y 5. Un valor fuera de ese rango no invalida la
    cuenca, pero suele delatar un control estructural del terreno o, más a
    menudo, una cartografía con detalle desigual: si la parte alta se levantó
    con más densidad que la baja, las corrientes de orden 1 salen infladas.

    Se devuelven DOS medias y se adopta la ponderada, que es la que Strahler
    propuso. La aritmética simple da el mismo peso al cociente entre los dos
    órdenes más bajos, calculado sobre miles de corrientes, y al de los dos más
    altos, calculado sobre una o dos: en el último par el denominador vale 1 por
    definición en una cuenca de una sola salida, de modo que ese cociente es
    grande siempre y arrastra la media sin aportar información. La ponderación
    por el número de corrientes que intervienen en cada par lo corrige.
    """
    ordenes = sorted(corrientes)
    pares: list[dict[str, Any]] = []
    for menor, mayor in zip(ordenes, ordenes[1:]):
        if mayor != menor + 1 or not corrientes[mayor]:
            continue
        pares.append({
            "orden": f"{menor}/{mayor}",
            "corrientes_menor": corrientes[menor],
            "corrientes_mayor": corrientes[mayor],
            "razon": round(corrientes[menor] / corrientes[mayor], 3),
            "peso": corrientes[menor] + corrientes[mayor],
        })

    if not pares:
        return {"pares": [], "media_simple": None, "media_ponderada": None,
                "adoptada": None, "dentro_del_rango_natural": False}

    simple = sum(p["razon"] for p in pares) / len(pares)
    peso_total = sum(p["peso"] for p in pares)
    ponderada = sum(p["razon"] * p["peso"] for p in pares) / peso_total
    return {
        "pares": pares,
        "media_simple": round(simple, 3),
        "media_ponderada": round(ponderada, 3),
        "adoptada": round(ponderada, 3),
        "dentro_del_rango_natural": 3.0 <= ponderada <= 5.0,
    }


def camino_mas_largo(
    afluentes: dict[int, list[int]],
    longitudes: dict[int, float],
    desembocadura: int,
) -> list[int]:
    """
    Devuelve el camino de mayor longitud acumulada que llega a un tramo.

    Es la definición hidrológica de cauce principal: no el río con nombre, sino
    el recorrido más largo desde la divisoria hasta el punto de salida, que es
    el que gobierna el tiempo de concentración.

    La alternativa, seguir el tramo de mayor orden en cada confluencia, da un
    resultado distinto y peor: el orden mide ramificación, no distancia, y en
    una cuenca alargada elige el afluente equivocado.
    """
    memoria: dict[int, tuple[float, list[int]]] = {}
    pila = [(desembocadura, False)]
    visitando: set[int] = set()

    while pila:
        actual, expandido = pila.pop()
        if expandido:
            visitando.discard(actual)
            mejor_largo, mejor_camino = 0.0, []
            for tributario in afluentes.get(actual, ()):
                if tributario not in memoria:
                    continue
                largo, camino = memoria[tributario]
                if largo > mejor_largo:
                    mejor_largo, mejor_camino = largo, camino
            memoria[actual] = (longitudes.get(actual, 0.0) + mejor_largo,
                               mejor_camino + [actual])
            continue
        if actual in memoria or actual in visitando:
            continue
        visitando.add(actual)
        pila.append((actual, True))
        for tributario in afluentes.get(actual, ()):
            if tributario not in memoria:
                pila.append((tributario, False))

    return memoria.get(desembocadura, (0.0, []))[1]


# =============================================================================
# Eje de los drenajes dobles
# =============================================================================
def eje_de_poligonos(
    ruta_poligonos: Path,
    destino: Path,
    resolucion_m: float,
    extension,
    crs_id: str,
    directorio_temporal: Path,
) -> Path:
    """
    Deriva el eje de un polígono de cauce por rasterización y adelgazamiento.

    El drenaje sencillo del IGAC no incluye el eje de los dobles, de modo que la
    red queda cortada en los ríos anchos, que suelen ser justamente los del
    estudio. Se repone rasterizando el polígono, adelgazándolo a un píxel de
    ancho y vectorizando el resultado.

    La resolución importa: el cauce del Río Bogotá tiene 55,8 m de ancho medio,
    de modo que a 12,5 m quedan cuatro o cinco celdas de través, suficientes
    para un esqueleto continuo. Con celdas más gruesas el polígono se fragmenta
    y el eje sale cortado.

    Se usa esta vía y no v.voronoi porque la implementación que expone QGIS
    4.2.0 no ofrece la opción de esqueleto: se ejecuta sin error y devuelve una
    capa vacía.

    Excepciones
    -----------
    ErrorFormato
        Si alguno de los tres pasos falla o no produce geometría.
    """
    import processing

    directorio_temporal.mkdir(parents=True, exist_ok=True)
    rasterizado = directorio_temporal / "cauce_doble.tif"
    adelgazado = directorio_temporal / "cauce_eje.tif"

    region = (f"{extension.xMinimum()},{extension.xMaximum()},"
              f"{extension.yMinimum()},{extension.yMaximum()}"
              f" [{crs_id}]")

    try:
        # Los índices de estas enumeraciones no son evidentes y equivocarlos no
        # produce error: v.to.rast devuelve un ráster vacío y el fallo aparece
        # dos pasos más adelante. type: ['point','line','boundary','area'],
        # use: ['attr','cat','val','z','dir'].
        processing.run("grass:v.to.rast", {
            "input": str(ruta_poligonos), "type": [3], "use": 2, "value": 1,
            "memory": 300, "output": str(rasterizado),
            "GRASS_REGION_PARAMETER": region,
            "GRASS_REGION_CELLSIZE_PARAMETER": resolucion_m,
        })
        processing.run("grass:r.thin", {
            "input": str(rasterizado), "iterations": 200,
            "output": str(adelgazado),
            "GRASS_REGION_PARAMETER": region,
            "GRASS_REGION_CELLSIZE_PARAMETER": resolucion_m,
        })
        # Sin suavizado. La opción -s desplaza los vértices para redondear las
        # esquinas y rompe la coincidencia exacta entre piezas consecutivas: el
        # eje sale partido en centenares de fragmentos inconexos y el recorrido
        # aguas arriba se detiene en el primero.
        processing.run("grass:r.to.vect", {
            "input": str(adelgazado), "type": 0, "column": "valor",
            "-s": False, "-v": True, "output": str(destino),
            "GRASS_REGION_PARAMETER": region,
            "GRASS_REGION_CELLSIZE_PARAMETER": resolucion_m,
        })
    except Exception as exc:
        raise ErrorFormato(
            f"No se pudo derivar el eje del drenaje doble: {exc}"
        ) from exc

    if not destino.exists():
        raise ErrorFormato(
            f"El adelgazamiento no produjo ninguna geometría en {destino}."
        )
    return destino


def orientar_eje(
    tramos_eje: Sequence[Tramo],
    cota: "Callable[[float, float], float]",
    limite: Any = None,
    rejilla_m: float = 12.5,
    tolerancia_limite_m: float = 100.0,
) -> tuple[list[Tramo], dict[str, Any]]:
    """
    Da sentido de flujo al eje derivado, con una sola referencia global.

    El adelgazamiento entrega el eje troceado en centenares de piezas con
    direcciones arbitrarias. Decidir el sentido de cada una por separado no
    funciona: miden unos cientos de metros y la mayoría no tiene información
    local con la que decidir, de modo que conservan su dirección original y la
    cadena se rompe en la primera discrepancia.

    La orientación es global, pero **por componente conexa**. Una ventana de
    trabajo contiene los esqueletos de varios ríos, que son componentes
    separadas y legítimas. Usar una sola referencia para todas deja orientada la
    de esa referencia y ninguna más.

    Para cada componente se toma como referencia de aguas abajo su nodo terminal
    de cota más baja, se mide la distancia sobre la red hasta él, y cada pieza se
    orienta hacia la distancia decreciente: el agua va hacia la referencia.

    `cota` es una función que devuelve la elevación en unas coordenadas. Se pide
    al módulo que llama para no atar este archivo a una fuente concreta. Se usa
    para una sola comparación por componente, sobre desniveles de decenas de
    metros a lo largo de kilómetros de cauce, no para encaminar flujo: es
    justamente el uso del DEM que sigue siendo fiable en terreno plano.

    La malla de fusión de nodos vale por defecto el tamaño de celda de la
    rasterización. Medido sobre el Río Bogotá, con 0,5 m coincide el 69,5% de
    los extremos y con 12,5 m el 77,2%: la diferencia son discontinuidades de
    una sola celda donde el polígono se estrecha, que son el mismo nodo. Por
    encima de una celda ya no debe fusionarse, porque se estarían uniendo
    confluencias entre ríos distintos.

    Devuelve (tramos orientados, diagnóstico).
    """
    from qgis.core import QgsGeometry

    if not tramos_eje:
        return [], {"piezas": 0, "componentes": 0, "invertidas": 0}

    def nodo(punto: tuple[float, float]) -> tuple[int, int]:
        return (round(punto[0] / rejilla_m), round(punto[1] / rejilla_m))

    # Grafo no dirigido: cada pieza une sus dos nodos extremos.
    vecinos: dict = defaultdict(list)
    for tramo in tramos_eje:
        a, b = nodo(tramo.inicio), nodo(tramo.fin)
        vecinos[a].append((b, tramo.longitud_m))
        vecinos[b].append((a, tramo.longitud_m))

    # --- Componentes conexas -------------------------------------------------
    componente: dict = {}
    componentes: list[list] = []
    for arranque in vecinos:
        if arranque in componente:
            continue
        indice = len(componentes)
        miembros = []
        cola = deque([arranque])
        componente[arranque] = indice
        while cola:
            actual = cola.popleft()
            miembros.append(actual)
            for vecino, _ in vecinos[actual]:
                if vecino not in componente:
                    componente[vecino] = indice
                    cola.append(vecino)
        componentes.append(miembros)

    # --- Distancia a la referencia de cada componente ------------------------
    distancia: dict = {}
    referencias = []
    from qgis.core import QgsPointXY as _Punto
    from qgis.core import QgsWkbTypes as _Wkb

    # El límite tiene que ser el CONTORNO, no el polígono. La distancia de un
    # punto interior a un polígono es cero, de modo que pasando el polígono
    # todos los nodos quedan marcados como fronterizos y el criterio se anula
    # sin dar ningún error: el resultado es idéntico al de no usarlo.
    contorno = limite
    if limite is not None and limite.type() == _Wkb.GeometryType.PolygonGeometry:
        contorno = QgsGeometry(limite).convertToType(_Wkb.GeometryType.LineGeometry)

    por_limite = 0
    for miembros in componentes:
        # Terminales: nodos de grado uno. Sin terminales (componente cerrada en
        # bucle) se consideran todos los de la componente.
        terminales = [n for n in miembros if len(vecinos[n]) == 1] or miembros

        # Referencia cartográfica: el nodo donde el eje abandona la subzona.
        # Un río sale de su subzona por un único punto, y ese punto es su
        # desembocadura por definición de la zonificación, sin medir cotas.
        #
        # Esto NO es un detalle de implementación. Elegir la referencia por cota
        # mínima falla en terreno plano: sobre el Río Bogotá, con 3 m de
        # desnivel real en 20 km de cauce y un DEM de radar con varios metros de
        # ruido, el nodo más bajo se elige por ruido y el eje entero queda
        # orientado al revés. El recorrido resultante baja por el río en lugar
        # de subir, y termina con cifras verosímiles y sentido invertido.
        en_limite = []
        if contorno is not None:
            for n in terminales:
                punto = QgsGeometry.fromPointXY(
                    _Punto(n[0] * rejilla_m, n[1] * rejilla_m)
                )
                if punto.distance(contorno) <= tolerancia_limite_m:
                    en_limite.append(n)

        if en_limite:
            # Si el eje cruza el límite en varios puntos (entra y sale), la cota
            # sí discrimina: entre entrada y salida de una cuenca hay desnivel
            # muy superior al ruido del modelo.
            referencia = min(
                en_limite, key=lambda n: cota(n[0] * rejilla_m, n[1] * rejilla_m)
            )
            por_limite += 1
        else:
            referencia = min(
                terminales,
                key=lambda n: cota(n[0] * rejilla_m, n[1] * rejilla_m),
            )
        referencias.append(referencia)

        distancia[referencia] = 0.0
        cola = deque([referencia])
        while cola:
            actual = cola.popleft()
            for vecino, longitud in vecinos[actual]:
                if vecino not in distancia:
                    distancia[vecino] = distancia[actual] + longitud
                    cola.append(vecino)

    # --- Orientación ---------------------------------------------------------
    orientados: list[Tramo] = []
    invertidas = 0
    sin_resolver = 0

    for tramo in tramos_eje:
        a, b = nodo(tramo.inicio), nodo(tramo.fin)
        if a not in distancia or b not in distancia or distancia[a] == distancia[b]:
            sin_resolver += 1
            orientados.append(tramo)
            continue

        # El agua baja hacia la referencia: el inicio debe estar más lejos.
        if distancia[a] < distancia[b]:
            invertida = list(reversed(tramo.geometria.asPolyline()))
            orientados.append(Tramo(
                identificador=tramo.identificador, nombre=tramo.nombre,
                geometria=QgsGeometry.fromPolylineXY(invertida),
                inicio=tramo.fin, fin=tramo.inicio, origen=tramo.origen,
                longitud_m=tramo.longitud_m,
            ))
            invertidas += 1
        else:
            orientados.append(tramo)

    mayor = max((len(m) for m in componentes), default=0)
    return orientados, {
        "piezas": len(tramos_eje),
        "nodos": len(vecinos),
        "componentes": len(componentes),
        "nodos_mayor_componente": mayor,
        "referencia_por_limite": por_limite,
        "referencia_por_cota": len(componentes) - por_limite,
        "invertidas": invertidas,
        "sin_resolver": sin_resolver,
    }


def empalmar_eje(
    tramos: Sequence[Tramo],
    afluentes: dict[int, list[int]],
    tolerancia_m: float = 200.0,
) -> dict[str, Any]:
    """
    Conecta el eje derivado con la red de drenaje sencillo en sus extremos.

    Es el empalme que faltaba, y sin él el recorrido se detiene sin ninguna
    señal de error. El motivo es cartográfico: un río se representa como drenaje
    doble solo donde es lo bastante ancho para dibujar dos orillas a 1:100.000.
    Aguas arriba de ese punto continúa como drenaje sencillo. Medido sobre el
    Río Bogotá, el polígono abarca de 2569 m a 282 m de cota, y su extremo
    superior coincide con el punto de descarga del estudio: justo ahí acaba el
    eje y empieza la polilínea.

    La tolerancia debe ser mucho mayor que la de la adyacencia ordinaria. Aquí
    no se busca un afluente que toca su receptor, sino dos representaciones
    distintas del mismo río que se encuentran: el esqueleto termina donde acaba
    el polígono rasterizado y la polilínea empieza donde la dibujó el cartógrafo,
    y entre ambos hay decenas de metros.

    Modifica `afluentes` en el sitio y devuelve el diagnóstico de lo empalmado.
    """
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsSpatialIndex

    ejes = [t for t in tramos if t.origen == "eje_doble"]
    sencillos = [t for t in tramos if t.origen != "eje_doble"]
    if not ejes or not sencillos:
        return {"empalmes": 0, "huerfanos_previos": 0}

    # Tramos que ya desembocan en alguno: los que no, son candidatos.
    con_receptor = {i for lista in afluentes.values() for i in lista}
    huerfanos = [t for t in sencillos if t.identificador not in con_receptor]

    # Índice sobre los inicios del eje, que son sus extremos de aguas arriba.
    indice = QgsSpatialIndex()
    por_id: dict[int, Tramo] = {}
    for tramo in ejes:
        entidad = QgsFeature(tramo.identificador)
        entidad.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*tramo.inicio)))
        indice.addFeature(entidad)
        por_id[tramo.identificador] = tramo

    empalmes = 0
    for tramo in huerfanos:
        punto = QgsGeometry.fromPointXY(QgsPointXY(*tramo.fin))
        caja = punto.boundingBox()
        caja.grow(tolerancia_m)

        mejor, mejor_distancia = None, float("inf")
        for candidato_id in indice.intersects(caja):
            candidato = por_id[candidato_id]
            distancia = math.hypot(
                candidato.inicio[0] - tramo.fin[0],
                candidato.inicio[1] - tramo.fin[1],
            )
            if distancia <= tolerancia_m and distancia < mejor_distancia:
                mejor, mejor_distancia = candidato_id, distancia

        if mejor is not None:
            afluentes.setdefault(mejor, []).append(tramo.identificador)
            empalmes += 1

    return {
        "empalmes": empalmes,
        "huerfanos_previos": len(huerfanos),
        "tolerancia_m": tolerancia_m,
    }


# =============================================================================
# Diagnóstico del escenario
# =============================================================================
ESCENARIO_DOBLE = 1
ESCENARIO_SENCILLO = 2
ESCENARIO_SIN_CARTOGRAFIA = 3

METODO_CARTOGRAFICO = "cartografico"
METODO_TERRENO = "terreno"


@dataclass
class Escenario:
    """Dónde cae el punto de descarga y qué método de delimitación procede."""

    numero: int
    metodo: str
    distancia_doble_m: float
    distancia_sencillo_m: float
    nombre_doble: str = ""
    nombre_sencillo: str = ""

    @property
    def descripcion(self) -> str:
        return {
            ESCENARIO_DOBLE: "punto sobre drenaje doble",
            ESCENARIO_SENCILLO: "punto sobre drenaje sencillo o su aferencia",
            ESCENARIO_SIN_CARTOGRAFIA:
                "punto sin cauce cartografiado a 1:100.000",
        }[self.numero]

    def como_dict(self) -> dict[str, Any]:
        return {
            "escenario": self.numero,
            "descripcion": self.descripcion,
            "metodo": self.metodo,
            "distancia_drenaje_doble_m": round(self.distancia_doble_m, 2),
            "distancia_drenaje_sencillo_m": round(self.distancia_sencillo_m, 2),
            "cauce_doble": self.nombre_doble,
            "cauce_sencillo": self.nombre_sencillo,
        }


def diagnosticar_escenario(
    punto: Any,
    capa_doble: Any,
    capa_sencillo: Any,
    umbral_doble_m: float = 50.0,
    umbral_sencillo_m: float = 150.0,
    campo_nombre: str = "NOMBRE_GEO",
) -> Escenario:
    """
    Determina el escenario del punto y, con él, el método de delimitación.

    No hay un método universal. Los dos disponibles tienen dominios
    complementarios, medidos en este proyecto:

    - Sobre un río ancho, que por serlo suele discurrir por un valle plano, el
      DEM de radar encamina el flujo por su ruido vertical y no por el cauce.
      Ahí manda la cartografía.
    - Sobre un cauce que la cartografía a 1:100.000 no recoge, que por eso mismo
      es pequeño y suele estar en ladera con pendiente marcada, el DEM es
      fiable, porque el fallo anterior exige terreno plano. Ahí manda el
      terreno.

    Elegir mal no produce error: produce una cuenca verosímil y equivocada. Por
    eso el escenario detectado se registra en el reporte y en la capa de salida.
    """
    from qgis.core import QgsFeatureRequest

    def mas_cercano(capa, radio: float) -> tuple[float, str]:
        if capa is None or not capa.isValid():
            return float("inf"), ""
        caja = punto.boundingBox()
        caja.grow(max(radio, 1.0))
        tiene = capa.fields().indexOf(campo_nombre) >= 0
        mejor, nombre = float("inf"), ""
        for entidad in capa.getFeatures(QgsFeatureRequest().setFilterRect(caja)):
            geometria = entidad.geometry()
            if geometria is None or geometria.isEmpty():
                continue
            distancia = geometria.distance(punto)
            if distancia < mejor:
                mejor = distancia
                nombre = (str(entidad[campo_nombre]).strip()
                          if tiene and entidad[campo_nombre] else "")
        return mejor, nombre

    # Se busca con holgura sobre el umbral para poder reportar la distancia
    # real aunque quede fuera: un punto a 3 km del río es un dato accionable,
    # y decir solo "fuera de umbral" no lo sería.
    distancia_doble, nombre_doble = mas_cercano(capa_doble, umbral_doble_m * 20)
    distancia_sencillo, nombre_sencillo = mas_cercano(
        capa_sencillo, umbral_sencillo_m * 20
    )

    if distancia_doble <= umbral_doble_m:
        numero, metodo = ESCENARIO_DOBLE, METODO_CARTOGRAFICO
    elif distancia_sencillo <= umbral_sencillo_m:
        numero, metodo = ESCENARIO_SENCILLO, METODO_CARTOGRAFICO
    else:
        numero, metodo = ESCENARIO_SIN_CARTOGRAFIA, METODO_TERRENO

    return Escenario(
        numero=numero, metodo=metodo,
        distancia_doble_m=distancia_doble,
        distancia_sencillo_m=distancia_sencillo,
        nombre_doble=nombre_doble, nombre_sencillo=nombre_sencillo,
    )


def medir_conectividad(
    tramos: Sequence[Tramo], tolerancia_m: float = 5.0, radio_m: float = 1000.0
) -> dict[str, Any]:
    """
    Mide a que distancia esta cada tramo de su receptor mas proximo.

    Es la medicion que decide si la red cartografica es utilizable para trazar
    una cuenca, y que en este proyecto se hizo a mano tras varios intentos de
    corregir el algoritmo a ciegas. Sobre la subzona del Rio Bogota: el 85,1%
    de los tramos toca a su receptor exactamente y el 15% restante esta a
    cientos de metros, no a unos pocos. Subir la tolerancia de 5 a 50 m
    recuperaba 52 tramos de 8.754, de modo que NO hay tolerancia que arregle la
    conectividad: la holgura se compensa con buffer del area de influencia.

    Ejecutarla de oficio evita repetir ese descubrimiento en cada proyecto.
    """
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsSpatialIndex

    if not tramos:
        return {"tramos": 0}

    indice = QgsSpatialIndex()
    por_id: dict[int, Tramo] = {}
    for tramo in tramos:
        entidad = QgsFeature(tramo.identificador)
        entidad.setGeometry(tramo.geometria)
        indice.addFeature(entidad)
        por_id[tramo.identificador] = tramo

    distancias: list[float] = []
    for tramo in tramos:
        punto = QgsGeometry.fromPointXY(QgsPointXY(*tramo.fin))
        caja = punto.boundingBox()
        caja.grow(radio_m)
        mejor = float("inf")
        for candidato_id in indice.intersects(caja):
            if candidato_id == tramo.identificador:
                continue
            candidato = por_id[candidato_id]
            if _misma_posicion(candidato.fin, tramo.fin, tolerancia_m):
                continue
            distancia = candidato.geometria.distance(punto)
            if distancia < mejor:
                mejor = distancia
        if mejor < float("inf"):
            distancias.append(mejor)

    if not distancias:
        return {"tramos": len(tramos), "con_receptor": 0}

    distancias.sort()
    total = len(distancias)
    umbrales = (0.0, tolerancia_m, 50.0, 200.0, 500.0)
    return {
        "tramos": len(tramos),
        "con_receptor": total,
        "sin_receptor": len(tramos) - total,
        "acumulado_pct": {
            f"<= {u:g} m": round(
                100.0 * sum(1 for d in distancias if d <= u) / total, 2)
            for u in umbrales
        },
        "mediana_m": round(distancias[total // 2], 2),
        "p90_m": round(distancias[int(total * 0.9)], 2),
        "tolerancia_util": (
            "si" if sum(1 for d in distancias if d <= tolerancia_m) / total > 0.95
            else "no: ampliarla apenas recupera tramos, compensar con buffer"
        ),
    }

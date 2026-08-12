#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M02b - Red de drenaje topológica
================================
Entorno: Python de QGIS.

Construye la ÚNICA red del estudio con topología resuelta, y la deja como capa
para que la consuman los módulos de análisis. El M10 la usa para el orden de
corrientes, la razón de bifurcación y el cauce principal; el M16 para la
cartografía temática.

POR QUÉ EXISTE COMO PASO APARTE. La cartografía del IGAC parte los ríos anchos
en dos representaciones: donde caben dos orillas a escala 1:100.000 se dibuja un
POLÍGONO, y aguas arriba una POLILÍNEA. Medido sobre este estudio, el Río Bogotá
aparece como una polilínea de 85,4 km que cubre el norte de la cuenca (Y de
2.107.033 a 2.136.694) y como un polígono que cubre el resto hacia el sur (Y de
2.032.121 a 2.107.689). Se tocan en Y = 2.107.000.

La consecuencia es la que importa: trazar el cauce principal sobre la capa de
líneas devolvería 85 km de los más de 300 reales, sin ninguna señal de error. Es
el mismo modo de fallo que produjo una cuenca de 6,59 km2 en el M02 antes de
sustituir el análisis de terreno por el cartográfico.

Cinco etapas:

    1. lectura del drenaje sencillo recortado y validación de su sentido
    2. derivación del eje de los cauces dobles, por rasterización y
       adelgazamiento, y orientación con la cota del DEM
    3. adyacencia por proximidad, porque la red del IGAC no es topológica: el
       afluente termina sobre el trazado del receptor, no en un vértice suyo
    4. empalme del eje con el drenaje sencillo, con tolerancia mayor
    5. orden de Strahler y conteo de corrientes

El motor está en 'red_drenaje.py', que ya se usaba para la delimitación
cartográfica del M02. Este módulo lo encadena y persiste el resultado, que hasta
ahora se calculaba y se descartaba en memoria.

Productos:
    data/03_SIG/vector/eje_drenaje_doble.shp
    data/03_SIG/vector/red_topologica.shp
    data/02_procesado/M02b_red_drenaje.json
    data/02_procesado/red_drenaje/M02b_red_drenaje.md

Uso:
    "C:/Program Files/QGIS <version>/bin/python-qgis.bat" src/M02b_red_drenaje.py

Códigos de salida:
    0  red construida
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import red_drenaje as red  # noqa: E402
import sig  # noqa: E402
from comun import campos as mod_campos  # noqa: E402
from comun import esquema, raster, registro, rutas  # noqa: E402
from comun.campos import CampoSalida  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M02b"
DESCRIPCION = "Red de drenaje topológica"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

CAMPOS_RED = (
    CampoSalida("id_tramo", "Identificador del tramo", "entero", 9),
    CampoSalida("receptor", "Tramo receptor aguas abajo", "entero", 9),
    CampoSalida("orden", "Orden de Strahler", "entero", 3),
    CampoSalida("long_m", "Longitud del tramo", "decimal", 12, 2, "m"),
    CampoSalida("origen", "Representación cartográfica de origen", "texto", 12),
    CampoSalida("nombre", "Nombre geográfico", "texto", 80),
)

CAMPOS_EJE = (
    CampoSalida("id_tramo", "Identificador del tramo", "entero", 9),
    CampoSalida("long_m", "Longitud del tramo", "decimal", 12, 2, "m"),
    CampoSalida("nombre", "Nombre geográfico", "texto", 80),
)

# Valor que se escribe cuando un tramo no desemboca en ningún otro. Es una
# desembocadura de la red, no un error: la red recortada tiene varias.
SIN_RECEPTOR = -1


@dataclass
class ResultadoM02b:
    tramos_sencillo: int = 0
    tramos_eje: int = 0
    tramos_totales: int = 0
    sentido: dict[str, Any] = field(default_factory=dict)
    orientacion: dict[str, Any] = field(default_factory=dict)
    empalme: dict[str, Any] = field(default_factory=dict)
    corrientes: dict[int, int] = field(default_factory=dict)
    bifurcacion: dict[str, Any] = field(default_factory=dict)
    orden_maximo: int = 0
    ciclos: list[int] = field(default_factory=list)
    desembocaduras: int = 0
    espolones: int = 0
    embalses: list = field(default_factory=list)
    capas: list[str] = field(default_factory=list)
    diccionarios: list[str] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Cota del terreno
# =============================================================================
def muestreador_de_cota(ruta_dem: Path):
    """
    Devuelve una función (x, y) -> cota, leyendo el DEM sin GDAL.

    La orientación del eje necesita saber hacia dónde baja el terreno. Se usa
    'comun/raster.py' y no la API de ráster de QGIS por una razón práctica: es
    el mismo lector que emplea el M10 en el venv, de modo que la cota que
    orienta la red y la que produce los parámetros de relieve salen del mismo
    código y no pueden discrepar.

    Fuera del ráster o sobre una celda sin dato devuelve NaN, que es lo que
    'orientar_eje' espera para descartar la muestra.
    """
    info = raster.leer_info(ruta_dem)
    lector = raster.LectorRaster(ruta_dem)
    import struct

    formato = {"<f4": "f", "<u2": "H", "<i2": "h", "<f8": "d"}.get(
        info.descriptor)
    if formato is None:
        lector.cerrar()
        raise ErrorFormato(
            f"{ruta_dem.name}: tipo {info.descriptor} no muestreable como cota.")
    tamano = info.bytes_por_muestra

    def cota(x: float, y: float) -> float:
        columna = info.columna_de(x)
        fila = info.fila_de(y)
        if not (0 <= columna < info.ancho and 0 <= fila < info.alto):
            return float("nan")
        crudo = lector.fila(fila)
        desde = columna * tamano
        valor = struct.unpack_from("<" + formato, crudo, desde)[0]
        if info.nodato is not None and valor == info.nodato:
            return float("nan")
        return float(valor)

    cota.cerrar = lector.cerrar  # type: ignore[attr-defined]
    return cota


# =============================================================================
# Construcción de la red
# =============================================================================
def renumerar(*grupos) -> list:
    """
    Reasigna identificadores correlativos a la unión de varios grupos de tramos.

    Es imprescindible: 'leer_tramos' numera desde cero en cada capa, de modo que
    unir el drenaje sencillo con el eje sin renumerar haría que dos tramos
    distintos compartieran identificador y la adyacencia mezclaría ramas de la
    red sin dejar ninguna señal.
    """
    from dataclasses import replace

    unidos = []
    contador = 0
    for grupo in grupos:
        for tramo in grupo:
            unidos.append(replace(tramo, identificador=contador))
            contador += 1
    return unidos


def nombrar_eje(tramos_eje, ruta_poligonos: Path, campo_nombre: str) -> int:
    """
    Da a cada pieza del eje el nombre del cauce doble del que salió.

    El adelgazamiento produce geometría, no atributos, de modo que las piezas
    del eje nacen anónimas. Sin nombre, el cauce principal que traza el M10
    aparece como una sucesión de tramos sin identificar y no hay forma de
    comprobar de un vistazo que efectivamente recorre el río que debe recorrer.
    Ese control barato es el que atrapa un eje mal empalmado.

    Devuelve cuántas piezas quedaron nombradas.
    """
    from qgis.core import QgsFeature, QgsSpatialIndex, QgsVectorLayer

    capa = QgsVectorLayer(str(ruta_poligonos), ruta_poligonos.stem, "ogr")
    if not capa.isValid():
        raise ErrorFormato(f"QGIS no pudo abrir {ruta_poligonos}")
    if capa.fields().indexOf(campo_nombre) < 0:
        return 0

    indice = QgsSpatialIndex()
    nombres: dict[int, str] = {}
    geometrias: dict[int, Any] = {}
    for entidad in capa.getFeatures():
        copia = QgsFeature(entidad.id())
        copia.setGeometry(entidad.geometry())
        indice.addFeature(copia)
        nombres[entidad.id()] = (str(entidad[campo_nombre]).strip()
                                 if entidad[campo_nombre] else "")
        geometrias[entidad.id()] = entidad.geometry()

    nombrados = 0
    for tramo in tramos_eje:
        centro = tramo.geometria.interpolate(tramo.geometria.length() / 2.0)
        if centro is None or centro.isEmpty():
            continue
        caja = centro.boundingBox()
        caja.grow(1.0)
        for identificador in indice.intersects(caja):
            if geometrias[identificador].contains(centro):
                if nombres[identificador]:
                    tramo.nombre = nombres[identificador]
                    nombrados += 1
                break
    return nombrados


def receptores_de(afluentes: dict[int, list[int]]) -> dict[int, int]:
    """Invierte la relación de afluencia: de cada tramo, hacia dónde va."""
    hacia: dict[int, int] = {}
    for receptor, tributarios in afluentes.items():
        for tributario in tributarios:
            hacia[tributario] = receptor
    return hacia


def descartar_espolones(
    tramos, afluentes: dict[int, list[int]], longitud_minima_m: float
) -> tuple[list, dict[int, list[int]], int]:
    """
    Quita del eje los fragmentos sueltos que deja el adelgazamiento.

    El criterio es TOPOLÓGICO y no de longitud. Medido sobre el eje de este
    estudio, el percentil 25 de las piezas vale 12,5 m, una sola celda: el
    adelgazamiento entrega el esqueleto troceado y esas piezas cortas son los
    ESLABONES que lo encadenan, no espolones. Filtrarlas por longitud parte el
    eje en fragmentos y deja el cauce principal cortado, que es exactamente el
    fallo que este módulo existe para evitar.

    Se descarta solo lo que además está SUELTO: una pieza de eje corta, sin
    tributarios y sin receptor, no conecta nada y solo puede sumar una
    corriente de orden 1 que no existe. Quitarla no puede romper la red porque
    no forma parte de ninguna cadena.

    No se itera. Una pieza que quede sin tributarios tras la poda puede ser un
    eslabón legítimo de la cadena, y encadenar podas se comería el eje entero
    desde la cabecera.
    """
    hacia = receptores_de(afluentes)
    sueltos = {
        t.identificador for t in tramos
        if t.origen == "eje_doble"
        and t.longitud_m < longitud_minima_m
        and not afluentes.get(t.identificador)
        and t.identificador not in hacia
    }
    if not sueltos:
        return list(tramos), afluentes, 0

    conservados = [t for t in tramos if t.identificador not in sueltos]
    podado = {receptor: [t for t in tributarios if t not in sueltos]
              for receptor, tributarios in afluentes.items()
              if receptor not in sueltos}
    return conservados, {r: v for r, v in podado.items() if v}, len(sueltos)


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Encadena las cinco etapas y persiste la red."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    ruta_sencillo = rutas.resolver(
        configuracion.obtener("referencia_nacional.salida_recorte_sencillo"), base)
    ruta_doble = rutas.resolver(
        configuracion.obtener("referencia_nacional.salida_recorte_doble"), base)
    ruta_dem = rutas.resolver(
        configuracion.obtener("dem.delimitacion.salida_dem"), base)
    # La extensión de trabajo es la de BÚSQUEDA, la subzona del M01, y no el
    # área de influencia. El área se acota trazando sobre esta misma red, de
    # modo que usarla aquí realimentaría: red más pequeña, área más pequeña,
    # red más pequeña todavía, encogiendo el estudio en cada pasada sin que
    # nada lo señale.
    ruta_area = rutas.resolver(
        configuracion.obtener("subzonas_hidrograficas.salida_subzona"), base)

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "drenaje sencillo": rutas.relativa(ruta_sencillo, base),
            "drenaje doble": rutas.relativa(ruta_doble, base),
            "modelo de elevación": rutas.relativa(ruta_dem, base),
            "extensión de trabajo": rutas.relativa(ruta_area, base),
        },
        parametros=configuracion.parametros((
            "crs.calculo",
            "red_topologica.resolucion_eje_m",
            "red_topologica.longitud_minima_tramo_m",
            "referencia_nacional.tolerancia_conexion_m",
            "referencia_nacional.max_incumplimiento_sentido_pct",
            "dem.delimitacion.tolerancia_empalme_eje_m",
        )),
    )

    resultado = ResultadoM02b()
    faltan = [rutas.relativa(r, base) for r in
              (ruta_sencillo, ruta_doble, ruta_dem, ruta_area) if not r.is_file()]
    if faltan:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "red.insumos",
            f"faltan insumos del M02: {', '.join(faltan)}. Ejecutar el M02."))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    crs_calculo = str(configuracion.obtener("crs.calculo"))
    campo_nombre = str(configuracion.obtener("referencia_nacional.campos.nombre"))
    tolerancia = float(configuracion.obtener(
        "referencia_nacional.tolerancia_conexion_m"))
    tolerancia_empalme = float(configuracion.obtener(
        "dem.delimitacion.tolerancia_empalme_eje_m"))
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")

    prefijo = configuracion.obtener("entornos.qgis.prefix_path")
    sig.iniciar_qgis(prefijo)
    sig.inicializar_processing(prefijo, logger)

    from qgis.core import QgsVectorLayer

    capa_area = QgsVectorLayer(str(ruta_area), "area", "ogr")
    if not capa_area.isValid():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "red.area", f"QGIS no pudo abrir {ruta_area}."))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)
    geometria_area = next(capa_area.getFeatures()).geometry()

    # --- 1. Drenaje sencillo -------------------------------------------------
    with registro.bloque(logger, "Lectura del drenaje sencillo"):
        tramos_sencillo = red.leer_tramos(ruta_sencillo, campo_nombre, "sencillo")
        resultado.tramos_sencillo = len(tramos_sencillo)
        sentido = red.validar_sentido(
            tramos_sencillo,
            float(configuracion.obtener(
                "referencia_nacional.max_incumplimiento_sentido_pct")))
        resultado.sentido = sentido.como_dict()
        logger.info("%d tramos | %d nodos | %.2f %% incumple el sentido",
                    sentido.tramos, sentido.nodos, sentido.incumplimiento_pct)
        if not sentido.aceptable:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "red.sentido",
                f"el {sentido.incumplimiento_pct:.2f} % de los nodos incumple "
                "la convención de sentido de digitalización, por encima del "
                "máximo declarado. Sin esa convención el sentido de flujo de "
                "cada tramo es una suposición, y con ella lo son el orden de "
                "Strahler y el cauce principal."))
            return _cerrar(logger, resultado, base, ruta_json, inicio,
                           SALIDA_BLOQUEANTE)

    # --- 2. Eje de los cauces dobles ----------------------------------------
    with registro.bloque(logger, "Eje de los cauces dobles"):
        destino_eje = rutas.resolver(
            configuracion.obtener("red_topologica.salida_eje"), base)
        with tempfile.TemporaryDirectory() as temporal:
            # LOS EMBALSES NO SE RASTERIZAN CON LOS CAUCES. Se intentó, y
            # el resultado lo desmiente: un embalse tiene lámina plana y su
            # esqueleto recorre los brazos laterales del polígono, no un cauce.
            # Medido sobre el Embalse San Rafael, la cadena derivada sube de
            # 2.789 a 3.097 m, imposible en un curso de agua.
            #
            # 'orientar_eje' decide el sentido por componente con una
            # referencia de cota, y sobre una mancha ramificada eso no tiene
            # solución: la dirección de flujo dentro de un embalse no existe.
            #
            # Un embalse es un NODO y no un tramo: entra agua por varios
            # afluentes y sale por uno. Conectarlo exige unir sus entradas con
            # su salida, no inventarle un canal. El recorte de embalses se
            # produce y se conserva para ese trabajo, que queda PENDIENTE, y
            # mientras tanto la red sigue cortada en cada embalse.
            red.eje_de_poligonos(
                ruta_doble, destino_eje,
                float(configuracion.obtener("red_topologica.resolucion_eje_m")),
                geometria_area.boundingBox(), crs_calculo, Path(temporal))
        tramos_eje = red.leer_tramos(destino_eje, campo_nombre, "eje_doble")
        nombrados = nombrar_eje(tramos_eje, ruta_doble, campo_nombre)
        logger.info("%d pieza(s) de eje derivadas, %d con nombre del cauce",
                    len(tramos_eje), nombrados)

        cota = muestreador_de_cota(ruta_dem)
        try:
            tramos_eje, orientacion = red.orientar_eje(
                tramos_eje, cota, limite=geometria_area)
        finally:
            cota.cerrar()
        resultado.orientacion = orientacion
        resultado.tramos_eje = len(tramos_eje)
        logger.info("Eje orientado: %s", orientacion)

        if not tramos_eje:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "red.eje",
                "no se derivó ningún tramo de eje de los cauces dobles. Sin él "
                "la red queda cortada donde el río se representa como polígono, "
                "y el cauce principal sale truncado sin señal de error."))
            return _cerrar(logger, resultado, base, ruta_json, inicio,
                           SALIDA_BLOQUEANTE)

    # --- 3. Unión, adyacencia y empalme -------------------------------------
    with registro.bloque(logger, "Topología de la red"):
        tramos = renumerar(tramos_sencillo, tramos_eje)
        afluentes = red.construir_adyacencia(tramos, tolerancia)
        resultado.empalme = red.empalmar_eje(tramos, afluentes,
                                             tolerancia_empalme)
        logger.info("Empalme del eje con el drenaje sencillo: %s",
                    resultado.empalme)

        # --- Puente sobre los embalses --------------------------------------
        # Se hace DESPUES del empalme y ANTES de podar y ordenar: el puente
        # necesita la adyacencia ya resuelta para no duplicar aristas, y el
        # orden de Strahler necesita la red ya continua.
        ruta_embalses = rutas.resolver(
            configuracion.obtener(
                "referencia_nacional.salida_recorte_embalses"), base)
        if ruta_embalses.is_file():
            from comun import shapefile as shp

            nombres_emb = list(shp.leer_registros(ruta_embalses, [campo_nombre]))
            geoms_emb = shp.leer_geometrias(ruta_embalses)
            embalses = [(str(r.get(campo_nombre, "")).strip() or "sin nombre", g)
                        for r, g in zip(nombres_emb, geoms_emb)]
            extremos = [(t.identificador, t.inicio, t.fin) for t in tramos]
            cota_emb = muestreador_de_cota(ruta_dem)
            try:
                afluentes, resultado.embalses = red.puentear_embalses(
                    extremos, afluentes, embalses, tolerancia_empalme,
                    cota_emb)
            finally:
                cota_emb.cerrar()
            puenteados = sum(e["puenteados"] for e in resultado.embalses)
            con_puente = [e for e in resultado.embalses if e["puenteados"]]
            logger.info("Embalses: %d evaluados, %d puenteados, %d tramo(s) "
                        "reconectados", len(embalses), len(con_puente),
                        puenteados)
            sumideros = [e["embalse"] for e in resultado.embalses
                         if "sumidero" in e["motivo"]]
            if sumideros:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, "red.embalses_sin_salida",
                    f"{len(sumideros)} embalse(s) sin salida identificada: la "
                    "red termina en ellos y lo que drena aguas arriba no "
                    f"alcanza el punto. {', '.join(sumideros[:6])}. Suele ser "
                    "un brazo mal cerrado en la cartografia, o un embalse cuyo "
                    "desague no esta digitalizado.",
                ))
            varias = [e for e in resultado.embalses if e["salidas"] > 1]
            if varias:
                resultado.hallazgos.append(Hallazgo(
                    INFORMATIVO, "red.embalses_varias_salidas",
                    f"{len(varias)} embalse(s) con mas de una salida. Se adopto "
                    "la de menor cota. Es posible (trasvases), pero tambien es "
                    "la senal de una cartografia con un brazo mal cerrado: "
                    + "; ".join(f"{e['embalse']} ({e['salidas']})"
                                for e in varias[:5]),
                ))
        else:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "red.embalses",
                f"no se encuentra {rutas.relativa(ruta_embalses, base)}: la red "
                "queda CORTADA en cada embalse y lo que drena aguas arriba de "
                "ellos no alcanza el punto. Ejecutar el M02 --fase preliminar.",
            ))

        tramos, afluentes, sueltos = descartar_espolones(
            tramos, afluentes,
            float(configuracion.obtener("red_topologica.longitud_minima_tramo_m")))
        resultado.espolones = sueltos
        if sueltos:
            logger.info("%d fragmento(s) sueltos del eje descartados", sueltos)
        resultado.tramos_totales = len(tramos)

        hacia = receptores_de(afluentes)
        resultado.desembocaduras = sum(1 for t in tramos
                                       if t.identificador not in hacia)
        sin_receptor_eje = sum(1 for t in tramos if t.origen == "eje_doble"
                               and t.identificador not in hacia)
        logger.info("%d desembocadura(s) en la red, %d de ellas del eje",
                    resultado.desembocaduras, sin_receptor_eje)

    # --- 4. Jerarquía --------------------------------------------------------
    with registro.bloque(logger, "Orden de Strahler"):
        identificadores = [t.identificador for t in tramos]
        orden, ciclos = red.orden_strahler(afluentes, identificadores)
        resultado.ciclos = ciclos
        resultado.orden_maximo = max(orden.values()) if orden else 0
        resultado.corrientes = red.contar_corrientes(orden, afluentes)
        resultado.bifurcacion = red.razon_bifurcacion(resultado.corrientes)

        logger.info("Orden máximo %d | corrientes por orden %s",
                    resultado.orden_maximo, resultado.corrientes)
        media = resultado.bifurcacion.get("adoptada")
        if media is not None:
            logger.info("Razón de bifurcación %.2f ponderada "
                        "(%.2f sin ponderar)", media,
                        resultado.bifurcacion["media_simple"])

        if ciclos:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "red.ciclos",
                f"{len(ciclos)} tramo(s) forman ciclo en la relación de "
                "afluencia. La adyacencia se resuelve por proximidad y no por "
                "topología declarada, de modo que dos tramos pueden quedar "
                "señalándose el uno al otro. Su orden queda indefinido y se "
                f"reporta como 1. Identificadores: {ciclos[:10]}."))

        if media is not None and not resultado.bifurcacion[
                "dentro_del_rango_natural"]:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "red.bifurcacion",
                f"la razón de bifurcación ponderada es {media:.2f}, fuera del rango "
                "de 3 a 5 que Horton observó en redes naturales. No invalida la "
                "cuenca, pero suele delatar un control estructural del terreno "
                "o, más a menudo, una cartografía con detalle desigual: si la "
                "parte alta se levantó con más densidad que la baja, las "
                "corrientes de orden 1 salen infladas."))

    # --- 5. Escritura --------------------------------------------------------
    with registro.bloque(logger, "Escritura de la red"):
        destino_red = rutas.resolver(
            configuracion.obtener("red_topologica.salida_red"), base)
        sig.escribir_capa(
            destino=destino_red, campos_salida=list(CAMPOS_RED),
            geometrias=[t.geometria for t in tramos],
            valores=[{
                "id_tramo": t.identificador,
                "receptor": hacia.get(t.identificador, SIN_RECEPTOR),
                "orden": orden.get(t.identificador, 1),
                "long_m": round(t.longitud_m, 2),
                "origen": t.origen,
                "nombre": t.nombre,
            } for t in tramos],
            crs_id=crs_calculo, tipo_geometria="LineString")
        _registrar_capa(resultado, base, destino_red, CAMPOS_RED, delimitador)
        _registrar_capa(resultado, base, destino_eje, CAMPOS_EJE, delimitador)

        _escribir_informe(configuracion, base, resultado)

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "red.resumen",
        f"red de {resultado.tramos_totales} tramos, orden máximo "
        f"{resultado.orden_maximo}, {resultado.desembocaduras} desembocadura(s). "
        f"El eje derivado aporta {resultado.tramos_eje} tramo(s) que la capa de "
        "líneas del IGAC no contiene, y sin los cuales el cauce principal "
        "quedaría cortado donde el río pasa a representarse como polígono."))

    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _registrar_capa(resultado, base, destino, campos_salida, delimitador) -> None:
    resultado.capas.append(rutas.relativa(destino, base))
    resultado.diccionarios.append(rutas.relativa(mod_campos.escribir_diccionario(
        campos_salida, destino.with_name(f"{destino.stem}_campos.csv"),
        destino.stem, delimitador,
    ), base))


def _escribir_informe(configuracion, base, resultado) -> None:
    directorio = rutas.directorio("procesado", base, crear=True) / "red_drenaje"
    directorio.mkdir(parents=True, exist_ok=True)
    destino = directorio / "M02b_red_drenaje.md"

    lineas = [
        "# M02b - Red de drenaje topologica",
        "",
        f"* Tramos del drenaje sencillo: {resultado.tramos_sencillo}",
        f"* Tramos del eje derivado: {resultado.tramos_eje}",
        f"* Tramos de la red: {resultado.tramos_totales}",
        f"* Orden maximo de Strahler: {resultado.orden_maximo}",
        f"* Desembocaduras: {resultado.desembocaduras}",
        "",
        "## Por que hace falta derivar un eje",
        "",
        "La cartografia del IGAC representa un rio como POLIGONO donde caben",
        "dos orillas a 1:100.000 y como POLILINEA aguas arriba. La capa de",
        "lineas no contiene el eje de los poligonos, de modo que la red queda",
        "cortada justo en el cauce principal del estudio. Trazarlo sin reponer",
        "ese eje devuelve una fraccion de su longitud real, y lo hace sin",
        "ninguna senal de error.",
        "",
        "## Corrientes por orden",
        "",
        "| orden | corrientes |",
        "|---|---|",
    ]
    for orden, cuantas in sorted(resultado.corrientes.items()):
        lineas.append(f"| {orden} | {cuantas} |")

    lineas += [
        "",
        "Se cuentan CORRIENTES y no tramos. Una corriente de orden n es la",
        "cadena completa de tramos consecutivos de ese orden: la cartografia",
        "parte un mismo rio en decenas de piezas por razones de dibujo, y",
        "contarlas multiplicaria el resultado por un factor arbitrario.",
        "",
        "## Razon de bifurcacion",
        "",
    ]
    pares = resultado.bifurcacion.get("pares") or []
    if pares:
        lineas += ["| ordenes | corrientes | corrientes | razon | peso |",
                   "|---|---|---|---|---|"]
        for par in pares:
            lineas.append(
                f"| {par['orden']} | {par['corrientes_menor']} | "
                f"{par['corrientes_mayor']} | {par['razon']} | {par['peso']} |")
        lineas += [
            "",
            f"Media ponderada: **{resultado.bifurcacion['media_ponderada']}**",
            f"(sin ponderar seria {resultado.bifurcacion['media_simple']}).",
            "",
            "Se adopta la PONDERADA, que es la que propuso Strahler. La media",
            "simple da el mismo peso al cociente entre los dos ordenes mas",
            "bajos, calculado sobre miles de corrientes, y al de los dos mas",
            "altos, calculado sobre una o dos: en el ultimo par el denominador",
            "vale 1 por definicion en una cuenca de una sola salida, de modo",
            "que ese cociente es grande siempre y arrastra la media sin",
            "aportar informacion.",
            "",
            "Horton observo que en redes naturales la razon cae entre 3 y 5.",
            "Fuera de ese rango suele delatar un control estructural del",
            "terreno o una cartografia con detalle desigual.",
        ]
    else:
        lineas.append("No hay ordenes consecutivos con los que formar la razon.")

    lineas += ["", "## Trazabilidad", ""]
    for clave, valor in (("sentido de digitalizacion", resultado.sentido),
                         ("orientacion del eje", resultado.orientacion),
                         ("empalme", resultado.empalme)):
        lineas.append(f"* {clave}: `{valor}`")
    lineas.append("")

    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    resultado.productos.append(rutas.relativa(destino, base))


def _cerrar(logger, resultado, base, ruta_json, inicio, codigo):
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
            emitir("  %-36s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    destino_json = (Path(ruta_json) if ruta_json is not None else
                    rutas.directorio("procesado", base, crear=True)
                    / f"{MODULO}_red_drenaje.json")
    destino_json.parent.mkdir(parents=True, exist_ok=True)
    destino_json.write_text(json.dumps({
        "modulo": MODULO,
        "tramos_sencillo": resultado.tramos_sencillo,
        "tramos_eje": resultado.tramos_eje,
        "tramos_totales": resultado.tramos_totales,
        "orden_maximo": resultado.orden_maximo,
        "desembocaduras": resultado.desembocaduras,
        "fragmentos_sueltos_descartados": resultado.espolones,
        "embalses": resultado.embalses,
        "corrientes_por_orden": resultado.corrientes,
        "razon_bifurcacion": resultado.bifurcacion,
        "sentido": resultado.sentido,
        "orientacion": resultado.orientacion,
        "empalme": resultado.empalme,
        "ciclos": resultado.ciclos,
        "capas": resultado.capas,
        "diccionarios": resultado.diccionarios,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    productos = {f"capa {i}": capa
                 for i, capa in enumerate(resultado.capas, start=1)}
    for indice, producto in enumerate(resultado.productos, start=1):
        productos[f"informe {indice}"] = producto
    productos["reporte JSON"] = rutas.relativa(destino_json, base)
    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecucion"] = rutas.relativa(archivo_log, base)

    registro.registrar_cierre(
        logger, MODULO, "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO",
        segundos=time.perf_counter() - inicio, productos=productos)
    sig.finalizar_qgis()
    return codigo, hallazgos


def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(
        description=f"{MODULO} - {DESCRIPCION}")
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato, ErrorHidrologia) as error:
        print(f"[{MODULO}] {error}", file=sys.stderr)
        return SALIDA_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

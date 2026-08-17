#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M16 - Cartografía temática
==========================
Entorno: Python de QGIS (OSGeo4W Shell en Windows). No importa ninguna librería
del venv del proyecto.

Produce las planchas del anexo cartográfico a partir del catálogo declarado en
config/mapas.yaml. Cada plancha se compone sobre una plantilla .qpt, se exporta
a PDF y a PNG, y queda registrada con su escala y sus capas.

TRES COSAS FUERA DEL CÓDIGO, igual que en el informe: el catálogo de planchas
está en config/mapas.yaml, el marco de la plancha en un .qpt que el consultor
ajusta en QGIS, y la simbología en los mismos .qml que usa el M00b. Añadir un
mapa, mover el rótulo o cambiar un color no exige tocar Python.

LA ESCALA SE CALCULA, NO SE DECLARA. Se toma la extensión de la capa que
encuadra, se le da holgura, y se redondea HACIA ARRIBA a la serie normalizada
declarada en config.yaml. Fijar la escala a mano es lo que produce planchas
donde la cuenca no cabe: una lista de escalas grandes es inservible para una
cuenca de decenas de kilómetros, y el error no se ve hasta imprimir.

LA PLANTILLA SE GENERA UNA VEZ Y NO SE SOBRESCRIBE. Si el .qpt declarado no
existe, el módulo escribe uno de partida con mapa, grilla de coordenadas, norte,
escala gráfica y numérica, leyenda, rótulo y créditos. A partir de ahí manda el
del consultor: se abre en QGIS, se ajusta al formato de casa y se guarda. Es el
mismo camino de la plantilla de Word.

QUÉ SIGNIFICA UNA CAPA SIN .qml. QGIS le asigna un color aleatorio. La plancha
sale geométricamente correcta y cromáticamente arbitraria, y eso no es
entregable. El módulo lo reporta como advertencia por capa.

Uso:
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" src/M16_cartografia.py
    ... src/M16_cartografia.py --solo localizacion --solo red_drenaje
    ... src/M16_cartografia.py --solo-plantilla
    ... src/M16_cartografia.py --json logs/M16_reporte.json

Códigos de salida:
    0  planchas producidas sin hallazgos bloqueantes
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración, la declaración o inicializar QGIS
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import entorno, esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorEntorno,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M16"
DESCRIPCION = "Cartografía temática"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

TIPOS_CAPA = ("vector", "raster")
PROVEEDOR = {"vector": "ogr", "raster": "gdal"}

# Intervalos de grilla admitidos, en metros. Son las cifras que se leen sin
# esfuerzo en el margen de una plancha; 3.700 m sería exacto y también ilegible.
INTERVALOS_GRILLA = (50, 100, 200, 250, 500, 1000, 2000, 2500, 5000,
                     10000, 20000, 25000, 50000, 100000)


# =============================================================================
# Estructuras de la declaración
# =============================================================================
@dataclass(frozen=True)
class CapaDeclarada:
    """Una capa del catálogo, ya resuelta a una ruta concreta del disco."""

    identificador: str
    tipo: str
    ruta: Path
    estilo: str | None
    nombre: str = ""

    @property
    def existe(self) -> bool:
        return self.ruta.is_file()


@dataclass(frozen=True)
class Plancha:
    """Una plancha del juego, con sus capas en orden de dibujo."""

    identificador: str
    titulo: str
    subtitulo: str
    nota: str
    encuadre: str
    capas: tuple[CapaDeclarada, ...]
    esenciales: tuple[str, ...] = ()
    escala_forzada: int | None = None


@dataclass
class ResultadoM16:
    """Lo que el módulo produjo y lo que midió al hacerlo."""

    plantilla: str = ""
    plantilla_creada: bool = False
    planchas: list[dict[str, Any]] = field(default_factory=list)
    omitidas: list[dict[str, Any]] = field(default_factory=list)
    capas_ausentes: list[str] = field(default_factory=list)
    capas_sin_estilo: list[str] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Escala y encuadre. Funciones puras: son el núcleo verificable del módulo.
# =============================================================================
def marco_del_mapa(plancha: dict[str, Any]) -> tuple[float, float, float, float]:
    """
    Rectángulo del marco cartográfico dentro de la plancha, en milímetros.

    Devuelve (x, y, ancho, alto). El panel de leyenda y rótulo ocupa la franja
    derecha, de modo que el mapa toma lo que queda entre los márgenes.

    Excepciones
    -----------
    ErrorConfiguracion
        Si las cifras dejan un marco de ancho o alto no positivo.
    """
    try:
        ancho = float(plancha["ancho_mm"])
        alto = float(plancha["alto_mm"])
        margen = float(plancha["margen_mm"])
        panel = float(plancha["ancho_panel_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ErrorConfiguracion(
            f"la plancha declarada no es utilizable: {exc}.") from exc

    ancho_marco = ancho - 2 * margen - panel - margen
    alto_marco = alto - 2 * margen
    if ancho_marco <= 0 or alto_marco <= 0:
        raise ErrorConfiguracion(
            f"la plancha de {ancho:g} x {alto:g} mm con margen {margen:g} mm y "
            f"panel {panel:g} mm no deja marco de mapa "
            f"({ancho_marco:g} x {alto_marco:g} mm).")
    return margen, margen, ancho_marco, alto_marco


def escala_normalizada(
    ancho_m: float,
    alto_m: float,
    ancho_marco_mm: float,
    alto_marco_mm: float,
    serie: Sequence[int],
    margen: float = 0.0,
) -> int:
    """
    Escala de la serie normalizada que hace caber la extensión en el marco.

    SE REDONDEA HACIA ARRIBA, nunca hacia el valor más próximo: una escala una
    posición por debajo deja la cuenca fuera del marco, y eso no se ve hasta
    imprimir. Se prefiere una plancha con aire a una plancha recortada.

    Parámetros
    ----------
    ancho_m, alto_m
        Extensión que debe caber, en metros del sistema de cálculo.
    ancho_marco_mm, alto_marco_mm
        Marco cartográfico disponible, en milímetros.
    serie
        Escalas admitidas, en cualquier orden.
    margen
        Holgura sobre la extensión antes de redondear. 0.05 es un 5 por ciento.

    Excepciones
    -----------
    ErrorHidrologia
        Si la extensión no es positiva, o si ninguna escala de la serie basta.
    """
    if ancho_m <= 0 or alto_m <= 0:
        raise ErrorHidrologia(
            f"la extensión a encuadrar es degenerada: {ancho_m:g} x {alto_m:g} "
            "m. La capa de encuadre tiene una sola entidad puntual o está "
            "vacía.")
    if ancho_marco_mm <= 0 or alto_marco_mm <= 0:
        raise ErrorHidrologia("el marco del mapa no tiene superficie.")

    factor = 1.0 + max(0.0, float(margen))
    necesaria = max(ancho_m / (ancho_marco_mm / 1000.0),
                    alto_m / (alto_marco_mm / 1000.0)) * factor
    for escala in sorted(int(v) for v in serie):
        if escala >= necesaria:
            return escala
    raise ErrorHidrologia(
        f"la extensión exige una escala de 1:{necesaria:,.0f} y la mayor "
        f"declarada es 1:{max(int(v) for v in serie):,.0f}. Ampliar "
        "cartografia.serie_escalas.")


def extension_para_escala(
    extension: tuple[float, float, float, float],
    escala: int,
    ancho_marco_mm: float,
    alto_marco_mm: float,
) -> tuple[float, float, float, float]:
    """
    Extensión centrada que llena el marco EXACTAMENTE a la escala pedida.

    Sin esto la escala impresa no es la anunciada: QGIS ajusta la extensión al
    marco y la escala resultante queda en una cifra rota. Un mapa cuyo rótulo
    dice 1:50.000 y mide 1:47.318 no es defendible.
    """
    x_min, y_min, x_max, y_max = extension
    centro_x = (x_min + x_max) / 2.0
    centro_y = (y_min + y_max) / 2.0
    ancho = escala * (ancho_marco_mm / 1000.0)
    alto = escala * (alto_marco_mm / 1000.0)
    return (centro_x - ancho / 2.0, centro_y - alto / 2.0,
            centro_x + ancho / 2.0, centro_y + alto / 2.0)


def intervalo_de_grilla(
    ancho_terreno_m: float,
    divisiones: int = 5,
    admitidos: Sequence[int] = INTERVALOS_GRILLA,
) -> int:
    """
    Separación de la grilla de coordenadas, en metros, redondeada a cifra legible.

    Se busca el intervalo admitido más próximo al que daría el número de
    divisiones pedido. Nunca devuelve cero.
    """
    if ancho_terreno_m <= 0 or divisiones <= 0:
        return int(admitidos[0])
    objetivo = ancho_terreno_m / float(divisiones)
    return min(admitidos, key=lambda v: abs(math.log(v / objetivo))
               if objetivo > 0 else v)


def escala_como_texto(escala: int) -> str:
    """'1:100.000', con el separador de miles del informe."""
    return "1:" + f"{int(escala):,}".replace(",", ".")


# =============================================================================
# Lectura de la declaración
# =============================================================================
def _capa_desde(bruto: dict[str, Any], identificador: str, base: Path,
                estilos: Path) -> CapaDeclarada:
    """Construye una capa del catálogo y valida su tipo."""
    tipo = str(bruto.get("tipo", "")).strip().lower()
    if tipo not in TIPOS_CAPA:
        raise ErrorFormato(
            f"la capa {identificador!r} declara tipo {tipo!r} y solo se admiten "
            f"{list(TIPOS_CAPA)}.")
    ruta_bruta = str(bruto.get("ruta", "")).strip()
    if not ruta_bruta:
        raise ErrorFormato(f"la capa {identificador!r} no declara ruta.")
    estilo = bruto.get("estilo")
    return CapaDeclarada(
        identificador=identificador, tipo=tipo,
        ruta=rutas.resolver(ruta_bruta, base),
        estilo=str(estilo) if estilo else None,
        nombre=str(bruto.get("nombre", "") or "").strip())


def _token_del_patron(patron: str, ruta: str) -> str | None:
    """
    Lo que el comodín capturó, dado el patrón y una ruta que casa con él.

    El patrón lleva un solo '*'. Se compara sobre la ruta con barras normales
    para que el resultado no dependa del separador del sistema.
    """
    if patron.count("*") != 1:
        return None
    izquierda, derecha = patron.split("*")
    normal = ruta.replace("\\", "/")
    izquierda = izquierda.replace("\\", "/")
    derecha = derecha.replace("\\", "/")
    if not normal.startswith(izquierda) or not normal.endswith(derecha):
        return None
    fin = len(normal) - len(derecha) if derecha else len(normal)
    return normal[len(izquierda):fin]


def expandir_series(
    series: Sequence[dict[str, Any]], base: Path
) -> list[tuple[dict[str, Any], str]]:
    """
    Una entrada por cada archivo que case con el patrón de cada serie.

    ASÍ UN ESTUDIO CON OTROS PERIODOS DE RETORNO NO EXIGE EDITAR NADA: el juego
    de planchas se deriva de lo que los módulos anteriores dejaron en disco, no
    de una lista escrita a mano que puede quedar desfasada.

    Devuelve pares (declaración de la serie, token), ordenados por token.
    """
    expandidas: list[tuple[dict[str, Any], str]] = []
    for serie in series or []:
        patron = str(serie.get("patron", "")).strip()
        if not patron:
            raise ErrorFormato(
                f"la serie {serie.get('id', '?')!r} no declara patrón.")
        absoluto = str(rutas.resolver(patron, base))
        tokens: list[str] = []
        for encontrado in sorted(glob.glob(absoluto)):
            relativo = rutas.relativa(Path(encontrado), base)
            token = _token_del_patron(patron, relativo)
            if token:
                tokens.append(token)
        for token in sorted(dict.fromkeys(tokens)):
            expandidas.append((serie, token))
    return expandidas


def leer_declaracion(ruta: Path, base: Path, estilos: Path) -> list[Plancha]:
    """
    Juego completo de planchas: las declaradas una a una y las de las series.

    Excepciones
    -----------
    ErrorRutas
        Si la declaración no existe.
    ErrorFormato
        Si una plancha referencia una capa que el catálogo no declara.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la declaración de mapas en {ruta}.")
    declaracion = leer_yaml(ruta) or {}

    catalogo: dict[str, CapaDeclarada] = {}
    for identificador, bruto in (declaracion.get("capas") or {}).items():
        catalogo[str(identificador)] = _capa_desde(
            bruto or {}, str(identificador), base, estilos)

    planchas: list[Plancha] = []

    def construir(bruto: dict[str, Any], token: str | None) -> Plancha:
        """Resuelve una declaración, sustituyendo {token} si viene de una serie."""
        def sustituir(texto: str) -> str:
            return texto.replace("{token}", token) if token else texto

        identificador = sustituir(str(bruto.get("id", "")).strip())
        if not identificador:
            raise ErrorFormato("hay una plancha sin identificador.")

        capas: list[CapaDeclarada] = []
        # Las capas propias de la serie van al fondo, y las comunes encima: el
        # área y el drenaje deben leerse SOBRE el campo interpolado.
        for extra in bruto.get("capas_extra") or []:
            resuelto = dict(extra)
            resuelto["ruta"] = sustituir(str(resuelto.get("ruta", "")))
            resuelto["nombre"] = sustituir(str(resuelto.get("nombre", "") or ""))
            capas.append(_capa_desde(
                resuelto, Path(resuelto["ruta"]).stem, base, estilos))
        for nombre in bruto.get("capas") or []:
            if str(nombre) not in catalogo:
                raise ErrorFormato(
                    f"la plancha {identificador!r} pide la capa {nombre!r}, que "
                    "el catálogo de config/mapas.yaml no declara.")
            capas.append(catalogo[str(nombre)])

        encuadre = str(bruto.get("encuadre", "")).strip()
        if encuadre and encuadre not in catalogo:
            raise ErrorFormato(
                f"la plancha {identificador!r} encuadra por {encuadre!r}, que "
                "el catálogo no declara.")

        esenciales = tuple(str(e) for e in (bruto.get("esenciales") or []))
        declaradas = {c.identificador for c in capas}
        desconocidas = sorted(set(esenciales) - declaradas)
        if desconocidas:
            raise ErrorFormato(
                f"la plancha {identificador!r} declara esenciales que no están "
                f"entre sus capas: {desconocidas}.")

        forzada = bruto.get("escala")
        return Plancha(
            identificador=identificador,
            titulo=sustituir(str(bruto.get("titulo", identificador))),
            subtitulo=sustituir(str(bruto.get("subtitulo", ""))),
            nota=sustituir(str(bruto.get("nota", "")).strip()),
            encuadre=encuadre,
            capas=tuple(capas),
            esenciales=esenciales,
            escala_forzada=int(forzada) if forzada else None,
        )

    for bruto in declaracion.get("mapas") or []:
        planchas.append(construir(bruto or {}, None))
    for serie, token in expandir_series(declaracion.get("series") or [], base):
        planchas.append(construir(serie, token))

    identificadores = [p.identificador for p in planchas]
    repetidos = sorted({i for i in identificadores
                        if identificadores.count(i) > 1})
    if repetidos:
        raise ErrorFormato(
            f"hay planchas con el mismo identificador: {repetidos}. Cada una "
            "escribe su archivo, y dos con el mismo nombre se pisan en "
            "silencio.")
    return planchas


def catalogo_de_capas(planchas: Sequence[Plancha]) -> list[CapaDeclarada]:
    """Capas distintas del juego, en orden de primera aparición."""
    vistas: dict[str, CapaDeclarada] = {}
    for plancha in planchas:
        for capa in plancha.capas:
            vistas.setdefault(str(capa.ruta), capa)
    return list(vistas.values())


# =============================================================================
# QGIS: capas
# =============================================================================
def leer_simbologia(ruta: Path, delimitador: str = ";") -> dict[str, dict[str, str]]:
    """
    Tabla de simbología por defecto, indexada por identificador de capa.

    ES DOCTRINA CARTOGRÁFICA, no código: qué va sin relleno, qué azul lleva la
    red hídrica y qué rampa el relieve son convenciones, y viven en
    data/referencia como cualquier otra tabla del estudio.

    Excepciones
    -----------
    ErrorFormato
        Si la tabla no tiene la columna que la indexa.
    """
    import csv

    ruta = Path(ruta)
    if not ruta.is_file():
        return {}
    with ruta.open(encoding="utf-8-sig", newline="") as archivo:
        filas = list(csv.DictReader(archivo, delimiter=delimitador))
    if filas and "capa" not in filas[0]:
        raise ErrorFormato(
            f"{ruta.name} no tiene columna 'capa' y no puede indexarse.")
    return {str(f["capa"]).strip(): f for f in filas if f.get("capa")}


def _aplicar_simbologia(capa_cargada, regla: dict[str, str], tipo: str) -> bool:
    """
    Aplica una fila de la tabla de simbología. Cierto si pudo aplicarla.

    Es deliberadamente sobria: un color, un grosor y una rampa. Lo que exija
    categorías o intervalos (la cobertura por clase CLC, por ejemplo) se define
    en QGIS y se guarda como .qml, que tiene precedencia sobre esta tabla.
    """
    from qgis.core import (
        QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsStyle,
    )

    def color(clave: str) -> str:
        valor = str(regla.get(clave, "") or "").strip()
        return valor if valor.startswith("#") else ""

    try:
        opacidad = float(regla.get("opacidad") or 1.0)
    except (TypeError, ValueError):
        opacidad = 1.0

    if tipo == "raster":
        from qgis.core import QgsSingleBandPseudoColorRenderer
        from qgis.core import QgsRasterShader, QgsColorRampShader

        nombre_rampa = str(regla.get("rampa", "") or "").strip()
        if not nombre_rampa:
            return False
        rampa = QgsStyle.defaultStyle().colorRamp(nombre_rampa)
        if rampa is None:
            return False
        if str(regla.get("invertir", "") or "").strip().lower() in ("si", "sí",
                                                                   "true", "1"):
            rampa.invert()
        proveedor = capa_cargada.dataProvider()
        estadistica = proveedor.bandStatistics(1)
        sombreado = QgsRasterShader()
        rampa_shader = QgsColorRampShader(
            estadistica.minimumValue, estadistica.maximumValue, rampa,
            QgsColorRampShader.Interpolated)
        # LA PRECISION VA ANTES DE CLASIFICAR: classifyColorRamp escribe las
        # etiquetas, y fijarla despues no las reescribe. La leyenda salia con
        # '133.09996' en lugar de '133'.
        rampa_shader.setLabelPrecision(0)
        rampa_shader.classifyColorRamp(255, 1)
        sombreado.setRasterShaderFunction(rampa_shader)
        capa_cargada.setRenderer(QgsSingleBandPseudoColorRenderer(
            proveedor, 1, sombreado))
        capa_cargada.setOpacity(opacidad)
        return True

    geometria = str(regla.get("geometria", "") or "").strip().lower()
    relleno, borde = color("relleno"), color("borde")
    try:
        grosor = float(regla.get("grosor_mm") or 0.3)
    except (TypeError, ValueError):
        grosor = 0.3

    if geometria == "poligono":
        simbolo = QgsFillSymbol.createSimple({
            "color": relleno or "transparent",
            "style": "solid" if relleno else "no",
            "outline_color": borde or "#000000",
            "outline_width": str(grosor),
            "outline_width_unit": "MM",
        })
    elif geometria == "linea":
        simbolo = QgsLineSymbol.createSimple({
            "color": borde or "#000000",
            "width": str(grosor),
            "width_unit": "MM",
        })
    elif geometria == "punto":
        simbolo = QgsMarkerSymbol.createSimple({
            "name": "circle",
            "color": relleno or "#000000",
            "outline_color": borde or "#ffffff",
            "outline_width": str(grosor),
            "outline_width_unit": "MM",
            "size": "2.8", "size_unit": "MM",
        })
    else:
        return False

    simbolo.setOpacity(opacidad)
    capa_cargada.renderer().setSymbol(simbolo)
    return True


def _cargar_capa(capa: CapaDeclarada, estilos: Path, simbologia=None,
                 logger=None):
    """
    Carga una capa y la simboliza. Devuelve None si no es válida.

    TRES NIVELES, EN ESTE ORDEN. El .qml del estudio manda, porque es lo que el
    consultor ajustó en QGIS. Si no está, se aplica la tabla de simbología de
    data/referencia. Si tampoco hay fila para esa capa, queda el color aleatorio
    de QGIS y el módulo lo reporta: una plancha con colores arbitrarios no es
    entregable, y eso hay que verlo en el reporte y no al abrir el PDF.
    """
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    if not capa.existe:
        return None
    # EL NOMBRE DE LA CAPA ES SU ENTRADA EN LA LEYENDA. Sin él la plancha
    # rotula 'estaciones_pmax_T100', que es un nombre de archivo y no una
    # convención.
    etiqueta = capa.nombre or capa.identificador
    if capa.tipo == "vector":
        cargada = QgsVectorLayer(str(capa.ruta), etiqueta, "ogr")
    else:
        cargada = QgsRasterLayer(str(capa.ruta), etiqueta, "gdal")
    if not cargada.isValid():
        if logger is not None:
            logger.warning("la capa %s no es válida y se omite", capa.ruta.name)
        return None
    ruta_estilo = Path(estilos) / capa.estilo if capa.estilo else None
    if ruta_estilo is not None and ruta_estilo.is_file():
        cargada.loadNamedStyle(str(ruta_estilo))
        cargada.triggerRepaint()
        return cargada

    # EL ROL DE SIMBOLOGIA ES EL NOMBRE DEL ESTILO, no el del archivo. Las
    # capas de una serie se llaman por su archivo ('pmax_T100'), y las quince
    # planchas de la serie comparten una sola convencion ('isoyetas_pmax'): sin
    # esto habria que escribir una fila de la tabla por periodo de retorno.
    rol = Path(capa.estilo).stem if capa.estilo else capa.identificador
    regla = (simbologia or {}).get(rol)
    if regla and _aplicar_simbologia(cargada, regla, capa.tipo):
        cargada.triggerRepaint()
        return cargada

    if logger is not None:
        logger.warning("la capa %s (rol %s) queda con el color aleatorio de "
                       "QGIS", capa.identificador, rol)
    setattr(cargada, "_sin_simbologia", True)
    return cargada


# =============================================================================
# QGIS: plantilla de la plancha
# =============================================================================
def _texto(layout, identificador: str, contenido: str, tamano: float,
           x: float, y: float, ancho: float, alto: float, negrita: bool = False):
    """Rótulo del panel, con identificador para que el M16 lo rellene luego."""
    from qgis.core import QgsLayoutItemLabel, QgsLayoutPoint, QgsLayoutSize
    from qgis.core import QgsUnitTypes
    from qgis.PyQt.QtGui import QFont

    etiqueta = QgsLayoutItemLabel(layout)
    etiqueta.setId(identificador)
    etiqueta.setText(contenido)
    fuente = QFont("Arial", int(tamano))
    fuente.setBold(negrita)
    try:
        from qgis.core import QgsTextFormat
        formato = QgsTextFormat()
        formato.setFont(fuente)
        formato.setSize(tamano)
        formato.setSizeUnit(QgsUnitTypes.RenderPoints)
        etiqueta.setTextFormat(formato)
    except (ImportError, AttributeError):  # QGIS anterior a la API de formato
        etiqueta.setFont(fuente)
    etiqueta.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
    etiqueta.attemptResize(QgsLayoutSize(ancho, alto,
                                         QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(etiqueta)
    return etiqueta


def plantilla_por_defecto(destino: Path, plancha: dict[str, Any],
                          crs_id: str) -> Path:
    """
    Escribe un .qpt de partida con toda la guarnición de una plancha.

    ES UN PUNTO DE PARTIDA, NO UN RESULTADO. Se abre en QGIS, se ajusta al
    formato de casa y se guarda: desde ese momento manda el del consultor, y
    este módulo no lo vuelve a tocar.

    Los identificadores de los elementos son el contrato con la composición:
    'mapa', 'titulo', 'subtitulo', 'leyenda', 'norte', 'escala_grafica',
    'escala_numerica', 'creditos' y 'nota'. Mientras se conserven, el aspecto
    puede cambiar libremente.
    """
    from qgis.core import (
        QgsApplication, QgsCoordinateReferenceSystem, QgsLayoutItemLegend,
        QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutItemScaleBar,
        QgsLayoutItemShape, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout,
        QgsProject, QgsReadWriteContext, QgsUnitTypes,
    )

    ancho_hoja = float(plancha["ancho_mm"])
    alto_hoja = float(plancha["alto_mm"])
    margen = float(plancha["margen_mm"])
    ancho_panel = float(plancha["ancho_panel_mm"])
    x_marco, y_marco, ancho_marco, alto_marco = marco_del_mapa(plancha)
    x_panel = x_marco + ancho_marco + margen

    layout = QgsPrintLayout(QgsProject.instance())
    layout.initializeDefaults()
    layout.setName("Plancha")
    pagina = layout.pageCollection().page(0)
    pagina.setPageSize(QgsLayoutSize(ancho_hoja, alto_hoja,
                                     QgsUnitTypes.LayoutMillimeters))

    # --- Mapa, con su grilla de coordenadas ---------------------------------
    mapa = QgsLayoutItemMap(layout)
    mapa.setId("mapa")
    mapa.attemptMove(QgsLayoutPoint(x_marco, y_marco,
                                    QgsUnitTypes.LayoutMillimeters))
    mapa.attemptResize(QgsLayoutSize(ancho_marco, alto_marco,
                                     QgsUnitTypes.LayoutMillimeters))
    mapa.setFrameEnabled(True)
    layout.addLayoutItem(mapa)

    grilla = mapa.grid()
    grilla.setEnabled(True)
    grilla.setCrs(QgsCoordinateReferenceSystem(crs_id))
    grilla.setAnnotationEnabled(True)
    grilla.setAnnotationPrecision(0)
    try:
        from qgis.core import QgsLayoutItemMapGrid
        grilla.setStyle(QgsLayoutItemMapGrid.FrameAnnotationsOnly)
        grilla.setFrameStyle(QgsLayoutItemMapGrid.InteriorTicks)
        for borde in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right,
                      QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
            grilla.setAnnotationDisplay(QgsLayoutItemMapGrid.ShowAll, borde)
        # LOS ROTULOS DE LOS COSTADOS VAN GIRADOS. Una coordenada CTM12 tiene
        # siete cifras y ocupa unos 12 mm en horizontal, mas que el margen de la
        # hoja: escritos de lado se salen de la plancha y se cortan al exportar.
        for borde in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right,
                      QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
            grilla.setAnnotationPosition(
                QgsLayoutItemMapGrid.OutsideMapFrame, borde)
        for borde in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right):
            grilla.setAnnotationDirection(
                QgsLayoutItemMapGrid.VerticalDescending, borde)
    except (ImportError, AttributeError):
        pass

    # --- Panel derecho -------------------------------------------------------
    marco_panel = QgsLayoutItemShape(layout)
    marco_panel.setId("panel")
    marco_panel.setShapeType(QgsLayoutItemShape.Rectangle)
    marco_panel.attemptMove(QgsLayoutPoint(x_panel, y_marco,
                                           QgsUnitTypes.LayoutMillimeters))
    marco_panel.attemptResize(QgsLayoutSize(ancho_panel, alto_marco,
                                            QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(marco_panel)

    interior = x_panel + 3.0
    util = ancho_panel - 6.0
    cursor = y_marco + 4.0

    _texto(layout, "titulo", "Título de la plancha", 13.0,
           interior, cursor, util, 14.0, negrita=True)
    cursor += 15.0
    _texto(layout, "subtitulo", "Subtítulo", 9.0, interior, cursor, util, 10.0)
    cursor += 12.0

    leyenda = QgsLayoutItemLegend(layout)
    leyenda.setId("leyenda")
    leyenda.setTitle("Convenciones")
    leyenda.setLinkedMap(mapa)
    leyenda.setLegendFilterByMapEnabled(True)
    leyenda.setResizeToContents(False)
    leyenda.attemptMove(QgsLayoutPoint(interior, cursor,
                                       QgsUnitTypes.LayoutMillimeters))
    leyenda.attemptResize(QgsLayoutSize(util, 130.0,
                                        QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(leyenda)
    cursor += 134.0

    # Norte a la izquierda del panel y escala gráfica a su derecha.
    norte = QgsLayoutItemPicture(layout)
    norte.setId("norte")
    flecha = Path(QgsApplication.svgPaths()[0]) / "arrows" / "NorthArrow_02.svg"
    if flecha.is_file():
        norte.setPicturePath(str(flecha))
    norte.attemptMove(QgsLayoutPoint(interior, cursor,
                                     QgsUnitTypes.LayoutMillimeters))
    norte.attemptResize(QgsLayoutSize(22.0, 22.0,
                                      QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(norte)

    barra = QgsLayoutItemScaleBar(layout)
    barra.setId("escala_grafica")
    barra.setLinkedMap(mapa)
    barra.applyDefaultSettings()
    barra.setStyle("Single Box")
    barra.setUnits(QgsUnitTypes.DistanceKilometers)
    barra.setUnitLabel("km")
    barra.setUnitsPerSegment(1.0)
    barra.setNumberOfSegments(4)
    barra.setNumberOfSegmentsLeft(0)
    barra.attemptMove(QgsLayoutPoint(interior + 26.0, cursor + 6.0,
                                     QgsUnitTypes.LayoutMillimeters))
    barra.attemptResize(QgsLayoutSize(util - 26.0, 14.0,
                                      QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(barra)
    cursor += 26.0

    _texto(layout, "escala_numerica", "Escala 1:0", 9.0,
           interior, cursor, util, 6.0, negrita=True)
    cursor += 8.0
    _texto(layout, "creditos",
           "Sistema de referencia\nEstudio\nFecha", 7.0,
           interior, cursor, util, 26.0)
    cursor += 28.0
    _texto(layout, "nota", "", 6.5, interior, cursor, util,
           max(8.0, y_marco + alto_marco - cursor - 2.0))

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    if not layout.saveAsTemplate(str(destino), QgsReadWriteContext()):
        raise ErrorFormato(f"QGIS no pudo escribir la plantilla en {destino}.")
    return destino


def _cargar_plantilla(ruta: Path):
    """Instancia una composición a partir del .qpt."""
    from qgis.core import QgsPrintLayout, QgsProject, QgsReadWriteContext
    from qgis.PyQt.QtXml import QDomDocument

    documento = QDomDocument()
    documento.setContent(Path(ruta).read_text(encoding="utf-8"))
    layout = QgsPrintLayout(QgsProject.instance())
    layout.initializeDefaults()
    elementos, correcto = layout.loadFromTemplate(documento,
                                                  QgsReadWriteContext(), True)
    if not correcto:
        raise ErrorFormato(
            f"QGIS no pudo interpretar la plantilla {Path(ruta).name}.")
    return layout


# =============================================================================
# QGIS: composición y exportación
# =============================================================================
def _extension_de(capa) -> tuple[float, float, float, float]:
    """Extensión de una capa cargada, como tupla."""
    caja = capa.extent()
    return (caja.xMinimum(), caja.yMinimum(), caja.xMaximum(), caja.yMaximum())


def componer(
    plancha: Plancha,
    capas_cargadas: Sequence[Any],
    capa_encuadre: Any,
    layout,
    configuracion: Config,
    crs_id: str,
    creditos: str,
) -> dict[str, Any]:
    """
    Rellena la composición con las capas, la extensión y los rótulos.

    Devuelve el detalle de la plancha, con su escala y su intervalo de grilla.

    Excepciones
    -----------
    ErrorFormato
        Si la plantilla no tiene el elemento de mapa.
    """
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle

    mapa = layout.itemById("mapa")
    if mapa is None:
        raise ErrorFormato(
            "la plantilla no tiene un elemento de mapa con identificador "
            "'mapa'. Es el único elemento imprescindible: sin él no hay dónde "
            "dibujar.")

    ancho_marco = mapa.rect().width()
    alto_marco = mapa.rect().height()

    extension = _extension_de(capa_encuadre)
    ancho_m = extension[2] - extension[0]
    alto_m = extension[3] - extension[1]
    serie = configuracion.obtener("cartografia.serie_escalas")
    margen = float(configuracion.obtener("cartografia.margen_encuadre", 0.05))

    if plancha.escala_forzada:
        escala = int(plancha.escala_forzada)
        minima = escala_normalizada(ancho_m, alto_m, ancho_marco, alto_marco,
                                    serie, margen)
        desbordada = escala < minima
    else:
        escala = escala_normalizada(ancho_m, alto_m, ancho_marco, alto_marco,
                                    serie, margen)
        desbordada = False

    encuadrada = extension_para_escala(extension, escala, ancho_marco,
                                       alto_marco)
    mapa.setCrs(QgsCoordinateReferenceSystem(crs_id))
    mapa.setLayers(list(capas_cargadas))
    mapa.setKeepLayerSet(True)
    mapa.setExtent(QgsRectangle(*encuadrada))
    mapa.setScale(float(escala))

    divisiones = int(configuracion.obtener("cartografia.divisiones_grilla", 5))
    intervalo = intervalo_de_grilla(encuadrada[2] - encuadrada[0], divisiones)
    grilla = mapa.grid()
    grilla.setIntervalX(float(intervalo))
    grilla.setIntervalY(float(intervalo))
    grilla.setOffsetX(0.0)
    grilla.setOffsetY(0.0)
    mapa.updateBoundingRect()
    mapa.refresh()

    leyenda = layout.itemById("leyenda")
    if leyenda is not None:
        leyenda.setLinkedMap(mapa)
        # EL MODELO AUTOMATICO SE ALIMENTA DEL ARBOL DE CAPAS DEL PROYECTO, y
        # aqui las capas se registran sin arbol para que cada plancha lleve solo
        # las suyas. Con el automatico la leyenda sale VACIA. Se arma a mano, en
        # el orden de lectura del mapa, de la superficie al fondo.
        leyenda.setAutoUpdateModel(False)
        raiz = leyenda.model().rootGroup()
        for hijo in list(raiz.children()):
            raiz.removeChildNode(hijo)
        for capa_visible in capas_cargadas:
            raiz.addLayer(capa_visible)
        leyenda.updateLegend()

    for identificador, contenido in (
            ("titulo", plancha.titulo),
            ("subtitulo", plancha.subtitulo),
            ("escala_numerica", f"Escala {escala_como_texto(escala)}"),
            ("creditos", creditos),
            ("nota", plancha.nota)):
        elemento = layout.itemById(identificador)
        if elemento is not None and hasattr(elemento, "setText"):
            elemento.setText(contenido)

    layout.refresh()
    return {
        "id": plancha.identificador,
        "titulo": plancha.titulo,
        "escala": escala,
        "escala_texto": escala_como_texto(escala),
        "escala_forzada": bool(plancha.escala_forzada),
        "escala_desbordada": desbordada,
        "grilla_m": intervalo,
        "extension_m": [round(v, 1) for v in encuadrada],
        "capas": [c.identificador for c in plancha.capas if c.existe],
        "capas_ausentes": [c.identificador for c in plancha.capas
                           if not c.existe],
    }


def exportar(layout, destino_sin_extension: Path, formatos: Sequence[str],
             dpi: int) -> list[Path]:
    """
    Escribe la plancha en cada formato pedido. Devuelve las rutas escritas.

    Excepciones
    -----------
    ErrorFormato
        Si QGIS rechaza la exportación de alguno.
    """
    from qgis.core import QgsLayoutExporter

    destino_sin_extension = Path(destino_sin_extension)
    destino_sin_extension.parent.mkdir(parents=True, exist_ok=True)
    exportador = QgsLayoutExporter(layout)
    escritas: list[Path] = []

    for formato in formatos:
        formato = str(formato).strip().lower()
        destino = destino_sin_extension.with_suffix(f".{formato}")
        if formato == "pdf":
            ajustes = QgsLayoutExporter.PdfExportSettings()
            ajustes.dpi = float(dpi)
            ajustes.rasterizeWholeImage = False
            resultado = exportador.exportToPdf(str(destino), ajustes)
        elif formato in ("png", "jpg", "jpeg", "tif", "tiff"):
            ajustes = QgsLayoutExporter.ImageExportSettings()
            ajustes.dpi = float(dpi)
            resultado = exportador.exportToImage(str(destino), ajustes)
        else:
            raise ErrorFormato(
                f"formato de salida no admitido: {formato!r}. Admitidos: pdf, "
                "png, jpg, tif.")
        if resultado != QgsLayoutExporter.Success:
            raise ErrorFormato(
                f"QGIS no pudo exportar {destino.name} (código {resultado}).")
        escritas.append(destino)
    return escritas


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    solo: Sequence[str] | None = None,
    solo_plantilla: bool = False,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Compone y exporta el juego de planchas declarado."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    entorno.exigir_entorno(entorno.ENTORNO_QGIS, MODULO)

    import sig
    sig.iniciar_qgis(configuracion.obtener("entornos.qgis.prefix_path"))

    crs_id = configuracion.obtener("crs.calculo")
    plancha_cfg = configuracion.obtener("cartografia.plancha")
    estilos = rutas.resolver(configuracion.obtener("proyecto_qgis.estilos"),
                             base)
    declaracion = rutas.resolver(
        configuracion.obtener("cartografia.declaracion"), base)
    salida = rutas.resolver(configuracion.obtener("cartografia.salida"), base)
    ruta_qpt = rutas.resolver(
        configuracion.obtener("cartografia.plantilla_qpt"),
        rutas.raiz_codigo())
    # La tabla de simbologia se busca primero en el estudio y, si no esta, en la
    # herramienta: asi un estudio puede apartarse de la convencion poniendo la
    # suya, y queda constancia de que lo hizo.
    tabla_simbologia = base / "data" / "referencia" / "simbologia_cartografia.csv"
    if not tabla_simbologia.is_file():
        tabla_simbologia = (rutas.raiz_codigo() / "data" / "referencia"
                            / "simbologia_cartografia.csv")
    simbologia = leer_simbologia(
        tabla_simbologia,
        configuracion.obtener("insumos_usuario.delimitador_csv", ";"))
    formatos = configuracion.obtener("cartografia.formato_salida") or ["pdf"]
    dpi = int(configuracion.obtener("cartografia.dpi", 300))

    resultado = ResultadoM16()

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"declaración de mapas": rutas.relativa(declaracion, base),
                 "plantilla": str(ruta_qpt),
                 "estilos": rutas.relativa(estilos, base)},
        parametros={
            "cartografia.plancha": f"{plancha_cfg['ancho_mm']} x "
                                   f"{plancha_cfg['alto_mm']} mm",
            "cartografia.dpi": dpi,
            "cartografia.formato_salida": list(formatos),
            "crs.calculo": crs_id,
        },
    )

    # --- Plantilla -----------------------------------------------------------
    with registro.bloque(logger, "Plantilla de la plancha"):
        if not ruta_qpt.is_file():
            if not configuracion.obtener(
                    "cartografia.escribir_plantilla_inicial", True):
                resultado.hallazgos.append(Hallazgo(
                    BLOQUEANTE, "cartografia.sin_plantilla",
                    f"no existe la plantilla {ruta_qpt.name} y la creación "
                    "automática está desactivada.",
                ))
                return _cerrar(logger, resultado, base, ruta_json, inicio,
                               SALIDA_BLOQUEANTE)
            plantilla_por_defecto(ruta_qpt, plancha_cfg, crs_id)
            resultado.plantilla_creada = True
            logger.info("plantilla de partida escrita en %s", ruta_qpt)
        else:
            logger.info("se usa la plantilla existente %s", ruta_qpt.name)
        resultado.plantilla = str(ruta_qpt)

    if solo_plantilla:
        resultado.hallazgos.extend(_resumir(resultado))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_CORRECTA)

    planchas = leer_declaracion(declaracion, base, estilos)
    if solo:
        pedidos = {str(s) for s in solo}
        desconocidos = pedidos - {p.identificador for p in planchas}
        if desconocidos:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "cartografia.plancha_desconocida",
                f"se pidieron planchas que la declaración no tiene: "
                f"{sorted(desconocidos)}.",
            ))
        planchas = [p for p in planchas if p.identificador in pedidos]

    # --- Capas ---------------------------------------------------------------
    from qgis.core import QgsProject

    proyecto = QgsProject.instance()
    proyecto.removeAllMapLayers()
    cargadas: dict[str, Any] = {}

    with registro.bloque(logger, "Capas"):
        for capa in catalogo_de_capas(planchas):
            clave = str(capa.ruta)
            if not capa.existe:
                resultado.capas_ausentes.append(
                    rutas.relativa(capa.ruta, base))
                continue
            objeto = _cargar_capa(capa, estilos, simbologia, logger)
            if objeto is None:
                resultado.capas_ausentes.append(
                    rutas.relativa(capa.ruta, base))
                continue
            proyecto.addMapLayer(objeto, False)
            cargadas[clave] = objeto
            if getattr(objeto, "_sin_simbologia", False):
                resultado.capas_sin_estilo.append(capa.identificador)
        logger.info("%d capa(s) cargadas, %d ausentes",
                    len(cargadas), len(resultado.capas_ausentes))

    creditos = (f"Sistema de referencia: {crs_id}\n"
                f"Estudio: {configuracion.obtener('proyecto.nombre', '')}\n"
                f"Elaboró: {configuracion.obtener('proyecto.consultor', '')}")

    # --- Planchas ------------------------------------------------------------
    for plancha in planchas:
        with registro.bloque(logger, f"Plancha {plancha.identificador}"):
            encuadre = None
            for capa in plancha.capas:
                if capa.identificador == plancha.encuadre:
                    encuadre = cargadas.get(str(capa.ruta))
                    break
            if encuadre is None:
                # Puede no estar entre las dibujadas: se carga solo para medir.
                suelta = next((c for c in plancha.capas
                               if c.identificador == plancha.encuadre), None)
                if suelta is not None:
                    encuadre = cargadas.get(str(suelta.ruta))
            if encuadre is None:
                resultado.omitidas.append({
                    "id": plancha.identificador,
                    "motivo": f"falta la capa de encuadre "
                              f"{plancha.encuadre!r}",
                })
                logger.warning("se omite: falta la capa de encuadre %r",
                               plancha.encuadre)
                continue

            # UNA ESENCIAL AUSENTE INVALIDA LA PLANCHA. Sin ella el mapa sale
            # con marco, norte y leyenda en su sitio, y sin el tema que anuncia
            # su título: parece completo y no lo está.
            faltan = sorted(
                c.identificador for c in plancha.capas
                if c.identificador in plancha.esenciales
                and str(c.ruta) not in cargadas)
            if faltan:
                resultado.omitidas.append({
                    "id": plancha.identificador,
                    "motivo": f"falta(n) su(s) capa(s) esencial(es) {faltan}",
                })
                logger.warning("se omite: falta la capa esencial %s",
                               ", ".join(faltan))
                continue

            visibles = [cargadas[str(c.ruta)] for c in plancha.capas
                        if str(c.ruta) in cargadas]
            if not visibles:
                resultado.omitidas.append({
                    "id": plancha.identificador,
                    "motivo": "ninguna de sus capas está disponible",
                })
                logger.warning("se omite: ninguna de sus capas existe")
                continue

            # QGIS dibuja la PRIMERA capa de la lista encima. La declaración va
            # del fondo a la superficie, que es como se lee un mapa, de modo que
            # aquí se invierte.
            layout = _cargar_plantilla(ruta_qpt)
            detalle = componer(plancha, list(reversed(visibles)), encuadre,
                               layout, configuracion, crs_id, creditos)
            escritas = exportar(layout, salida / plancha.identificador,
                                formatos, dpi)
            detalle["archivos"] = [rutas.relativa(r, base) for r in escritas]
            resultado.planchas.append(detalle)
            resultado.productos.extend(detalle["archivos"])
            logger.info("%s | grilla cada %s m | %d capa(s) | %s",
                        detalle["escala_texto"], f"{detalle['grilla_m']:,}",
                        len(visibles),
                        ", ".join(p.rsplit("/", 1)[-1]
                                  for p in detalle["archivos"]))

    resultado.hallazgos.extend(_resumir(resultado))
    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _resumir(resultado: ResultadoM16) -> list[Hallazgo]:
    """Lo que el módulo midió, con su severidad."""
    hallazgos: list[Hallazgo] = []

    if resultado.plantilla_creada:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "cartografia.plantilla_generada",
            "no existía la plantilla de la plancha y se escribió una de "
            "partida. ES UN PUNTO DE PARTIDA, NO UN RESULTADO: hay que abrirla "
            "en QGIS, ajustarla al formato de la entidad contratante y "
            "guardarla. Mientras no se haga, las planchas salen con la "
            "guarnición genérica del módulo.",
        ))

    if not resultado.planchas and not resultado.plantilla_creada:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "cartografia.sin_planchas",
            "no se produjo ninguna plancha. Revisar que los módulos SIG "
            "anteriores hayan dejado sus capas.",
        ))
        return hallazgos

    if resultado.planchas:
        escalas = sorted({p["escala"] for p in resultado.planchas})
        hallazgos.append(Hallazgo(
            INFORMATIVO, "cartografia.producidas",
            f"{len(resultado.planchas)} plancha(s) exportadas, en "
            f"{len(escalas)} escala(s): "
            + ", ".join(escala_como_texto(e) for e in escalas)
            + ". La escala se calcula desde la extensión y el marco, y se "
              "redondea hacia arriba a la serie normalizada.",
        ))

    desbordadas = [p["id"] for p in resultado.planchas
                   if p.get("escala_desbordada")]
    if desbordadas:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "cartografia.escala_forzada",
            f"{len(desbordadas)} plancha(s) llevan escala forzada por debajo de "
            f"la que su contenido exige: {desbordadas}. El contenido se sale "
            "del marco y la plancha queda recortada.",
        ))

    if resultado.omitidas:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "cartografia.omitidas",
            f"{len(resultado.omitidas)} plancha(s) no se produjeron: "
            + "; ".join(f"{o['id']} ({o['motivo']})"
                        for o in resultado.omitidas) + ".",
        ))

    if resultado.capas_ausentes:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "cartografia.capas_ausentes",
            f"{len(resultado.capas_ausentes)} capa(s) declaradas no están en "
            "disco, de modo que el módulo que las produce aún no se ha "
            f"ejecutado: {sorted(set(resultado.capas_ausentes))}.",
        ))

    if resultado.capas_sin_estilo:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "cartografia.sin_simbologia",
            f"{len(set(resultado.capas_sin_estilo))} capa(s) quedaron con el "
            f"color ALEATORIO de QGIS: {sorted(set(resultado.capas_sin_estilo))}"
            ". No tienen .qml en el estudio ni fila en la tabla de simbología. "
            "La plancha sale geométricamente correcta y cromáticamente "
            "arbitraria, y eso no es entregable: hay que simbolizarlas en QGIS "
            "y guardar el estilo, o añadirlas a la tabla.",
        ))
    return hallazgos


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

    if ruta_json is None:
        ruta_json = (rutas.directorio("procesado", base, crear=True)
                     / "M16_cartografia.json")
    reporte = {
        "modulo": MODULO,
        "plantilla": resultado.plantilla,
        "plantilla_creada": resultado.plantilla_creada,
        "planchas": resultado.planchas,
        "omitidas": resultado.omitidas,
        "capas_ausentes": sorted(set(resultado.capas_ausentes)),
        "capas_sin_estilo": sorted(set(resultado.capas_sin_estilo)),
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    productos = {f"plancha {i}": p
                 for i, p in enumerate(resultado.productos, start=1)}
    productos["reporte JSON"] = rutas.relativa(ruta_json, base)
    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecucion"] = rutas.relativa(archivo_log, base)

    registro.registrar_cierre(
        logger, MODULO, "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO",
        segundos=time.perf_counter() - inicio, productos=productos)
    try:
        import sig
        sig.finalizar_qgis()
    except Exception:  # noqa: BLE001
        pass
    return codigo, hallazgos


# =============================================================================
# Interfaz de linea de comandos
# =============================================================================
def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(
        prog="M16_cartografia.py",
        description="Cartografia tematica: planchas del anexo cartografico.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--solo", action="append", default=None,
                            help="identificador de plancha; admite repeticion")
    analizador.add_argument("--solo-plantilla", action="store_true",
                            dest="solo_plantilla",
                            help="escribe la plantilla y no compone planchas")
    analizador.add_argument("--json", type=Path, default=None,
                            dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida, solo=argumentos.solo,
            solo_plantilla=argumentos.solo_plantilla,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorRutas, ErrorConfiguracion, ErrorFormato, ErrorEntorno) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M16 - Cartografía temática
==========================
Entorno: Python de QGIS (OSGeo4W Shell en Windows) SOLO para exportar. Todo lo
demás es Python corriente y se puede probar en el venv.

SE EDITA LA PLANTILLA, NO SE RECONSTRUYE. Es la misma decisión que en el M15 y
por la misma razón: la presentación es la parte cara del trabajo, y la hizo el
consultor. templates/planchas.qgz trae las 29 planchas ya compuestas, con sus
encuadres, su simbología y su rótulo colocado. Rehacerlas desde código tiraría
todo eso y obligaría a volver a ajustarlas en cada cambio.

QUE TOCA ESTE MODULO Y QUE NO:

    FIJO       geometría del rótulo, "PROYECTO:", "CONTENIDO:", simbología,
               tamaño de página, rosa náutica. Se cambian abriendo la
               plantilla en QGIS, nunca desde aquí.
    POR ESTUDIO   nombre del proyecto, contratante, consultor, logos y el CRS
               de presentación. Salen de config.yaml.
    POR PLANCHA   título, subtítulo y capa que enmarca, declarados en
               config/planchas.yaml. La extensión y la escala se CALCULAN.

LA ESCALA SE CALCULA Y NO SE ESCRIBE, y no es una preferencia de estilo. De las
31 planchas que el consultor entregó, CUATRO llevaban mal la escala escrita
(y dos de ellas se retiraron después del juego):
una decía 1:200.000 sobre un marco en 1:234.656, otra decía 1:200.000 sobre uno
en 1:165.000, otra se contradecía a sí misma entre sus dos textos, y otra decía
1:16.500 con un cero de menos. Derivarla del marco hace imposible esa
discrepancia, y de paso ajusta el encuadre a una escala de serie: una plancha
en 1:234.656 no es una plancha, es un descuido.

EL CRS DE PRESENTACION NO ES EL DE CALCULO. La cadena calcula en CTM12
(EPSG:9377) porque es lo que la sección 5 manda, y la cartografía se presenta en
el CRS con que el consultor entregó las coordenadas del proyecto, que este
módulo lee del M01. En este estudio son EPSG:9377 y EPSG:3116.

Productos:
    data/05_resultados/mapas/planchas.qgz    proyecto con los datos del estudio
    data/05_resultados/mapas/<Figura N>.pdf
    data/05_resultados/mapas/<Figura N>.png
    data/02_procesado/M16_cartografia.json

Uso:
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" src/M16_cartografia.py
    ... src/M16_cartografia.py --solo "Figura 3. Área de influencia"
    ... src/M16_cartografia.py --sin-exportar   solo compone el proyecto

Códigos de salida:
    0  planchas producidas sin hallazgos bloqueantes
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o la plantilla
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from xml.etree import ElementTree as ET

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas, shapefile  # noqa: E402
from comun.campos import CampoSalida  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
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

# Identificadores del rótulo que este módulo escribe. Los demás no se tocan.
ID_MAPA = "mapa"
ID_ESCALA = "escala_numerica"
ID_REFERENCIA = "rot_referencia"
ID_CONTENIDO = "rot_contenido"
ID_PROYECTO = "rot_proyecto"
ID_CONTRATANTE = "rot_contratante"
ID_CONSULTOR = "rot_consultor"
ID_LOGO_CONTRATANTE = "logo_contratante"
ID_LOGO_CONSULTOR = "logo_consultor"


@dataclass
class ResultadoM16:
    """Lo que el módulo compuso y lo que dejó pendiente."""

    plantilla: str = ""
    proyecto: str = ""
    planchas: list[dict[str, Any]] = field(default_factory=list)
    sin_layout: list[str] = field(default_factory=list)
    sin_encuadre: list[str] = field(default_factory=list)
    exportadas: list[str] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def fecha_en_espanol(fecha) -> str:
    """
    Mes y ano en espanol, sin depender de la configuracion regional.

    strftime('%B') devuelve el mes en el idioma de la maquina, que en Windows
    sale en ingles salvo que alguien haya fijado el locale: el rotulo decia
    'AUGUST 2026'. Una plancha no puede salir en un idioma u otro segun quien
    la genere.
    """
    return f"{MESES[fecha.month - 1]} {fecha.year}"


def leer_catalogo(ruta: Path) -> dict[str, Any]:
    """
    Catálogo de planchas: qué título lleva cada una y qué capa la enmarca.

    ES UNA DECLARACION Y NO UNA DEDUCCION. El título de un mapa no se puede
    sacar del nombre de su layout sin inventar: 'Figura 22' no dice 'Año La
    Niña'. Se declara, y si un layout de la plantilla no está declarado el
    módulo lo reporta en lugar de componerlo con un título improvisado.

    Excepciones
    -----------
    ErrorRutas
        Si el catálogo no está: sin él no hay nada que componer.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra el catalogo de planchas en {ruta}.")
    datos = leer_yaml(ruta) or {}
    if not datos.get("planchas"):
        raise ErrorFormato(f"{ruta.name} no declara ninguna plancha.")
    return datos


def escala_normalizada(escala: float, serie: Sequence[int]) -> int:
    """
    Redondea una escala HACIA ARRIBA a la serie declarada.

    Hacia arriba y no a la más próxima: hacia abajo la escala se hace más
    grande, el terreno abarcado se encoge y lo que se quería encuadrar deja de
    caber. Un mapa que no muestra la cuenca entera no es un mapa de la cuenca.

    Por encima de la mayor de la serie se devuelve esa mayor y quien llame lo
    reporta: el encuadre se sale del juego de escalas de casa.
    """
    for valor in sorted(serie):
        if escala <= valor:
            return int(valor)
    return int(max(serie)) if serie else int(round(escala))


def extension_con_holgura(
    extension: tuple[float, float, float, float], holgura: float,
) -> tuple[float, float, float, float]:
    """Añade un margen proporcional alrededor de una extensión."""
    xmin, ymin, xmax, ymax = extension
    ancho = max(xmax - xmin, 1e-9)
    alto = max(ymax - ymin, 1e-9)
    dx = ancho * holgura
    dy = alto * holgura
    return (xmin - dx, ymin - dy, xmax + dx, ymax + dy)


def encuadrar(
    extension: tuple[float, float, float, float],
    ancho_marco_mm: float, alto_marco_mm: float, serie: Sequence[int],
) -> tuple[tuple[float, float, float, float], int]:
    """
    Ajusta la extensión al marco y devuelve (extensión final, escala).

    SE RESPETA LA RELACION DE FORMA DEL MARCO. Meter una extensión apaisada en
    un marco vertical sin recomponerla deforma el mapa o deja fuera lo que
    sobra por los lados; QGIS lo resolvería a su manera y el resultado
    dependería de su versión. Aquí se decide: se toma la escala que hace caber
    las DOS dimensiones, se normaliza, y se recentra la extensión sobre el
    mismo centro.

    Devuelve la extensión que corresponde exactamente a la escala normalizada,
    de modo que la escala escrita y la medida sobre el papel coinciden.
    """
    if ancho_marco_mm <= 0 or alto_marco_mm <= 0:
        raise ErrorHidrologia(
            f"el marco mide {ancho_marco_mm} x {alto_marco_mm} mm y las dos "
            "medidas deben ser positivas.")

    xmin, ymin, xmax, ymax = extension
    centro_x = (xmin + xmax) / 2.0
    centro_y = (ymin + ymax) / 2.0
    ancho_m = max(xmax - xmin, 1e-9)
    alto_m = max(ymax - ymin, 1e-9)

    # La escala que hace caber cada dimension; manda la mayor, que es la que
    # deja caber las dos.
    escala = max(ancho_m * 1000.0 / ancho_marco_mm,
                 alto_m * 1000.0 / alto_marco_mm)
    escala = escala_normalizada(escala, serie)

    medio_ancho = ancho_marco_mm * escala / 1000.0 / 2.0
    medio_alto = alto_marco_mm * escala / 1000.0 / 2.0
    return ((centro_x - medio_ancho, centro_y - medio_alto,
             centro_x + medio_ancho, centro_y + medio_alto), escala)


def segmento_de_barra(escala: int, milimetros_objetivo: float = 20.0) -> float:
    """
    Kilometros por segmento de la barra grafica, en numero redondo.

    LA BARRA NO PUEDE CONTRADECIR A LA ESCALA ESCRITA, y es lo que hacia: la
    plantilla traia 'numMapUnitsPerScaleBarUnit' en 10 cuando de metros a
    kilometros son 1000, y el modo de ajuste automatico de QGIS respondia con
    segmentos de cinco metros sobre una plancha a 1:150.000. Se calcula aqui,
    donde la escala ya esta decidida.

    Se busca el numero redondo (1, 2 o 5 por una potencia de diez) cuyo
    segmento mas se acerque a los milimetros pedidos sobre el papel.
    """
    if escala <= 0:
        raise ErrorHidrologia(
            f"la escala vale {escala} y debe ser positiva para dimensionar la "
            "barra grafica.")
    import math

    ideal_km = milimetros_objetivo * escala / 1e6
    if ideal_km <= 0:
        return 1.0
    potencia = 10 ** math.floor(math.log10(ideal_km))
    return min((1, 2, 5, 10),
               key=lambda n: abs(n * potencia - ideal_km)) * potencia


def formatear_escala(escala: int) -> str:
    """
    La escala con separador de miles, como se escribe en un rótulo.

    Se usa el punto, que es lo que la plantilla del consultor ya escribía
    ('1:165.000'), y no el separador que traiga la configuración regional de la
    máquina: una plancha no puede salir distinta según quién la genere.
    """
    return f"{escala:,}".replace(",", ".")


def texto_de_contenido(titulo: str, subtitulo: str) -> str:
    """El bloque de contenido, título arriba y subtítulo debajo."""
    titulo = (titulo or "").strip()
    subtitulo = (subtitulo or "").strip()
    return f"{titulo}\n{subtitulo}" if subtitulo else titulo


def texto_de_proyecto(nombre: str, fecha: str) -> str:
    """
    El bloque de proyecto, sin repetir la palabra que ya está en su título.

    LA PLANTILLA LO TENIA DE LAS DOS FORMAS: veintiséis planchas decían
    'REFUGIO DEL VALLE AGOSTO 2026' y cuatro 'PROYECTO REFUGIO DEL VALLE...',
    debajo de un título que ya dice 'PROYECTO:'. Se resuelve una vez aquí.
    """
    nombre = (nombre or "").strip()
    if nombre.upper().startswith("PROYECTO "):
        nombre = nombre[len("PROYECTO "):].strip()
    fecha = (fecha or "").strip()
    return f"{nombre} {fecha}".strip().upper()


# =============================================================================
# Lectura y escritura del proyecto de QGIS
# =============================================================================
def abrir_proyecto(ruta: Path) -> tuple[ET.Element, dict[str, Any]]:
    """
    Devuelve el árbol del .qgs y los demás archivos del contenedor .qgz.

    NO HACE FALTA QGIS PARA ESTO. Un .qgz es un zip con un .qgs, que es XML: se
    lee, se edita y se escribe con la biblioteca estándar. Solo exportar a PDF
    necesita QGIS, y eso deja probar todo lo demás en el venv.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está o no es un .qgz legible.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra el proyecto de QGIS en {ruta}.")
    try:
        with zipfile.ZipFile(ruta) as paquete:
            nombre = next(n for n in paquete.namelist() if n.endswith(".qgs"))
            otros = {n: paquete.read(n) for n in paquete.namelist()
                     if n != nombre}
            xml = paquete.read(nombre).decode("utf-8")
    except (zipfile.BadZipFile, StopIteration, UnicodeDecodeError) as error:
        raise ErrorRutas(
            f"{ruta.name} no es un proyecto de QGIS legible: {error}") from error
    return ET.fromstring(xml), {"otros": otros, "nombre": nombre}


def guardar_proyecto(raiz: ET.Element, contenedor: dict[str, Any],
                     destino: Path) -> Path:
    """Escribe el .qgz conservando los demás archivos que traía."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    cuerpo = ET.tostring(raiz, encoding="unicode", xml_declaration=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as paquete:
        paquete.writestr(contenedor["nombre"], cuerpo)
        for nombre, datos in contenedor["otros"].items():
            paquete.writestr(nombre, datos)
    return destino


def layouts_por_nombre(raiz: ET.Element) -> dict[str, ET.Element]:
    """Las composiciones de impresión del proyecto, indexadas por su nombre."""
    return {lay.get("name", ""): lay for lay in raiz.iter("Layout")
            if lay.get("name")}


def items_por_id(layout: ET.Element) -> dict[str, list[ET.Element]]:
    """Los elementos de una plancha, agrupados por identificador."""
    salida: dict[str, list[ET.Element]] = {}
    for item in layout.findall("LayoutItem"):
        idd = item.get("id") or ""
        if idd:
            salida.setdefault(idd, []).append(item)
    return salida


def fijar_etiqueta(item: ET.Element, texto: str) -> None:
    """Cambia el texto de una etiqueta sin tocar su tipografía ni su marco."""
    item.set("labelText", texto)


def fijar_imagen(item: ET.Element, ruta_relativa: str) -> None:
    """Apunta un marco de imagen a otro archivo."""
    item.set("file", ruta_relativa)


def fijar_extension(item: ET.Element, extension: tuple[float, float, float, float]) -> None:
    """Escribe la extensión de un marco de mapa."""
    nodo = item.find("Extent")
    if nodo is None:
        nodo = ET.SubElement(item, "Extent")
    xmin, ymin, xmax, ymax = extension
    nodo.set("xmin", f"{xmin:.6f}")
    nodo.set("ymin", f"{ymin:.6f}")
    nodo.set("xmax", f"{xmax:.6f}")
    nodo.set("ymax", f"{ymax:.6f}")


def _area_de_marco(item: ET.Element) -> float:
    """Superficie del marco en mm2, para distinguir el mapa de su recuadro."""
    try:
        ancho, alto = medidas_de_marco(item)
    except ErrorFormato:
        return 0.0
    return ancho * alto


def medidas_de_marco(item: ET.Element) -> tuple[float, float]:
    """Ancho y alto en milímetros de un marco de la plancha."""
    partes = (item.get("size") or "").split(",")
    try:
        return float(partes[0]), float(partes[1])
    except (IndexError, ValueError) as error:
        raise ErrorFormato(
            f"el marco declara size={item.get('size')!r}, que no se puede "
            "leer como ancho y alto en milimetros.") from error


def crs_del_marco(item: ET.Element) -> str:
    """El CRS declarado en un marco de mapa, o cadena vacía si hereda."""
    srs = item.find(".//spatialrefsys")
    return (srs.findtext("authid") or "") if srs is not None else ""


# =============================================================================
# Composición de una plancha
# =============================================================================
def componer_plancha(
    layout: ET.Element, plancha: dict[str, Any], extension, catalogo,
    crs_presentacion: str, unidades: str = "metros",
) -> dict[str, Any]:
    """
    Escribe en una plancha lo que varía: encuadre, escala y textos derivados.

    NO TOCA NADA MAS. Las posiciones, la tipografía, la simbología y los textos
    fijos del rótulo se quedan como el consultor los dejó.

    Devuelve la ficha de lo que quedó escrito, que es lo que el reporte
    necesita para poder decir a qué escala salió cada plancha.
    """
    items = items_por_id(layout)
    marcos = items.get(ID_MAPA) or []
    if not marcos:
        raise ErrorFormato(
            f"la plancha '{layout.get('name')}' no tiene ningun marco con "
            f"id '{ID_MAPA}': sin el no hay donde encuadrar.")

    serie = catalogo.get("escalas_normalizadas") or []
    holgura = float(plancha.get("holgura",
                                catalogo.get("holgura_por_defecto", 0.08)))
    # CON DOS MARCOS MANDA EL MAYOR. Dos planchas llevan una vista de detalle
    # ademas del mapa principal; encuadrar sobre el primero que aparece dejaba
    # el mapa grande a la escala del recuadro pequeno.
    principal = max(marcos, key=lambda m: _area_de_marco(m))
    ancho_mm, alto_mm = medidas_de_marco(principal)
    final, escala = encuadrar(
        extension_con_holgura(extension, holgura), ancho_mm, alto_mm, serie)
    fijar_extension(principal, final)

    escrita = formatear_escala(escala)
    for item in items.get(ID_ESCALA) or []:
        fijar_etiqueta(item, str(catalogo.get(
            "formato_escala", "Escala 1:{escala}")).format(escala=escrita))
    for item in items.get(ID_REFERENCIA) or []:
        fijar_etiqueta(item, str(catalogo.get("formato_referencia", "")).format(
            crs=crs_presentacion, unidades=unidades, escala=escrita))
    for item in items.get(ID_CONTENIDO) or []:
        fijar_etiqueta(item, texto_de_contenido(
            plancha.get("titulo", ""), plancha.get("subtitulo", "")))

    return {
        "layout": layout.get("name", ""),
        "escala": escala,
        "marcos_de_mapa": len(marcos),
        "extension": [round(v, 2) for v in final],
        "crs": crs_presentacion,
    }


def aplicar_datos_del_estudio(
    raiz: ET.Element, nombre_proyecto: str, fecha: str,
    contratante: str, consultor: str,
    logo_contratante: str, logo_consultor: str,
    escribir_nombres: bool = False,
) -> dict[str, int]:
    """
    Escribe en TODAS las planchas lo que es igual en el estudio entero.

    Devuelve cuántos marcos se escribieron de cada cosa, que es lo que permite
    notar que un rótulo no tiene dónde poner el nombre del contratante.
    """
    cuenta = {"proyecto": 0, "contratante": 0, "consultor": 0,
              "logo_contratante": 0, "logo_consultor": 0}
    texto_proyecto = texto_de_proyecto(nombre_proyecto, fecha)

    for layout in raiz.iter("Layout"):
        if not (layout.get("name") or "").startswith("Figura"):
            continue
        items = items_por_id(layout)
        for item in items.get(ID_PROYECTO) or []:
            fijar_etiqueta(item, texto_proyecto)
            cuenta["proyecto"] += 1
        # EL NOMBRE NO SE ESCRIBE ENCIMA DEL LOGO. En el diseno del consultor
        # estos marcos van vacios a proposito: quien identifica a la empresa es
        # su logo, y el marco de texto esta justo debajo. Al rellenarlos, el
        # nombre quedaba montado sobre la imagen. Se escriben solo si la
        # configuracion lo pide.
        if escribir_nombres:
            for item in items.get(ID_CONTRATANTE) or []:
                fijar_etiqueta(item, (contratante or "").strip())
                cuenta["contratante"] += 1
            for item in items.get(ID_CONSULTOR) or []:
                fijar_etiqueta(item, (consultor or "").strip())
                cuenta["consultor"] += 1
        if logo_contratante:
            for item in items.get(ID_LOGO_CONTRATANTE) or []:
                fijar_imagen(item, logo_contratante)
                cuenta["logo_contratante"] += 1
        if logo_consultor:
            for item in items.get(ID_LOGO_CONSULTOR) or []:
                fijar_imagen(item, logo_consultor)
                cuenta["logo_consultor"] += 1
    return cuenta


def extension_de_capa(ruta: Path, crs_origen: str, crs_destino: str):
    """
    Extensión de una capa, reproyectada al CRS de presentación.

    LA CAPA ESTA EN EL CRS DE CALCULO Y LA PLANCHA EN EL DE ENTREGA. Encuadrar
    con la extensión sin reproyectar pondría el mapa a cientos de kilómetros de
    donde debe: en este estudio, CTM12 da un este de 4,89 millones y Bogotá de
    1,01 millones.

    Excepciones
    -----------
    ErrorRutas
        Si la capa no está.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la capa que enmarca: {ruta}.")
    info = shapefile.leer_shapefile(ruta)
    xmin, ymin, xmax, ymax = info.extension
    if not crs_origen or not crs_destino or crs_origen == crs_destino:
        return (xmin, ymin, xmax, ymax)

    # SIN CRS DECLARADO NO SE ENCUADRA. Suponer que la capa esta en el CRS de
    # calculo produce una plancha con extension absurda y escala inventada, que
    # es peor que no producirla: medido sobre este estudio, las estaciones
    # estan en grados y salian a 1:1.000, y la cobertura a 1:1.000.000.
    # CLAUDE.md, seccion 5, exige escritura explicita del .prj.
    if info.crs_epsg and info.crs_epsg != crs_origen:
        crs_origen = info.crs_epsg
    elif not info.crs_epsg:
        raise ErrorFormato(
            f"{ruta.name} no declara su CRS en un .prj legible y hay que "
            f"reproyectarla de {crs_origen} a {crs_destino}. Suponerlo daria "
            "un encuadre inventado.")

    from pyproj import Transformer

    conversor = Transformer.from_crs(crs_origen, crs_destino, always_xy=True)
    # LAS CUATRO ESQUINAS Y NO SOLO DOS. Al cambiar de proyeccion los lados de
    # un rectangulo dejan de ser rectos, y con dos esquinas el encuadre se
    # queda corto por el lado que se curva.
    esquinas = [conversor.transform(x, y)
                for x in (xmin, xmax) for y in (ymin, ymax)]
    return (min(p[0] for p in esquinas), min(p[1] for p in esquinas),
            max(p[0] for p in esquinas), max(p[1] for p in esquinas))


def escribir_subzona_contexto(
    doctrina: Path, destino: Path, extension_estudio, crs_estudio: str,
    crs_declarado: str = "", holgura: float = 1.0,
) -> dict[str, Any]:
    """
    Recorta la zonificación hidrográfica nacional al entorno del estudio.

    LA PLANCHA DE LOCALIZACION NECESITA CONTEXTO y la doctrina trae 316
    subzonas de todo el país. El consultor la había filtrado a mano a catorce
    códigos; se comprobó que esa lista no seguía ninguna regla derivable, ni de
    proximidad ni de procedencia de las estaciones, y que la mayoría quedaban
    fuera del encuadre. Era un filtro de RENDIMIENTO, no cartográfico: lo que
    hace falta es acotar, no reproducir la lista.

    'holgura' es cuántas veces el tamaño del estudio se añade alrededor. Con 1,0
    se toma un entorno del doble de ancho, que es lo que da contexto sin que la
    cuenca se pierda dentro.

    SE CONSERVA LA SUBZONA ENTERA que toque el entorno, no su recorte: partir
    un polígono de zonificación por una caja inventada dibujaría un límite
    administrativo que no existe.

    Excepciones
    -----------
    ErrorRutas
        Si la capa de doctrina no está.
    """
    doctrina = Path(doctrina)
    if not doctrina.is_file():
        raise ErrorRutas(
            f"no se encuentra la zonificacion hidrografica en {doctrina}.")

    info = shapefile.leer_shapefile(doctrina)
    crs_doctrina = crs_declarado or info.crs_epsg
    xmin, ymin, xmax, ymax = extension_estudio
    if crs_doctrina and crs_estudio and crs_doctrina != crs_estudio:
        from pyproj import Transformer

        conversor = Transformer.from_crs(crs_estudio, crs_doctrina,
                                         always_xy=True)
        esquinas = [conversor.transform(x, y)
                    for x in (xmin, xmax) for y in (ymin, ymax)]
        xmin = min(p[0] for p in esquinas)
        ymin = min(p[1] for p in esquinas)
        xmax = max(p[0] for p in esquinas)
        ymax = max(p[1] for p in esquinas)

    ancho = (xmax - xmin) * holgura
    alto = (ymax - ymin) * holgura
    caja = (xmin - ancho, ymin - alto, xmax + ancho, ymax + alto)

    geometrias = shapefile.leer_geometrias(doctrina)
    registros = list(shapefile.leer_registros(doctrina, None))
    campos = tuple(
        CampoSalida(c.nombre[:10], c.nombre, "texto", 80)
        for c in info.campos)

    elegidas, atributos = [], []
    for poligono, registro_fila in zip(geometrias, registros):
        puntos = [p for anillo in poligono for p in anillo]
        if not puntos:
            continue
        pxmin = min(p[0] for p in puntos)
        pxmax = max(p[0] for p in puntos)
        pymin = min(p[1] for p in puntos)
        pymax = max(p[1] for p in puntos)
        if pxmax < caja[0] or pxmin > caja[2] or pymax < caja[1] or pymin > caja[3]:
            continue
        elegidas.append(poligono)
        atributos.append({c.corto: str(registro_fila.get(c.descriptivo, ""))[:80]
                          for c in campos})

    if not elegidas:
        raise ErrorHidrologia(
            "ninguna subzona hidrografica toca el entorno del estudio: revisar "
            "el CRS de la capa de doctrina.")

    # SE ESCRIBE EN EL CRS DE CALCULO, como todas las capas del estudio. La
    # doctrina viene en grados y con un .prj en dialecto ESRI que no declara su
    # codigo; heredarlo dejaria una capa que el propio modulo no puede
    # reproyectar despues.
    if crs_doctrina and crs_estudio and crs_doctrina != crs_estudio:
        from pyproj import Transformer

        vuelta = Transformer.from_crs(crs_doctrina, crs_estudio, always_xy=True)
        elegidas = [[[vuelta.transform(x, y) for x, y in anillo]
                     for anillo in poligono] for poligono in elegidas]

    wkt_salida = shapefile.leer_shapefile(
        Path(destino).parent / "subcuencas.shp").crs_wkt if (
        Path(destino).parent / "subcuencas.shp").is_file() else info.crs_wkt
    shapefile.escribir_poligonos(
        destino, elegidas, campos, atributos, wkt_salida or "",
        estructura=shapefile.ESTRUCTURA_CONSERVAR)
    return {"subzonas": len(elegidas), "de": info.n_registros,
            "destino": str(destino)}


# =============================================================================
# Exportación, que es la única parte que necesita QGIS
# =============================================================================
def _mapa_mayor(layout):
    """El marco de mapa de mayor superficie, que es el mapa principal."""
    from qgis.core import QgsLayoutItemMap

    mapas = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap)]
    if not mapas:
        return None
    return max(mapas, key=lambda m: m.rect().width() * m.rect().height())


def exportar_planchas(ruta_proyecto: Path, salida: Path,
                      formatos: Sequence[str], ppp: int,
                      solo: Sequence[str] = ()) -> list[str]:
    """
    Abre el proyecto en QGIS y exporta cada plancha a PDF y PNG.

    ES LO UNICO QUE NECESITA QGIS. Componer el proyecto es edicion de XML y se
    prueba en el venv; dibujar el mapa exige el motor de render, y ahi no hay
    atajo.

    Excepciones
    -----------
    ErrorConfiguracion
        Si se ejecuta fuera del Python de QGIS.
    """
    try:
        from qgis.core import (
            QgsApplication, QgsLayoutExporter, QgsLayoutItemMap,
            QgsLayoutItemScaleBar, QgsProject, QgsUnitTypes,
        )
    except ImportError as error:
        raise ErrorConfiguracion(
            "no se pudo importar qgis.core: este paso exige el Python de QGIS "
            "(OSGeo4W Shell). Componer el proyecto si funciona sin el, con "
            "--sin-exportar.") from error

    salida = Path(salida)
    salida.mkdir(parents=True, exist_ok=True)

    aplicacion = QgsApplication([], False)
    aplicacion.initQgis()
    try:
        proyecto = QgsProject.instance()
        if not proyecto.read(str(ruta_proyecto)):
            raise ErrorRutas(f"QGIS no pudo abrir {ruta_proyecto}.")
        gestor = proyecto.layoutManager()
        escritas: list[str] = []
        fallidas: list[str] = []
        for layout in gestor.printLayouts():
            nombre = layout.name()
            if solo and nombre not in solo:
                continue
            # LA BARRA LA DIMENSIONA QGIS CON SU PROPIA API. Escribir sus
            # atributos a mano en el XML no funciono: el valor mostrado sale de
            # dividir 'numUnitsPerSegment' entre 'numMapUnitsPerScaleBarUnit',
            # hay un 'segmentMillimeters' que se queda del ajuste anterior y el
            # modo automatico daba segmentos de cinco metros. applyDefaultSize
            # resuelve las tres cosas a la vez y con el mapa ya encuadrado.
            for elemento in layout.items():
                if not isinstance(elemento, QgsLayoutItemScaleBar):
                    continue
                mapa = _mapa_mayor(layout)
                if mapa is not None:
                    elemento.setLinkedMap(mapa)
                elemento.setUnits(QgsUnitTypes.DistanceKilometers)
                elemento.setUnitLabel("km")
                elemento.applyDefaultSize(QgsUnitTypes.DistanceKilometers)

            exportador = QgsLayoutExporter(layout)
            if "pdf" in formatos:
                opciones = QgsLayoutExporter.PdfExportSettings()
                destino = salida / f"{nombre}.pdf"
                # SE COMPRUEBA EL CODIGO DE RETORNO. Sin esto el modulo daba
                # por exportadas planchas que no se habian escrito: cuatro
                # quedaron con el PDF de una corrida anterior y el reporte
                # decia 29 de 29. Un entregable viejo que parece nuevo es peor
                # que uno que falta.
                codigo = exportador.exportToPdf(str(destino), opciones)
                if codigo != QgsLayoutExporter.ExportResult.Success:
                    fallidas.append(f"{nombre} (pdf: {codigo})")
                else:
                    escritas.append(str(destino))
            if "png" in formatos:
                opciones = QgsLayoutExporter.ImageExportSettings()
                opciones.dpi = ppp
                destino = salida / f"{nombre}.png"
                codigo = exportador.exportToImage(str(destino), opciones)
                if codigo != QgsLayoutExporter.ExportResult.Success:
                    fallidas.append(f"{nombre} (png: {codigo})")
                else:
                    escritas.append(str(destino))
        if fallidas:
            raise ErrorHidrologia(
                f"QGIS no pudo escribir {len(fallidas)} plancha(s): "
                f"{fallidas}. Lo mas comun es que el PDF este abierto en un "
                "visor, que bloquea el archivo.")
        return escritas
    finally:
        aplicacion.exitQgis()


# =============================================================================
# Ejecución
# =============================================================================
def _crs_de_presentacion(base: Path, configuracion: Config) -> str:
    """
    El CRS con que el consultor entregó las coordenadas del proyecto.

    NO ES EL DE CALCULO. La cadena calcula en CTM12 y la cartografía se
    presenta en el CRS de entrega, que es el que el consultor y su cliente
    reconocen sobre el papel. Se lee del reporte del M01 y, si no está, del
    punto declarado en la configuración.
    """
    import json

    reporte = rutas.directorio("procesado", base) / "M01_punto_descarga.json"
    if reporte.is_file():
        try:
            datos = json.loads(reporte.read_text(encoding="utf-8"))
            declaradas = (datos.get("resultado", {})
                          .get("coordenadas_declaradas", {}))
            if declaradas.get("crs"):
                return str(declaradas["crs"])
        except (OSError, ValueError, AttributeError):
            pass
    return str(configuracion.obtener("punto_descarga.crs",
                                     configuracion.obtener("crs.calculo")))


def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    exportar: bool = True,
    solo: Sequence[str] = (),
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Compone las planchas del estudio sobre la plantilla y las exporta."""
    import datetime as _dt
    import time

    inicio = time.perf_counter()
    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM16()

    plantilla = rutas.resolver(
        configuracion.obtener("planchas.plantilla"), rutas.raiz_codigo())
    catalogo_ruta = rutas.resolver(
        configuracion.obtener("planchas.catalogo"), rutas.raiz_codigo())
    salida = rutas.resolver(configuracion.obtener("planchas.salida"), base)
    destino_proyecto = rutas.resolver(
        configuracion.obtener("planchas.proyecto"), base)
    vector = rutas.directorio("sig_vector", base)
    crs_calculo = str(configuracion.obtener("crs.calculo"))
    crs_presentacion = _crs_de_presentacion(base, configuracion)
    resultado.plantilla = str(plantilla)

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"plantilla": rutas.relativa(plantilla, rutas.raiz_codigo()),
                 "catalogo": rutas.relativa(catalogo_ruta, rutas.raiz_codigo())},
        parametros={"CRS de calculo": crs_calculo,
                    "CRS de presentacion": crs_presentacion},
    )

    try:
        catalogo = leer_catalogo(catalogo_ruta)
        raiz_xml, contenedor = abrir_proyecto(plantilla)
    except (ErrorRutas, ErrorFormato) as error:
        resultado.hallazgos.append(Hallazgo(BLOQUEANTE, "planchas.insumo",
                                            str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_ERROR)

    # --- La subzona de contexto, que la plancha de localizacion necesita -----
    with registro.bloque(logger, "Subzona de contexto"):
        doctrina = rutas.resolver(
            configuracion.obtener("subzonas_hidrograficas.archivo"),
            rutas.raiz_codigo())
        referencia = vector / "area_influencia.shp"
        try:
            ficha = escribir_subzona_contexto(
                doctrina, vector / "subzona_contexto.shp",
                shapefile.leer_shapefile(referencia).extension, crs_calculo,
                str(configuracion.obtener("subzonas_hidrograficas.crs", "")))
            logger.info("subzona de contexto: %d de %d subzonas",
                        ficha["subzonas"], ficha["de"])
            resultado.productos.append(
                rutas.relativa(vector / "subzona_contexto.shp", base))
        except (ErrorRutas, ErrorHidrologia) as error:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "planchas.subzona_contexto", str(error)))

    # --- Lo que es igual en todo el estudio ----------------------------------
    with registro.bloque(logger, "Datos del estudio"):
        logos = configuracion.obtener("planchas.logos", {}) or {}
        cuenta = aplicar_datos_del_estudio(
            raiz_xml,
            str(configuracion.obtener("proyecto.nombre", "")),
            fecha_en_espanol(_dt.date.today()),
            str(configuracion.obtener("proyecto.contratante", "") or ""),
            str(configuracion.obtener("proyecto.consultor", "") or ""),
            str(logos.get("contratante", "")),
            str(logos.get("consultor", "")),
            bool(configuracion.obtener("planchas.escribir_nombres", False)),
        )
        logger.info("rotulo: %s", cuenta)
        for clave in ("contratante", "consultor") if configuracion.obtener(
                "planchas.escribir_nombres", False) else ():
            if not str(configuracion.obtener(f"proyecto.{clave}", "") or "").strip():
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"planchas.sin_{clave}",
                    f"proyecto.{clave} esta vacio en config.yaml y su marco "
                    f"del rotulo queda en blanco en las {cuenta[clave]} "
                    "planchas. No se inventa: es un dato del contrato."))

    # --- Cada plancha --------------------------------------------------------
    with registro.bloque(logger, "Composicion de planchas"):
        layouts = layouts_por_nombre(raiz_xml)
        declaradas = {str(p.get("layout", "")) for p in catalogo["planchas"]}
        for nombre in sorted(layouts):
            if nombre.startswith("Figura") and nombre not in declaradas:
                resultado.sin_layout.append(nombre)

        for plancha in catalogo["planchas"]:
            nombre = str(plancha.get("layout", ""))
            if solo and nombre not in solo:
                continue
            layout = layouts.get(nombre)
            if layout is None:
                resultado.sin_layout.append(nombre)
                continue
            capa = vector / f"{plancha.get('enmarca', '')}.shp"
            try:
                extension = extension_de_capa(capa, crs_calculo,
                                              crs_presentacion)
                ficha = componer_plancha(layout, plancha, extension, catalogo,
                                         crs_presentacion)
            except (ErrorRutas, ErrorFormato, ErrorHidrologia) as error:
                resultado.sin_encuadre.append(f"{nombre} ({error})")
                continue
            resultado.planchas.append(ficha)
        logger.info("%d plancha(s) compuestas, %d sin encuadre",
                    len(resultado.planchas), len(resultado.sin_encuadre))

    guardar_proyecto(raiz_xml, contenedor, destino_proyecto)
    resultado.proyecto = rutas.relativa(destino_proyecto, base)
    resultado.productos.append(resultado.proyecto)

    if exportar:
        with registro.bloque(logger, "Exportacion"):
            try:
                escritas = exportar_planchas(
                    destino_proyecto, salida,
                    configuracion.obtener("planchas.formatos", ["pdf"]),
                    int(configuracion.obtener("planchas.ppp_png", 300)),
                    solo)
                resultado.exportadas = [rutas.relativa(Path(r), base)
                                        for r in escritas]
                resultado.productos.extend(resultado.exportadas)
                logger.info("%d archivo(s) exportados", len(escritas))
            except (ErrorConfiguracion, ErrorRutas,
                    ErrorHidrologia) as error:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, "planchas.exportacion", str(error)))

    resultado.hallazgos.extend(_resumir(resultado))
    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _resumir(resultado: ResultadoM16) -> list[Hallazgo]:
    """Lo que quedó compuesto y lo que no."""
    hallazgos: list[Hallazgo] = []
    if resultado.planchas:
        escalas = sorted({f["escala"] for f in resultado.planchas})
        hallazgos.append(Hallazgo(
            INFORMATIVO, "planchas.compuestas",
            f"{len(resultado.planchas)} plancha(s) compuestas sobre la "
            f"plantilla, en {len(escalas)} escala(s): "
            f"{', '.join('1:' + formatear_escala(e) for e in escalas)}. La "
            "escala sale del marco y no de un texto escrito a mano."))
    if resultado.sin_layout:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "planchas.sin_layout",
            f"{len(resultado.sin_layout)} plancha(s) sin pareja entre el "
            f"catalogo y la plantilla: {sorted(set(resultado.sin_layout))}. "
            "Se emparejan por el NOMBRE del layout; si no coincide, el modulo "
            "no adivina."))
    if resultado.sin_encuadre:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "planchas.sin_encuadre",
            f"{len(resultado.sin_encuadre)} plancha(s) quedaron con el "
            f"encuadre de la plantilla: {resultado.sin_encuadre[:5]}. La capa "
            "que declaran en 'enmarca' no esta en el estudio."))
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
            emitir("  %-40s %s", hallazgo.clave, hallazgo.mensaje)

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
        "proyecto": resultado.proyecto,
        "planchas": resultado.planchas,
        "sin_layout": resultado.sin_layout,
        "sin_encuadre": resultado.sin_encuadre,
        "exportadas": resultado.exportadas,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=2),
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

def main() -> int:
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None)
    analizador.add_argument("--solo", action="append", default=[],
                            help="nombre exacto de una plancha; repetible")
    analizador.add_argument("--sin-exportar", action="store_true",
                            help="compone el proyecto y no llama a QGIS")
    argumentos = analizador.parse_args()
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json, exportar=not argumentos.sin_exportar,
            solo=tuple(argumentos.solo))
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())

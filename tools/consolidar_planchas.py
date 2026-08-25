#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Repara de una vez la plantilla de planchas que el consultor ajustó en QGIS.

POR QUE EXISTE. El proyecto que el consultor entregó tiene la presentación que
quiere, y esa es la parte cara: encuadres, simbología, posición del rótulo. Lo
que no puede quedarse es lo que apunta a su máquina o a una capa que no
sobrevive al cierre. Se arregla aquí, una vez, y a partir de ahí el M16 solo
hace el trabajo de cada estudio.

MISMO CAMINO QUE LA PLANTILLA DE WORD. templates/planchas.qgz es la fuente de
verdad y está versionada; el proyecto que queda en cada estudio es un producto,
no un original. No se ejecuta como parte de la cadena.

    python tools/consolidar_planchas.py            comprueba sin escribir
    python tools/consolidar_planchas.py --escribir aplica y guarda
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_RAIZ = Path(__file__).resolve().parents[1]
PLANTILLA = _RAIZ / "templates" / "planchas.qgz"

# Capas cuyo origen hay que reapuntar. La CLAVE es el nombre que tienen en el
# proyecto; el valor, la ruta relativa al .qgz y el proveedor que les toca.
#
# 'Limpiada' es la cuenca completa que el consultor disolvió a mano. Estaba en
# una capa EN MEMORIA, que QGIS no guarda: al reabrir el proyecto salía vacía y
# las siete planchas que la usan perdían el contorno sin avisar. Ahora la
# escribe el M10 en cada estudio.
REAPUNTAR = {
    "Limpiada": ("../../03_SIG/vector/cuenca_completa.shp", "ogr",
                 "Cuenca completa"),
    "Zonificacion_hidrografica_2013": (
        "../../03_SIG/vector/subzona_contexto.shp", "ogr",
        "Zonificación hidrográfica"),
}

# Capas cargadas y no usadas por ninguna plancha. Dos son pasos intermedios del
# disuelto y la tercera es la zonificación nacional entera, 316 polígonos.
QUITAR_CAPAS = ("Disuelto", "Monoparte")

# Un item cuyo archivo esté bajo una de estas rutas es una sobra: apunta a la
# carpeta de descargas de otra máquina y no existe en ningún estudio.
RUTAS_AJENAS = ("/Users/", "\\Users\\", "Downloads")

# Fuera de la página no se imprime, pero se arrastra en cada copia y confunde
# al leer el proyecto. La página más ancha del juego mide 432 mm.
ANCHO_MAXIMO_MM = 440.0


# Planchas que el consultor retiro del juego. Quedaron como layout en el
# proyecto pero NO estan entre los PDF que entrego, que son un juego limpio de
# 1 a 29 sin huecos: esa numeracion consecutiva es la decision.
QUITAR_PLANCHAS = (
    "Figura 4. Modelo digital de elevación",
    "Figura 13. Cobertura de la tierra por clase",
    # Composicion vacia que QGIS crea por defecto. Se exportaba como una
    # plancha mas, produciendo un PDF de 3 kB sin nada dentro.
    "Composición 1",
)

# El numero de cada plancha en el juego final, en el orden de los PDF. Se
# renumera para que la plantilla, el catalogo y el entregable digan lo mismo:
# mantener el numero viejo obligaria a una tabla de equivalencias, que es una
# fuente de error mas.
ORDEN_FINAL = (
    "Localización general",
    "Zonificacion Hidrografica",
    "Área de influencia",
    "Pendiente del terreno",
    "Red de drenaje",
    "Subcuencas del modelo hidrológico",
    "Cobertura de la tierra",
    "Estaciones hidrometeorológicas",
    "Número de curva",
    "Tiempo de rezago",
    "Orden de la red de drenaje",
    "Escorrentía media multianual",
    "Evapotranspiración real",
    "Evapotranspiración potencial",
    "Temperatura media",
    "Coeficiente de infiltración",
    "Precipitación media anual",
    "Precipitación Total Multianual Año compuesto",
    "Precipitación Total Multianual Año neutral",
    "Precipitación Total Multianual Año nina",
    "Precipitación Total Multianual Año nino",
    "Precipitación Máxima Tr 10 Años",
    "Precipitación Máxima Tr 100 Años",
    "Precipitación Máxima Tr 15 Años",
    "Precipitación Máxima Tr 25 Años",
    "Precipitación Máxima Tr 2.33 Años",
    "Precipitación Máxima Tr 5 Años",
    "Precipitación Máxima Tr 50 Años",
    "Precipitación Máxima Tr 500 Años",
)


def _titulo_sin_numero(nombre: str) -> str:
    """'Figura 12. Orden de la red' -> 'Orden de la red'."""
    partes = nombre.split(". ", 1)
    return partes[1] if len(partes) == 2 else nombre


# Los logos que el consultor coloco NO llevan id, y los marcos que SI lo llevan
# son sobras de una plantilla anterior. Rellenar los segundos ponia un logo
# duplicado en mitad del lienzo, otro desbordando el borde del rotulo y una
# equis roja donde el marco estaba vacio. Se quitan las sobras y se le da el id
# al marco de verdad, reconocido por el archivo al que apunta.
LOGOS_POR_ARCHIVO = (
    ("logo_amarilo", "logo_contratante"),
    ("logo real", "logo_consultor"),
    ("ROSA", "rosa_nautica"),
)


def identificar_logos(raiz, escribir: bool) -> list[str]:
    """Quita los marcos de logo heredados y nombra los que el consultor puso."""
    hechos: list[str] = []
    sobras = nombrados = 0
    for lay in raiz.iter("Layout"):
        if not (lay.get("name") or "").startswith("Figura"):
            continue
        for item in list(lay.findall("LayoutItem")):
            if item.get("file") is None:
                continue
            archivo = item.get("file") or ""
            esperado = next((idd for marca, idd in LOGOS_POR_ARCHIVO
                             if marca in archivo), "")
            if not esperado:
                # Marco de imagen sin archivo reconocible: es una sobra, y es
                # lo que se dibuja como una equis roja.
                if escribir:
                    lay.remove(item)
                sobras += 1
                continue
            if item.get("id") != esperado:
                if escribir:
                    item.set("id", esperado)
                nombrados += 1
    if sobras:
        hechos.append(f"quitados {sobras} marco(s) de imagen sin archivo util")
    if nombrados:
        hechos.append(f"nombrados {nombrados} marco(s) de logo por su archivo")
    return hechos



def reponer_logos(raiz, escribir: bool) -> list[str]:
    """
    Repone el marco de logo que le falte a una plancha, copiandolo de otra.

    LOS QUE FALTAN SON LOS QUE APUNTABAN A OTRA MAQUINA. Al quitar las rutas
    ajenas, dos planchas se quedaron sin logo y salieron con el hueco. Se copia
    el marco de otra plancha DEL MISMO TAMANO DE PAGINA, para que la posicion
    siga cayendo dentro del rotulo.
    """
    import copy as _copy

    por_pagina: dict[str, dict[str, object]] = {}
    paginas: dict[str, str] = {}
    for lay in raiz.iter("Layout"):
        nombre = lay.get("name") or ""
        if not nombre.startswith("Figura"):
            continue
        pagina = ""
        for pg in lay.iter("LayoutItem"):
            pagina = pg.get("size") or ""
            break
        paginas[nombre] = pagina
        for item in lay.findall("LayoutItem"):
            idd = item.get("id") or ""
            if idd in ("logo_contratante", "logo_consultor", "rosa_nautica"):
                por_pagina.setdefault(pagina, {}).setdefault(idd, item)

    hechos: list[str] = []
    for lay in raiz.iter("Layout"):
        nombre = lay.get("name") or ""
        if not nombre.startswith("Figura"):
            continue
        presentes = {i.get("id") for i in lay.findall("LayoutItem")}
        for idd in ("logo_contratante", "logo_consultor"):
            if idd in presentes:
                continue
            modelo = (por_pagina.get(paginas[nombre]) or {}).get(idd)
            if modelo is None:
                hechos.append(f"NO HAY DE DONDE COPIAR {idd} para '{nombre}'")
                continue
            if escribir:
                lay.append(_copy.deepcopy(modelo))
            hechos.append(f"repuesto {idd} en '{nombre}'")
    return hechos



# La cuenca completa se dibuja SOLO CON CONTORNO. Heredo el relleno opaco de la
# capa en memoria de la que salio, y en la plancha de pendientes tapaba el
# raster que la plancha existe para mostrar.
CAPAS_SIN_RELLENO = ("Cuenca completa", "Subcuencas")

# Planchas donde el area de influencia es el TEMA. En las demas se retira: a la
# escala de trabajo su contorno sale cortado por el borde del lienzo y se lee
# como un error de dibujo.
PLANCHAS_CON_AREA = ("Figura 3. Área de influencia",)

# La cuenca acompana a toda plancha tematica: es la unidad de la que habla el
# dato que se dibuja.
# La plancha de zonificacion no tiene hermana de la que copiar su juego de
# capas, de modo que se declara por nombre. El orden es el de dibujo, de arriba
# abajo, como QGIS lo escribe.
CAPAS_DE_ZONIFICACION = (
    "Punto de descarga",
    "Cuenca completa",
    "Subzona hidrográfica",
    "Zonificación hidrográfica",
    "Drenaje sencillo",
    "Cauce doble",
    "Embalses y lagunas",
    "Esri World Imagery",
)

CAPA_CUENCA = "Cuenca completa"


def quitar_relleno(raiz, escribir: bool) -> list[str]:
    """Deja en contorno las capas que no deben tapar lo que hay debajo."""
    hechos: list[str] = []
    for capa in raiz.iter("maplayer"):
        if (capa.findtext("layername") or "") not in CAPAS_SIN_RELLENO:
            continue
        cambiados = 0
        for opcion in capa.iter("Option"):
            if opcion.get("name") == "style" and opcion.get("value") != "no":
                if escribir:
                    opcion.set("value", "no")
                cambiados += 1
            if opcion.get("name") == "outline_width" and escribir:
                opcion.set("value", "0.46")
        if cambiados:
            hechos.append(f"'{capa.findtext('layername')}' pasa a contorno "
                          f"solo ({cambiados} relleno(s))")
    return hechos


def _conjuntos_de(lay):
    """
    TODOS los LayerSet de una plancha, no solo el primero.

    Dos planchas llevan una vista de detalle ademas del mapa principal, y cada
    marco tiene su propia lista de capas. Tocando solo la primera, la
    zonificacion seguia sin su subzona en el mapa grande por mucho que se le
    declarara: la lista que yo editaba era la del recuadro.
    """
    return [item.find("LayerSet") for item in lay.findall("LayoutItem")
            if item.find("LayerSet") is not None]


def corregir_capas_de_planchas(raiz, escribir: bool) -> list[str]:
    """
    Repone el juego de capas vacio, anade la cuenca y retira el area.

    RECORRE TODOS LOS MARCOS de cada plancha. Dos llevan una vista de detalle
    ademas del mapa principal, con su propia lista de capas: tocando solo la
    primera, la zonificacion seguia sin subzona en el mapa grande porque la
    lista que se editaba era la del recuadro.
    """
    import copy as _copy

    hechos: list[str] = []
    nombres = {ml.findtext("id"): (ml.findtext("layername") or "")
               for ml in raiz.iter("maplayer")}
    id_cuenca = next((i for i, n in nombres.items() if n == CAPA_CUENCA), "")
    por_nombre = {n: i for i, n in nombres.items()}

    # Un juego de referencia por familia, para reponer los que quedaron vacios.
    referencia = {}
    for lay in raiz.iter("Layout"):
        nombre = lay.get("name") or ""
        for conjunto in _conjuntos_de(lay):
            if len(conjunto):
                familia = nombre.split(". ", 1)[-1].split(" Tr ")[0][:18]
                referencia.setdefault(familia, conjunto)
                break

    for lay in raiz.iter("Layout"):
        nombre = lay.get("name") or ""
        if not nombre.startswith("Figura"):
            continue
        conjuntos = _conjuntos_de(lay)
        if not conjuntos:
            hechos.append(f"SIN MARCO DE MAPA: '{nombre}'")
            continue

        for orden, conjunto in enumerate(conjuntos, start=1):
            marca = "" if len(conjuntos) == 1 else f" (marco {orden})"

            # 1. Juego vacio: se repone.
            if not len(conjunto):
                familia = nombre.split(". ", 1)[-1].split(" Tr ")[0][:18]
                modelo = referencia.get(familia)
                if modelo is None and "Zonificacion" in nombre:
                    if escribir:
                        for etiqueta in CAPAS_DE_ZONIFICACION:
                            ident = por_nombre.get(etiqueta)
                            if ident:
                                elemento = ET.SubElement(conjunto, "Layer")
                                elemento.text = ident
                    puestas = sum(1 for e in CAPAS_DE_ZONIFICACION
                                  if por_nombre.get(e))
                    hechos.append(f"declarado el juego de capas de "
                                  f"'{nombre}'{marca} ({puestas} capas)")
                elif modelo is None:
                    hechos.append(f"JUEGO DE CAPAS VACIO y sin hermana de "
                                  f"donde copiarlo: '{nombre}'{marca}")
                    continue
                else:
                    if escribir:
                        for elemento in modelo:
                            conjunto.append(_copy.deepcopy(elemento))
                    hechos.append(f"repuesto el juego de capas de "
                                  f"'{nombre}'{marca} ({len(modelo)} capas)")

            actuales = [e.text for e in conjunto]

            # 2. La cuenca acompana a toda plancha tematica.
            if id_cuenca and id_cuenca not in actuales and len(conjunto):
                if escribir:
                    elemento = _copy.deepcopy(conjunto[0])
                    elemento.text = id_cuenca
                    conjunto.append(elemento)
                hechos.append(f"anadida la cuenca a '{nombre}'{marca}")

            # 3. El area de influencia solo donde es el tema.
            if nombre not in PLANCHAS_CON_AREA:
                for elemento in list(conjunto):
                    if nombres.get(elemento.text or "") == "Área de influencia":
                        if escribir:
                            conjunto.remove(elemento)
                        hechos.append(f"retirada el area de influencia de "
                                      f"'{nombre}'{marca}")

            # 4. La subzona, donde la plancha habla de ella.
            if "Zonificacion" in nombre:
                for etiqueta in ("Subzona hidrográfica",
                                 "Zonificación hidrográfica"):
                    ident = por_nombre.get(etiqueta)
                    if ident and ident not in [e.text for e in conjunto]:
                        if escribir:
                            elemento = ET.SubElement(conjunto, "Layer")
                            elemento.text = ident
                        hechos.append(f"anadida '{etiqueta}' a "
                                      f"'{nombre}'{marca}")
    return hechos

def sincronizar_marcos_y_leyendas(raiz, escribir: bool) -> list[str]:
    """
    Hace que cada marco respete su lista de capas y cada leyenda siga a su mapa.

    DOS AJUSTES QUE NO SE VEN EN EL XML PERO LO GOBIERNAN TODO:

    'keepLayerSet' en falso hace que el marco IGNORE su propia lista de capas y
    dibuje lo que el arbol del proyecto tenga marcado. Tres planchas lo tenian
    asi, justo las tres cuya lista estaba vacia: por mucho que se les declaren
    capas, no las miran. Es la causa de que la zonificacion siguiera sin su
    subzona despues de declararsela.

    'autoUpdateModel' ausente deja la leyenda con una COPIA CONGELADA de las
    capas que tenia el dia que se creo. Por eso el area de influencia seguia
    apareciendo en las convenciones despues de retirarla del mapa. Con el
    puesto, la leyenda sigue al marco al que ya apunta por 'map_uuid' y las dos
    cosas no pueden volver a discrepar.
    """
    hechos: list[str] = []
    marcos = leyendas = 0
    for lay in raiz.iter("Layout"):
        if not (lay.get("name") or "").startswith("Figura"):
            continue
        for item in lay.findall("LayoutItem"):
            if item.find("LayerSet") is not None:
                if item.get("keepLayerSet") != "true":
                    if escribir:
                        item.set("keepLayerSet", "true")
                    marcos += 1
            if item.find("layer-tree-group") is not None:
                if item.get("autoUpdateModel") != "1":
                    if escribir:
                        item.set("autoUpdateModel", "1")
                    leyendas += 1
    if marcos:
        hechos.append(f"{marcos} marco(s) pasan a respetar su lista de capas")
    if leyendas:
        hechos.append(f"{leyendas} leyenda(s) pasan a seguir a su mapa")
    return hechos


def limpiar_arbol(raiz, escribir: bool) -> list[str]:
    """
    Quita del arbol del proyecto los nodos que apuntan a capas que ya no estan.

    Al retirar las capas en memoria y las copias sin uso, sus nodos quedaron
    colgando. No rompen el dibujo, pero QGIS los muestra en blanco al abrir el
    proyecto y no se puede saber que eran.
    """
    existentes = {ml.findtext("id") for ml in raiz.iter("maplayer")}
    padres = _padres(raiz)
    colgando = [n for n in raiz.iter("layer-tree-layer")
                if n.get("id") not in existentes]
    if not colgando:
        return []
    if escribir:
        for nodo in colgando:
            padres[nodo].remove(nodo)
    return [f"quitados {len(colgando)} nodo(s) del arbol sin capa detras"]



# La plancha de subcuencas habla de las subcuencas, no del contorno de la
# cuenca. Se cambia la capa y se le ponen etiquetas con el nombre de cada una.
SUSTITUIR_CAPA = {
    "Figura 6. Subcuencas del modelo hidrológico": ("Cuenca completa",
                                                    "Subcuencas"),
}

# Capa de la que se copia la estructura de etiquetado. Se CLONA en lugar de
# escribirse a mano porque el bloque de etiquetas de QGIS tiene decenas de
# claves y una mal puesta no da error: deja la capa sin etiquetar y en silencio.
MODELO_ETIQUETAS = "Subzona hidrográfica"
ETIQUETAR = {"Subcuencas": "name"}


def ajustar_plancha_de_subcuencas(raiz, escribir: bool) -> list[str]:
    """Cambia la capa de la plancha de subcuencas y la etiqueta."""
    import copy as _copy

    hechos: list[str] = []
    por_nombre = {}
    for ml in raiz.iter("maplayer"):
        por_nombre.setdefault(ml.findtext("layername") or "", ml)

    # --- 1. La capa que dibuja la plancha ------------------------------------
    for lay in raiz.iter("Layout"):
        nombre = lay.get("name") or ""
        if nombre not in SUSTITUIR_CAPA:
            continue
        viejo, nuevo = SUSTITUIR_CAPA[nombre]
        capa_vieja = por_nombre.get(viejo)
        capa_nueva = por_nombre.get(nuevo)
        if capa_vieja is None or capa_nueva is None:
            hechos.append(f"NO SE ENCONTRARON las capas '{viejo}' o '{nuevo}'")
            continue
        id_viejo = capa_vieja.findtext("id")
        id_nuevo = capa_nueva.findtext("id")
        for item in lay.findall("LayoutItem"):
            conjunto = item.find("LayerSet")
            if conjunto is None:
                continue
            textos = [e.text for e in conjunto]
            if id_nuevo in textos:
                hechos.append(f"YA AJUSTADA: '{nombre}' dibuja '{nuevo}'")
                break
            for elemento in conjunto:
                if elemento.text == id_viejo:
                    if escribir:
                        elemento.text = id_nuevo
                    hechos.append(f"'{nombre}' pasa de '{viejo}' a '{nuevo}'")
                    break
            break

    # --- 2. Etiquetas, clonadas de una capa que ya las tiene bien ------------
    modelo = por_nombre.get(MODELO_ETIQUETAS)
    if modelo is None or modelo.find("labeling") is None:
        hechos.append(f"NO HAY DE DONDE COPIAR el etiquetado "
                      f"('{MODELO_ETIQUETAS}')")
        return hechos

    for etiqueta_capa, campo in ETIQUETAR.items():
        capa = por_nombre.get(etiqueta_capa)
        if capa is None:
            hechos.append(f"NO ESTA la capa '{etiqueta_capa}'")
            continue
        if capa.find("labeling") is not None:
            # Tener el bloque no basta: sin 'labelsEnabled' se guarda y no se
            # dibuja nada. Se comprueban las dos cosas.
            if capa.get("labelsEnabled") == "1":
                hechos.append(f"YA ETIQUETADA: '{etiqueta_capa}'")
                continue
            if escribir:
                capa.set("labelsEnabled", "1")
            hechos.append(f"activadas las etiquetas de '{etiqueta_capa}'")
            continue
        if escribir:
            copia = _copy.deepcopy(modelo.find("labeling"))
            for estilo in copia.iter("text-style"):
                estilo.set("fieldName", campo)
                estilo.set("isExpression", "0")
            capa.append(copia)
            # SIN ESTE ATRIBUTO EL BLOQUE SE GUARDA Y NO SE DIBUJA NADA. Es lo
            # que distingue una capa con etiquetado declarado de una que lo
            # rotula: 'Subzona hidrografica' lo tenia en 1 y por eso salia con
            # su nombre, y 'Subcuencas' en 0.
            capa.set("labelsEnabled", "1")
            # QGIS activa el etiquetado con esta propiedad; sin ella el bloque
            # se guarda y no se dibuja nada.
            propiedades = capa.find("customproperties")
            if propiedades is None:
                propiedades = ET.SubElement(capa, "customproperties")
            for opcion in propiedades.iter("Option"):
                if opcion.get("name") == "labeling/enabled":
                    opcion.set("value", "true")
                    break
        hechos.append(f"'{etiqueta_capa}' etiquetada por el campo '{campo}'")
    return hechos



def fondo_a_la_rosa(raiz, escribir: bool) -> list[str]:
    """
    Da fondo opaco a la rosa nautica, que se dibujaba sobre el trazo del mapa.

    Su PNG tiene transparencia y quedaba encima del perimetro de la cuenca sin
    nada que la separase. La leyenda ya tenia fondo y marco; la rosa no.
    """
    puestos = 0
    for lay in raiz.iter("Layout"):
        for item in lay.findall("LayoutItem"):
            if "ROSA" not in (item.get("file") or ""):
                continue
            if item.get("background") == "true":
                continue
            if escribir:
                item.set("background", "true")
                item.set("backgroundColor", "255,255,255,255")
            puestos += 1
    return [f"fondo opaco a {puestos} rosa(s) nautica(s)"] if puestos else []



def refrescar_leyendas_y_escalas(raiz, escribir: bool) -> list[str]:
    """
    Deja que la leyenda y la barra de escala se reconstruyan desde su mapa.

    LA LEYENDA GUARDA UN ARBOL PROPIO y lo dibuja tal cual, aunque
    'autoUpdateModel' este en 1: ese ajuste solo dice que QGIS puede
    reconstruirlo, no que lo haga si ya hay uno escrito. El de estas planchas
    conservaba los nombres de antes de tocar nada, 'Limpiada' y
    'Zonificacion_hidrografica_2013', y seguia listando el area de influencia
    despues de retirarla del mapa. Vaciandolo, QGIS lo repuebla al abrir el
    proyecto con las capas que el marco dibuja de verdad.

    LA BARRA DE ESCALA NO ESTABA VINCULADA A NINGUN MAPA: llevaba un tamano de
    segmento fijo, de modo que decia 2,6 km sobre una plancha a 1:750.000, donde
    esa distancia mide tres milimetros. Se le da el marco y se le pide que ajuste
    el segmento al ancho, para que no pueda volver a contradecir a la escala
    escrita.
    """
    hechos: list[str] = []
    vaciadas = 0
    # VACIAR EL ARBOL DEJA LA LEYENDA EN BLANCO: QGIS no lo repuebla al abrir,
    # 'autoUpdateModel' solo dice que PUEDE hacerlo. Se reconstruye aqui, con
    # las capas que el marco dibuja de verdad.
    capas = {ml.findtext("id"): (ml.findtext("layername") or "",
                                 ml.findtext("provider") or "ogr",
                                 ml.findtext("datasource") or "")
             for ml in raiz.iter("maplayer")}
    for lay in raiz.iter("Layout"):
        if not (lay.get("name") or "").startswith("Figura"):
            continue

        # El marco de mapa principal, que es el mayor.
        principal = None
        mayor = -1.0
        for item in lay.findall("LayoutItem"):
            if item.find("LayerSet") is None:
                continue
            medidas = (item.get("size") or "").split(",")
            try:
                superficie = float(medidas[0]) * float(medidas[1])
            except (IndexError, ValueError):
                continue
            if superficie > mayor:
                mayor, principal = superficie, item

        for item in lay.findall("LayoutItem"):
            arbol = item.find("layer-tree-group")
            if arbol is not None and principal is not None:
                conjunto = principal.find("LayerSet")
                deseadas = [e.text for e in conjunto] if conjunto is not None else []
                actuales = [n.get("id") for n in arbol.iter("layer-tree-layer")]
                if deseadas and actuales != deseadas:
                    if escribir:
                        for nodo in list(arbol.iter("layer-tree-layer")):
                            arbol.remove(nodo)
                        for ident in deseadas:
                            ficha = capas.get(ident)
                            if ficha is None:
                                continue
                            nodo = ET.SubElement(arbol, "layer-tree-layer")
                            nodo.set("checked", "Qt::Checked")
                            nodo.set("expanded", "1")
                            nodo.set("id", ident)
                            nodo.set("legend_exp", "")
                            nodo.set("legend_split_behavior", "0")
                            nodo.set("name", ficha[0])
                            nodo.set("patch_size", "-1,-1")
                            nodo.set("providerKey", ficha[1])
                            nodo.set("source", ficha[2])
                            ET.SubElement(nodo, "customproperties")
                    vaciadas += 1
    if vaciadas:
        hechos.append(f"reconstruido el arbol de {vaciadas} leyenda(s) con las capas de su mapa")
    return hechos


def _abrir(ruta: Path) -> tuple[ET.Element, dict]:
    """Devuelve el árbol del .qgs y los demás archivos del contenedor."""
    with zipfile.ZipFile(ruta) as paquete:
        otros = {n: paquete.read(n) for n in paquete.namelist()
                 if not n.endswith(".qgs")}
        nombre = next(n for n in paquete.namelist() if n.endswith(".qgs"))
        xml = paquete.read(nombre).decode("utf-8")
    return ET.fromstring(xml), {"otros": otros, "nombre": nombre}


def _guardar(raiz: ET.Element, contenedor: dict, destino: Path) -> None:
    """Reescribe el .qgz conservando el resto de sus archivos."""
    cuerpo = ET.tostring(raiz, encoding="unicode", xml_declaration=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as paquete:
        paquete.writestr(contenedor["nombre"], cuerpo)
        for nombre, datos in contenedor["otros"].items():
            paquete.writestr(nombre, datos)


def _padres(raiz: ET.Element) -> dict:
    """Mapa hijo -> padre, que ElementTree no ofrece y hace falta para borrar."""
    return {hijo: padre for padre in raiz.iter() for hijo in padre}


def consolidar(ruta: Path, escribir: bool) -> list[str]:
    """Aplica las reparaciones y devuelve lo que hizo, o lo que haría."""
    raiz, contenedor = _abrir(ruta)
    padres = _padres(raiz)
    hechos: list[str] = []

    usadas = {elemento.text for conjunto in raiz.iter("LayerSet")
              for elemento in conjunto if elemento.text}

    # --- 1. Capas que apuntan a donde no deben --------------------------------
    # SOLO LA QUE ALGUNA PLANCHA USA. El proyecto trae la zonificacion cargada
    # dos veces, una filtrada y otra entera; reapuntar las dos dejaria una capa
    # duplicada que nadie dibuja.
    for capa in list(raiz.iter("maplayer")):
        nombre = capa.findtext("layername") or ""
        if nombre not in REAPUNTAR:
            continue
        if capa.findtext("id") not in usadas:
            if escribir:
                padres[capa].remove(capa)
            hechos.append(f"quitada la copia sin uso de '{nombre}'")
            continue
        destino, proveedor, nombre_nuevo = REAPUNTAR[nombre]
        actual = capa.findtext("datasource") or ""
        if actual == destino:
            hechos.append(f"YA REAPUNTADA: {nombre}")
            continue
        if escribir:
            capa.find("datasource").text = destino
            nodo = capa.find("provider")
            if nodo is not None:
                nodo.text = proveedor
            capa.find("layername").text = nombre_nuevo
        hechos.append(f"'{nombre}' -> {destino} ({proveedor}), renombrada "
                      f"'{nombre_nuevo}'")

    # --- 2. Capas cargadas que ninguna plancha usa ----------------------------
    for capa in list(raiz.iter("maplayer")):
        nombre = capa.findtext("layername") or ""
        if nombre not in QUITAR_CAPAS:
            continue
        if capa.findtext("id") in usadas:
            hechos.append(f"NO SE QUITA '{nombre}': alguna plancha la usa")
            continue
        if escribir:
            padres[capa].remove(capa)
        hechos.append(f"quitada la capa sin uso '{nombre}'")

    # --- 3. Items que apuntan a la maquina de otro ----------------------------
    ajenos = 0
    for lay in raiz.iter("Layout"):
        for item in list(lay.findall("LayoutItem")):
            archivo = item.get("file") or ""
            if archivo and any(t in archivo for t in RUTAS_AJENAS):
                if escribir:
                    lay.remove(item)
                ajenos += 1
    if ajenos:
        hechos.append(f"quitados {ajenos} item(s) con ruta de otra maquina")

    # --- 4. Sobras fuera de la pagina -----------------------------------------
    fuera = 0
    for lay in raiz.iter("Layout"):
        # EL ANCHO SE LEE DE CADA PLANCHA. Las hay de 210, 420 y 432 mm, y un
        # umbral unico dejaria pasar un item a 229 mm sobre una pagina A4, que
        # esta fuera, o borraria uno legitimo de una tabloide.
        ancho = ANCHO_MAXIMO_MM
        for pagina in lay.iter("LayoutItem"):
            medidas = (pagina.get("size") or "").split(",")
            try:
                ancho = float(medidas[0])
            except (IndexError, ValueError):
                pass
            break
        for item in list(lay.findall("LayoutItem")):
            posicion = (item.get("position") or "").split(",")
            try:
                x = float(posicion[0])
            except (IndexError, ValueError):
                continue
            if x > ancho:
                if escribir:
                    lay.remove(item)
                fuera += 1
    if fuera:
        hechos.append(f"quitados {fuera} item(s) colocados fuera de la pagina")

    # --- 5. Etiquetas vacias repetidas ----------------------------------------
    # El rotulo lleva un solo marco por dato. Las copias vacias son de una
    # version anterior de la plantilla y solo estorban al leer el proyecto.
    vacias = 0
    for lay in raiz.iter("Layout"):
        vistos = set()
        for item in list(lay.findall("LayoutItem")):
            idd = item.get("id") or ""
            if not idd or item.get("labelText") is None:
                continue
            if (item.get("labelText") or "").strip():
                vistos.add(idd)
                continue
            if idd in vistos:
                if escribir:
                    lay.remove(item)
                vacias += 1
            else:
                vistos.add(idd)
    if vacias:
        hechos.append(f"quitadas {vacias} etiqueta(s) vacias repetidas")


    # --- 6. Planchas retiradas y renumeracion ---------------------------------
    for layout in list(raiz.iter("Layout")):
        if (layout.get("name") or "") in QUITAR_PLANCHAS:
            if escribir:
                padres[layout].remove(layout)
            hechos.append(f"retirada la plancha '{layout.get('name')}'")

    posicion = {titulo: numero
                for numero, titulo in enumerate(ORDEN_FINAL, start=1)}
    renumeradas = 0
    huerfanas = []
    for layout in raiz.iter("Layout"):
        nombre = layout.get("name") or ""
        if not nombre.startswith("Figura"):
            continue
        titulo = _titulo_sin_numero(nombre)
        numero = posicion.get(titulo)
        if numero is None:
            huerfanas.append(nombre)
            continue
        nuevo = f"Figura {numero}. {titulo}"
        if nuevo == nombre:
            continue
        if escribir:
            layout.set("name", nuevo)
        renumeradas += 1
    if renumeradas:
        hechos.append(f"renumeradas {renumeradas} plancha(s) al juego final "
                      f"de {len(ORDEN_FINAL)}")
    for nombre in huerfanas:
        hechos.append(f"SIN SITIO en el juego final: '{nombre}'")

    # --- 7. Logos -------------------------------------------------------------
    hechos.extend(identificar_logos(raiz, escribir))
    hechos.extend(reponer_logos(raiz, escribir))

    # --- 8. Capas de cada plancha --------------------------------------------
    hechos.extend(quitar_relleno(raiz, escribir))
    hechos.extend(corregir_capas_de_planchas(raiz, escribir))
    hechos.extend(ajustar_plancha_de_subcuencas(raiz, escribir))
    hechos.extend(sincronizar_marcos_y_leyendas(raiz, escribir))
    hechos.extend(refrescar_leyendas_y_escalas(raiz, escribir))
    hechos.extend(fondo_a_la_rosa(raiz, escribir))
    hechos.extend(limpiar_arbol(raiz, escribir))

    if escribir and hechos:
        _guardar(raiz, contenedor, ruta)
    return hechos


def main() -> int:
    analizador = argparse.ArgumentParser(description=__doc__)
    analizador.add_argument("--escribir", action="store_true",
                            help="aplica los cambios; sin esto solo los lista")
    argumentos = analizador.parse_args()

    if not PLANTILLA.is_file():
        print(f"no esta {PLANTILLA}")
        return 3
    if argumentos.escribir:
        shutil.copy2(PLANTILLA, PLANTILLA.with_suffix(".qgz.bak"))

    hechos = consolidar(PLANTILLA, argumentos.escribir)
    verbo = "aplicado" if argumentos.escribir else "pendiente"
    for linea in hechos:
        print(f"  [{verbo}] {linea}")
    print(f"\n{len(hechos)} cambio(s).")
    if not argumentos.escribir:
        print("Nada se escribio. Repetir con --escribir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

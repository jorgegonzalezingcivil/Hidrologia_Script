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
CAPAS_SIN_RELLENO = ("Cuenca completa",)

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


def _conjunto_de(lay):
    """El LayerSet del marco principal de una plancha, o None."""
    for item in lay.findall("LayoutItem"):
        conjunto = item.find("LayerSet")
        if conjunto is not None:
            return conjunto
    return None


def corregir_capas_de_planchas(raiz, escribir: bool) -> list[str]:
    """Repone el juego de capas vacio, anade la cuenca y retira el area."""
    import copy as _copy

    hechos: list[str] = []
    nombres = {ml.findtext("id"): (ml.findtext("layername") or "")
               for ml in raiz.iter("maplayer")}
    id_cuenca = next((i for i, n in nombres.items() if n == CAPA_CUENCA), "")

    # Un juego de referencia por familia, para reponer los que quedaron vacios.
    referencia = {}
    for lay in raiz.iter("Layout"):
        nombre = lay.get("name") or ""
        conjunto = _conjunto_de(lay)
        if conjunto is not None and len(conjunto):
            familia = nombre.split(". ", 1)[-1].split(" Tr ")[0][:18]
            referencia.setdefault(familia, conjunto)

    for lay in raiz.iter("Layout"):
        nombre = lay.get("name") or ""
        if not nombre.startswith("Figura"):
            continue
        conjunto = _conjunto_de(lay)
        if conjunto is None:
            hechos.append(f"SIN MARCO DE MAPA: '{nombre}'")
            continue

        # 1. Juego vacio: se repone del de una plancha de la misma familia.
        if not len(conjunto):
            familia = nombre.split(". ", 1)[-1].split(" Tr ")[0][:18]
            modelo = referencia.get(familia)
            if modelo is None and "Zonificacion" in nombre:
                # Declarada por nombre: es la unica de su clase.
                por_nombre = {n: i for i, n in nombres.items()}
                if escribir:
                    for etiqueta in CAPAS_DE_ZONIFICACION:
                        ident = por_nombre.get(etiqueta)
                        if ident:
                            elemento = ET.SubElement(conjunto, "Layer")
                            elemento.text = ident
                puestas = sum(1 for e in CAPAS_DE_ZONIFICACION
                              if por_nombre.get(e))
                hechos.append(f"declarado el juego de capas de '{nombre}' "
                              f"({puestas} de {len(CAPAS_DE_ZONIFICACION)})")
                continue
            if modelo is None:
                hechos.append(f"JUEGO DE CAPAS VACIO y sin hermana de donde "
                              f"copiarlo: '{nombre}'")
                continue
            if escribir:
                for elemento in modelo:
                    conjunto.append(_copy.deepcopy(elemento))
            hechos.append(f"repuesto el juego de capas de '{nombre}' "
                          f"({len(modelo)} capas)")
            conjunto = _conjunto_de(lay)

        actuales = [e.text for e in conjunto]

        # 2. La cuenca acompana a toda plancha tematica.
        if id_cuenca and id_cuenca not in actuales:
            if escribir:
                elemento = _copy.deepcopy(conjunto[0])
                elemento.text = id_cuenca
                conjunto.append(elemento)
            hechos.append(f"anadida la cuenca a '{nombre}'")

        # 3. El area de influencia solo donde es el tema.
        if nombre not in PLANCHAS_CON_AREA:
            for elemento in list(conjunto):
                if nombres.get(elemento.text or "") == "Área de influencia":
                    if escribir:
                        conjunto.remove(elemento)
                    hechos.append(f"retirada el area de influencia de "
                                  f"'{nombre}'")
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

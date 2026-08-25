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

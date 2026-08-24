#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Adaptador de plantillas de Word
===============================
Entorno: venv del proyecto.

Por qué existe y por qué aquí. El M15 escribe sobre una plantilla con los
estilos del consultor, y esa plantilla se deriva de un informe suyo ya
entregado. Todo lo que manipula el empaquetado OOXML (el .docx es un ZIP de
archivos XML) vive aquí, aislado del módulo que redacta, porque es el punto que
más se resiente ante un cambio de versión de Word o de python-docx (CLAUDE.md,
sección 2).

Sigue la misma separación que 'graficos.py' y 'dss.py':

    src/comun/            solo librería estándar    ambos entornos
    src/docx_plantilla.py depende de python-docx     módulos de análisis

QUÉ SE CONSERVA Y QUÉ NO. De un informe de 34 MB se toman los estilos, la
numeración, los encabezados y pies, el tema y la configuración de página; se
descarta el contenido y las 197 imágenes, salvo las que los encabezados
necesitan. El resultado pesa unos cientos de kilobytes.

REPARA UNA RELACIÓN ROTA. El informe de referencia trae
'<Relationship Id="rId194" Type=".../hdphoto" Target="NULL"/>', que apunta a una
parte inexistente. Word la tolera; python-docx se detiene con un KeyError al
abrir el archivo. Se descarta toda relación interna cuyo destino no exista, de
modo que la plantilla abre aunque el original no lo hiciera.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any

__all__ = [
    "ErrorPlantilla",
    "extraer_plantilla",
    "abrir",
]

# Tipo de la parte principal. Un .dotx la declara como plantilla y un .docx como
# documento; Word distingue el comportamiento al abrir (la plantilla crea una
# copia en lugar de editarse a sí misma).
TIPO_DOCUMENTO = ("application/vnd.openxmlformats-officedocument"
                  ".wordprocessingml.document.main+xml")
TIPO_PLANTILLA = ("application/vnd.openxmlformats-officedocument"
                  ".wordprocessingml.template.main+xml")

_RELACION = re.compile(r"<Relationship\b[^>]*/>")
_ATRIBUTO = re.compile(r'(\w+)="([^"]*)"')


class ErrorPlantilla(RuntimeError):
    """Falla al derivar o abrir una plantilla, que el módulo debe reportar."""


def _atributos(relacion: str) -> dict[str, str]:
    return dict(_ATRIBUTO.findall(relacion))


def _destino_absoluto(base: str, destino: str) -> str:
    """Resuelve el Target de una relación contra el directorio de su parte."""
    if destino.startswith("/"):
        return destino.lstrip("/")
    partes = base.split("/")[:-1]
    for pieza in destino.split("/"):
        if pieza == "..":
            if partes:
                partes.pop()
        elif pieza not in ("", "."):
            partes.append(pieza)
    return "/".join(partes)


def _medios_de_encabezados(archivo: zipfile.ZipFile) -> set[str]:
    """
    Imágenes que los encabezados y pies necesitan, normalmente el logotipo.

    Se conservan porque son parte del formato, no del contenido: sin ellas la
    plantilla sale sin membrete y cada informe habría que retocarlo a mano.
    """
    conservar: set[str] = set()
    for nombre in archivo.namelist():
        marca = re.match(r"word/_rels/((?:header|footer)\d+\.xml)\.rels$", nombre)
        if not marca:
            continue
        parte = f"word/{marca.group(1)}"
        texto = archivo.read(nombre).decode("utf-8", errors="replace")
        for relacion in _RELACION.findall(texto):
            atributos = _atributos(relacion)
            if atributos.get("TargetMode") == "External":
                continue
            destino = _destino_absoluto(parte, atributos.get("Target", ""))
            if "/media/" in f"/{destino}":
                conservar.add(destino)
    return conservar


def _cuerpo_vacio(documento: str) -> str:
    """
    Deja el documento sin contenido pero con su configuración de página.

    EL 'sectPr' FINAL NO SE PUEDE PERDER: lleva el tamaño de página, los
    márgenes y las referencias a los encabezados. Un documento sin él sale en A4
    con los márgenes por defecto, y el informe de referencia es carta.
    """
    inicio = documento.find("<w:body>")
    fin = documento.rfind("</w:body>")
    if inicio < 0 or fin < 0:
        raise ErrorPlantilla(
            "el documento de origen no tiene cuerpo: no parece un .docx válido.")
    cuerpo = documento[inicio + len("<w:body>"):fin]
    seccion = cuerpo.rfind("<w:sectPr")
    conservado = cuerpo[seccion:] if seccion >= 0 else ""
    return documento[:inicio + len("<w:body>")] + conservado + "</w:body></w:document>"


def _relaciones_utiles(texto: str, parte: str, existentes: set[str],
                       quitar_imagenes: bool) -> tuple[str, list[str]]:
    """
    Filtra las relaciones que no se pueden resolver, y las de imagen si se pide.

    Devuelve el XML y la lista de identificadores descartados, para que el
    módulo que llama pueda reportarlos en lugar de descartarlos en silencio.
    """
    descartadas: list[str] = []

    def decidir(coincidencia: re.Match) -> str:
        relacion = coincidencia.group(0)
        atributos = _atributos(relacion)
        if atributos.get("TargetMode") == "External":
            return relacion
        destino = atributos.get("Target", "")
        if quitar_imagenes and "/image" in atributos.get("Type", ""):
            descartadas.append(atributos.get("Id", "?"))
            return ""
        if _destino_absoluto(parte, destino) not in existentes:
            descartadas.append(atributos.get("Id", "?"))
            return ""
        return relacion

    return _RELACION.sub(decidir, texto), descartadas


def extraer_plantilla(origen: Path, destino: Path) -> dict[str, Any]:
    """
    Deriva una plantilla de Word a partir de un informe ya entregado.

    Conserva estilos, numeración, encabezados, pies, tema y configuración de
    página; descarta el contenido y las imágenes que no sean de membrete.

    Excepciones
    -----------
    ErrorPlantilla
        Si el origen no está, no es un .docx legible o no tiene cuerpo.
    """
    origen, destino = Path(origen), Path(destino)
    if not origen.is_file():
        raise ErrorPlantilla(
            f"no se encuentra el informe de origen en {origen}. Es el que "
            "define el formato de casa (CLAUDE.md, seccion 10).")

    try:
        archivo = zipfile.ZipFile(origen)
    except (zipfile.BadZipFile, OSError) as error:
        raise ErrorPlantilla(
            f"{origen.name} no es un .docx legible: {error}") from error

    with archivo:
        nombres = set(archivo.namelist())
        conservar = _medios_de_encabezados(archivo)
        # Las partes que sobreviven, para poder resolver las relaciones contra
        # el paquete NUEVO y no contra el original.
        finales = {n for n in nombres
                   if not n.startswith("word/media/") or n in conservar}

        documento = _cuerpo_vacio(
            archivo.read("word/document.xml").decode("utf-8", errors="replace"))

        descartadas: dict[str, list[str]] = {}
        destino.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as salida:
            for info in archivo.infolist():
                nombre = info.filename
                if nombre not in finales:
                    continue
                if nombre == "word/document.xml":
                    salida.writestr(nombre, documento)
                    continue
                if nombre == "[Content_Types].xml":
                    salida.writestr(nombre, archivo.read(nombre).decode(
                        "utf-8", errors="replace").replace(
                            TIPO_DOCUMENTO, TIPO_PLANTILLA))
                    continue
                if nombre.endswith(".rels"):
                    parte = nombre.replace("_rels/", "").removesuffix(".rels")
                    texto, fuera = _relaciones_utiles(
                        archivo.read(nombre).decode("utf-8", errors="replace"),
                        parte, finales,
                        quitar_imagenes=nombre == "word/_rels/document.xml.rels")
                    if fuera:
                        descartadas[nombre] = fuera
                    salida.writestr(nombre, texto)
                    continue
                salida.writestr(info, archivo.read(nombre))

    return {
        "origen": str(origen),
        "destino": str(destino),
        "kb": round(destino.stat().st_size / 1024, 1),
        "medios_conservados": sorted(conservar),
        "relaciones_descartadas": descartadas,
    }


def sanear_paquete(origen: Path, destino: Path) -> dict[str, Any]:
    """
    Copia un .docx quitando las relaciones cuyo destino no existe en el paquete.

    SOLO PARA IMPORTAR UNA PLANTILLA NUEVA. Desde que el repositorio es la
    fuente de verdad de templates/informe_base.docx, ejecutar esto sobre la
    plantilla en uso BORRARIA las correcciones consolidadas en ella: los
    nombres de figura arreglados, los encabezados y la teoria escrita. Esos
    cambios viven en el historial de git y no en el archivo de origen.

    POR QUE HACE FALTA. Word tolera una relacion colgada y python-docx no: se
    detiene con "There is no item named 'word/NULL' in the archive" y no hay
    forma de abrir el documento. La plantilla de este consultor trae una,
    'rId29', de tipo hdphoto: es la capa de efecto de nitidez que Word 2007
    adjunta a una imagen, y su destino se perdio en alguna edicion.

    SE QUITA LA RELACION Y NO LA REFERENCIA. Es la unica de las dos cosas que se
    puede hacer, y esta comprobado abriendo el resultado en Word: quitar
    ademas el <a14:imgLayer> que la usa deja a su padre <a14:imgProps> SIN
    HIJOS, y esa extension de DrawingML exige uno. Word valida la extension y
    rechaza el archivo entero con un error de apertura que no dice nada del
    motivo. La referencia colgada dentro de document.xml, en cambio, no molesta
    ni a Word ni a python-docx.

    CADA ENTRADA CONSERVA SU ZipInfo. El paquete mezcla entradas comprimidas y
    almacenadas sin comprimir, dieciocho de estas ultimas en esta plantilla, y
    reescribirlo con el nombre a secas en lugar de con su ZipInfo pierde el
    metodo de compresion, la fecha y los atributos de todas.

    Devuelve el detalle de lo que se quito.

    Excepciones
    -----------
    ErrorPlantilla
        Si el origen no existe o no es un paquete legible.
    """
    origen, destino = Path(origen), Path(destino)
    if not origen.is_file():
        raise ErrorPlantilla(f"no se encuentra la plantilla en {origen}.")

    try:
        with zipfile.ZipFile(origen) as paquete:
            entradas = paquete.infolist()
            partes = {i.filename: paquete.read(i.filename) for i in entradas}
    except Exception as error:  # noqa: BLE001 - zipfile no tipa sus fallos
        raise ErrorPlantilla(
            f"no se pudo leer {origen.name}: {error}") from error

    presentes = set(partes)
    quitadas: list[dict[str, str]] = []
    for nombre in [n for n in partes if n.endswith(".rels")]:
        texto = partes[nombre].decode("utf-8", errors="replace")
        carpeta = nombre.rsplit("/_rels/", 1)[0] if "/_rels/" in nombre else ""

        def sobrevive(elemento: str) -> bool:
            destino_rel = re.search(r'Target="([^"]*)"', elemento)
            modo = re.search(r'TargetMode="([^"]*)"', elemento)
            if destino_rel is None or (modo and modo.group(1) == "External"):
                return True
            ruta = destino_rel.group(1)
            if ruta.startswith(("http://", "https://", "mailto:", "file:")):
                return True
            resuelta = f"{carpeta}/{ruta}" if carpeta else ruta
            while "/../" in resuelta:
                resuelta = re.sub(r"[^/]+/\.\./", "", resuelta, count=1)
            resuelta = resuelta.replace("/./", "/").lstrip("/")
            if resuelta in presentes:
                return True
            quitadas.append({"parte": nombre, "id": (
                re.search(r'Id="([^"]*)"', elemento) or
                re.search(r"(x)", "x")).group(1), "destino": ruta})
            return False

        nuevo = "".join(
            trozo for trozo in re.split(r"(<Relationship\b[^>]*/>)", texto)
            if not trozo.startswith("<Relationship") or sobrevive(trozo))
        if nuevo != texto:
            partes[nombre] = nuevo.encode("utf-8")

    destino.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destino, "w") as salida:
        for entrada in entradas:
            salida.writestr(entrada, partes[entrada.filename])

    return {"origen": str(origen), "destino": str(destino),
            "relaciones_quitadas": quitadas}


def abrir(ruta: Path):
    """
    Abre una plantilla y devuelve el documento de python-docx.

    PYTHON-DOCX NO ADMITE EL TIPO DE CONTENIDO DE PLANTILLA. Se detiene con
    'is not a Word file' ante un .dotx legítimo. La conversión se hace EN
    MEMORIA, de modo que en disco queda un .dotx correcto (Word lo abre creando
    una copia, que es lo que se quiere de una plantilla) y la librería recibe
    algo que sabe leer. Escribir un .docx disfrazado de .dotx resolvería lo
    mismo dejando un archivo que miente sobre lo que es.

    Excepciones
    -----------
    ErrorPlantilla
        Si no está o si el paquete no se puede leer. El mensaje incluye el de la
        librería, que ante una relación rota nombra la parte que falta.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorPlantilla(f"no se encuentra la plantilla en {ruta}.")
    try:
        from docx import Document
    except ImportError as error:  # pragma: no cover - depende del entorno
        raise ErrorPlantilla(
            "no esta instalado 'python-docx', que es lo que escribe el "
            "documento. Instalarlo en el venv del proyecto.") from error

    try:
        with zipfile.ZipFile(ruta) as original:
            tipos = original.read("[Content_Types].xml").decode(
                "utf-8", errors="replace")
            if TIPO_PLANTILLA not in tipos:
                return Document(str(ruta))
            memoria = io.BytesIO()
            with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as copia:
                for info in original.infolist():
                    if info.filename == "[Content_Types].xml":
                        copia.writestr(info.filename, tipos.replace(
                            TIPO_PLANTILLA, TIPO_DOCUMENTO))
                    else:
                        copia.writestr(info, original.read(info.filename))
            memoria.seek(0)
            return Document(memoria)
    except ErrorPlantilla:
        raise
    except Exception as error:  # noqa: BLE001 - la libreria no tipa sus fallos
        raise ErrorPlantilla(
            f"no se pudo abrir {ruta.name}: {error}") from error

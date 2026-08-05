# -*- coding: utf-8 -*-
"""
comun.shapefile
===============
Lectura de shapefiles con librería estándar.

Doctrina (CLAUDE.md, sección 2): los puntos frágiles ante actualizaciones
externas se aíslan en adaptadores. Este archivo es el adaptador del formato
shapefile para los módulos que corren en el venv, donde no hay GDAL ni la API de
QGIS disponibles.

Alcance deliberadamente limitado. El shapefile se compone de varios archivos y
los tres que interesan para verificar un insumo se pueden leer sin librería
alguna:

    .prj   texto plano con el WKT del sistema de referencia
    .shp   cabecera de 100 bytes con tipo de geometría y rectángulo envolvente
    .dbf   tabla dBase con los campos y sus valores
    .cpg   texto plano opcional con la codificación del .dbf

Lo que este adaptador NO hace: validar geometrías, reproyectar, leer rásteres ni
escribir nada. Cualquiera de esas necesidades corresponde a un módulo del
entorno de QGIS.

Referencia del formato: ESRI Shapefile Technical Description, julio de 1998.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errores import ErrorFormato

__all__ = [
    "CampoDBF",
    "InfoShapefile",
    "TIPOS_GEOMETRIA",
    "EXTENSIONES_OBLIGATORIAS",
    "leer_shapefile",
    "valores_unicos",
    "area_poligonos",
    "epsg_de_wkt",
]

# Código de tipo de geometría del .shp y su nombre en la especificación.
TIPOS_GEOMETRIA: dict[int, str] = {
    0: "Nulo",
    1: "Punto",
    3: "Polilínea",
    5: "Polígono",
    8: "Multipunto",
    11: "PuntoZ",
    13: "PolilíneaZ",
    15: "PolígonoZ",
    18: "MultipuntoZ",
    21: "PuntoM",
    23: "PolilíneaM",
    25: "PolígonoM",
    28: "MultipuntoM",
    31: "MultiParche",
}

# Tipos que contienen anillos y admiten cálculo de área.
_TIPOS_POLIGONO = (5, 15, 25)

# Componentes sin los cuales el shapefile no es utilizable. El .prj no es
# obligatorio para el formato, pero sí para este repositorio: CLAUDE.md,
# sección 5, exige escritura explícita del .prj.
EXTENSIONES_OBLIGATORIAS = (".shp", ".shx", ".dbf", ".prj")

_CODIGO_ARCHIVO_SHP = 9994

# Codificaciones habituales declaradas en el .cpg, normalizadas al nombre de
# códec de Python.
_ALIAS_CODIFICACION = {
    "utf-8": "utf-8", "utf8": "utf-8", "65001": "utf-8",
    "iso-8859-1": "latin-1", "iso8859-1": "latin-1", "latin1": "latin-1",
    "latin-1": "latin-1", "8859": "latin-1",
    "windows-1252": "cp1252", "cp1252": "cp1252", "1252": "cp1252",
    "ansi": "cp1252",
}


@dataclass(frozen=True)
class CampoDBF:
    """Un campo de la tabla de atributos."""

    nombre: str
    tipo: str      # C texto, N numérico, F flotante, D fecha, L lógico, M memo
    longitud: int
    decimales: int

    @property
    def descripcion(self) -> str:
        equivalencias = {
            "C": "texto", "N": "numérico", "F": "flotante",
            "D": "fecha", "L": "lógico", "M": "memo",
        }
        return equivalencias.get(self.tipo, self.tipo)


@dataclass(frozen=True)
class InfoShapefile:
    """Resumen de un shapefile, suficiente para verificar un insumo."""

    ruta: Path
    codigo_geometria: int
    tipo_geometria: str
    extension: tuple[float, float, float, float]
    n_registros: int
    campos: tuple[CampoDBF, ...]
    crs_wkt: str | None
    crs_epsg: str | None
    codificacion: str
    componentes_faltantes: tuple[str, ...]

    @property
    def nombres_campos(self) -> tuple[str, ...]:
        return tuple(campo.nombre for campo in self.campos)

    def tiene_campo(self, nombre: str) -> bool:
        """Compara sin distinguir mayúsculas: el .dbf las normaliza."""
        objetivo = nombre.strip().upper()
        return any(campo.nombre.upper() == objetivo for campo in self.campos)

    def campo(self, nombre: str) -> CampoDBF | None:
        objetivo = nombre.strip().upper()
        for campo in self.campos:
            if campo.nombre.upper() == objetivo:
                return campo
        return None


# =============================================================================
# Sistema de referencia
# =============================================================================
def epsg_de_wkt(wkt: str | None) -> str | None:
    """
    Extrae el código EPSG declarado en un WKT.

    Heurística deliberada: se toma la última autoridad EPSG del texto, que en
    WKT1 corresponde al CRS y no a sus componentes (datum, meridiano, unidad).
    Un WKT sin autoridad devuelve None, lo que no significa que el CRS sea
    incorrecto sino que no se puede confirmar por este medio. Por eso una
    discrepancia detectada aquí nunca debe ser bloqueante.
    """
    if not wkt:
        return None

    # WKT2: ID["EPSG",9377]
    coincidencias = re.findall(r'ID\s*\[\s*"EPSG"\s*,\s*(\d+)', wkt, re.IGNORECASE)
    if coincidencias:
        return f"EPSG:{coincidencias[-1]}"

    # WKT1: AUTHORITY["EPSG","9377"]
    coincidencias = re.findall(
        r'AUTHORITY\s*\[\s*"EPSG"\s*,\s*"?(\d+)"?', wkt, re.IGNORECASE
    )
    if coincidencias:
        return f"EPSG:{coincidencias[-1]}"

    return None


def _leer_prj(base: Path) -> str | None:
    destino = base.with_suffix(".prj")
    if not destino.is_file():
        return None
    for codec in ("utf-8", "latin-1"):
        try:
            return destino.read_text(encoding=codec).strip()
        except UnicodeDecodeError:
            continue
    return None


def _leer_cpg(base: Path) -> str | None:
    destino = base.with_suffix(".cpg")
    if not destino.is_file():
        return None
    try:
        declarada = destino.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    return _ALIAS_CODIFICACION.get(declarada.lower().replace(" ", ""))


def _decodificar(bruto: bytes, codificacion: str | None) -> str:
    """
    Decodifica un valor del .dbf.

    Sin .cpg no hay forma fiable de saber la codificación. Se intenta UTF-8 y se
    cae a cp1252, que es lo que producen la mayoría de las herramientas de
    escritorio en Windows.
    """
    if codificacion:
        return bruto.decode(codificacion, errors="replace")
    try:
        return bruto.decode("utf-8")
    except UnicodeDecodeError:
        return bruto.decode("cp1252", errors="replace")


# =============================================================================
# Cabecera del .shp
# =============================================================================
def _leer_cabecera_shp(ruta: Path) -> tuple[int, tuple[float, float, float, float]]:
    """Devuelve (codigo_geometria, extension) leyendo los 100 bytes iniciales."""
    with ruta.open("rb") as manejador:
        cabecera = manejador.read(100)

    if len(cabecera) < 100:
        raise ErrorFormato(
            f"{ruta.name} tiene {len(cabecera)} bytes; la cabecera de un "
            f"shapefile ocupa 100. El archivo está truncado."
        )

    codigo_archivo = struct.unpack(">i", cabecera[0:4])[0]
    if codigo_archivo != _CODIGO_ARCHIVO_SHP:
        raise ErrorFormato(
            f"{ruta.name} no es un shapefile: el código de archivo es "
            f"{codigo_archivo} y debe ser {_CODIGO_ARCHIVO_SHP}."
        )

    codigo_geometria = struct.unpack("<i", cabecera[32:36])[0]
    extension = struct.unpack("<4d", cabecera[36:68])
    return codigo_geometria, extension


# =============================================================================
# Tabla .dbf
# =============================================================================
def _leer_cabecera_dbf(ruta: Path) -> tuple[int, int, int, tuple[CampoDBF, ...]]:
    """Devuelve (n_registros, longitud_cabecera, longitud_registro, campos)."""
    codificacion = _leer_cpg(ruta)

    with ruta.open("rb") as manejador:
        cabecera = manejador.read(32)
        if len(cabecera) < 32:
            raise ErrorFormato(
                f"{ruta.name} está truncado: la cabecera dBase ocupa 32 bytes."
            )

        n_registros = struct.unpack("<I", cabecera[4:8])[0]
        longitud_cabecera = struct.unpack("<H", cabecera[8:10])[0]
        longitud_registro = struct.unpack("<H", cabecera[10:12])[0]

        campos: list[CampoDBF] = []
        while True:
            descriptor = manejador.read(32)
            if len(descriptor) < 32 or descriptor[0] in (0x0D, 0x00):
                break
            nombre = _decodificar(
                descriptor[0:11].split(b"\x00")[0], codificacion
            ).strip()
            if not nombre:
                break
            campos.append(CampoDBF(
                nombre=nombre,
                tipo=chr(descriptor[11]),
                longitud=descriptor[16],
                decimales=descriptor[17],
            ))

    if not campos:
        raise ErrorFormato(f"{ruta.name} no declara ningún campo.")

    esperada = sum(campo.longitud for campo in campos) + 1
    if longitud_registro != esperada:
        raise ErrorFormato(
            f"{ruta.name} declara registros de {longitud_registro} bytes pero "
            f"sus {len(campos)} campos suman {esperada}. La cabecera es "
            f"inconsistente y los valores no se pueden leer con seguridad."
        )

    return n_registros, longitud_cabecera, longitud_registro, tuple(campos)


def _iterar_valores(
    ruta_dbf: Path,
    campo: CampoDBF,
    campos: tuple[CampoDBF, ...],
    n_registros: int,
    longitud_cabecera: int,
    longitud_registro: int,
) -> Iterator[str]:
    """Recorre los valores de un campo, omitiendo los registros marcados."""
    desplazamiento = 1  # el primer byte del registro es la marca de borrado
    for candidato in campos:
        if candidato.nombre == campo.nombre:
            break
        desplazamiento += candidato.longitud

    codificacion = _leer_cpg(ruta_dbf)

    with ruta_dbf.open("rb") as manejador:
        manejador.seek(longitud_cabecera)
        for _ in range(n_registros):
            registro = manejador.read(longitud_registro)
            if len(registro) < longitud_registro:
                break
            if registro[0:1] == b"*":  # registro marcado como borrado
                continue
            bruto = registro[desplazamiento:desplazamiento + campo.longitud]
            yield _decodificar(bruto, codificacion).strip()


# =============================================================================
# Entrada pública
# =============================================================================
def leer_shapefile(ruta: str | Path) -> InfoShapefile:
    """
    Lee la cabecera de un shapefile y devuelve su resumen.

    Excepciones
    -----------
    ErrorFormato
        Si el .shp o el .dbf no existen, están truncados o son inconsistentes.
    """
    base = Path(ruta)
    if base.suffix.lower() != ".shp":
        base = base.with_suffix(".shp")

    if not base.is_file():
        raise ErrorFormato(f"No existe el archivo: {base}")

    faltantes = tuple(
        extension for extension in EXTENSIONES_OBLIGATORIAS
        if not base.with_suffix(extension).is_file()
    )

    ruta_dbf = base.with_suffix(".dbf")
    if not ruta_dbf.is_file():
        raise ErrorFormato(
            f"Falta la tabla de atributos {ruta_dbf.name}. Sin ella el insumo no "
            f"se puede homologar."
        )

    codigo_geometria, extension = _leer_cabecera_shp(base)
    n_registros, _, _, campos = _leer_cabecera_dbf(ruta_dbf)
    wkt = _leer_prj(base)

    return InfoShapefile(
        ruta=base,
        codigo_geometria=codigo_geometria,
        tipo_geometria=TIPOS_GEOMETRIA.get(
            codigo_geometria, f"desconocido ({codigo_geometria})"
        ),
        extension=extension,
        n_registros=n_registros,
        campos=campos,
        crs_wkt=wkt,
        crs_epsg=epsg_de_wkt(wkt),
        codificacion=_leer_cpg(ruta_dbf) or "no declarada",
        componentes_faltantes=faltantes,
    )


def valores_unicos(
    ruta: str | Path,
    nombre_campo: str,
    limite: int | None = None,
) -> list[str]:
    """
    Devuelve los valores distintos de un campo, ordenados.

    Los valores vacíos se conservan como cadena vacía: un registro sin valor en
    el campo clave es justamente lo que el consultor debe conocer antes de
    diligenciar la tabla de homologación.

    Excepciones
    -----------
    ErrorFormato
        Si el campo no existe en la tabla.
    """
    base = Path(ruta)
    if base.suffix.lower() != ".shp":
        base = base.with_suffix(".shp")
    ruta_dbf = base.with_suffix(".dbf")

    n_registros, longitud_cabecera, longitud_registro, campos = _leer_cabecera_dbf(
        ruta_dbf
    )

    objetivo = nombre_campo.strip().upper()
    campo = next((c for c in campos if c.nombre.upper() == objetivo), None)
    if campo is None:
        disponibles = ", ".join(c.nombre for c in campos)
        raise ErrorFormato(
            f"El campo {nombre_campo!r} no existe en {ruta_dbf.name}. "
            f"Disponibles: {disponibles}"
        )

    distintos: set[str] = set()
    for valor in _iterar_valores(
        ruta_dbf, campo, campos, n_registros, longitud_cabecera, longitud_registro
    ):
        distintos.add(valor)
        if limite is not None and len(distintos) > limite:
            break

    return sorted(distintos)


def area_poligonos(ruta: str | Path) -> float:
    """
    Suma el área de todos los polígonos, en las unidades del CRS de la capa.

    Se usa la fórmula del área de Gauss sobre cada anillo. En el formato
    shapefile los anillos exteriores van en sentido horario y los interiores en
    sentido antihorario, de modo que la suma con signo descuenta los huecos.

    El resultado solo es un área métrica si la capa está en un CRS proyectado.
    Sobre coordenadas geográficas devuelve grados cuadrados, que no significan
    nada: verificar el CRS antes de usar este valor.

    Excepciones
    -----------
    ErrorFormato
        Si la geometría del shapefile no es poligonal o el archivo está roto.
    """
    base = Path(ruta)
    if base.suffix.lower() != ".shp":
        base = base.with_suffix(".shp")

    codigo_geometria, _ = _leer_cabecera_shp(base)
    if codigo_geometria not in _TIPOS_POLIGONO:
        raise ErrorFormato(
            f"{base.name} contiene geometría "
            f"{TIPOS_GEOMETRIA.get(codigo_geometria, codigo_geometria)!r}; el "
            f"área solo se calcula sobre polígonos."
        )

    total = 0.0
    with base.open("rb") as manejador:
        manejador.seek(100)  # tras la cabecera del archivo
        while True:
            cabecera_registro = manejador.read(8)
            if len(cabecera_registro) < 8:
                break
            longitud_palabras = struct.unpack(">i", cabecera_registro[4:8])[0]
            contenido = manejador.read(longitud_palabras * 2)
            if len(contenido) < longitud_palabras * 2:
                raise ErrorFormato(
                    f"{base.name} está truncado dentro de un registro de geometría."
                )
            total += _area_registro(contenido)

    return abs(total)


def _area_registro(contenido: bytes) -> float:
    """Área con signo de un registro de tipo polígono."""
    tipo = struct.unpack("<i", contenido[0:4])[0]
    if tipo == 0:  # geometría nula
        return 0.0

    n_partes = struct.unpack("<i", contenido[36:40])[0]
    n_puntos = struct.unpack("<i", contenido[40:44])[0]

    inicio_partes = 44
    inicio_puntos = inicio_partes + 4 * n_partes

    partes = struct.unpack(
        f"<{n_partes}i", contenido[inicio_partes:inicio_puntos]
    )
    puntos = struct.unpack(
        f"<{2 * n_puntos}d",
        contenido[inicio_puntos:inicio_puntos + 16 * n_puntos],
    )

    total = 0.0
    limites = list(partes) + [n_puntos]
    for indice in range(n_partes):
        desde, hasta = limites[indice], limites[indice + 1]
        acumulado = 0.0
        for punto in range(desde, hasta - 1):
            x1, y1 = puntos[2 * punto], puntos[2 * punto + 1]
            x2, y2 = puntos[2 * punto + 2], puntos[2 * punto + 3]
            acumulado += x1 * y2 - x2 * y1
        total += acumulado / 2.0

    return total

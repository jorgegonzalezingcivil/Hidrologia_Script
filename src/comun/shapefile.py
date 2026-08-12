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

import datetime as _dt
import re
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .errores import ErrorFormato

__all__ = [
    "CampoDBF",
    "InfoShapefile",
    "TIPOS_GEOMETRIA",
    "EXTENSIONES_OBLIGATORIAS",
    "leer_shapefile",
    "leer_registros",
    "escribir_puntos",
    "leer_geometrias",
    "leer_puntos",
    "longitud_lineas",
    "perimetro_poligonos",
    "distancia_maxima",
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
# Geometría
# =============================================================================
# Códigos de tipo de geometría del formato .shp. Solo se leen los que el estudio
# usa; los demás se rechazan con su código en lugar de devolver algo vacío.
_TIPO_NULO = 0
_TIPO_PUNTO = 1
_TIPO_POLILINEA = 3
_TIPO_POLIGONO = 5
_TIPO_MULTIPUNTO = 8


def leer_geometrias(ruta: str | Path) -> list[list[list[tuple[float, float]]]]:
    """
    Lee la geometría de un shapefile de líneas o polígonos.

    Devuelve una lista por entidad, cada una con sus PARTES, y cada parte con
    sus vértices. Un polígono con isla tiene dos partes; una polilínea partida,
    también. Respetar esa estructura importa: unir las partes produciría un
    perímetro y una longitud equivocados.

    Existe porque los módulos de análisis corren en el venv, donde no hay GDAL,
    y aun así deben calcular perímetros, longitudes de cauce y densidad de
    drenaje. Leer el formato binario es preferible a arrastrar una dependencia
    geoespacial al entorno que comparte el Python de QGIS.

    Se ignora la dimensión Z o M si el archivo la trae: el estudio trabaja en
    dos dimensiones y la tercera vendría del DEM, no de la capa vectorial.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no existe.
    ErrorFormato
        Si el tipo de geometría no es de líneas ni de polígonos, o si un
        registro está truncado.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorRutas(f"No existe el shapefile {ruta}.")

    codigo, _ = _leer_cabecera_shp(ruta)
    admitidos = (_TIPO_POLILINEA, _TIPO_POLIGONO,
                 _TIPO_POLILINEA + 10, _TIPO_POLIGONO + 10,   # con Z
                 _TIPO_POLILINEA + 20, _TIPO_POLIGONO + 20)   # con M
    if codigo not in admitidos:
        raise ErrorFormato(
            f"{ruta.name} tiene geometría de tipo {codigo}, y esta función lee "
            f"líneas ({_TIPO_POLILINEA}) o polígonos ({_TIPO_POLIGONO})."
        )

    entidades: list[list[list[tuple[float, float]]]] = []
    with ruta.open("rb") as manejador:
        manejador.seek(100)
        while True:
            cabecera = manejador.read(8)
            if len(cabecera) < 8:
                break
            _, longitud_palabras = struct.unpack(">2i", cabecera)
            cuerpo = manejador.read(longitud_palabras * 2)
            if len(cuerpo) < longitud_palabras * 2:
                raise ErrorFormato(
                    f"{ruta.name}: un registro de geometría está truncado."
                )
            tipo = struct.unpack("<i", cuerpo[0:4])[0]
            if tipo == _TIPO_NULO:
                entidades.append([])
                continue
            # Tras el tipo van la caja envolvente (4 dobles), el número de
            # partes, el de puntos, y el índice de inicio de cada parte.
            n_partes, n_puntos = struct.unpack("<2i", cuerpo[36:44])
            inicio_indices = 44
            indices = struct.unpack(
                f"<{n_partes}i",
                cuerpo[inicio_indices:inicio_indices + 4 * n_partes])
            inicio_puntos = inicio_indices + 4 * n_partes
            crudos = struct.unpack(
                f"<{2 * n_puntos}d",
                cuerpo[inicio_puntos:inicio_puntos + 16 * n_puntos])
            puntos = [(crudos[i], crudos[i + 1])
                      for i in range(0, len(crudos), 2)]

            partes: list[list[tuple[float, float]]] = []
            for orden, arranque in enumerate(indices):
                fin = indices[orden + 1] if orden + 1 < n_partes else n_puntos
                partes.append(puntos[arranque:fin])
            entidades.append(partes)
    return entidades


def leer_puntos(ruta: str | Path) -> list[tuple[float, float]]:
    """
    Lee la geometría de un shapefile de PUNTOS.

    Existe aparte de 'leer_geometrias' porque un punto no tiene partes ni
    vértices, y devolverlo con la misma estructura anidada obligaría a todos
    los consumidores a desenvolverla. El punto de descarga es el caso: una
    coordenada suelta que las figuras y los módulos necesitan tal cual.

    Se ignora la dimensión Z o M si el archivo la trae.

    Excepciones
    -----------
    ErrorRutas
        No existe el archivo.
    ErrorFormato
        La geometría no es de puntos, o el archivo está truncado.
    """
    destino = Path(ruta)
    if not destino.is_file():
        raise ErrorRutas(f"No existe el shapefile {destino}.")

    codigo, _ = _leer_cabecera_shp(destino)
    admitidos = (_TIPO_PUNTO, _TIPO_PUNTO + 10, _TIPO_PUNTO + 20)
    if codigo not in admitidos:
        raise ErrorFormato(
            f"{destino.name} tiene geometría de tipo {codigo}, y esta función "
            f"lee puntos ({_TIPO_PUNTO}). Para líneas y polígonos, "
            "leer_geometrias."
        )

    puntos: list[tuple[float, float]] = []
    with destino.open("rb") as manejador:
        manejador.seek(100)
        while True:
            cabecera = manejador.read(8)
            if len(cabecera) < 8:
                break
            _, longitud_palabras = struct.unpack(">2i", cabecera)
            contenido = manejador.read(longitud_palabras * 2)
            if len(contenido) < longitud_palabras * 2:
                raise ErrorFormato(
                    f"{destino.name}: registro truncado al leer puntos.")
            if len(contenido) < 20:
                continue
            tipo = struct.unpack("<i", contenido[:4])[0]
            if tipo == 0:          # geometría nula, legal en el formato
                continue
            x, y = struct.unpack("<2d", contenido[4:20])
            puntos.append((x, y))
    return puntos


def longitud_lineas(ruta: str | Path) -> float:
    """
    Longitud total de una capa de líneas, en las unidades de su CRS.

    Suma parte por parte. Unir las partes de una polilínea partida añadiría un
    segmento ficticio entre el final de una y el principio de la siguiente, que
    puede medir kilómetros.
    """
    total = 0.0
    for entidad in leer_geometrias(ruta):
        for parte in entidad:
            for uno, otro in zip(parte, parte[1:]):
                total += math.hypot(otro[0] - uno[0], otro[1] - uno[1])
    return total


def perimetro_poligonos(ruta: str | Path) -> float:
    """
    Perímetro total de una capa de polígonos, en las unidades de su CRS.

    Incluye los anillos interiores: el borde de una isla es perímetro de la
    entidad, y omitirlo subestimaría el coeficiente de compacidad.
    """
    total = 0.0
    for entidad in leer_geometrias(ruta):
        for anillo in entidad:
            cerrado = list(anillo)
            if cerrado and cerrado[0] != cerrado[-1]:
                cerrado.append(cerrado[0])
            for uno, otro in zip(cerrado, cerrado[1:]):
                total += math.hypot(otro[0] - uno[0], otro[1] - uno[1])
    return total


def _envolvente_convexa(
    puntos: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """
    Envolvente convexa por la cadena monotona de Andrew.

    La distancia maxima entre dos puntos de un conjunto se alcanza SIEMPRE entre
    dos vertices de su envolvente convexa. Calcularla primero reduce el problema
    de decenas de miles de vertices a unas decenas, y con ello el coste de
    cuadratico a practicamente lineal: sobre la cuenca de este estudio, de ocho
    minutos a menos de un segundo.
    """
    unicos = sorted(set(puntos))
    if len(unicos) < 3:
        return unicos

    def giro(o, a, b) -> float:
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    inferior: list[tuple[float, float]] = []
    for punto in unicos:
        while len(inferior) >= 2 and giro(inferior[-2], inferior[-1], punto) <= 0:
            inferior.pop()
        inferior.append(punto)
    superior: list[tuple[float, float]] = []
    for punto in reversed(unicos):
        while len(superior) >= 2 and giro(superior[-2], superior[-1], punto) <= 0:
            superior.pop()
        superior.append(punto)
    return inferior[:-1] + superior[:-1]


def distancia_maxima(ruta: str | Path, muestreo: int = 1) -> float:
    """
    Mayor distancia entre dos vertices de la capa: la longitud axial.

    Es la que alimenta el coeficiente de forma y varias formulas de tiempo de
    concentracion.

    Se calcula sobre la envolvente convexa y no sobre todos los vertices. El
    resultado es EXACTAMENTE el mismo, porque el par mas distante siempre cae
    sobre ella, y el coste deja de ser cuadratico sobre el total de vertices.

    'muestreo' se conserva por compatibilidad, pero ya no hace falta: con la
    envolvente el calculo es rapido sobre la capa completa, y saltarse vertices
    solo introduciria error.
    """
    puntos: list[tuple[float, float]] = []
    for entidad in leer_geometrias(ruta):
        for parte in entidad:
            puntos.extend(parte[::max(1, muestreo)])
    if len(puntos) < 2:
        return 0.0
    candidatos = _envolvente_convexa(puntos) or puntos
    mayor = 0.0
    for indice, uno in enumerate(candidatos):
        for otro in candidatos[indice + 1:]:
            distancia = math.hypot(otro[0] - uno[0], otro[1] - uno[1])
            if distancia > mayor:
                mayor = distancia
    return mayor



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


def leer_registros(
    ruta: str | Path,
    campos: Sequence[str] | None = None,
) -> Iterator[dict[str, str]]:
    """
    Recorre la tabla de atributos devolviendo un diccionario por registro.

    Los valores se entregan como texto sin espacios sobrantes, tal como están en
    el .dbf. La conversión a número o a fecha corresponde al módulo que los
    consume, que es quien sabe qué significa cada campo y qué hacer con un valor
    vacío.

    Con `campos` se limita la lectura a los indicados, sin distinguir mayúsculas.

    Excepciones
    -----------
    ErrorFormato
        Si el .dbf no existe, está truncado o algún campo pedido no existe.
    """
    base = Path(ruta)
    if base.suffix.lower() != ".shp":
        base = base.with_suffix(".shp")
    ruta_dbf = base.with_suffix(".dbf")

    if not ruta_dbf.is_file():
        raise ErrorFormato(f"No existe la tabla de atributos {ruta_dbf}")

    n_registros, longitud_cabecera, longitud_registro, todos = _leer_cabecera_dbf(
        ruta_dbf
    )

    if campos is None:
        elegidos = list(todos)
    else:
        indice = {campo.nombre.upper(): campo for campo in todos}
        elegidos = []
        faltantes = []
        for nombre in campos:
            campo = indice.get(nombre.strip().upper())
            if campo is None:
                faltantes.append(nombre)
            else:
                elegidos.append(campo)
        if faltantes:
            raise ErrorFormato(
                f"{ruta_dbf.name} no tiene el/los campo(s) "
                f"{', '.join(repr(f) for f in faltantes)}. Disponibles: "
                f"{', '.join(c.nombre for c in todos)}."
            )

    # Desplazamiento de cada campo dentro del registro, calculado una sola vez.
    desplazamientos: dict[str, tuple[int, int]] = {}
    posicion = 1  # el primer byte es la marca de borrado
    for campo in todos:
        desplazamientos[campo.nombre] = (posicion, campo.longitud)
        posicion += campo.longitud

    codificacion = _leer_cpg(ruta_dbf)

    with ruta_dbf.open("rb") as manejador:
        manejador.seek(longitud_cabecera)
        for _ in range(n_registros):
            registro = manejador.read(longitud_registro)
            if len(registro) < longitud_registro:
                break
            if registro[0:1] == b"*":  # marcado como borrado
                continue
            fila = {}
            for campo in elegidos:
                inicio, ancho = desplazamientos[campo.nombre]
                bruto = registro[inicio:inicio + ancho]
                fila[campo.nombre] = _decodificar(bruto, codificacion).strip()
            yield fila


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

    return abs(sum(_areas_con_signo(base)))


def areas_poligonos(ruta: str | Path) -> list[float]:
    """
    Área de CADA polígono de la capa, en el orden de los registros.

    Existe porque el área por entidad no se puede dar por leída de un atributo.
    La exportación de HEC-HMS trae los parámetros que el programa calculó
    (`long_len`, `basin_slo`, `drain_den`) y NO trae el área: buscarla entre los
    campos devuelve una lista vacía y el módulo que la use concluiría que no hay
    ninguna subcuenca diminuta. Medido sobre una exportación real de 125
    subcuencas: por atributo, ninguna por debajo de 0,5 km²; por geometría, 24,
    la menor de 0,006 km².

    El signo se descarta por entidad, no en la suma total: así un anillo interior
    resta a su propio polígono y no al del vecino.

    Excepciones
    -----------
    ErrorFormato
        Si la geometría del shapefile no es poligonal o el archivo está roto.
    """
    base = Path(ruta)
    if base.suffix.lower() != ".shp":
        base = base.with_suffix(".shp")
    return [abs(area) for area in _areas_con_signo(base)]


def _areas_con_signo(base: Path) -> list[float]:
    """Área con signo de cada registro poligonal del .shp."""
    codigo_geometria, _ = _leer_cabecera_shp(base)
    if codigo_geometria not in _TIPOS_POLIGONO:
        raise ErrorFormato(
            f"{base.name} contiene geometría "
            f"{TIPOS_GEOMETRIA.get(codigo_geometria, codigo_geometria)!r}; el "
            f"área solo se calcula sobre polígonos."
        )

    areas: list[float] = []
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
            areas.append(_area_registro(contenido))
    return areas


# =============================================================================
# Escritura de capas de puntos
# =============================================================================
_TIPO_PUNTO = 1


def escribir_puntos(
    destino: str | Path,
    puntos: Sequence[tuple[float, float]],
    campos: Sequence,
    valores: Sequence[dict],
    wkt_crs: str,
    codificacion: str = "UTF-8",
) -> Path:
    """
    Escribe una capa de puntos completa: .shp, .shx, .dbf, .prj y .cpg.

    Existe porque los módulos de análisis corren en el venv, donde no hay GDAL
    ni la API de QGIS, y aun así deben entregar sus productos en el formato que
    fija CLAUDE.md, sección 5. La alternativa sería duplicar la pila SIG en el
    segundo entorno o mover módulos de análisis al de QGIS.

    `campos` son objetos comun.campos.CampoSalida, cuyos nombres cortos se
    validan antes de escribir: el formato dBase trunca a 10 caracteres en
    silencio.

    Excepciones
    -----------
    ErrorFormato
        Si las listas no tienen la misma longitud, si los campos no son
        escribibles o si un tipo declarado no está soportado.
    """
    from .campos import validar_campos

    base = Path(destino)
    if base.suffix.lower() == ".shp":
        base = base.with_suffix("")

    if len(puntos) != len(valores):
        raise ErrorFormato(
            f"Se recibieron {len(puntos)} punto(s) y {len(valores)} juego(s) de "
            "atributos."
        )
    if not campos:
        raise ErrorFormato("Una capa debe declarar al menos un campo.")

    validar_campos(campos)
    base.parent.mkdir(parents=True, exist_ok=True)

    _escribir_shp_shx(base, puntos)
    _escribir_dbf(base.with_suffix(".dbf"), campos, valores, codificacion)
    base.with_suffix(".prj").write_text(wkt_crs, encoding="utf-8")
    base.with_suffix(".cpg").write_text(codificacion, encoding="ascii")

    return base.with_suffix(".shp")


def _cabecera_shapefile(longitud_palabras: int,
                        extension: tuple[float, float, float, float]) -> bytes:
    """Construye los 100 bytes de cabecera comunes al .shp y al .shx."""
    x_min, y_min, x_max, y_max = extension
    cabecera = struct.pack(">i", _CODIGO_ARCHIVO_SHP) + b"\x00" * 20
    cabecera += struct.pack(">i", longitud_palabras)
    cabecera += struct.pack("<i", 1000)
    cabecera += struct.pack("<i", _TIPO_PUNTO)
    cabecera += struct.pack("<4d", x_min, y_min, x_max, y_max)
    cabecera += struct.pack("<4d", 0.0, 0.0, 0.0, 0.0)
    return cabecera


def _escribir_shp_shx(base: Path, puntos: Sequence[tuple[float, float]]) -> None:
    """Escribe la geometría y su índice."""
    if puntos:
        xs = [p[0] for p in puntos]
        ys = [p[1] for p in puntos]
        extension = (min(xs), min(ys), max(xs), max(ys))
    else:
        extension = (0.0, 0.0, 0.0, 0.0)

    # Un registro de punto ocupa 20 bytes de contenido, es decir 10 palabras de
    # 16 bits, más 8 bytes de cabecera de registro.
    contenido_palabras = 10
    registros = b""
    indice = b""
    desplazamiento = 50  # la cabecera del archivo ocupa 50 palabras

    for numero, (x, y) in enumerate(puntos, start=1):
        registros += struct.pack(">i", numero)
        registros += struct.pack(">i", contenido_palabras)
        registros += struct.pack("<i", _TIPO_PUNTO)
        registros += struct.pack("<2d", float(x), float(y))

        indice += struct.pack(">i", desplazamiento)
        indice += struct.pack(">i", contenido_palabras)
        desplazamiento += contenido_palabras + 4

    base.with_suffix(".shp").write_bytes(
        _cabecera_shapefile((100 + len(registros)) // 2, extension) + registros
    )
    base.with_suffix(".shx").write_bytes(
        _cabecera_shapefile((100 + len(indice)) // 2, extension) + indice
    )


def _valor_dbf(campo, valor, codificacion: str) -> bytes:
    """
    Convierte un valor al formato de ancho fijo del .dbf.

    El texto se alinea a la izquierda y los números a la derecha, como manda el
    formato dBase. Un valor que no cabe se trunca, y en el caso numérico eso
    cambiaría la cifra, de modo que se rellena con asteriscos, que es la
    convención del formato para el desbordamiento.
    """
    ancho = campo.longitud

    if campo.tipo == "texto":
        bruto = ("" if valor is None else str(valor)).encode(
            codificacion, errors="replace"
        )
        return bruto[:ancho].ljust(ancho, b" ")

    if campo.tipo == "fecha":
        texto = "" if valor is None else str(valor).replace("-", "")[:8]
        return texto.encode("ascii", errors="replace").ljust(ancho, b" ")

    if valor is None or valor == "":
        return b" " * ancho

    if campo.tipo == "entero":
        texto = f"{int(valor):d}"
    else:
        texto = f"{float(valor):.{campo.precision}f}"

    bruto = texto.encode("ascii", errors="replace")
    if len(bruto) > ancho:
        return b"*" * ancho
    return bruto.rjust(ancho, b" ")


def _escribir_dbf(destino: Path, campos: Sequence, valores: Sequence[dict],
                  codificacion: str) -> None:
    """Escribe la tabla de atributos en formato dBase III."""
    equivalencias = {"texto": b"C", "entero": b"N", "decimal": b"N", "fecha": b"D"}

    descriptores = b""
    for campo in campos:
        marca = equivalencias.get(campo.tipo)
        if marca is None:
            raise ErrorFormato(
                f"El campo {campo.corto!r} declara el tipo {campo.tipo!r}, que no "
                "se puede escribir en un .dbf."
            )
        ancho = campo.longitud if campo.longitud > 0 else 18
        descriptores += campo.corto.encode("ascii")[:10].ljust(11, b"\x00")
        descriptores += marca
        descriptores += b"\x00" * 4
        descriptores += bytes([min(ancho, 254), campo.precision])
        descriptores += b"\x00" * 14

    longitud_cabecera = 32 + 32 * len(campos) + 1
    longitud_registro = 1 + sum(
        min(c.longitud if c.longitud > 0 else 18, 254) for c in campos
    )

    hoy = _dt.date.today()
    cabecera = bytes([0x03, hoy.year - 1900, hoy.month, hoy.day])
    cabecera += struct.pack("<I", len(valores))
    cabecera += struct.pack("<H", longitud_cabecera)
    cabecera += struct.pack("<H", longitud_registro)
    cabecera += b"\x00" * 20

    cuerpo = b""
    for fila in valores:
        cuerpo += b" "  # marca de registro vigente
        for campo in campos:
            ancho = min(campo.longitud if campo.longitud > 0 else 18, 254)
            copia = type(campo)(
                corto=campo.corto, descriptivo=campo.descriptivo,
                tipo=campo.tipo, longitud=ancho, precision=campo.precision,
                unidad=campo.unidad,
            )
            cuerpo += _valor_dbf(copia, fila.get(campo.corto), codificacion)

    destino.write_bytes(cabecera + descriptores + b"\x0d" + cuerpo + b"\x1a")


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

# -*- coding: utf-8 -*-
"""
Adaptador de lectura de GeoTIFF, sin GDAL.

Existe por la misma razón que 'comun/shapefile.py': los módulos de análisis
corren en el venv del proyecto, que no tiene GDAL, y aun así deben leer el
terreno para calcular cotas, curva hipsométrica y pendiente. Arrastrar una
dependencia geoespacial al venv rompería el esquema de doble entorno de
CLAUDE.md, sección 3, y trasladar el M10 al Python de QGIS lo rompería igual,
porque la sección 8 lo declara módulo del venv.

Alcance deliberadamente acotado. Se lee lo que el estudio produce y consume:

    TIFF clásico y BigTIFF, orden de bytes cualquiera
    organización por franjas y por teselas
    sin comprimir, Deflate y LZW
    predictor horizontal (2) en enteros
    una muestra por píxel

Los dos rásteres del estudio caen en extremos opuestos de ese alcance, lo que
es precisamente el motivo de cubrirlo entero de una vez:

    DEM ALOS PALSAR recortado   TIFF clásico, sin comprimir, franjas de una
                                fila, flotante de 32 bits
    HYSOGs250m                  BigTIFF, LZW, teselas de 128 x 128, entero de
                                8 bits

Lo que este adaptador NO hace: reproyectar, remuestrear ni interpretar
máscaras alfa. La reproyección es explícita y corresponde al entorno SIG.

El lector entrega BYTES crudos por fila, no listas de números. Así quien
consume decide: 'array.array' si le basta la biblioteca estándar, o
'numpy.frombuffer' sin copia si necesita aritmética sobre millones de celdas.
Devolver una lista de flotantes de Python obligaría a materializar cien
millones de objetos para un DEM de este tamaño.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errores import ErrorFormato, ErrorRutas

# Etiquetas TIFF que el adaptador entiende.
ANCHO = 256
ALTO = 257
BITS_POR_MUESTRA = 258
COMPRESION = 259
DESPLAZAMIENTOS_FRANJA = 273
MUESTRAS_POR_PIXEL = 277
FILAS_POR_FRANJA = 278
BYTES_POR_FRANJA = 279
CONFIGURACION_PLANAR = 284
PREDICTOR = 317
ANCHO_TESELA = 322
ALTO_TESELA = 323
DESPLAZAMIENTOS_TESELA = 324
BYTES_POR_TESELA = 325
FORMATO_MUESTRA = 339
ESCALA_PIXEL = 33550
PUNTO_DE_AMARRE = 33922
DIRECTORIO_GEOCLAVES = 34735
GEOCLAVES_ASCII = 34737
NODATO = 42113

SIN_COMPRESION = 1
LZW = 5
DEFLATE = 8
DEFLATE_ANTIGUO = 32946

ENTERO_SIN_SIGNO = 1
ENTERO_CON_SIGNO = 2
FLOTANTE = 3

# Geoclaves que declaran el sistema de referencia.
CLAVE_PROYECTADO = 3072
CLAVE_GEOGRAFICO = 2048

# tipo TIFF -> (código de struct, bytes)
_TIPOS: dict[int, tuple[str, int]] = {
    1: ("B", 1), 2: ("c", 1), 3: ("H", 2), 4: ("I", 4), 6: ("b", 1),
    7: ("B", 1), 8: ("h", 2), 9: ("i", 4), 11: ("f", 4), 12: ("d", 8),
    16: ("Q", 8), 17: ("q", 8),
}

# (formato de muestra, bits) -> descriptor de tipo, sin el orden de bytes
_DESCRIPTORES: dict[tuple[int, int], str] = {
    (ENTERO_SIN_SIGNO, 8): "u1", (ENTERO_SIN_SIGNO, 16): "u2",
    (ENTERO_SIN_SIGNO, 32): "u4",
    (ENTERO_CON_SIGNO, 8): "i1", (ENTERO_CON_SIGNO, 16): "i2",
    (ENTERO_CON_SIGNO, 32): "i4",
    (FLOTANTE, 32): "f4", (FLOTANTE, 64): "f8",
}


@dataclass(frozen=True)
class InfoRaster:
    """Todo lo que hace falta para situar y leer un ráster."""

    ruta: Path
    ancho: int
    alto: int
    bits_por_muestra: int
    formato_muestra: int
    muestras_por_pixel: int
    compresion: int
    predictor: int
    nodato: float | None
    origen_x: float
    origen_y: float
    tamano_x: float
    tamano_y: float
    crs_epsg: str | None
    es_bigtiff: bool
    ancho_tesela: int
    alto_tesela: int
    filas_por_franja: int

    @property
    def teselado(self) -> bool:
        return self.ancho_tesela > 0 and self.alto_tesela > 0

    @property
    def bytes_por_muestra(self) -> int:
        return self.bits_por_muestra // 8

    @property
    def bytes_por_fila(self) -> int:
        return self.ancho * self.bytes_por_muestra * self.muestras_por_pixel

    @property
    def descriptor(self) -> str:
        """Descriptor de tipo con el orden de bytes ya resuelto ('<f4')."""
        clave = (self.formato_muestra, self.bits_por_muestra)
        if clave not in _DESCRIPTORES:
            raise ErrorFormato(
                f"{self.ruta.name}: combinación de formato de muestra "
                f"{self.formato_muestra} y {self.bits_por_muestra} bits no "
                "contemplada por el adaptador."
            )
        # Se normaliza a orden nativo al decodificar, de modo que el
        # descriptor que se entrega siempre es little endian.
        return "<" + _DESCRIPTORES[clave]

    @property
    def extension(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) en las unidades del CRS del ráster."""
        return (
            self.origen_x,
            self.origen_y - self.alto * self.tamano_y,
            self.origen_x + self.ancho * self.tamano_x,
            self.origen_y,
        )

    @property
    def area_celda_m2(self) -> float:
        return self.tamano_x * self.tamano_y

    def columna_de(self, x: float) -> int:
        """Columna que contiene la abscisa dada. Puede caer fuera del ráster."""
        return int((x - self.origen_x) // self.tamano_x)

    def fila_de(self, y: float) -> int:
        """Fila que contiene la ordenada dada. Puede caer fuera del ráster."""
        return int((self.origen_y - y) // self.tamano_y)

    def x_de_columna(self, columna: int) -> float:
        """Abscisa del CENTRO de la columna."""
        return self.origen_x + (columna + 0.5) * self.tamano_x

    def y_de_fila(self, fila: int) -> float:
        """Ordenada del CENTRO de la fila."""
        return self.origen_y - (fila + 0.5) * self.tamano_y

    def contiene(self, xmin: float, ymin: float,
                 xmax: float, ymax: float) -> bool:
        """Indica si la extensión dada cabe entera dentro del ráster."""
        rxmin, rymin, rxmax, rymax = self.extension
        return (xmin >= rxmin and ymin >= rymin
                and xmax <= rxmax and ymax <= rymax)


# =============================================================================
# Lectura de la cabecera
# =============================================================================
def _leer_entradas(f: Any, orden: str, es_bigtiff: bool,
                   desplazamiento: int) -> dict[int, tuple[int, int, bytes]]:
    """
    Lee un directorio de imagen y devuelve, por etiqueta, (tipo, cuenta, datos).

    Los datos vienen ya resueltos: si no caben en la entrada, se sigue el
    puntero y se leen del archivo.
    """
    f.seek(desplazamiento)
    if es_bigtiff:
        cuantas = struct.unpack(orden + "Q", f.read(8))[0]
        tam_entrada, fmt_cuenta, tam_valor = 20, "Q", 8
    else:
        cuantas = struct.unpack(orden + "H", f.read(2))[0]
        tam_entrada, fmt_cuenta, tam_valor = 12, "I", 4

    crudas = f.read(tam_entrada * cuantas)
    if len(crudas) < tam_entrada * cuantas:
        raise ErrorFormato("directorio de imagen truncado.")

    pendientes: list[tuple[int, int, int, int]] = []
    entradas: dict[int, tuple[int, int, bytes]] = {}

    for i in range(cuantas):
        bloque = crudas[i * tam_entrada:(i + 1) * tam_entrada]
        etiqueta, tipo = struct.unpack(orden + "HH", bloque[:4])
        cuenta = struct.unpack(orden + fmt_cuenta, bloque[4:4 + tam_valor])[0]
        valor = bloque[4 + tam_valor:]
        if tipo not in _TIPOS:
            continue
        total = cuenta * _TIPOS[tipo][1]
        if total <= tam_valor:
            entradas[etiqueta] = (tipo, cuenta, valor[:total])
        else:
            puntero = struct.unpack(
                orden + ("Q" if es_bigtiff else "I"), valor[:tam_valor])[0]
            pendientes.append((etiqueta, tipo, cuenta, puntero))

    for etiqueta, tipo, cuenta, puntero in pendientes:
        f.seek(puntero)
        datos = f.read(cuenta * _TIPOS[tipo][1])
        entradas[etiqueta] = (tipo, cuenta, datos)
    return entradas


def _valores(entradas: dict[int, tuple[int, int, bytes]], orden: str,
             etiqueta: int) -> tuple:
    """Desempaqueta una entrada como tupla de números."""
    if etiqueta not in entradas:
        return ()
    tipo, cuenta, datos = entradas[etiqueta]
    if tipo == 2:
        return (datos.rstrip(b"\x00").decode("latin-1"),)
    codigo, tam = _TIPOS[tipo]
    cuantos = min(cuenta, len(datos) // tam)
    return struct.unpack(orden + codigo * cuantos, datos[:cuantos * tam])


def _entero(entradas, orden, etiqueta, defecto: int) -> int:
    valores = _valores(entradas, orden, etiqueta)
    return int(valores[0]) if valores else defecto


def _epsg_de_geoclaves(entradas, orden) -> str | None:
    """Lee el código EPSG del directorio de geoclaves."""
    claves = _valores(entradas, orden, DIRECTORIO_GEOCLAVES)
    if len(claves) < 8:
        return None
    cuantas = claves[3]
    for i in range(int(cuantas)):
        base = 4 + i * 4
        if base + 3 >= len(claves):
            break
        identificador, ubicacion, _cuenta, valor = claves[base:base + 4]
        if identificador in (CLAVE_PROYECTADO, CLAVE_GEOGRAFICO) and ubicacion == 0:
            if 1024 <= valor < 32767:
                return f"EPSG:{valor}"
    return None


def _cabecera(f: Any, ruta: Path) -> tuple[InfoRaster, tuple, tuple]:
    """Devuelve la información del ráster y los índices de sus bloques."""
    firma = f.read(2)
    if firma == b"II":
        orden = "<"
    elif firma == b"MM":
        orden = ">"
    else:
        raise ErrorFormato(
            f"{ruta.name}: no es un TIFF (firma {firma!r}).")

    version = struct.unpack(orden + "H", f.read(2))[0]
    if version == 42:
        es_bigtiff = False
        desplazamiento = struct.unpack(orden + "I", f.read(4))[0]
    elif version == 43:
        es_bigtiff = True
        tam_puntero = struct.unpack(orden + "H", f.read(2))[0]
        f.read(2)
        if tam_puntero != 8:
            raise ErrorFormato(
                f"{ruta.name}: BigTIFF con punteros de {tam_puntero} bytes.")
        desplazamiento = struct.unpack(orden + "Q", f.read(8))[0]
    else:
        raise ErrorFormato(
            f"{ruta.name}: versión TIFF {version} desconocida.")

    entradas = _leer_entradas(f, orden, es_bigtiff, desplazamiento)

    ancho = _entero(entradas, orden, ANCHO, 0)
    alto = _entero(entradas, orden, ALTO, 0)
    if not ancho or not alto:
        raise ErrorFormato(f"{ruta.name}: sin dimensiones declaradas.")

    muestras = _entero(entradas, orden, MUESTRAS_POR_PIXEL, 1)
    if muestras != 1:
        raise ErrorFormato(
            f"{ruta.name}: {muestras} muestras por píxel. El adaptador lee "
            "rásteres de una sola banda; separarlas corresponde al entorno SIG."
        )
    planar = _entero(entradas, orden, CONFIGURACION_PLANAR, 1)
    if planar != 1:
        raise ErrorFormato(f"{ruta.name}: configuración planar {planar}.")

    compresion = _entero(entradas, orden, COMPRESION, SIN_COMPRESION)
    if compresion not in (SIN_COMPRESION, LZW, DEFLATE, DEFLATE_ANTIGUO):
        raise ErrorFormato(
            f"{ruta.name}: compresión {compresion} no contemplada. El "
            "adaptador lee sin comprimir, Deflate y LZW.")

    escala = _valores(entradas, orden, ESCALA_PIXEL)
    amarre = _valores(entradas, orden, PUNTO_DE_AMARRE)
    if len(escala) < 2 or len(amarre) < 6:
        raise ErrorFormato(
            f"{ruta.name}: sin escala de píxel o punto de amarre. Un ráster "
            "sin georreferencia no se puede cruzar con la cuenca.")
    tamano_x, tamano_y = float(escala[0]), float(escala[1])
    origen_x = float(amarre[3]) - float(amarre[0]) * tamano_x
    origen_y = float(amarre[4]) + float(amarre[1]) * tamano_y

    crudo_nodato = _valores(entradas, orden, NODATO)
    try:
        nodato = float(str(crudo_nodato[0]).strip()) if crudo_nodato else None
    except (TypeError, ValueError):
        nodato = None

    info = InfoRaster(
        ruta=ruta,
        ancho=ancho,
        alto=alto,
        bits_por_muestra=_entero(entradas, orden, BITS_POR_MUESTRA, 8),
        formato_muestra=_entero(entradas, orden, FORMATO_MUESTRA,
                                ENTERO_SIN_SIGNO),
        muestras_por_pixel=muestras,
        compresion=compresion,
        predictor=_entero(entradas, orden, PREDICTOR, 1),
        nodato=nodato,
        origen_x=origen_x,
        origen_y=origen_y,
        tamano_x=tamano_x,
        tamano_y=tamano_y,
        crs_epsg=_epsg_de_geoclaves(entradas, orden),
        es_bigtiff=es_bigtiff,
        ancho_tesela=_entero(entradas, orden, ANCHO_TESELA, 0),
        alto_tesela=_entero(entradas, orden, ALTO_TESELA, 0),
        filas_por_franja=_entero(entradas, orden, FILAS_POR_FRANJA, alto),
    )

    if info.teselado:
        desplazamientos = _valores(entradas, orden, DESPLAZAMIENTOS_TESELA)
        tamanos = _valores(entradas, orden, BYTES_POR_TESELA)
    else:
        desplazamientos = _valores(entradas, orden, DESPLAZAMIENTOS_FRANJA)
        tamanos = _valores(entradas, orden, BYTES_POR_FRANJA)
    if not desplazamientos:
        raise ErrorFormato(f"{ruta.name}: sin índice de bloques de datos.")

    # El orden de bytes se necesita después para normalizar a nativo.
    return info, desplazamientos, tamanos + (orden,)


def leer_info(ruta: str | Path) -> InfoRaster:
    """
    Lee la cabecera de un GeoTIFF sin cargar la imagen.

    Excepciones
    -----------
    ErrorRutas
        El archivo no existe.
    ErrorFormato
        No es un TIFF, o usa una variante fuera del alcance del adaptador.
    """
    destino = Path(ruta)
    if not destino.is_file():
        raise ErrorRutas(f"no se encuentra el ráster {destino}.")
    with open(destino, "rb") as f:
        info, _, _ = _cabecera(f, destino)
    return info


# =============================================================================
# Descompresión
# =============================================================================
def _descomprimir_lzw(datos: bytes) -> bytes:
    """
    Descomprime un bloque LZW en la variante de TIFF.

    TIFF usa 'early change': el ancho de código sube un código antes de lo que
    haría el LZW de GIF. Ignorarlo produce una imagen que empieza bien y se
    corrompe a partir del primer cambio de ancho, que es el fallo más difícil
    de diagnosticar de este formato.
    """
    diccionario: list[bytes] = []

    def reiniciar() -> None:
        del diccionario[:]
        diccionario.extend(bytes([i]) for i in range(256))
        diccionario.append(b"")  # 256, reinicio
        diccionario.append(b"")  # 257, fin de información

    reiniciar()
    salida = bytearray()
    ancho = 9
    anterior = b""
    acumulador = 0
    bits = 0

    for byte in datos:
        acumulador = (acumulador << 8) | byte
        bits += 8
        while bits >= ancho:
            codigo = (acumulador >> (bits - ancho)) & ((1 << ancho) - 1)
            bits -= ancho
            if codigo == 256:
                reiniciar()
                ancho = 9
                anterior = b""
                continue
            if codigo == 257:
                return bytes(salida)
            if codigo < len(diccionario):
                entrada = diccionario[codigo]
            elif codigo == len(diccionario) and anterior:
                entrada = anterior + anterior[:1]
            else:
                raise ErrorFormato(
                    f"flujo LZW inconsistente: código {codigo} con "
                    f"diccionario de {len(diccionario)}.")
            salida += entrada
            if anterior:
                diccionario.append(anterior + entrada[:1])
            anterior = entrada
            if len(diccionario) + 1 >= (1 << ancho) and ancho < 12:
                ancho += 1
    return bytes(salida)


def _deshacer_predictor(datos: bytearray, ancho: int, filas: int,
                        bytes_muestra: int) -> None:
    """
    Revierte el predictor horizontal, en el sitio.

    El predictor guarda la diferencia con la celda anterior de la misma fila,
    lo que mejora la compresión de superficies suaves. Se revierte fila a fila:
    aplicarlo de corrido a través del salto de fila desplazaría la imagen.
    """
    if bytes_muestra == 1:
        for j in range(filas):
            base = j * ancho
            for i in range(base + 1, base + ancho):
                datos[i] = (datos[i] + datos[i - 1]) & 0xFF
        return
    if bytes_muestra == 2:
        vista = memoryview(datos).cast("H")
        for j in range(filas):
            base = j * ancho
            for i in range(base + 1, base + ancho):
                vista[i] = (vista[i] + vista[i - 1]) & 0xFFFF
        return
    raise ErrorFormato(
        f"predictor horizontal sobre muestras de {bytes_muestra} bytes no "
        "contemplado.")


# =============================================================================
# Lector
# =============================================================================
class LectorRaster:
    """
    Entrega filas del ráster como bytes crudos, sin cargarlo entero.

    Un DEM de 12.019 x 9.486 celdas ocupa 456 MB. Leerlo entero en memoria
    funciona en un equipo holgado y falla en otro, de modo que se lee por
    filas y se guarda en memoria solo el bloque en curso.

    Uso:
        with LectorRaster(ruta) as lector:
            for j in range(lector.info.alto):
                datos = lector.fila(j)
    """

    def __init__(self, ruta: str | Path) -> None:
        destino = Path(ruta)
        if not destino.is_file():
            raise ErrorRutas(f"no se encuentra el ráster {destino}.")
        self._f = open(destino, "rb")
        try:
            info, desplazamientos, tamanos = _cabecera(self._f, destino)
        except Exception:
            self._f.close()
            raise
        self.info = info
        self._orden = tamanos[-1]
        self._desplazamientos = desplazamientos
        self._tamanos = tamanos[:-1]
        self._indice_en_memoria = -1
        self._bloque = b""
        self._intercambiar = (
            self._orden == ">" and self.info.bytes_por_muestra > 1)

    # -- protocolo de contexto ------------------------------------------------
    def __enter__(self) -> "LectorRaster":
        return self

    def __exit__(self, *_excepcion: Any) -> None:
        self.cerrar()

    def cerrar(self) -> None:
        if not self._f.closed:
            self._f.close()

    # -- lectura --------------------------------------------------------------
    def _bytes_de_bloque(self, indice: int, celdas: int) -> bytes:
        """Lee y descomprime el bloque n, sin deshacer el predictor."""
        if indice >= len(self._desplazamientos):
            raise ErrorFormato(
                f"{self.info.ruta.name}: se pidió el bloque {indice} y solo "
                f"hay {len(self._desplazamientos)}.")
        self._f.seek(self._desplazamientos[indice])
        esperados = celdas * self.info.bytes_por_muestra
        cuantos = (self._tamanos[indice] if indice < len(self._tamanos)
                   else esperados)
        crudo = self._f.read(cuantos)
        if self.info.compresion == SIN_COMPRESION:
            return crudo
        if self.info.compresion in (DEFLATE, DEFLATE_ANTIGUO):
            return zlib.decompress(crudo)
        return _descomprimir_lzw(crudo)

    def _preparar(self, indice_bloque: int, celdas: int,
                  ancho_bloque: int, filas_bloque: int) -> None:
        """Deja el bloque pedido descomprimido y listo en memoria."""
        if self._indice_en_memoria == indice_bloque:
            return
        datos = bytearray(self._bytes_de_bloque(indice_bloque, celdas))
        if self.info.predictor == 2:
            _deshacer_predictor(datos, ancho_bloque, filas_bloque,
                                self.info.bytes_por_muestra)
        if self._intercambiar:
            datos = bytearray(
                _invertir_orden(bytes(datos), self.info.bytes_por_muestra))
        self._bloque = bytes(datos)
        self._indice_en_memoria = indice_bloque

    def fila(self, j: int) -> bytes:
        """
        Devuelve la fila j como bytes crudos, en orden nativo little endian.

        La longitud es ancho * bytes por muestra. Interpretarla corresponde a
        quien llama: 'array.array' o 'numpy.frombuffer' con info.descriptor.
        """
        info = self.info
        if not 0 <= j < info.alto:
            raise ErrorFormato(
                f"{info.ruta.name}: fila {j} fuera de un ráster de "
                f"{info.alto} filas.")

        if not info.teselado:
            indice = j // info.filas_por_franja
            filas_bloque = min(info.filas_por_franja,
                               info.alto - indice * info.filas_por_franja)
            self._preparar(indice, info.ancho * filas_bloque,
                           info.ancho, filas_bloque)
            desde = (j % info.filas_por_franja) * info.bytes_por_fila
            return self._bloque[desde:desde + info.bytes_por_fila]

        return self._fila_teselada(j)

    def _fila_teselada(self, j: int) -> bytes:
        """
        Arma una fila completa a partir de la hilera de teselas que la cruza.

        Se guarda la hilera entera y no la tesela suelta porque leer el ráster
        de arriba abajo recorre cada hilera 'alto_tesela' veces: descomprimirla
        una sola vez evita repetir ese trabajo por cada fila.
        """
        info = self.info
        fila_tesela = j // info.alto_tesela
        if self._indice_en_memoria != fila_tesela:
            por_hilera = (info.ancho + info.ancho_tesela - 1) // info.ancho_tesela
            celdas = info.ancho_tesela * info.alto_tesela
            bytes_fila_tesela = info.ancho_tesela * info.bytes_por_muestra
            hilera = bytearray(
                info.alto_tesela * info.bytes_por_fila)
            for columna_tesela in range(por_hilera):
                indice = fila_tesela * por_hilera + columna_tesela
                datos = bytearray(self._bytes_de_bloque(indice, celdas))
                if info.predictor == 2:
                    _deshacer_predictor(datos, info.ancho_tesela,
                                        info.alto_tesela,
                                        info.bytes_por_muestra)
                if self._intercambiar:
                    datos = bytearray(
                        _invertir_orden(bytes(datos), info.bytes_por_muestra))
                # Las teselas del borde derecho vienen completas y rellenas:
                # se recorta lo que sobresale del ancho declarado.
                inicio_x = columna_tesela * info.ancho_tesela
                utiles = min(info.ancho_tesela, info.ancho - inicio_x)
                if utiles <= 0:
                    continue
                anchura = utiles * info.bytes_por_muestra
                desde_x = inicio_x * info.bytes_por_muestra
                for fila_local in range(info.alto_tesela):
                    origen = fila_local * bytes_fila_tesela
                    destino = fila_local * info.bytes_por_fila + desde_x
                    hilera[destino:destino + anchura] = \
                        datos[origen:origen + anchura]
            self._bloque = bytes(hilera)
            self._indice_en_memoria = fila_tesela

        desde = (j % info.alto_tesela) * info.bytes_por_fila
        return self._bloque[desde:desde + info.bytes_por_fila]


def _invertir_orden(datos: bytes, bytes_muestra: int) -> bytes:
    """Pasa de big endian a little endian sin depender de numpy."""
    if bytes_muestra <= 1:
        return datos
    vista = memoryview(datos)
    partes = [vista[i:i + bytes_muestra].tobytes()[::-1]
              for i in range(0, len(datos) - bytes_muestra + 1, bytes_muestra)]
    return b"".join(partes)

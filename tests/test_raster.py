# -*- coding: utf-8 -*-
"""
Pruebas del adaptador de GeoTIFF y de la rasterización por barrido.

Los rásteres se construyen byte a byte, igual que los shapefiles del M00c. Eso
permite verificar el adaptador sin GDAL y sin depender de ningún insumo real:

    python tests/test_raster.py
"""

from __future__ import annotations

import math
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import geometria, raster  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402

_DEM_REAL = _RAIZ_REPO / "data" / "03_SIG" / "raster" / "dem_recortado.tif"


# =============================================================================
# Constructor de GeoTIFF sintéticos
# =============================================================================
def _geoclaves(epsg: int) -> tuple[int, ...]:
    return (1, 1, 0, 1, 3072, 0, 1, epsg)


def escribir_tiff(
    destino: Path,
    valores: list[list[float]],
    *,
    descriptor: str = "f4",
    compresion: int = raster.SIN_COMPRESION,
    filas_por_franja: int = 1,
    tesela: int = 0,
    bigtiff: bool = False,
    orden: str = "<",
    predictor: int = 1,
    nodato: float | None = -9999.0,
    origen: tuple[float, float] = (1000.0, 2000.0),
    celda: float = 10.0,
    epsg: int = 9377,
    omitir_georreferencia: bool = False,
) -> Path:
    """
    Escribe un GeoTIFF mínimo pero legal con el contenido dado.

    Cubre a propósito las cuatro combinaciones que el estudio encuentra en la
    práctica: franjas o teselas, con compresión o sin ella.
    """
    alto = len(valores)
    ancho = len(valores[0])
    codigo = {"f4": "f", "u1": "B", "u2": "H", "i2": "h"}[descriptor]
    bytes_muestra = {"f4": 4, "u1": 1, "u2": 2, "i2": 2}[descriptor]
    formato = {"f4": 3, "u1": 1, "u2": 1, "i2": 2}[descriptor]

    def empaquetar(fila: list[float]) -> bytes:
        return struct.pack(orden + codigo * len(fila),
                           *(v if codigo == "f" else int(v) for v in fila))

    bloques: list[bytes] = []
    if tesela:
        por_hilera = (ancho + tesela - 1) // tesela
        for j0 in range(0, alto, tesela):
            for i0 in range(0, ancho, tesela):
                crudo = b""
                for j in range(j0, j0 + tesela):
                    fila = [valores[j][i] if j < alto and i < ancho else 0.0
                            for i in range(i0, i0 + tesela)]
                    crudo += empaquetar(fila)
                bloques.append(crudo)
    else:
        for j0 in range(0, alto, filas_por_franja):
            crudo = b""
            for j in range(j0, min(j0 + filas_por_franja, alto)):
                crudo += empaquetar(valores[j])
            bloques.append(crudo)

    if compresion == raster.DEFLATE:
        bloques = [zlib.compress(b) for b in bloques]
    elif compresion == raster.LZW:
        bloques = [_comprimir_lzw(b) for b in bloques]

    # Cabecera, luego datos, luego el directorio y los arreglos largos.
    cuerpo = b"".join(bloques)
    inicio = 16 if bigtiff else 8
    desplazamientos = []
    posicion = inicio
    for bloque in bloques:
        desplazamientos.append(posicion)
        posicion += len(bloque)

    entradas: list[tuple[int, int, tuple]] = [
        (raster.ANCHO, 3, (ancho,)),
        (raster.ALTO, 3, (alto,)),
        (raster.BITS_POR_MUESTRA, 3, (bytes_muestra * 8,)),
        (raster.COMPRESION, 3, (compresion,)),
        (raster.MUESTRAS_POR_PIXEL, 3, (1,)),
        (raster.CONFIGURACION_PLANAR, 3, (1,)),
        (raster.PREDICTOR, 3, (predictor,)),
        (raster.FORMATO_MUESTRA, 3, (formato,)),
        (raster.DIRECTORIO_GEOCLAVES, 3, _geoclaves(epsg)),
    ]
    if not omitir_georreferencia:
        entradas += [
            (raster.ESCALA_PIXEL, 12, (celda, celda, 0.0)),
            (raster.PUNTO_DE_AMARRE, 12,
             (0.0, 0.0, 0.0, origen[0], origen[1], 0.0)),
        ]
    if tesela:
        entradas += [
            (raster.ANCHO_TESELA, 3, (tesela,)),
            (raster.ALTO_TESELA, 3, (tesela,)),
            (raster.DESPLAZAMIENTOS_TESELA, 4, tuple(desplazamientos)),
            (raster.BYTES_POR_TESELA, 4, tuple(len(b) for b in bloques)),
        ]
    else:
        entradas += [
            (raster.FILAS_POR_FRANJA, 3, (filas_por_franja,)),
            (raster.DESPLAZAMIENTOS_FRANJA, 4, tuple(desplazamientos)),
            (raster.BYTES_POR_FRANJA, 4, tuple(len(b) for b in bloques)),
        ]
    if nodato is not None:
        texto = f"{nodato:g}".encode("ascii") + b"\x00"
        entradas.append((raster.NODATO, 2, texto))

    entradas.sort(key=lambda e: e[0])

    tam_entrada = 20 if bigtiff else 12
    tam_valor = 8 if bigtiff else 4
    fmt_cuenta = "Q" if bigtiff else "I"
    inicio_ifd = posicion
    tam_ifd = (8 if bigtiff else 2) + tam_entrada * len(entradas) + tam_valor
    libre = inicio_ifd + tam_ifd

    cuerpo_ifd = b""
    extra = b""
    for etiqueta, tipo, datos in entradas:
        if tipo == 2:
            crudo = datos if isinstance(datos, bytes) else bytes(datos)
            cuenta = len(crudo)
        else:
            codigo_tipo, tam = raster._TIPOS[tipo]
            cuenta = len(datos)
            crudo = struct.pack(orden + codigo_tipo * cuenta, *datos)
        cuerpo_ifd += struct.pack(orden + "HH", etiqueta, tipo)
        cuerpo_ifd += struct.pack(orden + fmt_cuenta, cuenta)
        if len(crudo) <= tam_valor:
            cuerpo_ifd += crudo.ljust(tam_valor, b"\x00")
        else:
            cuerpo_ifd += struct.pack(orden + fmt_cuenta, libre + len(extra))
            extra += crudo

    if bigtiff:
        cabecera = (b"II" if orden == "<" else b"MM")
        cabecera += struct.pack(orden + "H", 43)
        cabecera += struct.pack(orden + "HH", 8, 0)
        cabecera += struct.pack(orden + "Q", inicio_ifd)
        encabezado_ifd = struct.pack(orden + "Q", len(entradas))
        cierre = struct.pack(orden + "Q", 0)
    else:
        cabecera = (b"II" if orden == "<" else b"MM")
        cabecera += struct.pack(orden + "H", 42)
        cabecera += struct.pack(orden + "I", inicio_ifd)
        encabezado_ifd = struct.pack(orden + "H", len(entradas))
        cierre = struct.pack(orden + "I", 0)

    destino.write_bytes(
        cabecera + cuerpo + encabezado_ifd + cuerpo_ifd + cierre + extra)
    return destino


def _comprimir_lzw(datos: bytes) -> bytes:
    """Comprime en LZW de TIFF. Solo lo usan las pruebas."""
    diccionario: dict[bytes, int] = {}

    def reiniciar() -> None:
        diccionario.clear()
        for i in range(256):
            diccionario[bytes([i])] = i

    reiniciar()
    siguiente = 258
    ancho = 9
    salida = bytearray()
    acumulador = 0
    bits = 0

    def emitir(codigo: int) -> None:
        nonlocal acumulador, bits
        acumulador = (acumulador << ancho) | codigo
        bits += ancho
        while bits >= 8:
            salida.append((acumulador >> (bits - 8)) & 0xFF)
            bits -= 8

    emitir(256)
    actual = b""
    for byte in datos:
        candidato = actual + bytes([byte])
        if candidato in diccionario:
            actual = candidato
            continue
        emitir(diccionario[actual])
        diccionario[candidato] = siguiente
        siguiente += 1
        if siguiente + 1 > (1 << ancho) and ancho < 12:
            ancho += 1
        if siguiente >= 4094:
            emitir(256)
            reiniciar()
            siguiente = 258
            ancho = 9
        actual = bytes([byte])
    if actual:
        emitir(diccionario[actual])
    emitir(257)
    if bits:
        salida.append((acumulador << (8 - bits)) & 0xFF)
    return bytes(salida)


def _rejilla(alto: int, ancho: int) -> list[list[float]]:
    """
    Superficie determinista y sin simetrías que oculten un desfase.

    Cada celda tiene un valor único, de modo que una fila leída del sitio
    equivocado, o una tesela pegada con un desplazamiento, se detecta en la
    comparación en lugar de pasar por buena.
    """
    return [[float(j * ancho + i) for i in range(ancho)] for j in range(alto)]


# =============================================================================
# Adaptador
# =============================================================================
class PruebaAdaptador(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _comprobar(self, **opciones) -> None:
        """Escribe, lee y compara celda a celda."""
        import array

        alto, ancho = opciones.pop("alto", 9), opciones.pop("ancho", 7)
        esperado = _rejilla(alto, ancho)
        destino = escribir_tiff(
            self.temporal / "prueba.tif", esperado, **opciones)
        info = raster.leer_info(destino)
        self.assertEqual((info.ancho, info.alto), (ancho, alto))
        codigo = {"<f4": "f", "<u1": "B", "<u2": "H", "<i2": "h"}[info.descriptor]
        with raster.LectorRaster(destino) as lector:
            for j in range(alto):
                leido = array.array(codigo)
                leido.frombytes(lector.fila(j))
                self.assertEqual(list(leido), esperado[j], f"fila {j}")

    def test_franjas_sin_comprimir(self) -> None:
        self._comprobar()

    def test_franjas_de_varias_filas(self) -> None:
        self._comprobar(filas_por_franja=4)

    def test_una_sola_franja(self) -> None:
        self._comprobar(filas_por_franja=9)

    def test_deflate(self) -> None:
        self._comprobar(compresion=raster.DEFLATE, filas_por_franja=3)

    def test_lzw(self) -> None:
        self._comprobar(compresion=raster.LZW, filas_por_franja=3)

    def test_teselas_sin_comprimir(self) -> None:
        # Teselas de 4 sobre 7 columnas: la hilera derecha sobresale y hay que
        # recortarla. Es el caso que rompe una implementación ingenua.
        self._comprobar(tesela=4)

    def test_teselas_con_lzw(self) -> None:
        self._comprobar(tesela=4, compresion=raster.LZW)

    def test_bigtiff(self) -> None:
        self._comprobar(bigtiff=True, filas_por_franja=2)

    def test_bigtiff_teselado_y_comprimido(self) -> None:
        # Es exactamente la variante de la capa global de suelos.
        self._comprobar(bigtiff=True, tesela=4, compresion=raster.LZW,
                        descriptor="u1", alto=6, ancho=6)

    def test_big_endian(self) -> None:
        self._comprobar(orden=">", descriptor="u2", alto=5, ancho=5)

    def test_enteros_de_un_byte(self) -> None:
        self._comprobar(descriptor="u1", alto=5, ancho=5)

    def test_predictor_horizontal(self) -> None:
        import array

        valores = [[10.0, 12.0, 15.0, 11.0], [20.0, 21.0, 25.0, 24.0]]
        # La diferencia puede ser negativa y el TIFF la guarda envuelta a 8
        # bits; el adaptador debe volver a envolverla al sumar.
        diferencias = [[fila[0]] + [(fila[i] - fila[i - 1]) % 256
                                    for i in range(1, len(fila))]
                       for fila in valores]
        destino = escribir_tiff(
            self.temporal / "pred.tif", diferencias, descriptor="u1",
            compresion=raster.DEFLATE, filas_por_franja=1, predictor=2)
        with raster.LectorRaster(destino) as lector:
            for j, esperada in enumerate(valores):
                leido = array.array("B")
                leido.frombytes(lector.fila(j))
                self.assertEqual(list(leido), [int(v) for v in esperada])

    def test_georreferencia(self) -> None:
        destino = escribir_tiff(self.temporal / "geo.tif", _rejilla(4, 4),
                                origen=(1000.0, 2000.0), celda=10.0)
        info = raster.leer_info(destino)
        self.assertEqual(info.crs_epsg, "EPSG:9377")
        self.assertEqual(info.nodato, -9999.0)
        self.assertEqual(info.extension, (1000.0, 1960.0, 1040.0, 2000.0))
        self.assertEqual(info.x_de_columna(0), 1005.0)
        self.assertEqual(info.y_de_fila(0), 1995.0)
        self.assertEqual(info.columna_de(1025.0), 2)
        self.assertEqual(info.fila_de(1975.0), 2)
        self.assertTrue(info.contiene(1010.0, 1970.0, 1030.0, 1990.0))
        self.assertFalse(info.contiene(990.0, 1970.0, 1030.0, 1990.0))

    def test_archivo_ausente(self) -> None:
        with self.assertRaises(ErrorRutas):
            raster.leer_info(self.temporal / "no_existe.tif")

    def test_lo_que_no_es_tiff(self) -> None:
        destino = self.temporal / "x.tif"
        destino.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        with self.assertRaises(ErrorFormato):
            raster.leer_info(destino)

    def test_compresion_no_contemplada_se_declara(self) -> None:
        destino = escribir_tiff(self.temporal / "jpeg.tif", _rejilla(3, 3),
                                compresion=7)
        with self.assertRaises(ErrorFormato) as contexto:
            raster.leer_info(destino)
        self.assertIn("compresión", str(contexto.exception))

    def test_sin_georreferencia_es_error(self) -> None:
        # Un ráster sin escala de píxel no se puede cruzar con la cuenca, y
        # dejarlo pasar produciría una estadística zonal en el sitio
        # equivocado sin avisar.
        destino = escribir_tiff(self.temporal / "sin_geo.tif", _rejilla(3, 3),
                                omitir_georreferencia=True)
        with self.assertRaises(ErrorFormato):
            raster.leer_info(destino)

    def test_fila_fuera_del_raster(self) -> None:
        destino = escribir_tiff(self.temporal / "g.tif", _rejilla(3, 3))
        with raster.LectorRaster(destino) as lector:
            with self.assertRaises(ErrorFormato):
                lector.fila(3)


# =============================================================================
# Rasterización por barrido
# =============================================================================
class PruebaBarrido(unittest.TestCase):
    CUADRADO = [[[(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)]]]

    def test_un_cuadrado_da_un_tramo(self) -> None:
        aristas = geometria.aristas_de(self.CUADRADO)
        self.assertEqual(geometria.tramos_de_barrido(aristas, 5.0),
                         [(0.0, 10.0)])

    def test_fuera_no_da_tramos(self) -> None:
        aristas = geometria.aristas_de(self.CUADRADO)
        self.assertEqual(geometria.tramos_de_barrido(aristas, 15.0), [])

    def test_un_hueco_parte_el_tramo_en_dos(self) -> None:
        con_isla = [[
            [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)],
            [(3.0, 3.0), (3.0, 7.0), (7.0, 7.0), (7.0, 3.0)],
        ]]
        aristas = geometria.aristas_de(con_isla)
        self.assertEqual(geometria.tramos_de_barrido(aristas, 5.0),
                         [(0.0, 3.0), (7.0, 10.0)])
        # Por debajo del hueco vuelve a ser un solo tramo.
        self.assertEqual(geometria.tramos_de_barrido(aristas, 1.0),
                         [(0.0, 10.0)])

    def test_un_vertice_compartido_no_abre_un_tramo_de_mas(self) -> None:
        # Un pico exacto sobre la altura del barrido. Con la convención de
        # mitad abierta, las dos aristas que lo comparten aportan un solo
        # cruce, no dos ni cero.
        triangulo = [[[(0.0, 0.0), (5.0, 10.0), (10.0, 0.0)]]]
        aristas = geometria.aristas_de(triangulo)
        for y in (0.0, 2.5, 5.0, 9.999):
            self.assertEqual(len(geometria.tramos_de_barrido(aristas, y)), 1,
                             f"y={y}")

    def test_las_horizontales_no_aportan_cruces(self) -> None:
        aristas = geometria.aristas_de(self.CUADRADO)
        self.assertEqual(len(aristas), 2)

    def test_dos_poligonos_sueltos(self) -> None:
        dos = [[[(0.0, 0.0), (0.0, 4.0), (2.0, 4.0), (2.0, 0.0)]],
               [[(6.0, 0.0), (6.0, 4.0), (9.0, 4.0), (9.0, 0.0)]]]
        aristas = geometria.aristas_de(dos)
        self.assertEqual(geometria.tramos_de_barrido(aristas, 2.0),
                         [(0.0, 2.0), (6.0, 9.0)])


class PruebaIndicePoligonos(unittest.TestCase):
    """
    El índice existe por rendimiento, de modo que la prueba que importa es que
    su respuesta sea idéntica a la del recorrido completo.
    """

    def setUp(self) -> None:
        # Corona dentada: contorno irregular y un hueco central.
        exterior = [(50.0 + 40.0 * math.cos(t * math.pi / 24)
                     * (1.0 + 0.25 * math.sin(t * math.pi / 3)),
                     50.0 + 40.0 * math.sin(t * math.pi / 24)
                     * (1.0 + 0.25 * math.sin(t * math.pi / 3)))
                    for t in range(48)]
        hueco = [(50.0 + 10.0 * math.cos(-t * math.pi / 12),
                  50.0 + 10.0 * math.sin(-t * math.pi / 12))
                 for t in range(24)]
        self.poligonos = [[exterior, hueco]]
        self.indice = geometria.IndicePoligonos(self.poligonos)

    def test_coincide_con_el_recorrido_completo(self) -> None:
        discrepancias = 0
        for j in range(60):
            for i in range(60):
                x, y = 1.0 + i * 1.7, 1.0 + j * 1.7
                if (self.indice.contiene(x, y)
                        != geometria.punto_en_alguno(x, y, self.poligonos)):
                    discrepancias += 1
        self.assertEqual(discrepancias, 0)

    def test_el_hueco_queda_fuera(self) -> None:
        self.assertFalse(self.indice.contiene(50.0, 50.0))

    def test_lejos_queda_fuera(self) -> None:
        self.assertFalse(self.indice.contiene(-500.0, -500.0))

    def test_sin_geometria_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            geometria.IndicePoligonos([])


# =============================================================================
# El DEM del estudio
# =============================================================================
@unittest.skipUnless(_DEM_REAL.is_file(), "no hay DEM recortado")
class PruebaDemReal(unittest.TestCase):
    def test_se_lee_y_esta_en_el_crs_de_calculo(self) -> None:
        info = raster.leer_info(_DEM_REAL)
        self.assertEqual(info.crs_epsg, "EPSG:9377")
        self.assertAlmostEqual(info.tamano_x, 12.5, places=6)
        self.assertEqual(info.descriptor, "<f4")

    def test_las_cotas_son_plausibles(self) -> None:
        import array

        info = raster.leer_info(_DEM_REAL)
        with raster.LectorRaster(_DEM_REAL) as lector:
            fila = array.array("f")
            fila.frombytes(lector.fila(info.alto // 2))
        validas = [v for v in fila if v != info.nodato]
        self.assertTrue(validas)
        # Colombia continental: ni bajo el nivel del mar ni sobre el Everest.
        self.assertGreater(min(validas), -100.0)
        self.assertLess(max(validas), 6000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

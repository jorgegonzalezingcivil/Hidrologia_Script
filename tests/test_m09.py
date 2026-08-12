# -*- coding: utf-8 -*-
"""
Pruebas del M09: intercambio con el paso manual de HEC-HMS.

    python tests/test_m09.py
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M09_hec_hms as m09  # noqa: E402
from comun import esquema, shapefile  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


class PruebaVerificacionDeArea(unittest.TestCase):
    """
    La cuenca preliminar es COTA SUPERIOR, no objetivo de igualdad.

    Para el escenario 1 el M02 adopta la subzona entera, que sobredimensiona el
    área varias veces: exigir que coincidan rechazaría cualquier delimitación
    correcta.
    """

    PRELIMINAR = 5925.89

    def test_una_delimitacion_razonable_pasa(self) -> None:
        resultado = m09.verificar_area(1500.0, self.PRELIMINAR, 5.0)
        self.assertFalse(resultado["excede_la_preliminar"])
        self.assertFalse(resultado["demasiado_pequena"])

    def test_exceder_la_preliminar_se_detecta(self) -> None:
        # La subzona es cerrada y contiene la cuenca por definición: delimitar
        # más significa tomar agua de otra vertiente.
        resultado = m09.verificar_area(6500.0, self.PRELIMINAR, 5.0)
        self.assertTrue(resultado["excede_la_preliminar"])

    def test_el_fallo_conocido_del_dem_se_atrapa(self) -> None:
        # 6,59 km² sobre una subzona de 5.926 es el 0,1%: es lo que produjo el
        # análisis de terreno del M02 con el DEM de radar sin reacondicionar.
        resultado = m09.verificar_area(6.59, self.PRELIMINAR, 5.0)
        self.assertTrue(resultado["demasiado_pequena"])
        self.assertLess(resultado["fraccion_pct"], 1.0)

    def test_el_borde_de_la_fraccion(self) -> None:
        justo = m09.verificar_area(self.PRELIMINAR * 0.05, self.PRELIMINAR, 5.0)
        self.assertFalse(justo["demasiado_pequena"])
        apenas = m09.verificar_area(self.PRELIMINAR * 0.049, self.PRELIMINAR, 5.0)
        self.assertTrue(apenas["demasiado_pequena"])

    def test_una_preliminar_nula_se_reporta(self) -> None:
        self.assertIn("error", m09.verificar_area(100.0, 0.0, 5.0))


class PruebaDiferenciaRelativa(unittest.TestCase):
    def test_calcula_el_porcentaje(self) -> None:
        self.assertAlmostEqual(m09.diferencia_relativa(110.0, 100.0), 10.0)

    def test_referencia_nula_no_divide(self) -> None:
        self.assertIsNone(m09.diferencia_relativa(10.0, 0.0))


class PruebaCopiaDeCapa(unittest.TestCase):
    """
    Copiar solo el .shp entrega una capa que ningún programa puede abrir: los
    atributos viven en el .dbf y el sistema de referencia en el .prj.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.origen = self.tmp / "capa.shp"
        for extension in (".shp", ".shx", ".dbf", ".prj"):
            self.origen.with_suffix(extension).write_bytes(b"x")
        # Un archivo que no acompaña a un shapefile no debe copiarse.
        self.origen.with_suffix(".txt").write_bytes(b"x")

    def test_copia_todos_los_acompanantes(self) -> None:
        destino = self.tmp / "salida"
        copiados = m09.copiar_capa(self.origen, destino)
        self.assertEqual(sorted(c.suffix for c in copiados),
                         [".dbf", ".prj", ".shp", ".shx"])

    def test_no_arrastra_archivos_ajenos(self) -> None:
        destino = self.tmp / "salida"
        m09.copiar_capa(self.origen, destino)
        self.assertFalse((destino / "capa.txt").exists())

    def test_un_origen_inexistente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorRutas):
            m09.copiar_capa(self.tmp / "no_existe.shp", self.tmp / "salida")


def _escribir_poligonos(destino: Path, poligonos) -> Path:
    """
    Shapefile de polígonos mínimo, solo geometría.

    El adaptador no escribe polígonos y estas pruebas necesitan una capa con
    áreas conocidas. Se genera aquí, sin .dbf: es exactamente el caso que
    importa, porque el área debe salir de la geometría y no de un atributo.
    """
    contenidos = []
    for anillos in poligonos:
        puntos = [punto for anillo in anillos for punto in anillo]
        equis = [p[0] for p in puntos]
        griegas = [p[1] for p in puntos]
        partes, acumulado = [], 0
        for anillo in anillos:
            partes.append(acumulado)
            acumulado += len(anillo)
        cuerpo = struct.pack("<i", 5)
        cuerpo += struct.pack("<4d", min(equis), min(griegas),
                              max(equis), max(griegas))
        cuerpo += struct.pack("<2i", len(partes), len(puntos))
        cuerpo += struct.pack(f"<{len(partes)}i", *partes)
        for x, y in puntos:
            cuerpo += struct.pack("<2d", x, y)
        contenidos.append(cuerpo)

    registros = b""
    for numero, cuerpo in enumerate(contenidos, start=1):
        registros += struct.pack(">2i", numero, len(cuerpo) // 2) + cuerpo

    todos = [p for anillos in poligonos for anillo in anillos for p in anillo]
    cabecera = struct.pack(">i", 9994) + b"\x00" * 20
    cabecera += struct.pack(">i", (100 + len(registros)) // 2)
    cabecera += struct.pack("<2i", 1000, 5)
    cabecera += struct.pack("<4d", min(p[0] for p in todos),
                            min(p[1] for p in todos),
                            max(p[0] for p in todos),
                            max(p[1] for p in todos))
    cabecera += struct.pack("<4d", 0.0, 0.0, 0.0, 0.0)

    destino.write_bytes(cabecera + registros)
    return destino


def _cuadrado(x: float, y: float, lado: float):
    """Anillo exterior en sentido horario, como manda el formato."""
    return [(x, y), (x, y + lado), (x + lado, y + lado), (x + lado, y), (x, y)]


class PruebaAreasPorEntidad(unittest.TestCase):
    """
    El área por entidad sale de la GEOMETRÍA, nunca de un atributo.

    La exportación de HEC-HMS trae los parámetros que el programa calculó
    (long_len, basin_slo, drain_den) y ninguno es el área. Buscarla entre los
    campos devolvía lista vacía, y el módulo concluía que no había ninguna
    subcuenca diminuta.
    """

    def test_cada_entidad_trae_su_area(self) -> None:
        with tempfile.TemporaryDirectory() as temporal:
            ruta = _escribir_poligonos(
                Path(temporal) / "capa.shp",
                [[_cuadrado(0.0, 0.0, 1000.0)],      # 1 km²
                 [_cuadrado(5000.0, 0.0, 100.0)]],   # 0,01 km²
            )
            areas = shapefile.areas_poligonos(ruta)
        self.assertEqual(len(areas), 2)
        self.assertAlmostEqual(areas[0], 1_000_000.0, places=3)
        self.assertAlmostEqual(areas[1], 10_000.0, places=3)

    def test_la_suma_coincide_con_el_area_total(self) -> None:
        with tempfile.TemporaryDirectory() as temporal:
            ruta = _escribir_poligonos(
                Path(temporal) / "capa.shp",
                [[_cuadrado(0.0, 0.0, 300.0)],
                 [_cuadrado(1000.0, 0.0, 400.0)],
                 [_cuadrado(2000.0, 0.0, 500.0)]],
            )
            self.assertAlmostEqual(sum(shapefile.areas_poligonos(ruta)),
                                   shapefile.area_poligonos(ruta), places=3)

    def test_el_hueco_resta_a_su_propio_poligono(self) -> None:
        # Anillo interior en sentido antihorario. Si el signo se descartara
        # antes de sumar, el hueco engordaría el polígono en vez de vaciarlo.
        interior = list(reversed(_cuadrado(250.0, 250.0, 500.0)))
        with tempfile.TemporaryDirectory() as temporal:
            ruta = _escribir_poligonos(
                Path(temporal) / "capa.shp",
                [[_cuadrado(0.0, 0.0, 1000.0), interior],
                 [_cuadrado(5000.0, 0.0, 100.0)]],
            )
            areas = shapefile.areas_poligonos(ruta)
        self.assertAlmostEqual(areas[0], 1_000_000.0 - 250_000.0, places=3)
        self.assertAlmostEqual(areas[1], 10_000.0, places=3)


class PruebaSubcuencasPequenas(unittest.TestCase):
    def test_sin_archivo_devuelve_lista_vacia(self) -> None:
        self.assertEqual(
            m09.subcuencas_pequenas(Path("no_existe.shp"), 0.5), [])

    def test_se_detectan_sin_campo_de_area(self) -> None:
        # Sin .dbf, que es el caso de la exportación de HEC-HMS en lo que al
        # área respecta: no hay atributo del que leerla.
        with tempfile.TemporaryDirectory() as temporal:
            ruta = _escribir_poligonos(
                Path(temporal) / "subcuencas.shp",
                [[_cuadrado(0.0, 0.0, 2000.0)],      # 4 km²
                 [_cuadrado(9000.0, 0.0, 77.5)],     # 0,006 km²
                 [_cuadrado(9500.0, 0.0, 500.0)]],   # 0,25 km²
            )
            pequenas = m09.subcuencas_pequenas(ruta, 0.5)
        self.assertEqual([fila["indice"] for fila in pequenas], [1, 2])
        self.assertLess(pequenas[0]["area_km2"], 0.01)

    def test_el_listado_se_escribe_para_poder_actuar(self) -> None:
        with tempfile.TemporaryDirectory() as temporal:
            destino = m09.escribir_pequenas(
                Path(temporal) / "pequenas.csv",
                [{"indice": 3, "nombre": "W310", "area_km2": 0.006}],
            )
            lineas = destino.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(lineas[0], "indice;nombre;area_km2")
        self.assertEqual(lineas[1], "3;W310;0.006")


class PruebaContrasteConLaDrenada(unittest.TestCase):
    """
    La superficie drenada es la referencia con significado hidrológico.

    No es una divisoria, sino los puntos a menos de un radio de algún cauce que
    llega al punto. Por eso la banda es ancha: sirve para detectar el orden de
    magnitud equivocado, no para arbitrar una diferencia del veinte por ciento.
    """

    DRENADA = 305.45

    def test_el_caso_medido_esta_dentro_de_banda(self) -> None:
        # 220,60 km² delimitados en HEC-HMS frente a 305,45 km² drenados.
        resultado = m09.contrastar_con_la_drenada(220.60, self.DRENADA, 50.0)
        self.assertFalse(resultado["fuera_de_banda"])
        self.assertAlmostEqual(resultado["desviacion_pct"], -27.79, places=1)

    def test_un_orden_de_magnitud_se_detecta(self) -> None:
        resultado = m09.contrastar_con_la_drenada(990.70, self.DRENADA, 50.0)
        self.assertTrue(resultado["fuera_de_banda"])

    def test_el_area_de_influencia_no_habria_detectado_nada(self) -> None:
        # La misma delimitación de 990,70 km² pasa la cota superior sin señal:
        # es exactamente por eso que la cota superior no basta.
        cota = m09.verificar_area(990.70, 990.70, 5.0)
        self.assertFalse(cota["excede_la_preliminar"])
        self.assertFalse(cota["demasiado_pequena"])

    def test_sin_referencia_no_inventa_un_veredicto(self) -> None:
        self.assertIn("error", m09.contrastar_con_la_drenada(220.6, 0.0, 50.0))


class PruebaReferenciaDrenada(unittest.TestCase):
    """Sin los productos que la sostienen, la referencia no existe."""

    def test_sin_red_ni_reporte_devuelve_none(self) -> None:
        self.assertIsNone(m09.superficie_drenada_de_referencia(
            Path("no_existe.shp"), Path("tampoco.json"), 1400.0))

    def test_un_radio_no_declarado_devuelve_none(self) -> None:
        # Un radio de cero produciría una superficie nula y una desviación del
        # cien por cien contra cualquier delimitación correcta.
        self.assertIsNone(m09.superficie_drenada_de_referencia(
            Path("no_existe.shp"), Path("tampoco.json"), 0.0))

    def test_sin_acotado_no_hay_referencia(self) -> None:
        with tempfile.TemporaryDirectory() as temporal:
            reporte = Path(temporal) / "M02_delimitacion.json"
            reporte.write_text('{"acotado": {}}', encoding="utf-8")
            red = _escribir_poligonos(Path(temporal) / "red.shp",
                                      [[_cuadrado(0.0, 0.0, 10.0)]])
            self.assertIsNone(m09.superficie_drenada_de_referencia(
                red, reporte, 1400.0))


class PruebaConfiguracion(unittest.TestCase):
    def test_el_intercambio_esta_declarado(self) -> None:
        for clave in ("insumos", "salida", "subcuencas", "corrientes"):
            self.assertTrue(
                _CFG.obtener(f"hec_hms.intercambio.{clave}"), clave)

    def test_el_origen_de_las_corrientes_se_declara(self) -> None:
        # Se declara y no se deduce del disco: si el módulo mirase qué archivos
        # hay, la misma orden daría resultados distintos y el log no podría
        # explicar cuál se usó.
        origen = str(_CFG.obtener("hec_hms.intercambio.origen_corrientes"))
        self.assertIn(origen, ("hec_hms", "red_topologica"))

    def test_la_banda_de_area_es_ancha_pero_finita(self) -> None:
        # Estrecha, rechazaría delimitaciones correctas por contrastarlas con
        # algo que no es una divisoria. Sin tope, no detectaría nada.
        banda = float(_CFG.obtener("hec_hms.intercambio.banda_area_pct"))
        self.assertGreaterEqual(banda, 10.0)
        self.assertLessEqual(banda, 100.0)

    def test_la_politica_de_subcuencas_pequenas_se_declara(self) -> None:
        # El M09 nunca elimina ni fusiona: identifica y deja constancia del
        # criterio, que es lo que exige la sección 7 del CLAUDE.md.
        politica = str(_CFG.obtener(
            "hec_hms.intercambio.politica_subcuencas_pequenas"))
        self.assertIn(politica, ("conservar", "fusionar"))

    def test_el_esquema_declara_las_dos_claves_nuevas(self) -> None:
        # Sin esquema, un estudio que no las tenga fallaría al leerlas en
        # lugar de ser rechazado al validar la configuración.
        for clave in ("origen_corrientes", "banda_area_pct"):
            self.assertIn(f"hec_hms.intercambio.{clave}", esquema.ESQUEMA, clave)

    def test_la_fraccion_minima_es_una_cota_por_abajo(self) -> None:
        fraccion = float(_CFG.obtener("hec_hms.intercambio.fraccion_minima_pct"))
        self.assertGreater(fraccion, 0.0)
        self.assertLess(fraccion, 100.0)

    def test_los_drenajes_recortados_estan_declarados(self) -> None:
        # El M02 debe producirlos: el M09 los entrega a HEC-HMS como capa de
        # verificación del paso manual.
        for clave in ("salida_recorte_sencillo", "salida_recorte_doble"):
            self.assertTrue(_CFG.obtener(f"referencia_nacional.{clave}"), clave)


if __name__ == "__main__":
    unittest.main(verbosity=2)

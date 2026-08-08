# -*- coding: utf-8 -*-
"""
Pruebas del M09: intercambio con el paso manual de HEC-HMS.

    python tests/test_m09.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M09_hec_hms as m09  # noqa: E402
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


class PruebaSubcuencasPequenas(unittest.TestCase):
    def test_sin_archivo_devuelve_lista_vacia(self) -> None:
        self.assertEqual(
            m09.subcuencas_pequenas(Path("no_existe.shp"), 0.5), [])


class PruebaConfiguracion(unittest.TestCase):
    def test_el_intercambio_esta_declarado(self) -> None:
        for clave in ("insumos", "salida", "subcuencas", "corrientes"):
            self.assertTrue(
                _CFG.obtener(f"hec_hms.intercambio.{clave}"), clave)

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

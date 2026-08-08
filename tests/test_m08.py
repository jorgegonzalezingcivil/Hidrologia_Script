# -*- coding: utf-8 -*-
"""
Pruebas del M08 y del módulo compartido de interpolación.

Solo las funciones puras. El geoprocesamiento exige el Python de QGIS y no puede
verificarse desde el venv, pero la interpolación, su validación y la lectura de
los cuantiles sí, y son lo que decide si el mapa es defendible.

    python tests/test_m08.py
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

import M08_isoyetas_pmax as m08  # noqa: E402
import interpolacion as itp  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


class PruebaLecturaDeCuantiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ruta = self.tmp / "cuantiles.csv"
        self.ruta.write_text(
            "codigo;distribucion;metodo;n_anios;T2.33;T25;T100\n"
            "A;gumbel_max;momentos_l;36;38.6;58.7;69.4\n"
            "B;lognormal2;momentos;30;41.0;;75.0\n",
            encoding="utf-8-sig")

    def test_los_periodos_se_deducen_de_las_columnas(self) -> None:
        # Sin lista duplicada que pueda quedar desincronizada con el M07.
        periodos, _ = m08.leer_cuantiles(self.ruta, ";")
        self.assertEqual(periodos, [2.33, 25.0, 100.0])

    def test_una_estacion_sin_cuantil_no_se_rellena(self) -> None:
        # Sustituirlo por el de otro periodo metería en el campo un valor que la
        # distribución adoptada no produjo.
        _, filas = m08.leer_cuantiles(self.ruta, ";")
        utiles, sin_valor = m08.muestras_de_periodo(filas, 25.0)
        self.assertEqual(len(utiles), 1)
        self.assertEqual(sin_valor, ["B"])

    def test_conserva_la_distribucion_de_cada_estacion(self) -> None:
        _, filas = m08.leer_cuantiles(self.ruta, ";")
        utiles, _ = m08.muestras_de_periodo(filas, 100.0)
        self.assertEqual({u["codigo"]: u["distrib"] for u in utiles},
                         {"A": "gumbel_max", "B": "lognormal2"})

    def test_un_archivo_sin_periodos_es_error(self) -> None:
        ruta = self.tmp / "sin_periodos.csv"
        ruta.write_text("codigo;distribucion\nA;gumbel_max\n",
                        encoding="utf-8-sig")
        with self.assertRaises(ErrorFormato):
            m08.leer_cuantiles(ruta, ";")

    def test_archivo_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorRutas):
            m08.leer_cuantiles(self.tmp / "no_existe.csv", ";")


class PruebaNombrePeriodo(unittest.TestCase):
    def test_el_decimal_no_ensucia_el_nombre(self) -> None:
        self.assertEqual(m08.nombre_periodo(2.33), "T2_33")

    def test_el_entero_queda_limpio(self) -> None:
        self.assertEqual(m08.nombre_periodo(100.0), "T100")


class PruebaIntervaloDeCurvas(unittest.TestCase):
    """
    El rango crece con el periodo de retorno: un intervalo fijo que da ocho
    curvas en T2.33 daría dos en T500.
    """

    def test_produce_del_orden_de_ocho_curvas(self) -> None:
        intervalo = m08.intervalo_de_curvas([30.0, 110.0], objetivo=8)
        self.assertGreaterEqual(80.0 / intervalo, 4)
        self.assertLessEqual(80.0 / intervalo, 16)

    def test_se_redondea_a_un_valor_legible(self) -> None:
        # Una leyenda con intervalos de 6,375 mm no se lee.
        for extremo in (37.0, 81.0, 116.0, 250.0):
            intervalo = m08.intervalo_de_curvas([29.0, extremo])
            mantisa = intervalo / (10 ** int(f"{intervalo:e}".split("e")[1]))
            self.assertIn(round(mantisa, 6), (1.0, 2.0, 5.0), intervalo)

    def test_un_rango_mayor_da_intervalo_mayor(self) -> None:
        estrecho = m08.intervalo_de_curvas([29.0, 61.0])
        ancho = m08.intervalo_de_curvas([44.0, 300.0])
        self.assertGreater(ancho, estrecho)

    def test_valores_constantes_no_revientan(self) -> None:
        self.assertGreater(m08.intervalo_de_curvas([50.0, 50.0]), 0)

    def test_un_solo_valor(self) -> None:
        self.assertGreater(m08.intervalo_de_curvas([50.0]), 0)


class PruebaInterpolacionCompartida(unittest.TestCase):
    """
    El M06 y el M08 comparten el motor: duplicarlo garantizaría que se corrijan
    por separado.
    """

    MUESTRAS = [(0.0, 0.0, 100.0), (100.0, 0.0, 200.0),
                (0.0, 100.0, 300.0), (100.0, 100.0, 400.0)]

    def test_sobre_una_muestra_devuelve_su_valor(self) -> None:
        self.assertEqual(itp.idw(0.0, 0.0, self.MUESTRAS), 100.0)

    def test_en_el_centro_promedia_por_simetria(self) -> None:
        self.assertAlmostEqual(itp.idw(50.0, 50.0, self.MUESTRAS), 250.0)

    def test_el_radio_excluye_lo_lejano(self) -> None:
        self.assertEqual(itp.idw(10.0, 10.0, self.MUESTRAS, radio=50.0), 100.0)

    def test_un_campo_plano_se_reproduce_sin_error(self) -> None:
        muestras = [(float(i), 0.0, 500.0) for i in range(0, 500, 50)]
        self.assertAlmostEqual(
            itp.validacion_dejando_uno_fuera(muestras)["rmse_mm"], 0.0,
            places=6)

    def test_la_validacion_reporta_todas_las_cifras(self) -> None:
        muestras = [(float(i * 10), float(i * 5), 100.0 + i) for i in range(8)]
        resultado = itp.validacion_dejando_uno_fuera(muestras)
        for clave in ("rmse_mm", "mae_mm", "sesgo_mm", "rmse_relativo_pct",
                      "nash_sutcliffe"):
            self.assertIn(clave, resultado)

    def test_el_gradiente_reconoce_una_relacion_perfecta(self) -> None:
        muestras = [(float(z), 2.0 * z + 100.0) for z in range(1000, 3000, 200)]
        self.assertAlmostEqual(
            itp.gradiente_altitudinal(muestras)["r2"], 1.0, places=3)

    def test_las_curvas_se_ajustan_a_multiplos(self) -> None:
        # Si cada tanda empezara en su propio mínimo, dos mapas del mismo
        # estudio tendrían leyendas que no se corresponden.
        self.assertEqual(itp.rango_de_curvas([581.0, 1341.0], 100.0),
                         (600.0, 1300.0))

    def test_el_metodo_declarado_esta_implementado(self) -> None:
        metodo = str(_CFG.obtener("interpolacion.metodo")).upper()
        self.assertIn(metodo, itp.METODOS_IMPLEMENTADOS)


class PruebaConfiguracion(unittest.TestCase):
    def test_la_malla_es_la_de_isoyetas_y_no_la_del_terreno(self) -> None:
        self.assertGreater(float(_CFG.obtener("isoyetas.resolucion_m")),
                           float(_CFG.obtener("interpolacion.resolucion_raster_m")))

    def test_los_periodos_del_m07_son_los_del_estudio(self) -> None:
        self.assertEqual(list(_CFG.obtener("frecuencia.periodos_retorno")),
                         [2.33, 5, 10, 15, 25, 50, 100, 500])


if __name__ == "__main__":
    unittest.main(verbosity=2)

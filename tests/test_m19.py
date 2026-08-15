# -*- coding: utf-8 -*-
"""
Pruebas del M19: curva de duración de caudales.

    python tests/test_m19.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M19_duracion as m19  # noqa: E402
from comun.errores import ErrorHidrologia  # noqa: E402


class PruebaCurva(unittest.TestCase):
    """
    La posición de Weibull, m/(n+1), deja margen a ambos lados: con n el máximo
    tendría excedencia cero y el mínimo cien, y la muestra no autoriza ninguna
    de las dos cosas.
    """

    def test_ordena_de_mayor_a_menor(self) -> None:
        curva = m19.curva_de_duracion([1.0, 5.0, 3.0])
        self.assertEqual([f["caudal_m3s"] for f in curva], [5.0, 3.0, 1.0])

    def test_la_excedencia_usa_n_mas_uno(self) -> None:
        curva = m19.curva_de_duracion([3.0, 2.0, 1.0])
        self.assertAlmostEqual(curva[0]["excedencia_pct"], 25.0, places=3)
        self.assertAlmostEqual(curva[-1]["excedencia_pct"], 75.0, places=3)

    def test_ningun_extremo_llega_a_cero_ni_a_cien(self) -> None:
        curva = m19.curva_de_duracion(list(range(1, 51)))
        self.assertGreater(curva[0]["excedencia_pct"], 0.0)
        self.assertLess(curva[-1]["excedencia_pct"], 100.0)

    def test_la_excedencia_crece_con_el_orden(self) -> None:
        curva = m19.curva_de_duracion([9.0, 1.0, 5.0, 3.0])
        valores = [f["excedencia_pct"] for f in curva]
        self.assertEqual(valores, sorted(valores))

    def test_una_serie_vacia_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m19.curva_de_duracion([])

    def test_un_caudal_negativo_es_error(self) -> None:
        # No es ruido: es un fallo del módulo que alimenta.
        with self.assertRaises(ErrorHidrologia):
            m19.curva_de_duracion([1.0, -0.5, 2.0])


class PruebaInterpolacion(unittest.TestCase):
    def setUp(self) -> None:
        self.curva = m19.curva_de_duracion([float(v) for v in range(1, 100)])

    def test_en_un_punto_devuelve_su_caudal(self) -> None:
        punto = self.curva[10]
        self.assertAlmostEqual(
            m19.caudal_para_excedencia(self.curva, punto["excedencia_pct"]),
            punto["caudal_m3s"], places=6)

    def test_interpola_entre_dos_puntos(self) -> None:
        # Entre dos puntos consecutivos el valor queda estrictamente en medio.
        uno, otro = self.curva[10], self.curva[11]
        medio = (uno["excedencia_pct"] + otro["excedencia_pct"]) / 2.0
        valor = m19.caudal_para_excedencia(self.curva, medio)
        self.assertLess(valor, uno["caudal_m3s"])
        self.assertGreater(valor, otro["caudal_m3s"])

    def test_el_caudal_decrece_al_crecer_la_excedencia(self) -> None:
        self.assertGreater(m19.caudal_para_excedencia(self.curva, 10.0),
                           m19.caudal_para_excedencia(self.curva, 90.0))

    def test_fuera_del_rango_devuelve_el_extremo(self) -> None:
        # Es lo unico que la muestra autoriza a afirmar.
        self.assertAlmostEqual(m19.caudal_para_excedencia(self.curva, 0.001),
                               self.curva[0]["caudal_m3s"], places=6)
        self.assertAlmostEqual(m19.caudal_para_excedencia(self.curva, 99.999),
                               self.curva[-1]["caudal_m3s"], places=6)

    def test_una_excedencia_imposible_es_error(self) -> None:
        for valor in (0.0, 100.0, -5.0, 120.0):
            with self.assertRaises(ErrorHidrologia):
                m19.caudal_para_excedencia(self.curva, valor)

    def test_una_curva_vacia_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m19.caudal_para_excedencia([], 50.0)


class PruebaResumen(unittest.TestCase):
    def setUp(self) -> None:
        self.curva = m19.curva_de_duracion([float(v) for v in range(1, 101)])

    def test_trae_los_percentiles_declarados(self) -> None:
        resumen = m19.resumir_curva(self.curva)
        for percentil in m19.PERCENTILES:
            self.assertIn(f"Q{percentil:g}", resumen["percentiles"])

    def test_el_q50_es_la_mediana(self) -> None:
        resumen = m19.resumir_curva(self.curva)
        self.assertAlmostEqual(resumen["percentiles"]["Q50"], 50.5, delta=1.0)

    def test_el_indice_de_variabilidad_no_cambia_al_reescalar(self) -> None:
        # El factor de almacenamiento es multiplicativo y afecta por igual a
        # numerador y denominador: la FORMA del regimen no depende del ajuste.
        crudo = m19.resumir_curva(self.curva)["indice_variabilidad_q10_q90"]
        escalada = m19.curva_de_duracion(
            [f["caudal_m3s"] * 0.6303 for f in self.curva])
        self.assertAlmostEqual(
            m19.resumir_curva(escalada)["indice_variabilidad_q10_q90"],
            crudo, places=3)

    def test_cuenta_los_meses_bajo_la_media(self) -> None:
        resumen = m19.resumir_curva(self.curva)
        self.assertEqual(resumen["meses_bajo_la_media"], 50)
        self.assertAlmostEqual(resumen["fraccion_bajo_la_media"], 0.5)

    def test_sin_curva_no_inventa_resumen(self) -> None:
        self.assertEqual(m19.resumir_curva([]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

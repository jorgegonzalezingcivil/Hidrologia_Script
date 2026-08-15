# -*- coding: utf-8 -*-
"""
Pruebas del M18: precipitación del balance y balance hídrico.

    python tests/test_m18.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M18_balance as m18  # noqa: E402
from comun.errores import ErrorHidrologia  # noqa: E402


class PruebaIdw(unittest.TestCase):
    """
    Se evalúa en el destino y no sobre una malla: rellenar un ráster para luego
    promediarlo da lo mismo con más pasos y una resolución que no aporta.
    """

    FUENTES = [(0.0, 0.0, 1000.0), (100.0, 0.0, 1200.0)]

    def test_el_punto_medio_promedia(self) -> None:
        self.assertAlmostEqual(m18.idw((50.0, 0.0), self.FUENTES)["valor"],
                               1100.0, places=6)

    def test_sobre_una_fuente_devuelve_su_valor(self) -> None:
        # La distancia es cero y el peso seria infinito.
        self.assertAlmostEqual(m18.idw((0.0, 0.0), self.FUENTES)["valor"],
                               1000.0, places=6)

    def test_manda_la_cercana(self) -> None:
        cerca = m18.idw((10.0, 0.0), self.FUENTES)["valor"]
        self.assertLess(cerca, 1100.0)
        self.assertGreater(cerca, 1000.0)

    def test_nunca_extrapola(self) -> None:
        # Es la limitación que obliga a advertir cuando la cuenca tiene zonas
        # sin estaciones: la lámina de ahí queda acotada por lo de más abajo.
        for x in (-500.0, 50.0, 600.0):
            resultado = m18.idw((x, 0.0), self.FUENTES)
            self.assertGreaterEqual(resultado["valor"], resultado["minimo"])
            self.assertLessEqual(resultado["valor"], resultado["maximo"])

    def test_el_exponente_concentra_el_peso(self) -> None:
        suave = m18.idw((10.0, 0.0), self.FUENTES, exponente=1.0)["valor"]
        fuerte = m18.idw((10.0, 0.0), self.FUENTES, exponente=4.0)["valor"]
        self.assertLess(fuerte, suave)

    def test_sin_estaciones_suficientes_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18.idw((0.0, 0.0), self.FUENTES, radio_max_m=1.0,
                    minimo_estaciones=2)

    def test_sin_fuentes_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18.idw((0.0, 0.0), [])


class PruebaBalance(unittest.TestCase):
    def test_cierra_con_las_tres_formulaciones(self) -> None:
        salida = m18.balance(1000.0, 937.0, 220.31, 365.25, temperatura_c=11.2)
        for metodo in ("budyko", "dekop", "turc"):
            self.assertIn(f"etr_{metodo}_mm", salida)
            self.assertIn(f"caudal_{metodo}_m3s", salida)

    def test_la_escorrentia_es_el_residuo(self) -> None:
        salida = m18.balance(1000.0, 937.0, 220.31, 365.25)
        self.assertAlmostEqual(
            salida["etr_budyko_mm"] + salida["escorrentia_budyko_mm"],
            1000.0, places=1)

    def test_sin_temperatura_no_se_calcula_turc(self) -> None:
        # Su polinomio esta calibrado con valores anuales: la funcion no puede
        # saber la escala y por eso solo entra cuando quien llama lo garantiza.
        salida = m18.balance(1000.0, 937.0, 220.31, 365.25)
        self.assertNotIn("etr_turc_mm", salida)

    def test_mas_lluvia_da_mas_caudal(self) -> None:
        seco = m18.balance(600.0, 937.0, 220.31, 365.25)
        humedo = m18.balance(1400.0, 937.0, 220.31, 365.25)
        self.assertGreater(humedo["caudal_budyko_m3s"],
                           seco["caudal_budyko_m3s"])


class PruebaContrasteDeEscalas(unittest.TestCase):
    """
    Las dos escalas parten de la misma lluvia y la misma evapotranspiración: si
    su promedio no coincide, el informe no puede presentarlas juntas sin
    explicarlo.
    """

    def test_mide_la_diferencia(self) -> None:
        contraste = m18.contrastar_escalas(2.0, [1.8, 2.0, 2.2, 2.4])
        self.assertAlmostEqual(contraste["promedio_mensual_m3s"], 2.1)
        self.assertAlmostEqual(contraste["diferencia_pct"], 5.0)

    def test_reporta_el_rango_de_la_serie(self) -> None:
        contraste = m18.contrastar_escalas(2.0, [1.0, 2.0, 4.0])
        self.assertAlmostEqual(contraste["minimo_mensual_m3s"], 1.0)
        self.assertAlmostEqual(contraste["maximo_mensual_m3s"], 4.0)
        self.assertAlmostEqual(contraste["razon_max_min"], 4.0)

    def test_sin_serie_no_inventa_contraste(self) -> None:
        self.assertEqual(m18.contrastar_escalas(2.0, []), {})

    def test_un_minimo_nulo_no_divide_por_cero(self) -> None:
        contraste = m18.contrastar_escalas(2.0, [0.0, 2.0])
        self.assertIsNone(contraste["razon_max_min"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

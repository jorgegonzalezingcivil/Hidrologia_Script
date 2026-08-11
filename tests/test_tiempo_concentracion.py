# -*- coding: utf-8 -*-
"""
Pruebas de las fórmulas de tiempo de concentración.

    python tests/test_tiempo_concentracion.py
"""

from __future__ import annotations

import csv
import math
import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import tiempo_concentracion as tc  # noqa: E402
from comun.config import cargar  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

# Cuenca de contraste, dentro del rango de calibración de casi todas.
CUENCA = {
    "area_km2": 10.0,
    "longitud_km": 5.0,
    "pendiente": 0.05,
    "desnivel_m": 250.0,     # coherente: 0,05 * 5.000 m
    "cota_media_m": 120.0,
    "cn": 75.0,
}


class PruebaValoresConocidos(unittest.TestCase):
    """
    Cada valor se comprueba contra la expresión evaluada a mano, no contra lo
    que devolvió el código la primera vez. Una prueba que consagra la salida
    del propio programa no comprueba nada.
    """

    def test_kirpich(self) -> None:
        # 0,0195 * 5000^0,77 * 0,05^-0,385 / 60
        esperado = 0.0195 * (5000 ** 0.77) * (0.05 ** -0.385) / 60.0
        self.assertAlmostEqual(tc.kirpich(**CUENCA), esperado, places=9)

    def test_temez(self) -> None:
        esperado = 0.3 * (5.0 / (0.05 ** 0.25)) ** 0.76
        self.assertAlmostEqual(tc.temez(**CUENCA), esperado, places=9)

    def test_giandotti(self) -> None:
        esperado = (4 * math.sqrt(10.0) + 1.5 * 5.0) / (0.8 * math.sqrt(120.0))
        self.assertAlmostEqual(tc.giandotti(**CUENCA), esperado, places=9)

    def test_ventura(self) -> None:
        self.assertAlmostEqual(tc.ventura(**CUENCA),
                               0.05 * math.sqrt(10.0 / 0.05), places=9)

    def test_pilgrim(self) -> None:
        self.assertAlmostEqual(tc.pilgrim(**CUENCA), 0.76 * 10.0 ** 0.38,
                               places=9)

    def test_v_te_chow(self) -> None:
        esperado = 0.1602 * (5.0 / math.sqrt(0.05)) ** 0.64
        self.assertAlmostEqual(tc.v_te_chow(**CUENCA), esperado, places=9)

    def test_scs_lag(self) -> None:
        pies = 5000.0 / 0.3048
        retencion = 1000.0 / 75.0 - 10.0
        rezago = ((pies ** 0.8) * ((retencion + 1.0) ** 0.7)
                  / (1900.0 * math.sqrt(5.0)))
        self.assertAlmostEqual(tc.scs_lag(**CUENCA), rezago / 0.6, places=9)

    def test_california_coincide_con_kirpich_por_construccion(self) -> None:
        """
        California reescribe Kirpich con el desnivel en lugar de la pendiente:
        (L^3/H)^0,385 = (L^2/S)^0,385 = L^0,77 * S^-0,385 cuando S = H/L.

        Que las dos coincidan cuando el desnivel es coherente con la pendiente
        es la comprobación cruzada más fuerte que admite esta familia: si una
        de las dos tuviera mal una conversión de unidades, discreparían.
        """
        self.assertAlmostEqual(tc.california(**CUENCA), tc.kirpich(**CUENCA),
                               places=6)


class PruebaRobustez(unittest.TestCase):
    def test_la_pendiente_nula_no_devuelve_infinito(self) -> None:
        # Casi todas la llevan en el denominador. Propagar un infinito lo
        # convertiria mas adelante en un numero con apariencia normal.
        datos = dict(CUENCA, pendiente=0.0)
        for nombre in ("kirpich", "temez", "ventura", "passini", "bransby",
                       "johnstone", "clark", "valencia", "v_te_chow"):
            with self.subTest(formula=nombre):
                self.assertIsNone(tc.FORMULAS[nombre](**datos))

    def test_las_magnitudes_negativas_se_rechazan(self) -> None:
        datos = dict(CUENCA, longitud_km=-1.0)
        self.assertIsNone(tc.kirpich(**datos))

    def test_una_magnitud_ausente_se_rechaza(self) -> None:
        valor, motivo = tc.calcular("giandotti", area_km2=10.0, longitud_km=5.0)
        self.assertIsNone(valor)
        self.assertIn("cota_media_m", motivo)

    def test_sin_numero_de_curva_no_hay_scs_lag(self) -> None:
        datos = dict(CUENCA)
        datos.pop("cn")
        valor, motivo = tc.calcular("scs_lag", **datos)
        self.assertIsNone(valor)
        self.assertIn("cn", motivo)

    def test_una_formula_inexistente_se_declara(self) -> None:
        valor, motivo = tc.calcular("inventada", **CUENCA)
        self.assertIsNone(valor)
        self.assertIn("implementación", motivo)

    def test_todas_dan_un_valor_positivo_en_la_cuenca_de_contraste(self) -> None:
        resultado = tc.calcular_todas(**CUENCA)
        sin_valor = [n for n, r in resultado.items() if r["horas"] is None]
        self.assertEqual(sin_valor, [], f"sin valor: {sin_valor}")
        for nombre, datos in resultado.items():
            with self.subTest(formula=nombre):
                self.assertGreater(datos["horas"], 0.0)
                self.assertAlmostEqual(datos["minutos"], datos["horas"] * 60.0,
                                       places=1)


class PruebaCorrespondenciaConLaMatriz(unittest.TestCase):
    """
    La matriz guarda los rangos de calibración y el código el álgebra. Si las
    dos mitades se desincronizan, una fórmula podría declararse aplicable y no
    calcularse nunca, o al revés.
    """

    def setUp(self) -> None:
        ruta = _RAIZ_REPO / _CFG.obtener(
            "tiempo_concentracion.tabla_aplicabilidad")
        with ruta.open(encoding="utf-8-sig") as manejador:
            self.filas = list(csv.DictReader(manejador, delimiter=";"))
        self.claves = {f["formula"].strip() for f in self.filas}

    def test_toda_formula_de_la_matriz_tiene_implementacion(self) -> None:
        self.assertEqual(self.claves - set(tc.FORMULAS), set())

    def test_toda_implementacion_esta_en_la_matriz(self) -> None:
        self.assertEqual(set(tc.FORMULAS) - self.claves, set())

    def test_toda_formula_declara_sus_requisitos(self) -> None:
        self.assertEqual(set(tc.FORMULAS), set(tc.REQUISITOS))


class PruebaEstadisticos(unittest.TestCase):
    def test_mediana_de_un_numero_par_de_valores(self) -> None:
        resumen = tc.estadisticos([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(resumen["mediana"], 2.5)
        self.assertEqual(resumen["n"], 4)

    def test_mediana_de_un_numero_impar(self) -> None:
        self.assertAlmostEqual(tc.estadisticos([1.0, 5.0, 3.0])["mediana"], 3.0)

    def test_descarta_los_no_calculados(self) -> None:
        resumen = tc.estadisticos([2.0, None, 4.0, float("nan"), -1.0])
        self.assertEqual(resumen["n"], 2)
        self.assertAlmostEqual(resumen["mediana"], 3.0)

    def test_la_dispersion_se_reporta(self) -> None:
        resumen = tc.estadisticos([10.0, 20.0, 30.0])
        self.assertAlmostEqual(resumen["media"], 20.0)
        self.assertAlmostEqual(resumen["razon_extremos"], 3.0)
        self.assertGreater(resumen["cv"], 0.0)

    def test_sin_valores_no_inventa_nada(self) -> None:
        resumen = tc.estadisticos([None, None])
        self.assertEqual(resumen["n"], 0)
        self.assertIsNone(resumen["mediana"])
        self.assertIsNone(resumen["cv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

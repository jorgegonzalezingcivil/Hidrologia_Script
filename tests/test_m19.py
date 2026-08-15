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


class PruebaIrh(unittest.TestCase):
    """
    Mide cuánto del agua se entrega de forma sostenida en lugar de concentrarse
    en unos pocos meses de crecida.
    """

    def test_un_caudal_constante_da_indice_uno(self) -> None:
        # Todo el caudal es igual a la media: regulación perfecta.
        curva = m19.curva_de_duracion([5.0] * 50)
        self.assertAlmostEqual(m19.indice_de_retencion(curva)["irh"], 1.0,
                               places=4)

    def test_un_regimen_torrencial_da_indice_bajo(self) -> None:
        # Un mes enorme y el resto casi seco: casi toda el área está por encima
        # de la media en ese punto, y el índice cae.
        curva = m19.curva_de_duracion([1000.0] + [1.0] * 99)
        self.assertLess(m19.indice_de_retencion(curva)["irh"], 0.3)

    def test_no_cambia_al_reescalar(self) -> None:
        # El factor de almacenamiento multiplica numerador y denominador por
        # igual: la regulación es una forma del régimen, no un nivel.
        caudales = [float(v) for v in range(1, 61)]
        uno = m19.indice_de_retencion(m19.curva_de_duracion(caudales))["irh"]
        otro = m19.indice_de_retencion(
            m19.curva_de_duracion([c * 0.6303 for c in caudales]))["irh"]
        self.assertAlmostEqual(uno, otro, places=6)

    def test_esta_entre_cero_y_uno(self) -> None:
        for caudales in ([1.0, 2.0, 3.0], [100.0, 1.0, 1.0, 1.0],
                         [5.0, 5.0, 5.1]):
            indice = m19.indice_de_retencion(
                m19.curva_de_duracion(caudales))["irh"]
            self.assertGreaterEqual(indice, 0.0)
            self.assertLessEqual(indice, 1.0)

    def test_las_categorias_siguen_los_rangos_del_ideam(self) -> None:
        self.assertEqual(m19.categoria_de_irh(0.90), "muy alta")
        self.assertEqual(m19.categoria_de_irh(0.80), "alta")
        self.assertEqual(m19.categoria_de_irh(0.70), "moderada")
        self.assertEqual(m19.categoria_de_irh(0.55), "baja")
        self.assertEqual(m19.categoria_de_irh(0.30), "muy baja")

    def test_con_un_solo_punto_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m19.indice_de_retencion(m19.curva_de_duracion([1.0]))

    def test_una_curva_toda_en_cero_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m19.indice_de_retencion(m19.curva_de_duracion([0.0] * 10))


class PruebaCaudalAmbiental(unittest.TestCase):
    """
    Una cuenca que regula mal entrega su agua a golpes: sus estiajes son más
    profundos y el ecosistema depende más de que se le reserve caudal.
    """

    def setUp(self) -> None:
        self.curva = m19.curva_de_duracion([float(v) for v in range(1, 101)])

    def test_un_irh_bajo_reserva_mas_caudal(self) -> None:
        # El percentil 75 da un caudal MAYOR que el 85: menos regulación,
        # más reserva.
        poco = m19.caudal_ambiental(self.curva, 0.5, 0.7, 75.0, 85.0)
        mucho = m19.caudal_ambiental(self.curva, 0.9, 0.7, 75.0, 85.0)
        self.assertGreater(poco["qirh_m3s"], mucho["qirh_m3s"])
        self.assertEqual(poco["percentil_aplicado"], 75.0)
        self.assertEqual(mucho["percentil_aplicado"], 85.0)

    def test_el_umbral_pertenece_al_lado_alto(self) -> None:
        justo = m19.caudal_ambiental(self.curva, 0.7, 0.7, 75.0, 85.0)
        self.assertEqual(justo["percentil_aplicado"], 85.0)

    def test_los_dos_metodos_se_calculan_siempre(self) -> None:
        # Adoptar uno en silencio no es defendible: la diferencia decide
        # cuánta agua queda para el proyecto.
        salida = m19.caudal_ambiental(self.curva, 0.5, 0.7, 75.0, 85.0)
        self.assertIn("q95_m3s", salida)
        self.assertIn("qirh_m3s", salida)

    def test_el_adoptado_es_el_declarado(self) -> None:
        salida = m19.caudal_ambiental(self.curva, 0.5, 0.7, 75.0, 85.0,
                                      metodo_adoptado="q95")
        self.assertAlmostEqual(salida["caudal_ambiental_m3s"],
                               salida["q95_m3s"], places=6)

    def test_un_metodo_desconocido_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m19.caudal_ambiental(self.curva, 0.5, 0.7, 75.0, 85.0,
                                 metodo_adoptado="otro")


class PruebaDisponible(unittest.TestCase):
    def test_es_la_resta(self) -> None:
        salida = m19.caudal_disponible(2.488, 0.7007)
        self.assertAlmostEqual(salida["caudal_disponible_m3s"], 1.7873,
                               places=4)
        self.assertAlmostEqual(salida["reserva_pct"], 28.16, places=1)

    def test_nunca_es_negativo_y_lo_senala(self) -> None:
        # Un ambiental por encima del medio significa que no hay agua para el
        # proyecto: una resta negativa lo escondería.
        salida = m19.caudal_disponible(1.0, 1.5)
        self.assertEqual(salida["caudal_disponible_m3s"], 0.0)
        self.assertTrue(salida["sin_disponibilidad"])

    def test_sin_caudal_medio_no_hay_porcentaje(self) -> None:
        self.assertIsNone(m19.caudal_disponible(0.0, 0.0)["reserva_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

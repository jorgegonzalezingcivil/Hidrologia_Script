# -*- coding: utf-8 -*-
"""
Pruebas del M14c: verificación de crecientes contra caudal observado.

    python tests/test_m14c.py
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M14c_verificacion as m14c  # noqa: E402
import frecuencia as fr  # noqa: E402


class PruebaMediaMovil(unittest.TestCase):
    """
    El equivalente en el modelo del dato observado.

    El limnígrafo reporta el mayor de los caudales MEDIOS DIARIOS; el modelo
    produce un hidrograma cuyo pico dura minutos. Comparar el pico contra una
    media diaria daría un sesgo de un factor de diez, medido en este estudio.
    """

    def test_una_serie_constante_da_ese_valor(self) -> None:
        # 48 h de caudal constante: cualquier ventana de 24 h da lo mismo.
        serie = [7.0] * (48 * 60 // 30)
        self.assertAlmostEqual(
            m14c.media_movil_maxima(serie, paso_min=30.0), 7.0)

    def test_encuentra_la_ventana_de_mayor_media(self) -> None:
        # Un dia en cero y el siguiente en diez: la mejor ventana es el segundo.
        por_dia = 24 * 60 // 60          # paso de 60 min
        serie = [0.0] * por_dia + [10.0] * por_dia
        self.assertAlmostEqual(
            m14c.media_movil_maxima(serie, paso_min=60.0), 10.0)

    def test_el_pico_se_diluye_al_promediar(self) -> None:
        # Una punta de una hora en 24 h de calma: la media es muy inferior al
        # pico. Es exactamente el efecto que obliga a promediar el modelo.
        serie = [0.0] * 23 + [240.0]
        media = m14c.media_movil_maxima(serie, paso_min=60.0)
        self.assertAlmostEqual(media, 10.0)
        self.assertGreater(max(serie) / media, 20)

    def test_un_hidrograma_mas_corto_que_la_ventana_no_tiene_media(self) -> None:
        # Devolver la media de lo que hay daria un numero que PARECE comparable
        # y no lo es: es el caso que obligo a ampliar la ventana a 36 h.
        serie = [1.0] * 12               # 12 h con paso de 60 min
        self.assertIsNone(m14c.media_movil_maxima(serie, paso_min=60.0))

    def test_un_paso_no_positivo_es_error(self) -> None:
        with self.assertRaises(ValueError):
            m14c.media_movil_maxima([1.0, 2.0], paso_min=0.0)


class PruebaPeriodosSostenidos(unittest.TestCase):

    PERIODOS = [2.33, 5, 10, 15, 25, 50, 100, 500]

    def test_recorta_lo_que_es_extrapolacion(self) -> None:
        # Con 29 anios y factor 2 se llega a Tr 58: 100 y 500 quedan fuera.
        sostenidos = m14c.periodos_sostenidos(29, self.PERIODOS, 2.0)
        self.assertEqual(sostenidos, [2.33, 5.0, 10.0, 15.0, 25.0, 50.0])

    def test_una_serie_corta_sostiene_muy_poco(self) -> None:
        self.assertEqual(m14c.periodos_sostenidos(15, self.PERIODOS, 2.0),
                         [2.33, 5.0, 10.0, 15.0, 25.0])

    def test_sin_registro_no_sostiene_nada(self) -> None:
        self.assertEqual(m14c.periodos_sostenidos(0, self.PERIODOS, 2.0), [])


class PruebaBanda(unittest.TestCase):

    def test_dentro_incluye_los_extremos(self) -> None:
        self.assertTrue(m14c.dentro_de_la_banda(5.0, 5.0, 9.0))
        self.assertTrue(m14c.dentro_de_la_banda(9.0, 5.0, 9.0))

    def test_fuera_por_arriba_y_por_abajo(self) -> None:
        self.assertFalse(m14c.dentro_de_la_banda(9.1, 5.0, 9.0))
        self.assertFalse(m14c.dentro_de_la_banda(4.9, 5.0, 9.0))


class PruebaEmparejamiento(unittest.TestCase):
    """
    Se busca la union mas proxima, no se declara una lista fija.

    Una lista serviria a un estudio y a ninguno mas. Sobre el estudio real la
    busqueda encontro TRES estaciones, una mas de las identificadas a mano.
    """

    UNIONES = {"J1": (0.0, 0.0), "J2": (1000.0, 0.0), "J3": (5000.0, 5000.0)}

    def test_empareja_con_la_mas_proxima(self) -> None:
        parejas, sin = m14c.emparejar_con_uniones(
            [("A", "EST A", 1100.0, 0.0)], self.UNIONES, 500.0)
        self.assertEqual(len(parejas), 1)
        self.assertEqual(parejas[0].union, "J2")
        self.assertAlmostEqual(parejas[0].distancia_m, 100.0)
        self.assertEqual(sin, [])

    def test_no_fuerza_una_union_lejana(self) -> None:
        # Compararia el modelo en un sitio contra la medida de otro, con areas
        # drenadas distintas.
        parejas, sin = m14c.emparejar_con_uniones(
            [("B", "EST B", 3000.0, 0.0)], self.UNIONES, 500.0)
        self.assertEqual(parejas, [])
        self.assertEqual(len(sin), 1)
        self.assertEqual(sin[0]["union_mas_cercana"], "J2")
        self.assertGreater(sin[0]["distancia_m"], 500.0)

    def test_un_modelo_sin_uniones_se_reporta(self) -> None:
        parejas, sin = m14c.emparejar_con_uniones(
            [("C", "EST C", 0.0, 0.0)], {}, 500.0)
        self.assertEqual(parejas, [])
        self.assertIn("uniones", sin[0]["motivo"])


class PruebaMaximosAnualesDeCaudal(unittest.TestCase):

    def test_exige_los_doce_meses(self) -> None:
        # Un anio al que le falta la temporada de lluvias daria un maximo que no
        # es comparable con los demas de la muestra.
        completo = {m: float(m) for m in range(1, 13)}
        incompleto = {m: 99.0 for m in range(1, 12)}
        maximos = m14c.maximos_anuales_de_mensuales(
            {2019: completo, 2020: incompleto})
        self.assertEqual(maximos, {2019: 12.0})


class PruebaBandaDeConfianza(unittest.TestCase):
    """El bootstrap que sustituye a treinta formulas analiticas."""

    @classmethod
    def setUpClass(cls) -> None:
        import numpy as np
        generador = np.random.default_rng(7)
        cls.datos = list(generador.gumbel(loc=20.0, scale=5.0, size=30))

    def _banda(self, periodos=(2.33, 25.0)):
        return fr.banda_confianza(self.datos, "gumbel_max", "momentos_l",
                                  periodos, repeticiones=200)

    def test_el_cuantil_cae_dentro_de_su_banda(self) -> None:
        for periodo, dato in self._banda().items():
            self.assertLessEqual(dato["inferior"], dato["cuantil"])
            self.assertLessEqual(dato["cuantil"], dato["superior"])

    def test_es_reproducible_entre_corridas(self) -> None:
        # Una banda que cambia sola entre corridas es indefendible ante una
        # revision, aunque la diferencia sea pequena.
        self.assertEqual(self._banda(), self._banda())

    def test_la_banda_se_ensancha_con_el_periodo(self) -> None:
        banda = self._banda((2.33, 100.0))
        estrecha = banda[2.33]["superior"] - banda[2.33]["inferior"]
        ancha = banda[100.0]["superior"] - banda[100.0]["inferior"]
        self.assertGreater(ancha, estrecha)

    def test_una_distribucion_que_no_ajusta_no_inventa_banda(self) -> None:
        self.assertEqual(
            fr.banda_confianza(self.datos, "no_existe", "momentos_l", [10.0]),
            {})

    def test_rechaza_una_confianza_imposible(self) -> None:
        with self.assertRaises(fr.ErrorFrecuencia):
            fr.banda_confianza(self.datos, "gumbel_max", "momentos_l", [10.0],
                               confianza=1.5)

    def test_rechaza_una_muestra_insuficiente(self) -> None:
        with self.assertRaises(fr.ErrorFrecuencia):
            fr.banda_confianza([1.0, 2.0], "gumbel_max", "momentos_l", [10.0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

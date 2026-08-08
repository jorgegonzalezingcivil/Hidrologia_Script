# -*- coding: utf-8 -*-
"""
Pruebas de las pruebas estadísticas.

Se verifican contra series construidas, donde la respuesta correcta se conoce de
antemano: una serie con quiebre en un punto sabido, una con tendencia de
pendiente sabida, una homogénea. Es la única forma de comprobar que una prueba
de hipótesis hace lo que dice.

    python tests/test_estadistica.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

try:
    import numpy as np
    import estadistica as est
    HAY_NUMPY = True
except ImportError:  # pragma: no cover
    HAY_NUMPY = False


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaLimitesAnomalos(unittest.TestCase):
    """
    Los defectos de la rutina heredada, fijados como regresión.

    CLAUDE.md, sección 9: cuartiles fuera de norma y signo del límite inferior.
    """

    def test_los_cuartiles_por_defecto_son_los_normativos(self) -> None:
        # La rutina heredada usaba 0.08 y 0.95, que ensanchan tanto el rango
        # que la prueba deja de filtrar.
        datos = list(range(1, 21)) + [500]
        estrictos = est.limites_iqr(datos)
        laxos = est.limites_iqr(datos, q1=0.08, q3=0.95)
        self.assertLess(estrictos.superior, laxos.superior)
        self.assertTrue(est.marcar_anomalos(datos, estrictos)[-1])

    def test_el_limite_inferior_se_recorta_al_minimo_fisico(self) -> None:
        # Sin recorte el límite sale negativo, y con tratamiento 'cap' la
        # rutina heredada escribía precipitación negativa en la serie.
        lluvia = [0.0, 0.0, 5.0, 12.0, 30.0, 45.0, 300.0]
        sin_recorte = est.limites_iqr(lluvia)
        con_recorte = est.limites_iqr(lluvia, valor_minimo=0.0)
        self.assertLess(sin_recorte.inferior, 0.0)
        self.assertEqual(con_recorte.inferior, 0.0)
        self.assertTrue(con_recorte.recortado_en_minimo)

    def test_un_limite_negativo_no_marca_nada_por_abajo(self) -> None:
        lluvia = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
        limites = est.limites_iqr(lluvia, valor_minimo=0.0)
        self.assertFalse(est.marcar_anomalos(lluvia, limites).any())

    def test_los_nulos_nunca_se_marcan(self) -> None:
        limites = est.LimitesAnomalos("IQR", 0.0, 10.0)
        marcados = est.marcar_anomalos([1.0, float("nan"), 99.0], limites)
        self.assertFalse(bool(marcados[1]))
        self.assertTrue(bool(marcados[2]))

    def test_er_y_zscore_coinciden_con_el_mismo_factor(self) -> None:
        datos = [10.0, 12.0, 11.0, 40.0, 9.0, 13.0]
        self.assertAlmostEqual(est.limites_er(datos, k=3.0).superior,
                               est.limites_zscore(datos, umbral=3.0).superior)

    def test_muestra_insuficiente_es_error_explicito(self) -> None:
        with self.assertRaises(est.ErrorEstadistica):
            est.limites_iqr([1.0, 2.0])


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaHomogeneidad(unittest.TestCase):
    def setUp(self) -> None:
        generador = np.random.default_rng(42)
        self.con_quiebre = np.concatenate([
            generador.normal(100, 10, 30), generador.normal(160, 10, 30)])
        self.homogenea = generador.normal(100, 10, 60)

    def test_pettitt_encuentra_el_quiebre_donde_esta(self) -> None:
        resultado = est.pettitt(self.con_quiebre)
        self.assertTrue(resultado.hay_indicio)
        self.assertAlmostEqual(resultado.detalle["indice_quiebre"], 29, delta=2)

    def test_pettitt_no_inventa_quiebre_en_serie_homogenea(self) -> None:
        self.assertFalse(est.pettitt(self.homogenea).hay_indicio)

    def test_snht_encuentra_el_quiebre(self) -> None:
        resultado = est.snht(self.con_quiebre)
        self.assertTrue(resultado.hay_indicio)
        self.assertGreater(resultado.estadistico, resultado.detalle["critico"])

    def test_snht_no_senala_serie_homogenea(self) -> None:
        self.assertFalse(est.snht(self.homogenea).hay_indicio)

    def test_snht_rechaza_serie_constante(self) -> None:
        with self.assertRaises(est.ErrorEstadistica):
            est.snht([5.0] * 20)

    def test_el_critico_crece_con_la_muestra(self) -> None:
        self.assertLess(est._critico_snht(10, 0.05), est._critico_snht(100, 0.05))

    def test_el_critico_al_uno_por_ciento_es_mayor(self) -> None:
        self.assertGreater(est._critico_snht(50, 0.01), est._critico_snht(50, 0.05))

    def test_muestra_corta_es_error_explicito(self) -> None:
        for funcion in (est.pettitt, est.snht, est.rachas):
            with self.assertRaises(est.ErrorEstadistica):
                funcion([1.0, 2.0, 3.0])


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaTendencia(unittest.TestCase):
    def test_mann_kendall_detecta_tendencia_y_estima_la_pendiente(self) -> None:
        generador = np.random.default_rng(7)
        serie = np.arange(40) * 0.5 + generador.normal(0, 1, 40)
        resultado = est.mann_kendall(serie)
        self.assertTrue(resultado.hay_indicio)
        self.assertEqual(resultado.detalle["sentido"], "creciente")
        self.assertAlmostEqual(resultado.detalle["pendiente_sen"], 0.5, delta=0.1)

    def test_reconoce_el_sentido_decreciente(self) -> None:
        resultado = est.mann_kendall(list(range(40, 0, -1)))
        self.assertEqual(resultado.detalle["sentido"], "decreciente")
        self.assertLess(resultado.detalle["pendiente_sen"], 0)

    def test_no_senala_serie_sin_tendencia(self) -> None:
        generador = np.random.default_rng(3)
        self.assertFalse(est.mann_kendall(generador.normal(0, 1, 60)).hay_indicio)

    def test_los_empates_no_revientan(self) -> None:
        # Una serie de precipitación mensual tiene muchos ceros.
        serie = [0.0] * 20 + [5.0, 8.0, 3.0, 0.0, 12.0]
        self.assertIsInstance(est.mann_kendall(serie), est.Resultado)


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaRachas(unittest.TestCase):
    def test_detecta_persistencia(self) -> None:
        # Dos bloques: pocas rachas, mucha persistencia.
        resultado = est.rachas([1.0] * 15 + [9.0] * 15)
        self.assertTrue(resultado.hay_indicio)
        self.assertEqual(resultado.detalle["lectura"], "persistencia")

    def test_detecta_alternancia(self) -> None:
        resultado = est.rachas([1.0, 9.0] * 15)
        self.assertTrue(resultado.hay_indicio)
        self.assertEqual(resultado.detalle["lectura"], "alternancia")

    def test_serie_aleatoria_no_se_senala(self) -> None:
        generador = np.random.default_rng(11)
        self.assertFalse(est.rachas(generador.normal(0, 1, 100)).hay_indicio)

    def test_todo_al_mismo_lado_es_error(self) -> None:
        with self.assertRaises(est.ErrorEstadistica):
            est.rachas([5.0] * 20)


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaDobleMasa(unittest.TestCase):
    def test_solo_acumula_periodos_comunes(self) -> None:
        # Acumular con huecos desplaza la curva y produce quiebres falsos.
        estacion = [10.0, float("nan"), 30.0, 40.0]
        patron = [10.0, 20.0, 30.0, float("nan")]
        acum_e, acum_p = est.curva_doble_masa(estacion, patron)
        self.assertEqual(list(acum_e), [10.0, 40.0])
        self.assertEqual(list(acum_p), [10.0, 40.0])

    def test_longitudes_distintas_son_error(self) -> None:
        with self.assertRaises(est.ErrorEstadistica):
            est.curva_doble_masa([1.0, 2.0], [1.0])

    def test_una_serie_proporcional_no_tiene_quiebre(self) -> None:
        patron = np.cumsum(np.full(40, 100.0))
        estacion = patron * 1.2
        self.assertFalse(est.quiebre_doble_masa(estacion, patron)["hay_quiebre"])

    def test_un_cambio_de_pendiente_se_detecta(self) -> None:
        base = np.full(40, 100.0)
        propia = np.concatenate([base[:20] * 1.0, base[20:] * 1.8])
        resultado = est.quiebre_doble_masa(np.cumsum(propia), np.cumsum(base))
        self.assertTrue(resultado["hay_quiebre"])
        self.assertAlmostEqual(resultado["indice"], 20, delta=2)
        self.assertGreater(resultado["razon_pendientes"], 1.5)

    def test_serie_corta_no_inventa_quiebre(self) -> None:
        resultado = est.quiebre_doble_masa([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        self.assertFalse(resultado["hay_quiebre"])
        self.assertIsNone(resultado["indice"])


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaCorrelacion(unittest.TestCase):
    def test_devuelve_cuantos_periodos_la_sustentan(self) -> None:
        # Una correlación de 0,95 sobre seis meses no dice lo mismo que sobre
        # veinte años, y el conteo es lo único que los distingue.
        a = [1.0, 2.0, 3.0, 4.0, float("nan")]
        b = [2.0, 4.0, 6.0, 8.0, 10.0]
        valor, cuantos = est.correlacion_pareada(a, b, minimo_comun=4)
        self.assertAlmostEqual(valor, 1.0)
        self.assertEqual(cuantos, 4)

    def test_muestra_insuficiente_devuelve_nan_con_su_conteo(self) -> None:
        valor, cuantos = est.correlacion_pareada([1.0, 2.0], [2.0, 4.0])
        self.assertTrue(np.isnan(valor))
        self.assertEqual(cuantos, 2)

    def test_serie_constante_no_tiene_correlacion(self) -> None:
        valor, _ = est.correlacion_pareada([5.0] * 20, list(range(20)))
        self.assertTrue(np.isnan(valor))


if __name__ == "__main__":
    unittest.main(verbosity=2)

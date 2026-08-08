# -*- coding: utf-8 -*-
"""
Pruebas del M05: construcción de la serie mensual, anómalos y complemento.

    python tests/test_m05.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun.config import cargar  # noqa: E402

try:
    import numpy as np
    import M05_precipitacion_mensual as m05
    HAY_NUMPY = True
except ImportError:  # pragma: no cover
    HAY_NUMPY = False

_CFG = cargar(raiz=_RAIZ_REPO)


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaAgregacionMensual(unittest.TestCase):
    """
    Totalizar la diaria exige un umbral de completitud.

    CLAUDE.md, sección 7: sumar los días presentes sin ese control subestima los
    meses incompletos. Un mes al que le falten diez días de temporada de lluvias
    entraría al análisis como un mes seco que nunca existió.
    """

    def test_un_mes_completo_se_totaliza(self) -> None:
        dias = {(2020, 1): [1.0] * 31}
        mensual, rechazados = m05.agregar_diaria_a_mensual(dias, 3)
        self.assertEqual(mensual[(2020, 1)], 31.0)
        self.assertEqual(rechazados, 0)

    def test_un_mes_al_que_le_faltan_pocos_dias_se_admite(self) -> None:
        mensual, rechazados = m05.agregar_diaria_a_mensual(
            {(2020, 1): [1.0] * 29}, 3)
        self.assertIn((2020, 1), mensual)
        self.assertEqual(rechazados, 0)

    def test_un_mes_incompleto_se_rechaza_y_no_se_suma(self) -> None:
        mensual, rechazados = m05.agregar_diaria_a_mensual(
            {(2020, 1): [1.0] * 20}, 3)
        self.assertNotIn((2020, 1), mensual)
        self.assertEqual(rechazados, 1)

    def test_el_bisiesto_cambia_el_denominador(self) -> None:
        self.assertEqual(m05.dias_del_mes(2020, 2), 29)
        self.assertEqual(m05.dias_del_mes(2021, 2), 28)
        # 28 días en febrero bisiesto son 1 faltante: admitido con tolerancia 3.
        mensual, _ = m05.agregar_diaria_a_mensual({(2020, 2): [1.0] * 28}, 3)
        self.assertIn((2020, 2), mensual)

    def test_tolerancia_cero_exige_el_mes_entero(self) -> None:
        mensual, rechazados = m05.agregar_diaria_a_mensual(
            {(2021, 4): [1.0] * 29}, 0)
        self.assertEqual(mensual, {})
        self.assertEqual(rechazados, 1)


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaFusionDeFuentes(unittest.TestCase):
    """La mensual del IDEAM manda; la agregación solo rellena."""

    MENSUAL = {(2000, 1): 100.0, (2000, 2): 80.0}
    AGREGADO = {(2000, 1): 95.0, (2000, 3): 120.0}

    def test_la_primaria_no_se_sobreescribe(self) -> None:
        serie = m05.construir_serie("X", self.MENSUAL, self.AGREGADO, True)
        self.assertEqual(serie.valores[(2000, 1)], 100.0)
        self.assertEqual(serie.origen[(2000, 1)], m05.ORIGEN_MENSUAL)

    def test_la_agregacion_rellena_los_huecos(self) -> None:
        serie = m05.construir_serie("X", self.MENSUAL, self.AGREGADO, True)
        self.assertEqual(serie.valores[(2000, 3)], 120.0)
        self.assertEqual(serie.origen[(2000, 3)], m05.ORIGEN_AGREGADO)

    def test_sin_completar_solo_queda_la_primaria(self) -> None:
        serie = m05.construir_serie("X", self.MENSUAL, self.AGREGADO, False)
        self.assertNotIn((2000, 3), serie.valores)

    def test_la_discrepancia_se_reporta_y_no_se_corrige(self) -> None:
        # 100 frente a 95 es un 5% justo; se usa una diferencia mayor.
        discrepancias = m05.comparar_fuentes(
            {(2000, 1): 100.0}, {(2000, 1): 80.0})
        self.assertEqual(len(discrepancias), 1)
        self.assertAlmostEqual(discrepancias[0]["diferencia_rel"], 0.20)

    def test_meses_coincidentes_no_se_reportan(self) -> None:
        self.assertEqual(
            m05.comparar_fuentes({(2000, 1): 100.0}, {(2000, 1): 101.0}), [])

    def test_solo_se_comparan_los_meses_comunes(self) -> None:
        self.assertEqual(
            m05.comparar_fuentes({(2000, 1): 100.0}, {(2000, 2): 500.0}), [])


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaAnomalosPorMes(unittest.TestCase):
    """
    La detección compara cada mes con los mismos meses de otros años.

    Es la corrección de fondo sobre la rutina heredada. En un régimen bimodal,
    un solo rango para los doce meses marcaría toda la temporada húmeda.
    """

    def setUp(self) -> None:
        # Régimen bimodal: abril y octubre húmedos, enero y julio secos.
        self.serie = m05.SerieMensual("X")
        humedos = {4: 180.0, 5: 150.0, 10: 190.0, 11: 160.0}
        for anio in range(1990, 2021):
            for mes in range(1, 13):
                base = humedos.get(mes, 25.0)
                self.serie.fijar(anio, mes, base + (anio % 5) * 2.0,
                                 m05.ORIGEN_MENSUAL)

    def test_la_temporada_humeda_no_se_marca_como_anomala(self) -> None:
        marcados = m05.detectar_anomalos_por_mes(self.serie, "IQR", _CFG)
        self.assertEqual([m for m in marcados if m["mes"] in (4, 10)], [])

    def test_un_valor_extremo_dentro_de_su_mes_si_se_marca(self) -> None:
        self.serie.fijar(2015, 1, 900.0, m05.ORIGEN_MENSUAL)
        marcados = m05.detectar_anomalos_por_mes(self.serie, "IQR", _CFG)
        self.assertTrue(any(m["anio"] == 2015 and m["mes"] == 1
                            for m in marcados))

    def test_un_mes_con_pocos_datos_se_omite_sin_fallar(self) -> None:
        corta = m05.SerieMensual("Y")
        corta.fijar(2000, 1, 10.0, m05.ORIGEN_MENSUAL)
        self.assertEqual(m05.detectar_anomalos_por_mes(corta, "IQR", _CFG), [])

    def test_metodo_desconocido_es_error_explicito(self) -> None:
        from comun.errores import ErrorConfiguracion
        with self.assertRaises(ErrorConfiguracion):
            m05.limites_de_metodo([1.0, 2.0, 3.0, 4.0, 5.0], "INVENTADO", _CFG)


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaTotalesAnuales(unittest.TestCase):
    def test_solo_cuentan_los_anios_completos(self) -> None:
        serie = m05.SerieMensual("X")
        for mes in range(1, 13):
            serie.fijar(2000, mes, 10.0, m05.ORIGEN_MENSUAL)
        for mes in range(1, 7):
            serie.fijar(2001, mes, 10.0, m05.ORIGEN_MENSUAL)
        anuales = m05.totales_anuales(serie)
        self.assertEqual(anuales, {2000: 120.0})

    def test_un_anio_incompleto_no_entra_como_anio_seco(self) -> None:
        # Sumar sus meses daría un total menor que las pruebas de homogeneidad
        # leerían como un quiebre.
        serie = m05.SerieMensual("X")
        for mes in (1, 2, 3):
            serie.fijar(1999, mes, 100.0, m05.ORIGEN_MENSUAL)
        self.assertNotIn(1999, m05.totales_anuales(serie))


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaComplemento(unittest.TestCase):
    def setUp(self) -> None:
        generador = np.random.default_rng(5)
        base = generador.gamma(2.0, 40.0, size=(120, 1))
        self.datos = np.hstack([base * factor + generador.normal(0, 3, (120, 1))
                                for factor in (1.0, 1.1, 0.9, 1.2)])
        self.datos = np.maximum(self.datos, 0.0)
        self.con_huecos = np.array(self.datos, copy=True)
        self.con_huecos[10:20, 0] = np.nan
        self.con_huecos[50:55, 2] = np.nan

    def test_los_metodos_rellenan_todos_los_huecos(self) -> None:
        for metodo in ("razon_normal", "regresion_vecinas", "idw", "knn", "mice"):
            salida = m05.rellenar(self.con_huecos, metodo, _CFG)
            self.assertFalse(np.isnan(salida[10:20, 0]).any(), metodo)

    def test_ningun_metodo_produce_precipitacion_negativa(self) -> None:
        # Rellenar lluvia con un valor negativo es un resultado incorrecto, no
        # una aproximación.
        for metodo in ("razon_normal", "regresion_vecinas", "idw", "knn", "mice"):
            salida = m05.rellenar(self.con_huecos, metodo, _CFG)
            finitos = salida[np.isfinite(salida)]
            self.assertGreaterEqual(float(finitos.min()), 0.0, metodo)

    def test_el_dato_observado_no_se_altera(self) -> None:
        for metodo in ("razon_normal", "regresion_vecinas", "idw"):
            salida = m05.rellenar(self.con_huecos, metodo, _CFG)
            presentes = np.isfinite(self.con_huecos)
            self.assertTrue(
                np.allclose(salida[presentes], self.con_huecos[presentes]),
                metodo)

    def test_metodo_desconocido_es_error_explicito(self) -> None:
        from comun.errores import ErrorConfiguracion
        with self.assertRaises(ErrorConfiguracion):
            m05.rellenar(self.con_huecos, "inventado", _CFG)

    def test_la_validacion_cruzada_mide_error(self) -> None:
        resultado = m05.validacion_cruzada(self.datos, "razon_normal", _CFG)
        self.assertIn("rmse", resultado)
        self.assertGreater(resultado["n_validacion"], 0)
        self.assertGreaterEqual(resultado["rmse"], 0.0)

    def test_la_validacion_no_altera_la_matriz_original(self) -> None:
        copia = np.array(self.datos, copy=True)
        m05.validacion_cruzada(self.datos, "idw", _CFG)
        self.assertTrue(np.array_equal(copia, self.datos, equal_nan=True))

    def test_una_matriz_sin_datos_se_reporta_sin_reventar(self) -> None:
        vacia = np.full((10, 3), np.nan)
        self.assertIn("error", m05.validacion_cruzada(vacia, "idw", _CFG))


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaDistribucionCorrelaciones(unittest.TestCase):
    """
    El umbral de correlación no puede elegirse a ciegas.

    Medido en este estudio, la mediana entre parejas es 0,555 y exigir 0,80
    dejaría 24 de 42 estaciones sin ninguna vecina.
    """

    def setUp(self) -> None:
        claves = [(2000 + a, m) for a in range(10) for m in range(1, 13)]
        generador = np.random.default_rng(9)
        comun = generador.gamma(2.0, 40.0, len(claves))
        self.claves = claves
        self.matriz = {
            "A": {k: float(v) for k, v in zip(claves, comun)},
            "B": {k: float(v * 1.05 + generador.normal(0, 2))
                  for k, v in zip(claves, comun)},
            "C": {k: float(generador.gamma(2.0, 40.0))
                  for k in claves},
        }

    def test_reporta_percentiles_y_aisladas(self) -> None:
        salida = m05.distribucion_correlaciones(self.matriz, self.claves)
        self.assertEqual(salida["estaciones"], 3)
        self.assertIn("p50", salida["percentiles"])
        self.assertIn("0.80", salida["aisladas_por_umbral"])

    def test_una_estacion_sin_relacion_queda_aislada_en_umbral_alto(self) -> None:
        salida = m05.distribucion_correlaciones(self.matriz, self.claves)
        self.assertGreaterEqual(salida["aisladas_por_umbral"]["0.80"], 1)

    def test_el_conteo_de_aisladas_crece_con_el_umbral(self) -> None:
        salida = m05.distribucion_correlaciones(self.matriz, self.claves)
        valores = [salida["aisladas_por_umbral"][k]
                   for k in ("0.60", "0.70", "0.80", "0.90")]
        self.assertEqual(valores, sorted(valores))

    def test_matriz_vacia_no_revienta(self) -> None:
        self.assertEqual(
            m05.distribucion_correlaciones({}, self.claves)["parejas"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
Pruebas del M06: interpolación, validación cruzada y gradiente altitudinal.

Solo las funciones puras. El geoprocesamiento exige el Python de QGIS y no puede
verificarse desde el venv, pero la interpolación y su validación sí, y son
justamente lo que decide si el mapa es defendible.

    python tests/test_m06.py
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M06_isoyetas as m06  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


class PruebaIDW(unittest.TestCase):
    """
    Se implementa la fórmula aquí para poder validar sin generar un raster por
    cada punto excluido. Debe coincidir con la de QGIS.
    """

    MUESTRAS = [(0.0, 0.0, 100.0), (100.0, 0.0, 200.0),
                (0.0, 100.0, 300.0), (100.0, 100.0, 400.0)]

    def test_sobre_una_muestra_devuelve_su_valor(self) -> None:
        # La ponderación no está definida a distancia cero, y aproximarla
        # introduciría un valor que no es el medido.
        self.assertEqual(m06.idw(0.0, 0.0, self.MUESTRAS), 100.0)

    def test_en_el_centro_promedia_por_simetria(self) -> None:
        self.assertAlmostEqual(m06.idw(50.0, 50.0, self.MUESTRAS), 250.0)

    def test_se_acerca_al_valor_de_la_muestra_mas_proxima(self) -> None:
        cerca = m06.idw(1.0, 1.0, self.MUESTRAS)
        self.assertLess(abs(cerca - 100.0), abs(cerca - 400.0))

    def test_una_potencia_mayor_pesa_mas_lo_cercano(self) -> None:
        suave = m06.idw(10.0, 10.0, self.MUESTRAS, potencia=1.0)
        duro = m06.idw(10.0, 10.0, self.MUESTRAS, potencia=6.0)
        self.assertLess(abs(duro - 100.0), abs(suave - 100.0))

    def test_el_radio_excluye_lo_lejano(self) -> None:
        # Con radio corto solo entra la muestra del origen.
        self.assertEqual(m06.idw(10.0, 10.0, self.MUESTRAS, radio=50.0), 100.0)

    def test_sin_muestras_dentro_del_radio_no_inventa_valor(self) -> None:
        self.assertIsNone(m06.idw(1000.0, 1000.0, self.MUESTRAS, radio=10.0))

    def test_sin_muestras(self) -> None:
        self.assertIsNone(m06.idw(0.0, 0.0, []))


class PruebaValidacionCruzada(unittest.TestCase):
    """
    Interpolar siempre produce una superficie; la pregunta es cuánto se parece a
    lo que habría medido una estación que no participó.
    """

    def test_un_campo_plano_se_reproduce_sin_error(self) -> None:
        muestras = [(float(i), 0.0, 500.0) for i in range(0, 500, 50)]
        resultado = m06.validacion_dejando_uno_fuera(muestras)
        self.assertAlmostEqual(resultado["rmse_mm"], 0.0, places=6)

    def test_un_campo_ruidoso_da_error_alto(self) -> None:
        muestras = [(float(i * 100), 0.0, 100.0 if i % 2 else 900.0)
                    for i in range(10)]
        resultado = m06.validacion_dejando_uno_fuera(muestras)
        self.assertGreater(resultado["rmse_mm"], 100.0)
        self.assertLess(resultado["nash_sutcliffe"], 0.5)

    def test_reporta_todas_las_cifras(self) -> None:
        muestras = [(float(i * 10), float(i * 5), 100.0 + i)
                    for i in range(8)]
        resultado = m06.validacion_dejando_uno_fuera(muestras)
        for clave in ("rmse_mm", "mae_mm", "sesgo_mm", "rmse_relativo_pct",
                      "nash_sutcliffe", "n"):
            self.assertIn(clave, resultado)

    def test_menos_de_tres_estaciones_se_reporta_sin_reventar(self) -> None:
        resultado = m06.validacion_dejando_uno_fuera(
            [(0.0, 0.0, 1.0), (1.0, 1.0, 2.0)])
        self.assertIn("error", resultado)


class PruebaGradienteAltitudinal(unittest.TestCase):
    """
    Prescribir un método no garantiza que el dato lo sustente.

    CLAUDE.md define la zonificación del M11 con gradiente altitudinal, y este
    módulo mide si ese gradiente existe. Sobre la red real de este estudio no
    existe: r2 de 0,011 en la fase neutral.
    """

    def test_una_relacion_perfecta_se_reconoce(self) -> None:
        muestras = [(float(z), 2.0 * z + 100.0) for z in range(1000, 3000, 200)]
        resultado = m06.gradiente_altitudinal(muestras)
        self.assertAlmostEqual(resultado["pendiente_mm_por_m"], 2.0, places=3)
        self.assertAlmostEqual(resultado["r2"], 1.0, places=3)

    def test_sin_relacion_el_r2_es_bajo(self) -> None:
        muestras = [(1000.0, 800.0), (1500.0, 750.0), (2000.0, 820.0),
                    (2500.0, 770.0), (3000.0, 810.0)]
        self.assertLess(m06.gradiente_altitudinal(muestras)["r2"], 0.1)

    def test_una_relacion_inversa_da_pendiente_negativa(self) -> None:
        muestras = [(float(z), 3000.0 - z) for z in range(1000, 3000, 200)]
        self.assertLess(
            m06.gradiente_altitudinal(muestras)["pendiente_mm_por_m"], 0)

    def test_sin_variacion_en_altitud_no_hay_gradiente(self) -> None:
        muestras = [(2000.0, 800.0 + i) for i in range(5)]
        self.assertIn("error", m06.gradiente_altitudinal(muestras))

    def test_muestra_corta_se_reporta(self) -> None:
        self.assertIn("error", m06.gradiente_altitudinal([(1.0, 2.0)]))


class PruebaRangoDeCurvas(unittest.TestCase):
    """
    Las curvas se ajustan a múltiplos del intervalo.

    Si cada fase empezara en su propio mínimo, dos mapas del mismo estudio
    tendrían leyendas que no se corresponden.
    """

    def test_se_ajusta_a_multiplos(self) -> None:
        self.assertEqual(m06.rango_de_curvas([581.0, 1341.0], 100.0),
                         (600.0, 1300.0))

    def test_un_intervalo_mayor_da_menos_curvas(self) -> None:
        primera, ultima = m06.rango_de_curvas([581.0, 1341.0], 250.0)
        self.assertEqual((primera, ultima), (750.0, 1250.0))

    def test_sin_valores(self) -> None:
        self.assertEqual(m06.rango_de_curvas([], 100.0), (0.0, 0.0))


class PruebaLecturaDeTotales(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ruta = self.tmp / "precipitacion_por_fase.csv"
        self.ruta.write_text(
            "codigo;fase;total_anual_mm;completo;n_muestras;anios_equivalentes\n"
            "A;nino;800.0;True;120;10.0\n"
            "B;nino;900.0;False;60;5.0\n"
            "A;nina;1100.0;True;150;12.5\n",
            encoding="utf-8-sig")

    def test_excluye_los_totales_incompletos(self) -> None:
        # Mezclarlos produciría un campo con saltos que son del muestreo.
        por_fase, excluidos = m06.leer_totales_por_fase(self.ruta, ";", True)
        self.assertEqual(excluidos, 1)
        self.assertEqual(len(por_fase["nino"]), 1)

    def test_puede_admitirlos_si_se_pide(self) -> None:
        por_fase, excluidos = m06.leer_totales_por_fase(self.ruta, ";", False)
        self.assertEqual(excluidos, 0)
        self.assertEqual(len(por_fase["nino"]), 2)

    def test_agrupa_por_fase(self) -> None:
        por_fase, _ = m06.leer_totales_por_fase(self.ruta, ";", True)
        self.assertEqual(sorted(por_fase), ["nina", "nino"])

    def test_archivo_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorRutas):
            m06.leer_totales_por_fase(self.tmp / "no_existe.csv", ";", True)


class PruebaConfiguracion(unittest.TestCase):
    def test_la_malla_de_isoyetas_es_mas_gruesa_que_la_del_terreno(self) -> None:
        # Interpolar decenas de estaciones sobre la malla del DEM aparenta una
        # precisión que el dato no tiene.
        isoyetas = float(_CFG.obtener("isoyetas.resolucion_m"))
        terreno = float(_CFG.obtener("interpolacion.resolucion_raster_m"))
        self.assertGreater(isoyetas, terreno)

    def test_el_metodo_declarado_esta_implementado(self) -> None:
        metodo = str(_CFG.obtener("interpolacion.metodo")).upper()
        self.assertIn(metodo, m06.METODOS_IMPLEMENTADOS)

    def test_solo_se_interpolan_totales_completos(self) -> None:
        self.assertTrue(_CFG.obtener("isoyetas.solo_totales_completos"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

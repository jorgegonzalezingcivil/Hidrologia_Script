# -*- coding: utf-8 -*-
"""
Pruebas del M10 y del lector de geometría de comun/shapefile.

    python tests/test_m10.py
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

import M10_morfometria as m10  # noqa: E402
from comun import shapefile  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)
_CUENCA = _RAIZ_REPO / "data" / "03_SIG" / "vector" / "area_influencia.shp"
HAY_CUENCA = _CUENCA.is_file()


class PruebaEnvolventeConvexa(unittest.TestCase):
    """
    El par más distante de un conjunto cae siempre sobre su envolvente convexa.

    Calcularla primero da el mismo resultado y quita el coste cuadrático: sobre
    la cuenca de este estudio, de ocho minutos a menos de un segundo.
    """

    def test_un_cuadrado_conserva_sus_cuatro_esquinas(self) -> None:
        puntos = [(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)]
        envolvente = shapefile._envolvente_convexa(puntos)
        self.assertEqual(len(envolvente), 4)
        self.assertNotIn((0.5, 0.5), envolvente)

    def test_puntos_colineales_no_revientan(self) -> None:
        envolvente = shapefile._envolvente_convexa([(0, 0), (1, 1), (2, 2)])
        self.assertGreaterEqual(len(envolvente), 2)

    def test_menos_de_tres_puntos(self) -> None:
        self.assertEqual(len(shapefile._envolvente_convexa([(0, 0), (1, 1)])), 2)


class PruebaAplicabilidadDeTc(unittest.TestCase):
    """
    La matriz existe para impedir la extrapolación más frecuente de la práctica.

    Kirpich se calibró sobre siete cuencas agrícolas de 0,4 a 45 hectáreas.
    """

    FILA = {"formula": "kirpich", "area_min_km2": "0.004",
            "area_max_km2": "0.45", "pendiente_min": "0.03",
            "pendiente_max": "0.10"}

    def test_una_cuenca_dentro_del_rango_aplica(self) -> None:
        aplicable, motivo = m10.es_aplicable(self.FILA, 0.2, 0.05)
        self.assertTrue(aplicable)
        self.assertEqual(motivo, "")

    def test_una_cuenca_grande_se_rechaza_con_su_motivo(self) -> None:
        aplicable, motivo = m10.es_aplicable(self.FILA, 5925.89, None)
        self.assertFalse(aplicable)
        self.assertIn("excede el máximo", motivo)
        self.assertIn("veces", motivo)

    def test_una_cuenca_diminuta_se_rechaza(self) -> None:
        aplicable, motivo = m10.es_aplicable(self.FILA, 0.001, None)
        self.assertFalse(aplicable)
        self.assertIn("por debajo del mínimo", motivo)

    def test_la_pendiente_tambien_filtra(self) -> None:
        aplicable, motivo = m10.es_aplicable(self.FILA, 0.2, 0.5)
        self.assertFalse(aplicable)
        self.assertIn("pendiente", motivo)

    def test_sin_pendiente_solo_filtra_el_area(self) -> None:
        self.assertTrue(m10.es_aplicable(self.FILA, 0.2, None)[0])

    def test_un_rango_ilegible_no_aplica(self) -> None:
        aplicable, motivo = m10.es_aplicable({"formula": "x"}, 1.0, None)
        self.assertFalse(aplicable)
        self.assertIn("ilegible", motivo)


class PruebaAdopcion(unittest.TestCase):
    """
    CLAUDE.md, sección 7: con menos fórmulas aplicables que el mínimo, se
    advierte y NO se adopta la mediana automáticamente.
    """

    def test_con_suficientes_procede(self) -> None:
        evaluadas = [{"formula": f"f{i}", "aplicable": True} for i in range(6)]
        self.assertTrue(m10.resumir_adopcion(evaluadas, 5)["procede_adoptar"])

    def test_con_pocas_no_procede(self) -> None:
        evaluadas = [{"formula": f"f{i}", "aplicable": i < 3} for i in range(6)]
        resumen = m10.resumir_adopcion(evaluadas, 5)
        self.assertFalse(resumen["procede_adoptar"])
        self.assertEqual(resumen["formulas_aplicables"], 3)

    def test_sin_ninguna_aplicable(self) -> None:
        evaluadas = [{"formula": "f", "aplicable": False}]
        self.assertEqual(
            m10.resumir_adopcion(evaluadas, 5)["formulas_aplicables"], 0)


class PruebaMatrizReal(unittest.TestCase):
    """La matriz es doctrina y vive en data/referencia, no en el código."""

    def setUp(self) -> None:
        self.ruta = _RAIZ_REPO / _CFG.obtener(
            "tiempo_concentracion.tabla_aplicabilidad")

    def test_la_matriz_existe(self) -> None:
        self.assertTrue(self.ruta.is_file(), str(self.ruta))

    def test_trae_rango_y_procedencia_de_cada_formula(self) -> None:
        filas = m10.leer_matriz_aplicabilidad(self.ruta, ";")
        self.assertGreaterEqual(len(filas), 10)
        for fila in filas:
            for campo in ("formula", "area_min_km2", "area_max_km2", "origen"):
                self.assertTrue(str(fila.get(campo, "")).strip(),
                                f"{fila.get('formula')}: falta {campo}")

    def test_ninguna_formula_aplica_a_la_cuenca_del_estudio(self) -> None:
        # Es el hallazgo que sustenta el modo general: la mayor de la matriz
        # llega a 4200 km2 y la cuenca tiene 5.926.
        filas = m10.leer_matriz_aplicabilidad(self.ruta, ";")
        evaluadas = m10.evaluar_aplicabilidad(filas, 5925.89, None)
        self.assertEqual(sum(1 for e in evaluadas if e["aplicable"]), 0)

    def test_una_cuenca_pequena_si_encuentra_formulas(self) -> None:
        filas = m10.leer_matriz_aplicabilidad(self.ruta, ";")
        evaluadas = m10.evaluar_aplicabilidad(filas, 10.0, None)
        self.assertGreaterEqual(sum(1 for e in evaluadas if e["aplicable"]), 5)

    def test_archivo_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorRutas):
            m10.leer_matriz_aplicabilidad(Path("no_existe.csv"), ";")


@unittest.skipUnless(HAY_CUENCA, "no hay capa de cuenca")
class PruebaGeometriaReal(unittest.TestCase):
    def test_los_parametros_son_coherentes_entre_si(self) -> None:
        p = m10.parametros_geometricos(_CUENCA)
        self.assertGreater(p["area_km2"], 0)
        self.assertGreater(p["perimetro_km"], 0)
        # El perímetro no puede ser menor que el del círculo de igual área.
        minimo = 2 * math.sqrt(math.pi * p["area_km2"])
        self.assertGreaterEqual(p["perimetro_km"], minimo)
        # Gravelius vale 1 en un círculo y crece con la irregularidad.
        self.assertGreaterEqual(p["coef_compacidad"], 1.0)

    def test_la_longitud_axial_no_excede_el_perimetro(self) -> None:
        p = m10.parametros_geometricos(_CUENCA)
        self.assertLess(p["longitud_axial_km"], p["perimetro_km"])


class PruebaModoDeAnalisis(unittest.TestCase):
    def test_el_modo_esta_declarado(self) -> None:
        self.assertIn(_CFG.obtener("analisis.modo"), ("general", "detallado"))

    def test_el_modo_general_lleva_motivo_escrito(self) -> None:
        # Un estudio que no explica por qué no modeló la cuenca no es
        # defendible ante interventoría.
        if _CFG.obtener("analisis.modo") != "general":
            self.skipTest("modo detallado")
        motivo = str(_CFG.obtener("analisis.motivo_general") or "")
        self.assertGreater(len(motivo.strip()), 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)

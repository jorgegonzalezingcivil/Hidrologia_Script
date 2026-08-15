# -*- coding: utf-8 -*-
"""
Pruebas del M18b: infiltración por Schosinsky y Losilla.

    python tests/test_m18b.py
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

import M18b_infiltracion as m18b  # noqa: E402
from comun.errores import ErrorFormato, ErrorHidrologia  # noqa: E402

TABLA_KP = [
    {"clave": "muy_plana", "desde": 0.0, "hasta": 0.02, "valor": 0.30},
    {"clave": "plana", "desde": 0.02, "hasta": 0.06, "valor": 0.20},
    {"clave": "algo_plana", "desde": 0.06, "hasta": 0.12, "valor": 0.15},
    {"clave": "promedio", "desde": 0.12, "hasta": 0.20, "valor": 0.10},
    {"clave": "fuerte", "desde": 0.20, "hasta": 99.0, "valor": 0.06},
]


class PruebaKfc(unittest.TestCase):
    """
    Los tres tramos son del modelo: la relación logarítmica se ajustó entre 16 y
    1568 mm/día, y fuera de ahí el propio modelo la sustituye.
    """

    def test_en_el_tramo_ajustado_usa_el_logaritmo(self) -> None:
        salida = m18b.kfc_por_infiltracion_basica(40.0)
        esperado = 0.267 * math.log(40.0) - 0.000154 * 40.0 - 0.723
        self.assertAlmostEqual(salida["kfc"], esperado, places=4)
        self.assertFalse(salida["fuera_de_rango"])

    def test_crece_con_la_infiltracion_basica(self) -> None:
        lento = m18b.kfc_por_infiltracion_basica(20.0)["kfc"]
        rapido = m18b.kfc_por_infiltracion_basica(200.0)["kfc"]
        self.assertGreater(rapido, lento)

    def test_por_debajo_del_rango_cae_a_la_recta(self) -> None:
        salida = m18b.kfc_por_infiltracion_basica(8.0)
        self.assertAlmostEqual(salida["kfc"], 0.0148 * 8.0 / 16.0, places=6)
        self.assertTrue(salida["fuera_de_rango"])
        self.assertEqual(salida["tramo"], "bajo")

    def test_por_encima_del_rango_satura(self) -> None:
        salida = m18b.kfc_por_infiltracion_basica(2000.0)
        self.assertEqual(salida["kfc"], 1.0)
        self.assertTrue(salida["fuera_de_rango"])

    def test_nunca_sale_de_cero_a_uno(self) -> None:
        for fc in (1.0, 16.0, 100.0, 1568.0, 5000.0):
            self.assertGreaterEqual(
                m18b.kfc_por_infiltracion_basica(fc)["kfc"], 0.0)
            self.assertLessEqual(
                m18b.kfc_por_infiltracion_basica(fc)["kfc"], 1.0)

    def test_una_infiltracion_nula_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18b.kfc_por_infiltracion_basica(0.0)


class PruebaKp(unittest.TestCase):
    def test_a_menos_pendiente_mas_infiltracion(self) -> None:
        llano = m18b.kp_por_pendiente(0.01, TABLA_KP)["kp"]
        empinado = m18b.kp_por_pendiente(0.30, TABLA_KP)["kp"]
        self.assertGreater(llano, empinado)

    def test_la_pendiente_de_este_estudio_cae_en_la_clase_fuerte(self) -> None:
        # La mediana de las subcuencas es 0,214.
        self.assertEqual(m18b.kp_por_pendiente(0.214, TABLA_KP)["clase"],
                         "fuerte")

    def test_el_limite_pertenece_a_la_clase_superior(self) -> None:
        # Los intervalos son cerrados por abajo y abiertos por arriba: sin ese
        # criterio, un valor justo en el borde caería en dos clases.
        self.assertEqual(m18b.kp_por_pendiente(0.02, TABLA_KP)["clase"], "plana")
        self.assertEqual(m18b.kp_por_pendiente(0.12, TABLA_KP)["clase"],
                         "promedio")

    def test_una_pendiente_sin_clase_es_error(self) -> None:
        # No se extrapola: una pendiente fuera de todas las clases señala un
        # dato erróneo, no una clase que falte.
        with self.assertRaises(ErrorFormato):
            m18b.kp_por_pendiente(200.0, TABLA_KP)

    def test_una_pendiente_negativa_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18b.kp_por_pendiente(-0.1, TABLA_KP)


class PruebaCoeficiente(unittest.TestCase):
    def test_es_la_suma_de_los_tres(self) -> None:
        salida = m18b.coeficiente_de_infiltracion(0.25, 0.06, 0.18)
        self.assertAlmostEqual(salida["c"], 0.49)
        self.assertFalse(salida["saturado"])

    def test_no_pasa_de_uno_y_lo_reporta(self) -> None:
        # No se infiltra más agua de la que llega al suelo.
        salida = m18b.coeficiente_de_infiltracion(0.9, 0.30, 0.21)
        self.assertEqual(salida["c"], 1.0)
        self.assertTrue(salida["saturado"])
        self.assertGreater(salida["suma_sin_acotar"], 1.0)


class PruebaRetencion(unittest.TestCase):
    def test_es_una_fraccion_de_la_lluvia(self) -> None:
        self.assertAlmostEqual(m18b.retencion_de_follaje(100.0, 0.12), 12.0)

    def test_un_mes_muy_seco_se_retiene_entero(self) -> None:
        # No alcanza a mojar el follaje lo bastante para que gotee.
        self.assertAlmostEqual(m18b.retencion_de_follaje(3.0, 0.12), 3.0)

    def test_el_bosque_retiene_mas_que_el_pasto(self) -> None:
        self.assertGreater(m18b.retencion_de_follaje(100.0, 0.20),
                           m18b.retencion_de_follaje(100.0, 0.12))

    def test_sin_lluvia_no_hay_retencion(self) -> None:
        self.assertEqual(m18b.retencion_de_follaje(0.0, 0.12), 0.0)


class PruebaInfiltracionMensual(unittest.TestCase):
    def test_el_reparto_suma_lo_disponible(self) -> None:
        salida = m18b.infiltracion_mensual(100.0, 0.5, 12.0)
        self.assertAlmostEqual(
            salida["infiltracion_mm"] + salida["escorrentia_superficial_mm"],
            salida["disponible_mm"], places=6)

    def test_no_supera_lo_que_llego_al_suelo(self) -> None:
        salida = m18b.infiltracion_mensual(100.0, 1.0, 12.0)
        self.assertAlmostEqual(salida["infiltracion_mm"], 88.0)
        self.assertAlmostEqual(salida["escorrentia_superficial_mm"], 0.0)

    def test_un_mes_retenido_entero_no_infiltra(self) -> None:
        salida = m18b.infiltracion_mensual(3.0, 0.5, 3.0)
        self.assertEqual(salida["infiltracion_mm"], 0.0)

    def test_una_retencion_mayor_que_la_lluvia_es_error(self) -> None:
        # Sería devolver a la atmósfera agua que no cayó.
        with self.assertRaises(ErrorHidrologia):
            m18b.infiltracion_mensual(10.0, 0.5, 20.0)

    def test_laminas_negativas_son_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18b.infiltracion_mensual(-5.0, 0.5, 0.0)


class PruebaTablasDeDoctrina(unittest.TestCase):
    """
    Los parámetros salen del suelo del estudio, no de un valor tomado de otra
    zona. Las tablas viven en data/referencia con su criterio escrito.
    """

    def _leer(self, nombre):
        ruta = _RAIZ_REPO / "data/referencia" / nombre
        self.assertTrue(ruta.is_file(), f"falta {nombre}")
        with ruta.open(encoding="utf-8-sig", newline="") as manejador:
            return list(csv.DictReader(manejador, delimiter=";"))

    def test_hay_un_fc_por_cada_grupo_hidrologico(self) -> None:
        filas = self._leer("infiltracion_schosinsky.csv")
        grupos = {f["clave"] for f in filas if f["tipo"] == "fc_por_grupo"}
        self.assertEqual(grupos, {"A", "B", "C", "D"})

    def test_el_fc_decrece_de_a_hacia_d(self) -> None:
        # A son arenas y D arcillas: si el orden se invirtiera, el balance
        # infiltraría más en el suelo más impermeable.
        filas = {f["clave"]: float(f["valor"])
                 for f in self._leer("infiltracion_schosinsky.csv")
                 if f["tipo"] == "fc_por_grupo"}
        self.assertGreater(filas["A"], filas["B"])
        self.assertGreater(filas["B"], filas["C"])
        self.assertGreater(filas["C"], filas["D"])

    def test_las_clases_de_pendiente_no_dejan_huecos(self) -> None:
        filas = sorted((f for f in self._leer("infiltracion_schosinsky.csv")
                        if f["tipo"] == "kp_pendiente"),
                       key=lambda f: float(f["desde"]))
        self.assertAlmostEqual(float(filas[0]["desde"]), 0.0)
        for anterior, siguiente in zip(filas, filas[1:]):
            self.assertAlmostEqual(float(anterior["hasta"]),
                                   float(siguiente["desde"]))

    def test_cada_cobertura_del_estudio_tiene_su_clase(self) -> None:
        filas = self._leer("kv_cobertura_schosinsky.csv")
        clases = {f["clase_schosinsky"] for f in filas}
        de_schosinsky = {f["clave"]
                         for f in self._leer("infiltracion_schosinsky.csv")
                         if f["tipo"] == "kv_cobertura"}
        self.assertTrue(clases <= de_schosinsky,
                        f"clases inventadas: {clases - de_schosinsky}")

    def test_cada_homologacion_lleva_su_criterio(self) -> None:
        # CLAUDE.md sección 7: toda decisión con margen queda registrada.
        for fila in self._leer("kv_cobertura_schosinsky.csv"):
            self.assertTrue(str(fila.get("criterio", "")).strip(),
                            f"{fila['cobertura']} sin criterio escrito")

    def test_las_tablas_se_declaran_sin_validar(self) -> None:
        # Ninguna se ha contrastado aún con la fuente original.
        for nombre in ("infiltracion_schosinsky.csv",
                       "kv_cobertura_schosinsky.csv"):
            for fila in self._leer(nombre):
                self.assertEqual(str(fila.get("validado", "")).strip().lower(),
                                 "no")


if __name__ == "__main__":
    unittest.main(verbosity=2)

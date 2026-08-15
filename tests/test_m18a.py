# -*- coding: utf-8 -*-
"""
Pruebas del M18a: temperatura por gradiente altitudinal.

    python tests/test_m18a.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M18a_temperatura as m18a  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorHidrologia  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


def diarios(codigo, etiqueta, anio, mes, cuantos, valor=15.0):
    return [{"codigo": codigo, "etiqueta": etiqueta,
             "fecha": f"{anio:04d}-{mes:02d}-{dia:02d}", "valor": str(valor)}
            for dia in range(1, cuantos + 1)]


class PruebaAgregacionMensual(unittest.TestCase):
    """
    Con temperatura, un mes incompleto no subestima el total como en la lluvia:
    sesga la media hacia los días que quedaron.
    """

    def test_un_mes_completo_se_promedia(self) -> None:
        mensual, fuera = m18a.agregar_mensual(
            diarios("E1", "TMX_CON", 2020, 1, 31, 20.0), 0.8)
        self.assertEqual(fuera, 0)
        self.assertAlmostEqual(mensual[0]["media_c"], 20.0)
        self.assertEqual(mensual[0]["dias_del_mes"], 31)

    def test_un_mes_corto_se_descarta_y_se_cuenta(self) -> None:
        mensual, fuera = m18a.agregar_mensual(
            diarios("E1", "TMX_CON", 2020, 1, 10), 0.8)
        self.assertEqual(mensual, [])
        self.assertEqual(fuera, 1)

    def test_febrero_no_se_juzga_con_la_vara_de_julio(self) -> None:
        # 25 días de 29 son el 86 % de febrero de un año bisiesto y solo el
        # 81 % de julio: exigir 24 de 30 descartaría febreros válidos.
        mensual, _ = m18a.agregar_mensual(
            diarios("E1", "TMX_CON", 2020, 2, 25), 0.85)
        self.assertEqual(len(mensual), 1)
        self.assertEqual(mensual[0]["dias_del_mes"], 29)

    def test_una_fecha_ilegible_no_detiene_la_agregacion(self) -> None:
        registros = diarios("E1", "TMX_CON", 2020, 1, 31)
        registros.append({"codigo": "E1", "etiqueta": "TMX_CON",
                          "fecha": "sin fecha", "valor": "20"})
        mensual, _ = m18a.agregar_mensual(registros, 0.8)
        self.assertEqual(len(mensual), 1)


class PruebaMediaDeMaximaYMinima(unittest.TestCase):
    def test_es_la_semisuma(self) -> None:
        mensual = [{"codigo": "E1", "etiqueta": "TMX_CON", "anio": 2020,
                    "mes": 1, "media_c": 20.0},
                   {"codigo": "E1", "etiqueta": "TMN_CON", "anio": 2020,
                    "mes": 1, "media_c": 10.0}]
        salida = m18a.combinar_maxima_y_minima(mensual, "TMX_CON", "TMN_CON")
        self.assertAlmostEqual(salida[0]["t_media_c"], 15.0)
        self.assertAlmostEqual(salida[0]["amplitud_c"], 10.0)

    def test_sin_las_dos_no_hay_media(self) -> None:
        # Usar solo la máxima daría una serie que parece temperatura media y
        # está varios grados por encima.
        mensual = [{"codigo": "E1", "etiqueta": "TMX_CON", "anio": 2020,
                    "mes": 1, "media_c": 20.0}]
        self.assertEqual(
            m18a.combinar_maxima_y_minima(mensual, "TMX_CON", "TMN_CON"), [])


class PruebaGradiente(unittest.TestCase):
    """
    El R² mide cuánta varianza explica la recta, no cuán segura es su
    inclinación. Lo que se extrapola sobre las partes altas es la pendiente.
    """

    def test_recupera_una_recta_exacta(self) -> None:
        alturas = [1000.0, 2000.0, 3000.0]
        # 6,5 °C/km de enfriamiento
        temperaturas = [30.0 - 0.0065 * h for h in alturas]
        ajuste = m18a.ajustar_gradiente(alturas, temperaturas)
        self.assertAlmostEqual(ajuste["gradiente_c_por_km"], 6.5, places=3)
        self.assertAlmostEqual(ajuste["intercepto_c"], 30.0, places=3)
        self.assertAlmostEqual(ajuste["r2"], 1.0, places=6)

    def test_el_signo_del_gradiente_es_de_enfriamiento(self) -> None:
        # La pendiente es negativa y el gradiente positivo: así se compara con
        # los valores de referencia, que se citan en positivo.
        ajuste = m18a.ajustar_gradiente(
            [1000.0, 2000.0, 3000.0], [20.0, 14.0, 8.0])
        self.assertLess(ajuste["pendiente_c_por_m"], 0)
        self.assertGreater(ajuste["gradiente_c_por_km"], 0)

    def test_el_intervalo_contiene_la_pendiente(self) -> None:
        ajuste = m18a.ajustar_gradiente(
            [2500.0, 2700.0, 2900.0, 3100.0], [15.0, 13.5, 12.2, 10.4])
        self.assertLessEqual(ajuste["gradiente_min_c_por_km"],
                             ajuste["gradiente_c_por_km"])
        self.assertGreaterEqual(ajuste["gradiente_max_c_por_km"],
                                ajuste["gradiente_c_por_km"])

    def test_con_menos_de_tres_estaciones_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18a.ajustar_gradiente([1000.0, 2000.0], [20.0, 14.0])

    def test_todas_a_la_misma_cota_es_error(self) -> None:
        # Sin rango de elevación no hay pendiente que ajustar, y una división
        # por cero devolvería un gradiente infinito con aspecto de número.
        with self.assertRaises(ErrorHidrologia):
            m18a.ajustar_gradiente([2600.0] * 4, [14.0, 14.5, 13.8, 14.2])

    def test_evaluar_devuelve_la_recta(self) -> None:
        ajuste = m18a.ajustar_gradiente(
            [1000.0, 2000.0, 3000.0], [30.0 - 0.0065 * h
                                       for h in (1000.0, 2000.0, 3000.0)])
        self.assertAlmostEqual(m18a.evaluar(ajuste, 2500.0),
                               30.0 - 0.0065 * 2500.0, places=3)


class PruebaCobertura(unittest.TestCase):
    """
    Es el dato que decide si el campo está medido o extrapolado. Por encima de
    la estación más alta la recta se prolonga sin nada que la sujete.
    """

    FRANJAS = [
        {"cota_inf": "2500", "cota_sup": "3000", "area_km2": "100"},
        {"cota_inf": "3000", "cota_sup": "3500", "area_km2": "100"},
    ]

    def test_sin_extrapolacion_cuando_las_estaciones_cubren(self) -> None:
        cobertura = m18a.cobertura_altitudinal(2500.0, 3500.0, self.FRANJAS)
        self.assertAlmostEqual(cobertura["pct_extrapolado"], 0.0)

    def test_mide_lo_que_queda_por_encima(self) -> None:
        # Con la estación más alta en 3000, la franja superior entera queda
        # fuera: la mitad del área.
        cobertura = m18a.cobertura_altitudinal(2500.0, 3000.0, self.FRANJAS)
        self.assertAlmostEqual(cobertura["area_sobre_estaciones_km2"], 100.0)
        self.assertAlmostEqual(cobertura["pct_extrapolado"], 50.0)

    def test_reparte_una_franja_partida_por_el_limite(self) -> None:
        cobertura = m18a.cobertura_altitudinal(2500.0, 3250.0, self.FRANJAS)
        self.assertAlmostEqual(cobertura["area_sobre_estaciones_km2"], 50.0)

    def test_tambien_cuenta_lo_que_queda_por_debajo(self) -> None:
        cobertura = m18a.cobertura_altitudinal(2750.0, 3500.0, self.FRANJAS)
        self.assertAlmostEqual(cobertura["area_bajo_estaciones_km2"], 50.0)


class PruebaContraste(unittest.TestCase):
    REFERENCIAS = [
        {"criterio": "adiabatico_ambiental", "gradiente_c_por_km": "6.5",
         "tolerancia_c_por_km": "2.0", "fuente": "ISA"},
        {"criterio": "adiabatico_seco", "gradiente_c_por_km": "9.8",
         "tolerancia_c_por_km": "0.0", "fuente": "termodinamica"},
    ]

    def _ajuste(self, gradiente, ancho=1.0):
        return {"gradiente_c_por_km": gradiente,
                "gradiente_min_c_por_km": gradiente - ancho,
                "gradiente_max_c_por_km": gradiente + ancho}

    def test_un_gradiente_normal_queda_dentro(self) -> None:
        contraste = m18a.contrastar_con_referencia(
            self._ajuste(6.8), self.REFERENCIAS)
        ambiental = contraste[0]
        self.assertTrue(ambiental["dentro_de_tolerancia"])
        self.assertTrue(ambiental["contenido_en_el_intervalo"])

    def test_un_gradiente_empinado_se_delata(self) -> None:
        contraste = m18a.contrastar_con_referencia(
            self._ajuste(9.0), self.REFERENCIAS)
        self.assertFalse(contraste[0]["contenido_en_el_intervalo"])
        self.assertGreater(contraste[0]["diferencia_pct"], 0)

    def test_por_encima_del_adiabatico_seco_hay_diferencia_positiva(self) -> None:
        # Es el límite físico: un campo térmico no puede enfriarse más rápido.
        contraste = m18a.contrastar_con_referencia(
            self._ajuste(11.0), self.REFERENCIAS)
        seco = next(c for c in contraste if c["criterio"] == "adiabatico_seco")
        self.assertGreater(seco["diferencia_c_por_km"], 0)


class PruebaConfiguracion(unittest.TestCase):
    def test_las_etiquetas_estan_declaradas(self) -> None:
        for clave in ("temperatura.etiqueta_maxima", "temperatura.etiqueta_minima"):
            self.assertTrue(str(_CFG.obtener(clave)).strip())

    def test_la_completitud_es_exigente(self) -> None:
        self.assertGreaterEqual(
            float(_CFG.obtener("temperatura.completitud_mensual_min")), 0.5)

    def test_la_tabla_de_referencia_existe_y_trae_el_ambiental(self) -> None:
        import csv
        ruta = _RAIZ_REPO / _CFG.obtener("temperatura.tabla_gradiente")
        self.assertTrue(ruta.is_file())
        with ruta.open(encoding="utf-8-sig", newline="") as manejador:
            criterios = {f["criterio"] for f in csv.DictReader(manejador,
                                                               delimiter=";")}
        self.assertIn("adiabatico_ambiental", criterios)
        self.assertIn("adiabatico_seco", criterios)


if __name__ == "__main__":
    unittest.main(verbosity=2)

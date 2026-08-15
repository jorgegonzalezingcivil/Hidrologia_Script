# -*- coding: utf-8 -*-
"""
Pruebas del M18a: temperatura por gradiente altitudinal.

    python tests/test_m18a.py
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


class PruebaGradientesMensuales(unittest.TestCase):
    """
    Doce ajustes reparten las mismas estaciones entre doce meses: un mes que no
    sostiene una pendiente hereda el compuesto en lugar de publicar una recta
    que no se puede afirmar.
    """

    ALTURAS = {"A": 2500.0, "B": 2700.0, "C": 2900.0, "D": 3100.0, "E": 3300.0}
    COMPUESTO = {"intercepto_c": 30.0, "pendiente_c_por_m": -0.0065,
                 "gradiente_c_por_km": 6.5, "gradiente_min_c_por_km": 5.5,
                 "gradiente_max_c_por_km": 7.5, "r2": 0.9, "n": 5}

    def _mensual(self, meses, codigos):
        return [{"codigo": c, "anio": 2020, "mes": m,
                 "t_media_c": 30.0 - 0.0065 * self.ALTURAS[c]}
                for m in meses for c in codigos]

    def test_devuelve_los_doce_meses(self) -> None:
        salida = m18a.ajustar_gradientes_mensuales(
            self._mensual(range(1, 13), self.ALTURAS), self.ALTURAS,
            self.COMPUESTO, estaciones_min=5)
        self.assertEqual([a["mes"] for a in salida], list(range(1, 13)))

    def test_un_mes_con_datos_ajusta_lo_suyo(self) -> None:
        salida = m18a.ajustar_gradientes_mensuales(
            self._mensual(range(1, 13), self.ALTURAS), self.ALTURAS,
            self.COMPUESTO, estaciones_min=5)
        self.assertFalse(salida[0]["heredado"])
        self.assertAlmostEqual(salida[0]["gradiente_c_por_km"], 6.5, places=3)

    def test_un_mes_con_pocas_estaciones_hereda_y_lo_dice(self) -> None:
        salida = m18a.ajustar_gradientes_mensuales(
            self._mensual([1], ["A", "B", "C"]), self.ALTURAS,
            self.COMPUESTO, estaciones_min=5)
        enero = salida[0]
        self.assertTrue(enero["heredado"])
        self.assertIn("estacion", enero["motivo_herencia"])
        self.assertEqual(enero["estaciones_del_mes"], 3)

    def test_un_mes_sin_datos_hereda(self) -> None:
        salida = m18a.ajustar_gradientes_mensuales(
            self._mensual([1], self.ALTURAS), self.ALTURAS,
            self.COMPUESTO, estaciones_min=5)
        self.assertTrue(all(a["heredado"] for a in salida[1:]))


class PruebaIsotermas(unittest.TestCase):
    """
    Con el campo ajustado contra la elevación, la isoterma de un valor ES la
    curva de nivel de la cota que la recta le asigna.
    """

    AJUSTE = {"intercepto_c": 30.0, "pendiente_c_por_m": -0.0065}
    FRANJAS = [
        {"cota_inf": "2000", "cota_sup": "2500", "area_km2": "50"},
        {"cota_inf": "2500", "cota_sup": "3000", "area_km2": "50"},
    ]

    def test_el_area_se_conserva(self) -> None:
        franjas = m18a.isotermas_por_franja(self.AJUSTE, self.FRANJAS, 1.0)
        self.assertAlmostEqual(sum(f["area_km2"] for f in franjas), 100.0,
                               places=3)
        self.assertAlmostEqual(sum(f["area_pct"] for f in franjas), 100.0,
                               places=2)

    def test_las_franjas_van_de_frio_a_caliente(self) -> None:
        franjas = m18a.isotermas_por_franja(self.AJUSTE, self.FRANJAS, 1.0)
        temperaturas = [f["t_inferior_c"] for f in franjas]
        self.assertEqual(temperaturas, sorted(temperaturas))

    def test_la_cota_de_cada_isoterma_es_la_de_la_recta(self) -> None:
        franjas = m18a.isotermas_por_franja(self.AJUSTE, self.FRANJAS, 1.0)
        for franja in franjas:
            self.assertAlmostEqual(
                m18a.evaluar(self.AJUSTE, franja["cota_superior_m"]),
                franja["t_inferior_c"], places=2)

    def test_sin_pendiente_no_hay_isotermas(self) -> None:
        # Una recta horizontal no define curvas de nivel de temperatura.
        self.assertEqual(m18a.isotermas_por_franja(
            {"intercepto_c": 15.0, "pendiente_c_por_m": 0.0},
            self.FRANJAS, 1.0), [])


class PruebaEvapotranspiracionPotencial(unittest.TestCase):
    """
    Cada método aporta lo que sabe: Cenicafé el nivel multianual, Thornthwaite
    el reparto en el año. Ninguno hace lo del otro.
    """

    def test_cenicafe_baja_con_la_elevacion(self) -> None:
        baja = m18a.etp_cenicafe(1000.0, 1700.17, -0.0002)
        alta = m18a.etp_cenicafe(3000.0, 1700.17, -0.0002)
        self.assertGreater(baja, alta)
        self.assertAlmostEqual(baja, 1700.17 * math.exp(-0.2), places=6)

    def test_thornthwaite_necesita_los_doce_meses(self) -> None:
        # El índice de calor es anual: con menos meses no es el mismo número.
        with self.assertRaises(ErrorHidrologia):
            m18a.etp_thornthwaite([15.0] * 6)

    def test_thornthwaite_devuelve_el_ciclo_y_su_anual(self) -> None:
        salida = m18a.etp_thornthwaite([12.0] * 12)
        self.assertEqual(len(salida["etp_mensual_mm"]), 12)
        self.assertAlmostEqual(salida["etp_anual_mm"],
                               sum(salida["etp_mensual_mm"]), places=1)
        self.assertGreater(salida["indice_calor"], 0)

    def test_un_mes_bajo_cero_no_evapora(self) -> None:
        # Elevar un negativo a un exponente fraccionario no da un número.
        salida = m18a.etp_thornthwaite([15.0] * 11 + [-3.0])
        self.assertEqual(salida["etp_mensual_mm"][-1], 0.0)

    def test_con_todo_bajo_cero_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18a.etp_thornthwaite([-1.0] * 12)

    def test_el_factor_lleva_el_anual_al_multianual(self) -> None:
        ajuste = m18a.factor_de_ajuste(937.0, 610.0)
        self.assertAlmostEqual(610.0 * ajuste["factor"], 937.0, places=2)
        self.assertLess(ajuste["discrepancia_pct"], 0)

    def test_un_valor_nulo_no_produce_factor(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m18a.factor_de_ajuste(937.0, 0.0)


class PruebaEvapotranspiracionReal(unittest.TestCase):
    """
    Las tres formulaciones comparten dos límites físicos: no se puede devolver a
    la atmósfera más agua de la que cayó, ni más de la que la energía permite.
    """

    def test_budyko_no_supera_la_lluvia_ni_la_potencial(self) -> None:
        for lluvia, potencial in ((100.0, 2000.0), (2000.0, 100.0),
                                  (800.0, 900.0)):
            etr = m18a.etr_budyko(lluvia, potencial)
            self.assertLessEqual(etr, lluvia + 1e-9)
            self.assertLessEqual(etr, potencial + 1e-9)

    def test_dekop_tampoco(self) -> None:
        for lluvia, potencial in ((100.0, 2000.0), (2000.0, 100.0)):
            etr = m18a.etr_dekop(lluvia, potencial)
            self.assertLessEqual(etr, min(lluvia, potencial) + 1e-9)

    def test_sin_lluvia_no_hay_evapotranspiracion(self) -> None:
        self.assertEqual(m18a.etr_budyko(0.0, 900.0), 0.0)
        self.assertEqual(m18a.etr_dekop(0.0, 900.0), 0.0)
        self.assertEqual(m18a.etr_turc(0.0, 15.0), 0.0)

    def test_budyko_crece_con_la_lluvia(self) -> None:
        seco = m18a.etr_budyko(400.0, 900.0)
        humedo = m18a.etr_budyko(1200.0, 900.0)
        self.assertGreater(humedo, seco)

    def test_una_lamina_negativa_es_error(self) -> None:
        # No es ruido: es un fallo de la cadena que alimenta.
        with self.assertRaises(ErrorHidrologia):
            m18a.etr_budyko(-10.0, 900.0)
        with self.assertRaises(ErrorHidrologia):
            m18a.etr_dekop(100.0, -5.0)
        with self.assertRaises(ErrorHidrologia):
            m18a.etr_turc(-1.0, 15.0)

    def test_budyko_no_desborda_con_etp_muy_alta(self) -> None:
        # cosh y sinh crecen como exponenciales: sin el corte, desbordan.
        self.assertAlmostEqual(m18a.etr_budyko(1.0, 1.0e6), 1.0, places=6)

    def test_turc_por_debajo_del_umbral_evapora_toda_la_lluvia(self) -> None:
        # L con 15 C vale unos 844 mm; 100 mm dan una razon de 0,12.
        self.assertAlmostEqual(m18a.etr_turc(100.0, 15.0), 100.0, places=6)

    def test_turc_por_encima_del_umbral_deja_escorrentia(self) -> None:
        etr = m18a.etr_turc(1500.0, 15.0)
        self.assertLess(etr, 1500.0)
        self.assertGreater(etr, 0.0)

    def test_la_escorrentia_es_el_residuo_y_nunca_negativa(self) -> None:
        self.assertAlmostEqual(m18a.escorrentia(1200.0, 800.0), 400.0)
        self.assertEqual(m18a.escorrentia(800.0, 900.0), 0.0)

    def test_el_caudal_convierte_lamina_y_area(self) -> None:
        # 1000 mm sobre 1 km2 en un ano son 1e6 m3, es decir 0,0317 m3/s.
        self.assertAlmostEqual(m18a.caudal_medio(1000.0, 1.0, 365.25),
                               1.0e6 / (365.25 * 86400.0), places=9)

    def test_sin_area_no_hay_caudal(self) -> None:
        self.assertEqual(m18a.caudal_medio(1000.0, 0.0, 365.25), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

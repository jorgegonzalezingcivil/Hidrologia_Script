# -*- coding: utf-8 -*-
"""
Pruebas del M12a: curvas IDF, desagregación y cambio climático.

    python tests/test_m12a.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M12a_idf as m12a  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorHidrologia, ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

ANDINA = {"a": 0.94, "b": 0.18, "c": 0.66, "d": 0.83,
          "unidad_duracion": "horas"}


class PruebaIntensidadInvias(unittest.TestCase):
    def test_crece_con_el_periodo_de_retorno(self) -> None:
        self.assertLess(m12a.intensidad_invias(180.0, 5.0, 42.0, ANDINA),
                        m12a.intensidad_invias(180.0, 100.0, 42.0, ANDINA))

    def test_decrece_con_la_duracion(self) -> None:
        self.assertGreater(m12a.intensidad_invias(5.0, 10.0, 42.0, ANDINA),
                           m12a.intensidad_invias(1440.0, 10.0, 42.0, ANDINA))

    def test_la_unidad_de_duracion_cambia_el_resultado(self) -> None:
        # La misma ecuación se publica con horas y con minutos: confundirlas
        # multiplica la intensidad por 60^c, catorce veces con c = 0,66.
        en_horas = m12a.intensidad_invias(1440.0, 10.0, 42.0, ANDINA)
        en_minutos = m12a.intensidad_invias(
            1440.0, 10.0, 42.0, dict(ANDINA, unidad_duracion="minutos"))
        self.assertAlmostEqual(en_horas / en_minutos, 60.0 ** 0.66, places=3)

    def test_magnitudes_no_positivas(self) -> None:
        for argumentos in ((0.0, 10.0, 42.0), (180.0, 0.0, 42.0),
                           (180.0, 10.0, 0.0)):
            with self.assertRaises(ErrorHidrologia):
                m12a.intensidad_invias(*argumentos, ANDINA)


class PruebaContraElInformeDeReferencia(unittest.TestCase):
    """
    Reproduce la Tabla 58 del informe de referencia, numeral 5.5.1.

    Es la prueba de oro del método: treinta valores publicados, calculados con
    los coeficientes de la región Orinoquía y la media multianual de máximos en
    24 h de aquel estudio, 126,69 mm. Si la ecuación, los coeficientes o la
    unidad de la duración cambiaran, esto se rompe.

    Fue además la que resolvió la ambigüedad de la unidad. El manual lista sus
    variables diciendo "Duración de la lluvia (min)", pero su propia tabla solo
    se reproduce con la duración en HORAS: con minutos la desviación llega al
    92 %.
    """

    ORINOQUIA = {"a": 5.53, "b": 0.17, "c": 0.63, "d": 0.42,
                 "unidad_duracion": "horas"}
    MEDIA_MM = 126.69
    # Duración en minutos -> periodo de retorno -> intensidad en mm/h.
    TABLA_58 = {
        10: {2.33: 150.86, 5: 171.77, 10: 193.25, 25: 225.82, 50: 254.06,
             100: 285.83},
        20: {2.33: 97.48, 5: 110.99, 10: 124.87, 25: 145.92, 50: 164.17,
             100: 184.70},
        30: {2.33: 75.50, 5: 85.97, 10: 96.72, 25: 113.03, 50: 127.16,
             100: 143.06},
        60: {2.33: 48.79, 5: 55.55, 10: 62.50, 25: 73.03, 50: 82.17,
             100: 92.44},
        90: {2.33: 37.79, 5: 43.03, 10: 48.41, 25: 56.57, 50: 63.64,
             100: 71.60},
    }

    def test_reproduce_la_tabla_publicada(self) -> None:
        for duracion, por_periodo in self.TABLA_58.items():
            for periodo, esperado in por_periodo.items():
                obtenido = m12a.intensidad_invias(
                    duracion, periodo, self.MEDIA_MM, self.ORINOQUIA)
                self.assertAlmostEqual(
                    obtenido, esperado, delta=0.02,
                    msg=f"{duracion} min, T {periodo}: {obtenido:.2f} frente a "
                        f"{esperado:.2f} publicado")

    def test_con_la_duracion_en_minutos_no_reproduce_nada(self) -> None:
        # La comprobación que descartó la lectura literal del manual.
        en_minutos = dict(self.ORINOQUIA, unidad_duracion="minutos")
        obtenido = m12a.intensidad_invias(10.0, 2.33, self.MEDIA_MM, en_minutos)
        self.assertLess(obtenido, 0.5 * self.TABLA_58[10][2.33])

    def test_los_coeficientes_de_la_doctrina_son_los_publicados(self) -> None:
        # La Tabla 57 del mismo numeral. Amazonía no figura allí y por eso la
        # tabla del repositorio la declara sin validar.
        tabla = m12a.leer_coeficientes(
            _RAIZ_REPO / _CFG.obtener("idf.coeficientes_invias"), ";")
        publicados = {
            "andina": (0.94, 0.18, 0.66, 0.83),
            "caribe": (24.85, 0.22, 0.50, 0.10),
            "pacifica": (13.92, 0.19, 0.58, 0.20),
            "orinoquia": (5.53, 0.17, 0.63, 0.42),
        }
        for region, (a, b, c, d) in publicados.items():
            valores = tabla[region]
            self.assertEqual(
                (valores["a"], valores["b"], valores["c"], valores["d"]),
                (a, b, c, d), region)
            self.assertTrue(valores["validado"],
                            f"{region} figura en la Tabla 57 y debe constar "
                            "como validada")
        self.assertFalse(tabla["amazonia"]["validado"])


class PruebaIntensidadSilva(unittest.TestCase):
    """
    Silva (1998): I = K / (d + b)^n, con d en minutos. NO es una ley potencial.

    La primera version lo implemento como P(t) = P24h * (t/1440)^0,25, que es
    una regla de desagregacion corriente pero no es este metodo, y hacia decaer
    la curva con exponente 0,75 en lugar de 0,6.
    """

    B, N, COEF = 10.0, 0.6, 0.369

    def _i(self, duracion, pmax=87.7):
        return m12a.intensidad_silva(duracion, pmax, self.COEF, self.B, self.N)

    def test_reproduce_la_k_del_informe_de_referencia(self) -> None:
        # Tabla 59 del numeral 5.5.2: Pmax24 de 87,7 mm da Pmax1h de 32,35 mm
        # y K de 413,95 con b = 10 min y n = 0,6.
        # El informe publica 32,35 y 0,369 x 87,7 da 32,3613: el coeficiente
        # que uso viene redondeado a tres decimales en el texto.
        intensidad_1h = self.COEF * 87.7
        self.assertAlmostEqual(intensidad_1h, 32.35, delta=0.02)
        k = intensidad_1h * (60.0 + self.B) ** self.N
        self.assertAlmostEqual(k, 413.95, delta=0.2)

    def test_la_curva_pasa_por_su_ancla_de_una_hora(self) -> None:
        self.assertAlmostEqual(self._i(60.0), self.COEF * 87.7, places=6)

    def test_decae_con_exponente_n_y_no_con_una_potencia_de_la_duracion(self) -> None:
        # Con la forma de Talbot el cociente entre dos duraciones depende de
        # (d + b) y no de d: es lo que separa este metodo de la ley potencial.
        esperado = ((180.0 + self.B) / (30.0 + self.B)) ** -self.N
        self.assertAlmostEqual(self._i(180.0) / self._i(30.0), esperado,
                               places=6)

    def test_la_intensidad_crece_al_acortar_la_duracion(self) -> None:
        self.assertGreater(self._i(5.0), self._i(180.0))

    def test_escala_linealmente_con_la_lamina_de_24_horas(self) -> None:
        self.assertAlmostEqual(self._i(60.0, 100.0) / self._i(60.0, 50.0), 2.0,
                               places=6)

    def test_magnitudes_no_positivas(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m12a.intensidad_silva(0.0, 87.7, self.COEF, self.B, self.N)
        with self.assertRaises(ErrorHidrologia):
            m12a.intensidad_silva(60.0, 87.7, self.COEF, self.B, -0.1)


class PruebaDesagregacion(unittest.TestCase):
    """Las tres hipótesis se calculan en paralelo y no se adopta ninguna."""

    def test_h1_toma_la_lamina_entera(self) -> None:
        salida = m12a.desagregar(57.3, 180.0, None, None)
        self.assertAlmostEqual(salida["h1_directa_mm"], 57.3, places=2)
        self.assertAlmostEqual(salida["h1_directa_sobre_p24"], 1.0, places=4)

    def test_h2_integra_la_curva_sobre_la_duracion(self) -> None:
        salida = m12a.desagregar(57.3, 180.0, 10.0, None)
        self.assertAlmostEqual(salida["h2_idf_mm"], 30.0, places=2)

    def test_h3_solo_aparece_con_coeficiente(self) -> None:
        sin_el = m12a.desagregar(57.3, 180.0, 10.0, None)
        self.assertNotIn("h3_factor_mm", sin_el)
        con_el = m12a.desagregar(57.3, 180.0, 10.0, 0.6)
        self.assertAlmostEqual(con_el["h3_factor_mm"], 34.38, places=2)

    def test_el_cociente_permite_comparar_las_tres(self) -> None:
        salida = m12a.desagregar(57.3, 180.0, 10.0, 0.6)
        self.assertAlmostEqual(salida["h3_factor_sobre_p24"], 0.6, places=3)


class PruebaCambioClimatico(unittest.TestCase):
    """
    Regla condicional de la sección 6: el factor se aplica SOLO si incrementa.
    """

    def test_un_incremento_se_aplica(self) -> None:
        salida = m12a.factor_de_cambio_climatico(12.0, True)
        self.assertTrue(salida["aplicado"])
        self.assertAlmostEqual(salida["factor_aplicado"], 1.12, places=4)

    def test_una_reduccion_no_afecta_al_hietograma(self) -> None:
        # Una reducción proyectada no es margen que se pueda gastar.
        salida = m12a.factor_de_cambio_climatico(-8.0, True)
        self.assertFalse(salida["aplicado"])
        self.assertEqual(salida["factor_aplicado"], 1.0)
        self.assertAlmostEqual(salida["factor_proyectado"], 0.92, places=4)
        self.assertIn("baja", salida["motivo"])

    def test_sin_la_regla_la_reduccion_si_se_aplicaria(self) -> None:
        salida = m12a.factor_de_cambio_climatico(-8.0, False)
        self.assertTrue(salida["aplicado"])
        self.assertAlmostEqual(salida["factor_aplicado"], 0.92, places=4)

    def test_el_factor_proyectado_se_registra_siempre(self) -> None:
        # Aunque no se aplique: el informe debe poder decir cuál era.
        self.assertIn("factor_proyectado",
                      m12a.factor_de_cambio_climatico(-20.0, True))


class PruebaCoeficientes(unittest.TestCase):
    def _escribir(self, texto: str) -> Path:
        ruta = Path(tempfile.mkdtemp()) / "coef.csv"
        ruta.write_text(texto, encoding="utf-8")
        return ruta

    def test_una_unidad_desconocida_se_rechaza(self) -> None:
        ruta = self._escribir(
            "region;a;b;c;d;unidad_duracion;origen;validado\n"
            "x;1;1;1;1;segundos;y;no\n")
        with self.assertRaises(ErrorFormato):
            m12a.leer_coeficientes(ruta, ";")

    def test_faltar_un_coeficiente_es_error(self) -> None:
        ruta = self._escribir("region;a;b;c;origen\nx;1;1;1;y\n")
        with self.assertRaises(ErrorFormato):
            m12a.leer_coeficientes(ruta, ";")

    def test_archivo_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorRutas):
            m12a.leer_coeficientes(Path("no_existe.csv"), ";")


class PruebaTablaReal(unittest.TestCase):
    """La tabla es doctrina y vive en data/referencia."""

    def setUp(self) -> None:
        self.tabla = m12a.leer_coeficientes(
            _RAIZ_REPO / _CFG.obtener("idf.coeficientes_invias"), ";")

    def test_trae_la_region_declarada(self) -> None:
        self.assertIn(str(_CFG.obtener("idf.region")), self.tabla)

    def test_cada_region_declara_origen_y_unidad(self) -> None:
        for region, valores in self.tabla.items():
            self.assertTrue(valores["origen"], f"{region} sin origen")
            self.assertIn(valores["unidad_duracion"], ("horas", "minutos"))

    def test_el_estado_de_validacion_viaja_al_reporte(self) -> None:
        # Unos coeficientes transcritos y no contrastados contra el manual no
        # valen lo mismo que unos verificados, y el informe debe decirlo.
        for valores in self.tabla.values():
            self.assertIsInstance(valores["validado"], bool)


if __name__ == "__main__":
    unittest.main(verbosity=2)

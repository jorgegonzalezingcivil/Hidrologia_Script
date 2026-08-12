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


class PruebaIntensidadSilva(unittest.TestCase):
    def test_reproduce_la_lamina_de_24_horas(self) -> None:
        # Por construcción: en t = 1440 min devuelve la P24h de partida, y por
        # eso en ese punto no verifica nada.
        intensidad = m12a.intensidad_silva(1440.0, 10.0, 57.3)
        self.assertAlmostEqual(
            m12a.lamina_de_intensidad(intensidad, 1440.0), 57.3, places=4)

    def test_la_intensidad_crece_al_acortar_la_duracion(self) -> None:
        self.assertGreater(m12a.intensidad_silva(5.0, 10.0, 57.3),
                           m12a.intensidad_silva(180.0, 10.0, 57.3))

    def test_la_lamina_crece_con_la_duracion(self) -> None:
        # La intensidad baja pero la lámina acumulada sube: son cosas distintas.
        corta = m12a.lamina_de_intensidad(
            m12a.intensidad_silva(5.0, 10.0, 57.3), 5.0)
        larga = m12a.lamina_de_intensidad(
            m12a.intensidad_silva(180.0, 10.0, 57.3), 180.0)
        self.assertLess(corta, larga)


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

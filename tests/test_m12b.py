# -*- coding: utf-8 -*-
"""
Pruebas del M12b: hietogramas de diseño por el método de Huff.

    python tests/test_m12b.py
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

import M12b_hietogramas as m12b  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorHidrologia, ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

CURVA = [
    {"tiempo_pct": 0.0, "precipitacion_pct": 0.0},
    {"tiempo_pct": 50.0, "precipitacion_pct": 74.0},
    {"tiempo_pct": 100.0, "precipitacion_pct": 100.0},
]


class PruebaCurva(unittest.TestCase):
    """
    La curva se valida al leerla: una mal transcrita no da error en ninguna
    parte, reparte la misma lámina de otra manera y produce un hidrograma
    verosímil y equivocado.
    """

    CABECERA = ("cuartil;probabilidad_pct;tiempo_pct;precipitacion_pct;"
                "origen;validado\n")

    def _escribir(self, filas: str) -> Path:
        ruta = Path(tempfile.mkdtemp()) / "huff.csv"
        ruta.write_text(self.CABECERA + filas, encoding="utf-8")
        return ruta

    def test_una_curva_que_no_empieza_en_cero_se_rechaza(self) -> None:
        ruta = self._escribir("2;50;10;5;x;no\n2;50;100;100;x;no\n")
        with self.assertRaises(ErrorFormato):
            m12b.leer_curva_huff(ruta, ";", 2, 50.0)

    def test_una_curva_que_no_termina_en_cien_se_rechaza(self) -> None:
        ruta = self._escribir("2;50;0;0;x;no\n2;50;100;95;x;no\n")
        with self.assertRaises(ErrorFormato):
            m12b.leer_curva_huff(ruta, ";", 2, 50.0)

    def test_una_acumulada_decreciente_se_rechaza(self) -> None:
        # Una lluvia acumulada no puede disminuir.
        ruta = self._escribir(
            "2;50;0;0;x;no\n2;50;50;60;x;no\n2;50;60;40;x;no\n"
            "2;50;100;100;x;no\n")
        with self.assertRaises(ErrorFormato):
            m12b.leer_curva_huff(ruta, ";", 2, 50.0)

    def test_un_cuartil_ausente_es_error_explicito(self) -> None:
        ruta = self._escribir("2;50;0;0;x;no\n2;50;100;100;x;no\n")
        with self.assertRaises(ErrorFormato):
            m12b.leer_curva_huff(ruta, ";", 3, 50.0)

    def test_archivo_ausente(self) -> None:
        with self.assertRaises(ErrorRutas):
            m12b.leer_curva_huff(Path("no_existe.csv"), ";", 2, 50.0)


class PruebaInterpolacion(unittest.TestCase):
    def test_en_un_nodo_devuelve_su_valor(self) -> None:
        self.assertAlmostEqual(m12b.acumulada_en(CURVA, 50.0), 74.0)

    def test_interpola_entre_nodos(self) -> None:
        self.assertAlmostEqual(m12b.acumulada_en(CURVA, 25.0), 37.0)

    def test_fuera_de_rango_se_satura(self) -> None:
        self.assertAlmostEqual(m12b.acumulada_en(CURVA, -5.0), 0.0)
        self.assertAlmostEqual(m12b.acumulada_en(CURVA, 150.0), 100.0)


class PruebaReparto(unittest.TestCase):
    """
    Cada intervalo es una DIFERENCIA de acumuladas: así la suma es exactamente
    la lámina de partida, y cualquier otra forma deja un residuo que se arrastra
    al volumen de escorrentía.
    """

    def test_la_suma_es_la_lamina(self) -> None:
        intervalos = m12b.repartir(100.0, 180.0, 5.0, CURVA)
        self.assertEqual(len(intervalos), 36)
        self.assertAlmostEqual(sum(i["lamina_mm"] for i in intervalos), 100.0,
                               delta=36 * 0.5e-4)

    def test_el_acumulado_final_es_la_lamina(self) -> None:
        intervalos = m12b.repartir(57.3, 180.0, 5.0, CURVA)
        self.assertAlmostEqual(intervalos[-1]["acumulado_mm"], 57.3, places=3)

    def test_el_acumulado_nunca_decrece(self) -> None:
        intervalos = m12b.repartir(80.0, 180.0, 10.0, CURVA)
        acumulados = [i["acumulado_mm"] for i in intervalos]
        self.assertEqual(acumulados, sorted(acumulados))

    def test_la_intensidad_es_coherente_con_la_lamina(self) -> None:
        intervalos = m12b.repartir(60.0, 120.0, 10.0, CURVA)
        for paso in intervalos:
            self.assertAlmostEqual(paso["intensidad_mm_h"],
                                   paso["lamina_mm"] * 6.0, places=2)

    def test_una_duracion_no_multiplo_del_intervalo_es_error(self) -> None:
        # Un intervalo truncado repartiría menos lámina sin decirlo.
        with self.assertRaises(ErrorHidrologia):
            m12b.repartir(100.0, 180.0, 7.0, CURVA)

    def test_magnitudes_no_positivas(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m12b.repartir(100.0, 0.0, 5.0, CURVA)


class PruebaResumen(unittest.TestCase):
    def test_el_pico_cae_en_el_segundo_cuartil(self) -> None:
        # Es lo que distingue una curva de Huff del segundo cuartil de otra: si
        # el pico no cae donde debe, la curva leída no es la que se cree.
        ruta = _RAIZ_REPO / _CFG.obtener("tormenta.huff.tabla")
        curva = m12b.leer_curva_huff(ruta, ";", 2, 50.0)
        intervalos = m12b.repartir(100.0, 180.0, 5.0, curva)
        resumen = m12b.resumir_hietograma(intervalos, 100.0)
        self.assertEqual(resumen["cuartil_del_pico"], 2)

    def test_el_residuo_se_reporta(self) -> None:
        intervalos = m12b.repartir(100.0, 180.0, 5.0, CURVA)
        resumen = m12b.resumir_hietograma(intervalos, 100.0)
        self.assertLess(abs(resumen["residuo_mm"]), 36 * 0.5e-4)

    def test_sin_intervalos_no_inventa_resumen(self) -> None:
        self.assertIn("error", m12b.resumir_hietograma([], 100.0))


class PruebaRazonDeDesagregacion(unittest.TestCase):
    """
    Es lo que distingue h2_idf de h1_directa: la primera mete la lámina de 24 h
    entera en la duración de diseño, la segunda toma de la IDF qué fracción le
    corresponde.
    """

    CABECERA = ("periodo_retorno;pmax24_mm;h2_idf_invias_razon_interna;"
                "h2_idf_silva_razon_interna\n")

    def _escribir(self, filas: str) -> Path:
        ruta = Path(tempfile.mkdtemp()) / "desagregacion.csv"
        ruta.write_text(self.CABECERA + filas, encoding="utf-8")
        return ruta

    def test_devuelve_la_razon_de_cada_periodo(self) -> None:
        ruta = self._escribir("2.33;42.0;0.4931;0.4232\n100.0;82.1;0.4931;0.4233\n")
        razones = m12b.razones_de_desagregacion(ruta, ";", "invias")
        self.assertAlmostEqual(razones["2.33"], 0.4931)
        self.assertAlmostEqual(razones["100.0"], 0.4931)

    def test_el_periodo_entero_queda_en_las_dos_formas(self) -> None:
        # La columna del M11 lo trae sin decimales y la tabla del M12a con
        # ellos: buscar solo una forma dejaría periodos sin razón, y esos
        # heredarían 1.0 en silencio, es decir, h1_directa.
        ruta = self._escribir("100.0;82.1;0.4931;0.4233\n")
        razones = m12b.razones_de_desagregacion(ruta, ";", "invias")
        self.assertIn("100", razones)
        self.assertIn("100.0", razones)

    def test_cada_fuente_da_lo_suyo(self) -> None:
        ruta = self._escribir("100.0;82.1;0.4931;0.4233\n")
        self.assertAlmostEqual(
            m12b.razones_de_desagregacion(ruta, ";", "silva")["100"], 0.4233)

    def test_una_razon_mayor_que_uno_se_rechaza(self) -> None:
        # La lámina de una duración parcial no puede superar la de 24 h.
        ruta = self._escribir("100.0;82.1;1.4;0.4233\n")
        with self.assertRaises(ErrorFormato):
            m12b.razones_de_desagregacion(ruta, ";", "invias")

    def test_una_fuente_inexistente_es_error_explicito(self) -> None:
        ruta = self._escribir("100.0;82.1;0.4931;0.4233\n")
        with self.assertRaises(ErrorFormato):
            m12b.razones_de_desagregacion(ruta, ";", "otra")

    def test_tabla_ausente(self) -> None:
        with self.assertRaises(ErrorRutas):
            m12b.razones_de_desagregacion(Path("no_existe.csv"), ";", "invias")


class PruebaAgrupacionPorZona(unittest.TestCase):
    """
    Para esto existe la zonificacion: en HEC-HMS cada hietograma distinto es un
    pluviometro, y uno por subcuenca y periodo son series que nadie mantiene.
    """

    SUBCUENCAS = [
        {"subcuenca": "A", "zona": "1", "area_km2": "9.0", "p_T10_mm": "50.0"},
        {"subcuenca": "B", "zona": "1", "area_km2": "1.0", "p_T10_mm": "60.0"},
        {"subcuenca": "C", "zona": "2", "area_km2": "4.0", "p_T10_mm": "80.0"},
    ]

    def test_una_fila_por_zona(self) -> None:
        zonas, _ = m12b.agrupar_por_zona(self.SUBCUENCAS, ["p_T10_mm"])
        self.assertEqual([z["zona"] for z in zonas], ["1", "2"])

    def test_la_lamina_de_la_zona_pondera_por_area(self) -> None:
        # 50 con peso 9 y 60 con peso 1 dan 51, no 55.
        zonas, _ = m12b.agrupar_por_zona(self.SUBCUENCAS, ["p_T10_mm"])
        self.assertAlmostEqual(zonas[0]["p_T10_mm"], 51.0, places=3)

    def test_cada_subcuenca_queda_asignada_a_su_pluviometro(self) -> None:
        _, asignacion = m12b.agrupar_por_zona(self.SUBCUENCAS, ["p_T10_mm"])
        self.assertEqual(
            {a["subcuenca"]: a["pluviometro"] for a in asignacion},
            {"A": "Z1", "B": "Z1", "C": "Z2"})

    def test_una_subcuenca_sin_zona_no_se_pierde(self) -> None:
        # Dejarla fuera la sacaria del modelo sin que nada lo senalara.
        subcuencas = self.SUBCUENCAS + [
            {"subcuenca": "D", "zona": "", "area_km2": "2.0",
             "p_T10_mm": "70.0"}]
        zonas, asignacion = m12b.agrupar_por_zona(subcuencas, ["p_T10_mm"])
        self.assertIn("sin_zona", [z["zona"] for z in zonas])
        self.assertEqual(len(asignacion), 4)

    def test_el_area_de_las_zonas_suma_la_del_conjunto(self) -> None:
        zonas, _ = m12b.agrupar_por_zona(self.SUBCUENCAS, ["p_T10_mm"])
        self.assertAlmostEqual(sum(z["area_km2"] for z in zonas), 14.0,
                               places=3)


class PruebaFactorArf(unittest.TestCase):
    FILAS = [{"duracion_h": "24.0", "arf": "0.9372"},
             {"duracion_h": "3.0", "arf": "0.8558"}]

    def test_toma_el_de_la_duracion_de_diseno(self) -> None:
        # No se recalcula: si dos partes del estudio interpolasen la misma
        # tabla, una discrepancia entre ellas no tendría dónde detectarse.
        self.assertAlmostEqual(m12b.factor_arf(self.FILAS, 3.0), 0.8558)

    def test_una_duracion_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m12b.factor_arf(self.FILAS, 6.0)


class PruebaTablaReal(unittest.TestCase):
    """La curva es doctrina y vive en data/referencia."""

    def setUp(self) -> None:
        self.curva = m12b.leer_curva_huff(
            _RAIZ_REPO / _CFG.obtener("tormenta.huff.tabla"), ";",
            int(_CFG.obtener("tormenta.huff.cuartil")),
            float(_CFG.obtener("tormenta.huff.probabilidad_excedencia")))

    def test_la_curva_existe_y_declara_su_origen(self) -> None:
        self.assertGreaterEqual(len(self.curva), 5)
        self.assertTrue(self.curva[0]["origen"])

    def test_la_unidad_del_hietograma_esta_declarada(self) -> None:
        self.assertIn(str(_CFG.obtener("tormenta.unidad_hietograma")),
                      ("zona", "subcuenca"))

    def test_la_duracion_es_multiplo_del_intervalo(self) -> None:
        # Con un intervalo que no divide la duración, el último quedaría
        # truncado y el módulo se detiene.
        duracion = float(_CFG.obtener("tormenta.duracion_h")) * 60.0
        intervalo = float(_CFG.obtener("tormenta.intervalo_calculo_min"))
        self.assertAlmostEqual(duracion % intervalo, 0.0, places=6)


class PruebaFactorDeEscalaTemporal(unittest.TestCase):
    """
    Una relación de escala liga DURACIONES, no frecuencias: el mismo factor
    para todos los periodos de retorno. Si varía, lo que se lee no es una
    escala, y eso fue justo lo que delató la primera version de h2_idf.
    """

    CABECERA = "periodo_retorno;pmax24_mm;h3_factor_mm;h3_factor_escala\n"

    def _escribir(self, filas: str) -> Path:
        ruta = Path(tempfile.mkdtemp()) / "desagregacion.csv"
        ruta.write_text(self.CABECERA + filas, encoding="utf-8")
        return ruta

    def test_devuelve_el_factor_unico(self) -> None:
        # 42.0 * 0.5946 = 24.97 y 82.1 * 0.5946 = 48.82
        ruta = self._escribir("2.33;42.0;24.97;0.5946\n100.0;82.1;48.82;0.5946\n")
        self.assertAlmostEqual(
            m12b.coeficiente_de_escala_del_m12a(ruta, ";"), 0.5946, places=3)

    def test_un_factor_que_varia_entre_periodos_se_rechaza(self) -> None:
        ruta = self._escribir("2.33;42.0;24.97;0.5946\n100.0;82.1;69.60;0.8477\n")
        with self.assertRaises(ErrorFormato):
            m12b.coeficiente_de_escala_del_m12a(ruta, ";")

    def test_un_factor_mayor_que_uno_se_rechaza(self) -> None:
        ruta = self._escribir("2.33;42.0;50.0;1.19\n100.0;82.1;97.74;1.19\n")
        with self.assertRaises(ErrorFormato):
            m12b.coeficiente_de_escala_del_m12a(ruta, ";")

    def test_sin_la_columna_es_error_explicito(self) -> None:
        ruta = Path(tempfile.mkdtemp()) / "desagregacion.csv"
        ruta.write_text("periodo_retorno;pmax24_mm\n100.0;82.1\n", encoding="utf-8")
        with self.assertRaises(ErrorFormato):
            m12b.coeficiente_de_escala_del_m12a(ruta, ";")

    def test_tabla_ausente(self) -> None:
        with self.assertRaises(ErrorRutas):
            m12b.coeficiente_de_escala_del_m12a(Path("no_existe.csv"), ";")


if __name__ == "__main__":
    unittest.main(verbosity=2)

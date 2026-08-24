# -*- coding: utf-8 -*-
"""
Pruebas del M14: ejecución de simulaciones y extracción de resultados.

    python tests/test_m14.py
"""

from __future__ import annotations

import datetime as _dt
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import hms  # noqa: E402
import M14_simulaciones as m14  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorHidrologia  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

INICIO = _dt.datetime(2000, 1, 1, 0, 0)


def instantes(cuantos: int, paso_min: int = 5) -> list[_dt.datetime]:
    return [INICIO + _dt.timedelta(minutes=paso_min * i) for i in range(cuantos)]


MODELO = """Basin: Basin 1
     Unit System: Metric
End:

Subbasin: SB1
     Area: 11.3349
     Downstream: J1

     LossRate: SCS
     Curve Number: 74.0
End:

Reach: R1
     Downstream: J1
End:

Junction: J1
     Downstream: Sink-1
End:

Sink: Sink-1
     Computation Point: Yes
End:
"""

SIMULACIONES = """Run: TR_2_33
     Log File: TR_2_33.log
     Basin: Basin 1
     Precip: T2_33
     Control: Tormenta_diseno
End:

Run: TR_500
     Log File: TR_500.log
     Basin: Basin 1
     Precip: T500
     Control: Tormenta_diseno
End:
"""


class PruebaVolumen(unittest.TestCase):
    """
    El caudal es un valor INSTANTÁNEO ('INST-VAL' en el DSS), no una media del
    intervalo: integrar por rectángulos sobrestima la rama ascendente.
    """

    def test_un_caudal_constante_da_su_volumen(self) -> None:
        # 1 m3/s durante 60 min son 3600 m3.
        volumen = m14.volumen_m3(instantes(13), [1.0] * 13)
        self.assertAlmostEqual(volumen, 3600.0, places=6)

    def test_un_triangulo_da_la_mitad_del_rectangulo(self) -> None:
        # Rampa de 0 a 2 m3/s en 60 min: 0.5 * 2 * 3600 = 3600 m3.
        valores = [2.0 * i / 12.0 for i in range(13)]
        self.assertAlmostEqual(m14.volumen_m3(instantes(13), valores), 3600.0,
                               places=6)

    def test_por_rectangulos_daria_otra_cosa(self) -> None:
        # La diferencia no es despreciable y por eso se integra por trapecios.
        valores = [2.0 * i / 12.0 for i in range(13)]
        rectangulos = sum(valores) * 300.0
        self.assertGreater(rectangulos, m14.volumen_m3(instantes(13), valores))

    def test_una_serie_de_un_punto_no_tiene_volumen(self) -> None:
        self.assertEqual(m14.volumen_m3(instantes(1), [5.0]), 0.0)


class PruebaLamina(unittest.TestCase):
    def test_un_millon_de_metros_cubicos_sobre_un_km2_son_mil_mm(self) -> None:
        self.assertAlmostEqual(m14.lamina_mm(1.0e6, 1.0), 1000.0, places=6)

    def test_area_nula_no_divide_por_cero(self) -> None:
        self.assertEqual(m14.lamina_mm(1.0e6, 0.0), 0.0)


class PruebaResumen(unittest.TestCase):
    def test_encuentra_el_pico_y_su_instante(self) -> None:
        valores = [0.0, 1.0, 5.0, 3.0, 1.0]
        resumen = m14.resumir_hidrograma(instantes(5), valores)
        self.assertAlmostEqual(resumen["qmax_m3s"], 5.0)
        self.assertAlmostEqual(resumen["t_pico_min"], 10.0)
        self.assertFalse(resumen["pico_en_el_borde"])

    def test_un_pico_al_final_se_marca(self) -> None:
        # No es un pico: es el mayor valor de una ventana que no contiene la
        # creciente, y el caudal que se leería sería menor que el real.
        resumen = m14.resumir_hidrograma(instantes(5), [0.0, 1.0, 2.0, 3.0, 4.0])
        self.assertTrue(resumen["pico_en_el_borde"])

    def test_un_pico_al_principio_tambien(self) -> None:
        resumen = m14.resumir_hidrograma(instantes(5), [4.0, 3.0, 2.0, 1.0, 0.0])
        self.assertTrue(resumen["pico_en_el_borde"])

    def test_una_serie_vacia_es_error_y_no_un_caudal_cero(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m14.resumir_hidrograma([], [])

    def test_marcas_y_valores_descuadrados_son_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m14.resumir_hidrograma(instantes(3), [1.0, 2.0])


class PruebaBalance(unittest.TestCase):
    """
    La lámina de exceso y el volumen del hidrograma directo son la misma agua
    contada de dos maneras.
    """

    def test_coincidencia_exacta_da_desviacion_nula(self) -> None:
        # 10 mm sobre 2 km2 son 20 000 m3.
        balance = m14.balance_de_subcuenca([4.0, 6.0], [20.0, 10.0],
                                           20000.0, 2.0)
        self.assertAlmostEqual(balance["exceso_mm"], 10.0)
        self.assertAlmostEqual(balance["perdida_mm"], 30.0)
        self.assertAlmostEqual(balance["precipitacion_mm"], 40.0)
        self.assertAlmostEqual(balance["desviacion_pct"], 0.0, places=6)

    def test_el_coeficiente_de_escorrentia_es_exceso_sobre_precipitacion(self) -> None:
        balance = m14.balance_de_subcuenca([10.0], [30.0], 0.0, 1.0)
        self.assertAlmostEqual(balance["coef_escorrentia"], 0.25)

    def test_un_area_equivocada_se_delata_en_la_desviacion(self) -> None:
        # El mismo volumen sobre la mitad del área duplica la lámina.
        balance = m14.balance_de_subcuenca([10.0], [0.0], 20000.0, 1.0)
        self.assertAlmostEqual(balance["desviacion_pct"], 100.0, places=6)

    def test_sin_exceso_no_hay_contra_que_comparar(self) -> None:
        # None no es cero: cero afirmaría que el balance cierra.
        balance = m14.balance_de_subcuenca([0.0], [30.0], 0.0, 1.0)
        self.assertIsNone(balance["desviacion_pct"])


class PruebaMonotonia(unittest.TestCase):
    ORDEN = ["2.33", "5", "10", "25"]

    def test_una_serie_creciente_no_tiene_fallos(self) -> None:
        self.assertEqual(m14.periodos_no_monotonos(
            {"2.33": 10.0, "5": 12.0, "10": 15.0, "25": 20.0}, self.ORDEN), [])

    def test_un_descenso_se_detecta(self) -> None:
        # Con la misma cuenca y el mismo método es imposible: delata una lámina
        # mal asignada o un DSS de un modelo anterior.
        fallos = m14.periodos_no_monotonos(
            {"2.33": 10.0, "5": 12.0, "10": 9.0, "25": 20.0}, self.ORDEN)
        self.assertEqual(fallos, [("5", "10")])

    def test_un_periodo_ausente_no_inventa_comparacion(self) -> None:
        fallos = m14.periodos_no_monotonos(
            {"2.33": 10.0, "25": 20.0}, self.ORDEN)
        self.assertEqual(fallos, [])


class PruebaLecturaDelModelo(unittest.TestCase):
    def test_clasifica_cada_elemento(self) -> None:
        elementos = m14.elementos_del_modelo(MODELO)
        self.assertEqual(elementos["SB1"]["tipo"], "Subbasin")
        self.assertEqual(elementos["R1"]["tipo"], "Reach")
        self.assertEqual(elementos["J1"]["tipo"], "Junction")
        self.assertEqual(elementos["Sink-1"]["tipo"], "Sink")

    def test_el_area_sale_del_basin(self) -> None:
        # Es la que HEC-HMS usó: verificar el balance contra otra cifra
        # compararía dos cosas distintas.
        elementos = m14.elementos_del_modelo(MODELO)
        self.assertAlmostEqual(elementos["SB1"]["area_km2"], 11.3349)
        self.assertIsNone(elementos["J1"]["area_km2"])

    def test_las_corridas_salen_del_run_con_su_meteorologia(self) -> None:
        self.assertEqual(m14.corridas_declaradas(SIMULACIONES),
                         [("TR_2_33", "T2_33"), ("TR_500", "T500")])

    def test_el_periodo_deshace_la_sustitucion_del_punto(self) -> None:
        self.assertEqual(m14.periodo_de_meteorologia("T2_33"), "2.33")
        self.assertEqual(m14.periodo_de_meteorologia("T500"), "500")


class PruebaOrdenDeLosPeriodos(unittest.TestCase):
    """
    HEC-HMS reordena las corridas ALFABÉTICAMENTE al guardar. Medido sobre el
    proyecto del estudio, el .run quedó T10, T100, T15, T2_33, T25, T5, T50,
    T500: en ese orden la comprobación de que el caudal crece con el periodo
    comparaba T100 contra T15 y detenía el módulo por un descenso inexistente.
    """

    ALFABETICO = ["10", "100", "15", "2.33", "25", "5", "50", "500"]
    NUMERICO = ["2.33", "5", "10", "15", "25", "50", "100", "500"]

    def test_ordena_por_valor_y_no_por_texto(self) -> None:
        self.assertEqual(m14.ordenar_periodos(self.ALFABETICO), self.NUMERICO)

    def test_quita_repetidos(self) -> None:
        self.assertEqual(m14.ordenar_periodos(["5", "10", "5"]), ["5", "10"])

    def test_una_creciente_real_no_se_denuncia_en_orden_de_archivo(self) -> None:
        # Los caudales del sitio de proyecto, que sí crecen con el periodo.
        caudales = dict(zip(self.NUMERICO,
                            [104.5, 184.1, 265.8, 318.9, 393.3, 508.5, 646.1,
                             1078.9]))
        self.assertEqual(
            m14.periodos_no_monotonos(caudales,
                                      m14.ordenar_periodos(self.ALFABETICO)),
            [])
        # Y en el orden del archivo saldrían fallos que no existen.
        self.assertNotEqual(
            m14.periodos_no_monotonos(caudales, self.ALFABETICO), [])


class PruebaAdaptadorHms(unittest.TestCase):
    """
    El vocabulario del guion está leído de 'hms/model/JythonHms.class' en el
    hms.jar de la instalación.
    """

    def test_el_guion_abre_el_proyecto_y_computa_cada_corrida(self) -> None:
        texto = hms.guion_de_corridas(
            "Refugio_del_Valle", Path("C:/HMS_refugio_del_valle"),
            ["TR_2_33", "TR_500"])
        self.assertIn("from hms.model.JythonHms import *", texto)
        self.assertIn('OpenProject("Refugio_del_Valle", ', texto)
        self.assertIn('ComputeRun("TR_2_33")', texto)
        self.assertIn('ComputeRun("TR_500")', texto)
        self.assertIn("Exit(0)", texto)

    def test_las_rutas_van_con_barra_normal(self) -> None:
        # Jython lee la contrabarra como escape: 'C:\\HMS' se convierte en un
        # tabulador y el proyecto no se encuentra, sin dar error.
        texto = hms.guion_de_corridas(
            "P", Path("C:/HMS_refugio_del_valle"), ["TR_5"])
        self.assertNotIn("\\", texto)

    def test_no_se_guarda_el_proyecto(self) -> None:
        # Reescribiría los archivos que el M13 acaba de dejar.
        texto = hms.guion_de_corridas("P", Path("C:/x"), ["TR_5"])
        self.assertNotIn("SaveAllProjectComponents", texto)

    def test_sin_corridas_es_error(self) -> None:
        with self.assertRaises(hms.ErrorHms):
            hms.guion_de_corridas("P", Path("C:/x"), [])

    def test_un_nombre_con_comillas_se_rechaza(self) -> None:
        with self.assertRaises(hms.ErrorHms):
            hms.guion_de_corridas("P", Path("C:/x"), ['TR"5'])

    def test_una_instalacion_inexistente_se_reporta(self) -> None:
        with self.assertRaises(hms.ErrorHms):
            hms.ruta_lanzador(Path("C:/no_existe_hec_hms"))


class PruebaLogDeCorrida(unittest.TestCase):
    """
    El log es la ÚNICA autoridad sobre si el cálculo sirvió: el proceso puede
    terminar en cero habiendo abortado todas las corridas.
    """

    def _log(self, texto: str) -> Path:
        ruta = Path(tempfile.mkdtemp()) / "TR_2_33.log"
        ruta.write_text(texto, encoding="utf-8")
        return ruta

    TERMINADA = (
        'NOTE 15301:  Began computing simulation run "TR_2_33" at time X.\n'
        'NOTE 20364:  Found no parameter problems in meteorologic model "T2_33".\n'
        'WARNING 41074:  Length for reach "R36" is very short.\n'
        'NOTE 15302:  Finished computing simulation run "TR_2_33" at time Y.\n'
        "NOTE 15312:  The total runtime for this simulation is 00:15.\n"
    )

    ABORTADA = (
        'NOTE 15301:  Began computing simulation run "TR_2_33" at time X.\n'
        'ERROR 45900:  No time of concentration set for subbasin "SB92".\n'
        'ERROR 40052:  Found "21" errors in basin model "Basin 1".\n'
        'WARNING 15303:  Aborted run "TR_2_33" at time Y.\n'
    )

    def test_una_corrida_terminada_es_utilizable(self) -> None:
        estado = hms.leer_log_de_corrida(self._log(self.TERMINADA))
        self.assertTrue(estado.terminada)
        self.assertFalse(estado.abortada)
        self.assertTrue(estado.utilizable)
        self.assertEqual(estado.duracion, "00:15")
        self.assertEqual(len(estado.advertencias), 1)

    def test_una_corrida_abortada_no_lo_es(self) -> None:
        estado = hms.leer_log_de_corrida(self._log(self.ABORTADA))
        self.assertTrue(estado.abortada)
        self.assertFalse(estado.utilizable)
        self.assertEqual(len(estado.errores), 2)

    def test_un_error_invalida_aunque_termine(self) -> None:
        estado = hms.leer_log_de_corrida(
            self._log(self.TERMINADA + "ERROR 12345:  algo salio mal.\n"))
        self.assertTrue(estado.terminada)
        self.assertFalse(estado.utilizable)

    def test_un_log_ausente_es_error_explicito(self) -> None:
        # Que falte significa que la corrida no llegó a empezar, y eso no puede
        # confundirse con una corrida sin incidencias.
        with self.assertRaises(hms.ErrorHms):
            hms.leer_log_de_corrida(Path("no_existe.log"))


class PruebaConfiguracion(unittest.TestCase):
    def test_el_computo_esta_declarado(self) -> None:
        self.assertIsInstance(_CFG.obtener("hec_hms.simulacion.ejecutar"), bool)

    def test_la_plantilla_no_fija_el_punto_de_un_estudio(self) -> None:
        # Es un dato del modelo de cada estudio: heredarlo apuntaría al elemento
        # de otro.
        self.assertEqual(
            str(_CFG.obtener("hec_hms.resultados.punto_de_proyecto", "")).strip(),
            "")

    def test_la_tolerancia_del_balance_es_estricta(self) -> None:
        self.assertLessEqual(
            float(_CFG.obtener("hec_hms.resultados.tolerancia_balance_pct")), 5.0)



class PruebaProfundidadNormal(unittest.TestCase):
    """Manning en seccion trapezoidal, resuelto por biseccion."""

    def test_el_calado_devuelve_el_caudal_que_se_le_pidio(self) -> None:
        # La comprobacion que importa: metido de vuelta en Manning, tiene que
        # dar el mismo caudal.
        import math
        n, b, z, s = 0.04, 10.0, 2.0, 0.01
        y = m14.profundidad_normal(8.0, n, b, z, s)
        area = (b + z * y) * y
        perimetro = b + 2.0 * y * math.sqrt(1.0 + z ** 2)
        caudal = area * (area / perimetro) ** (2 / 3) * math.sqrt(s) / n
        self.assertAlmostEqual(caudal, 8.0, places=2)

    def test_mas_caudal_pide_mas_calado(self) -> None:
        uno = m14.profundidad_normal(5.0, 0.04, 10.0, 2.0, 0.01)
        otro = m14.profundidad_normal(50.0, 0.04, 10.0, 2.0, 0.01)
        self.assertGreater(otro, uno)

    def test_un_caudal_grande_no_se_queda_sin_cota_superior(self) -> None:
        # El limite se duplica hasta pasarse: fijarlo a ojo dejaria sin
        # solucion los tramos de cierre, que son los de mas caudal.
        self.assertGreater(
            m14.profundidad_normal(5000.0, 0.04, 2.0, 1.0, 0.001), 0.0)

    def test_lo_que_no_es_positivo_es_error(self) -> None:
        for caudal, n, s in ((0.0, 0.04, 0.01), (5.0, 0.0, 0.01),
                             (5.0, 0.04, 0.0)):
            with self.assertRaises(ErrorHidrologia):
                m14.profundidad_normal(caudal, n, 10.0, 2.0, s)


class PruebaParametrosMuskingum(unittest.TestCase):
    """
    MUSKINGUM PIDE K Y X y la tabla del informe solo traia K. Sin X la
    parametrizacion no esta definida.
    """

    def _calcular(self, longitud=1000.0, celeridad=None):
        return m14.parametros_muskingum(
            8.0, 0.04, 10.0, 2.0, 0.01, longitud, celeridad)

    def test_la_k_es_la_longitud_sobre_la_celeridad(self) -> None:
        ficha = self._calcular(celeridad=2.0)
        self.assertAlmostEqual(ficha["k_s"], 500.0)
        self.assertAlmostEqual(ficha["k_min"], 8.33, places=2)

    def test_sin_celeridad_declarada_se_deriva_de_la_hidraulica(self) -> None:
        ficha = self._calcular()
        self.assertAlmostEqual(
            ficha["celeridad_ms"], 5.0 / 3.0 * ficha["velocidad_ms"], places=2)
        self.assertIn("velocidad", ficha["celeridad_origen"])

    def test_la_celeridad_declarada_manda(self) -> None:
        self.assertEqual(self._calcular(celeridad=1.5)["celeridad_ms"], 1.5)
        self.assertEqual(
            self._calcular(celeridad=1.5)["celeridad_origen"], "declarada")

    def test_x_queda_dentro_del_rango_fisico(self) -> None:
        for longitud in (50.0, 500.0, 5000.0):
            ficha = self._calcular(longitud=longitud)
            self.assertGreaterEqual(ficha["x"], 0.0)
            self.assertLessEqual(ficha["x"], 0.5)

    def test_un_tramo_corto_da_x_negativo_y_se_recorta_diciendolo(self) -> None:
        # No significa un cauce raro: significa que el tramo es demasiado largo
        # para un solo elemento a ese caudal. HEC-HMS rechazaria el crudo.
        ficha = self._calcular(longitud=5.0)
        self.assertEqual(ficha["x"], 0.0)
        self.assertTrue(ficha["x_recortado"])
        self.assertLess(ficha["x_crudo"], 0.0)

    def test_el_ancho_superior_es_mayor_que_el_de_fondo(self) -> None:
        ficha = self._calcular()
        self.assertGreater(ficha["ancho_superior_m"], 10.0)

    def test_una_longitud_nula_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            self._calcular(longitud=0.0)

if __name__ == "__main__":
    unittest.main(verbosity=2)

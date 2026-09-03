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


class PruebaLogViejo(unittest.TestCase):
    """
    Un log de la vez anterior no puede pasar por una corrida buena.

    Cuando HEC-HMS aborta al abrir el proyecto, por ejemplo porque un elemento
    del .basin esta mal escrito, no llega a tocar los logs de corrida: quedan
    los de la ejecucion previa, con su 'Finished' y sin errores, y el DSS
    conserva aquellos resultados. Sin mirar la fecha, el modulo devolvia
    CORRECTO sobre un modelo que ya no existe. Ocurrio de verdad.
    """

    CONTENIDO = ('NOTE 15301:  Began computing simulation run "TR_100".\n'
                 'NOTE 15302:  Finished computing simulation run "TR_100".\n')

    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.log = self.temporal / "TR_100.log"
        self.log.write_text(self.CONTENIDO, encoding="utf-8")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _envejecer(self, segundos: float) -> None:
        import os
        viejo = self.log.stat().st_mtime - segundos
        os.utime(self.log, (viejo, viejo))

    def test_sin_fecha_minima_se_lee_como_siempre(self) -> None:
        self._envejecer(86400)
        estado = hms.leer_log_de_corrida(self.log, "TR_100")
        self.assertTrue(estado.terminada)

    def test_un_log_anterior_al_lanzamiento_es_error(self) -> None:
        self._envejecer(86400)
        import time
        with self.assertRaises(hms.ErrorHms) as caso:
            hms.leer_log_de_corrida(self.log, "TR_100", desde=time.time())
        self.assertIn("ejecución anterior", str(caso.exception))

    def test_un_log_recien_escrito_pasa(self) -> None:
        import time
        estado = hms.leer_log_de_corrida(self.log, "TR_100",
                                         desde=time.time() - 60)
        self.assertTrue(estado.terminada)



class PruebaClasePorPendiente(unittest.TestCase):
    """
    Vive en el adaptador 'hms', que comparten el M13 y el M14.

    Estuvo en el M14, pero el M13 tambien la necesita para no depender de que
    el M14 haya corrido antes, y duplicarla era pedir que las dos copias se
    separaran con el tiempo.

    La celeridad y la X se declaran por clase de tramo.

    NO SALEN DE LA HIDRAULICA por dos razones medidas en este estudio: la
    linealizacion de Cunge devuelve X de 0,497 de mediana en los 62 tramos, es
    decir traslacion pura, y la celeridad que deriva cuelga de un calado de
    18 cm calculado sobre una seccion que nadie levanto. Ademas obligaba a
    linealizar en el caudal que el propio modelo produce, con lo que K y caudal
    se perseguian entre corridas.
    """

    CLASES = [
        {"nombre": "valle", "pendiente_min_pct": 0.0, "celeridad_ms": 0.7,
         "x": 0.15},
        {"nombre": "ladera", "pendiente_min_pct": 2.0, "celeridad_ms": 2.0,
         "x": 0.30},
        {"nombre": "montana", "pendiente_min_pct": 5.0, "celeridad_ms": 3.0,
         "x": 0.40},
    ]

    def test_elige_la_ultima_que_no_supera_la_pendiente(self) -> None:
        for pendiente, nombre in ((0.0, "valle"), (1.9, "valle"),
                                  (2.0, "ladera"), (4.9, "ladera"),
                                  (5.0, "montana")):
            with self.subTest(pendiente=pendiente):
                self.assertEqual(
                    hms.clase_por_pendiente(pendiente, self.CLASES)["nombre"],
                    nombre)

    def test_la_ultima_recoge_todo_lo_de_arriba(self) -> None:
        # Un tramo al 40 % no puede quedarse sin clase y sin parametros.
        self.assertEqual(
            hms.clase_por_pendiente(40.0, self.CLASES)["nombre"], "montana")

    def test_sin_clases_declaradas_no_se_inventa_una(self) -> None:
        self.assertIsNone(hms.clase_por_pendiente(3.0, []))

    def test_una_pendiente_por_debajo_de_la_primera_no_encaja(self) -> None:
        # Con la primera clase arrancando por encima de cero, una pendiente
        # menor no pertenece a ninguna y hay que decirlo, no asignarla a ojo.
        self.assertIsNone(hms.clase_por_pendiente(0.1, self.CLASES[1:]))


class PruebaEscenariosDeCambioClimatico(unittest.TestCase):
    """
    Los dos escenarios salen de una sola sesion de HEC-HMS y hay que separarlos.
    """

    def test_el_sufijo_dice_el_escenario(self) -> None:
        self.assertEqual(m14.escenario_de_meteorologia("T100"), "diseno")
        self.assertEqual(m14.escenario_de_meteorologia("T100_SF"), "referencia")

    def test_el_sufijo_no_entra_en_el_periodo_de_retorno(self) -> None:
        """
        'T100' y 'T100_SF' son el MISMO periodo con dos lluvias.

        Si el sufijo entrara en el periodo, el modulo veria dieciseis periodos
        de retorno en lugar de ocho y la tabla de caudales del informe quedaria
        sin sentido. Ocurrio: la extraccion cayo al graficar Qmax contra Tr.
        """
        self.assertEqual(m14.periodo_de_meteorologia("T2_33"), "2.33")
        self.assertEqual(m14.periodo_de_meteorologia("T2_33_SF"), "2.33")
        self.assertEqual(m14.periodo_de_meteorologia("T100_SF"), "100")

    def test_la_comparacion_da_el_aporte_del_factor(self) -> None:
        filas = m14.comparar_escenarios(
            {"Sink-1": {"100": 73.666}}, {"Sink-1": {"100": 60.279}}, "Sink-1")
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["q_diseno_m3s"], 73.666)
        self.assertEqual(filas[0]["q_referencia_m3s"], 60.279)
        self.assertAlmostEqual(filas[0]["aporte_factor_m3s"], 13.387)
        self.assertAlmostEqual(filas[0]["aporte_factor_pct"], 22.2)

    def test_el_aporte_no_es_el_factor(self) -> None:
        """
        Las perdidas del SCS no son lineales y el segundo escenario hay que
        correrlo, no deducirlo escalando el primero: en este estudio un 10,6 %
        mas de lluvia da un 22 % mas de caudal.
        """
        filas = m14.comparar_escenarios(
            {"S": {"100": 73.666}}, {"S": {"100": 60.279}}, "S")
        self.assertGreater(filas[0]["aporte_factor_pct"], 2 * 10.58)

    def test_solo_compara_los_periodos_que_tienen_los_dos(self) -> None:
        # Una corrida que no converge deja su periodo sin pareja, y restar
        # contra el que no esta daria un aporte inventado.
        filas = m14.comparar_escenarios(
            {"S": {"50": 57.7, "100": 73.7}}, {"S": {"100": 60.3}}, "S")
        self.assertEqual([f["periodo_retorno"] for f in filas], ["100"])

    def test_un_punto_que_no_esta_no_es_error(self) -> None:
        # Significa que ese elemento no lleva hidrograma, no que falte algo.
        self.assertEqual(m14.comparar_escenarios({}, {}, "Sink-1"), [])


class PruebaNombreDelTransito(unittest.TestCase):
    """
    El pie de la figura dice que metodo de transito corrio el modelo.

    ESTUVO ESCRITO A MANO. Decia 'Muskingum-Cunge', y cuando el consultor
    cambio el metodo adoptado a Muskingum el pie siguio diciendo lo mismo: una
    afirmacion falsa sobre el metodo, impresa en el informe, que nada
    comprobaba. Sale de la configuracion, que es la que el M13 honra al
    escribir el .basin.
    """

    def test_los_dos_metodos_que_la_cadena_escribe(self) -> None:
        self.assertEqual(m14.nombre_del_transito("muskingum"), "Muskingum")
        self.assertEqual(m14.nombre_del_transito("muskingum_cunge"),
                         "Muskingum-Cunge")

    def test_no_distingue_mayusculas_ni_espacios(self) -> None:
        self.assertEqual(m14.nombre_del_transito("  MUSKINGUM "), "Muskingum")

    def test_un_metodo_sin_declarar_se_dice(self) -> None:
        # Callarlo dejaria el pie afirmando un metodo por omision; decirlo
        # hace visible que la configuracion no lo declara.
        self.assertEqual(m14.nombre_del_transito(""), "no declarado")
        self.assertEqual(m14.nombre_del_transito(None), "no declarado")

    def test_un_metodo_desconocido_se_muestra_tal_cual(self) -> None:
        # Un metodo nuevo en la configuracion tiene que verse en la figura,
        # no convertirse en el nombre de otro.
        self.assertEqual(m14.nombre_del_transito("lag_and_route"),
                         "lag_and_route")


class PruebaTablaAncha(unittest.TestCase):
    """
    La tabla del informe, una fila por elemento y una columna por periodo.

    SE USA PARA LOS DOS ESCENARIOS. La plantilla presenta 'Qmax Vs. Periodo de
    Retorno (sin cambio climatico)' y la misma tabla con el factor aplicado, y
    las dos tienen que salir con la misma forma: si difirieran, la declaracion
    de una de las dos quedaria sin emparejar y su tabla se quedaria vacia en el
    informe, sin que nada lo advirtiera.
    """

    FILAS = [
        {"elemento": "Sink-1", "tipo": "Sink", "area_km2": 120.0,
         "periodo_retorno": "100", "qmax_m3s": 73.666, "t_pico_h": 6.5},
        {"elemento": "Sink-1", "tipo": "Sink", "area_km2": 120.0,
         "periodo_retorno": "2.33", "qmax_m3s": 13.98, "t_pico_h": 6.75},
    ]

    def test_las_columnas_van_en_orden_de_periodo(self) -> None:
        # HEC-HMS reordena las corridas alfabeticamente al guardar, y una tabla
        # de caudales con T100 antes que T15 se lee mal.
        filas = m14.tabla_ancha(self.FILAS, ["2.33", "100"])
        claves = [c for c in filas[0] if c.startswith("q_T")]
        self.assertEqual(claves, ["q_T2_33_m3s", "q_T100_m3s"])

    def test_el_punto_decimal_pasa_a_guion_bajo(self) -> None:
        # Es la misma sustitucion que el M13 hace en los nombres de las
        # meteorologias, porque HEC-HMS no admite el punto en un identificador.
        filas = m14.tabla_ancha(self.FILAS, ["2.33"])
        self.assertAlmostEqual(filas[0]["q_T2_33_m3s"], 13.98)

    def test_un_periodo_sin_corrida_queda_vacio_y_no_se_salta(self) -> None:
        """
        Saltarlo correria las columnas.

        Si una corrida no converge, su periodo no tiene fila. Omitir la columna
        dejaria la tabla del informe con siete encabezados y seis datos, y el
        caudal de cada periodo bajo el encabezado del siguiente.
        """
        filas = m14.tabla_ancha(self.FILAS, ["2.33", "5", "100"])
        self.assertIsNone(filas[0]["q_T5_m3s"])
        self.assertIsNone(filas[0]["tp_T5_h"])

    def test_los_dos_escenarios_dan_la_misma_forma(self) -> None:
        referencia = [dict(f, qmax_m3s=f["qmax_m3s"] / 1.2) for f in self.FILAS]
        periodos = ["2.33", "100"]
        self.assertEqual(list(m14.tabla_ancha(self.FILAS, periodos)[0]),
                         list(m14.tabla_ancha(referencia, periodos)[0]))

    def test_una_fila_por_elemento(self) -> None:
        mezcla = self.FILAS + [dict(self.FILAS[0], elemento="J24")]
        filas = m14.tabla_ancha(mezcla, ["2.33", "100"])
        self.assertEqual(sorted(f["elemento"] for f in filas),
                         ["J24", "Sink-1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

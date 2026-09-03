# -*- coding: utf-8 -*-
"""
Pruebas del M14c: verificación de crecientes contra caudal observado.

    python tests/test_m14c.py
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

import M14c_verificacion as m14c  # noqa: E402
import frecuencia as fr  # noqa: E402
import M13_hec_hms as m13  # noqa: E402


class PruebaMediaMovil(unittest.TestCase):
    """
    El equivalente en el modelo del dato observado.

    El limnígrafo reporta el mayor de los caudales MEDIOS DIARIOS; el modelo
    produce un hidrograma cuyo pico dura minutos. Comparar el pico contra una
    media diaria daría un sesgo de un factor de diez, medido en este estudio.
    """

    def test_una_serie_constante_da_ese_valor(self) -> None:
        # 48 h de caudal constante: cualquier ventana de 24 h da lo mismo.
        serie = [7.0] * (48 * 60 // 30)
        self.assertAlmostEqual(
            m14c.media_movil_maxima(serie, paso_min=30.0), 7.0)

    def test_encuentra_la_ventana_de_mayor_media(self) -> None:
        # Un dia en cero y el siguiente en diez: la mejor ventana es el segundo.
        por_dia = 24 * 60 // 60          # paso de 60 min
        serie = [0.0] * por_dia + [10.0] * por_dia
        self.assertAlmostEqual(
            m14c.media_movil_maxima(serie, paso_min=60.0), 10.0)

    def test_el_pico_se_diluye_al_promediar(self) -> None:
        # Una punta de una hora en 24 h de calma: la media es muy inferior al
        # pico. Es exactamente el efecto que obliga a promediar el modelo.
        serie = [0.0] * 23 + [240.0]
        media = m14c.media_movil_maxima(serie, paso_min=60.0)
        self.assertAlmostEqual(media, 10.0)
        self.assertGreater(max(serie) / media, 20)

    def test_un_hidrograma_mas_corto_que_la_ventana_no_tiene_media(self) -> None:
        # Devolver la media de lo que hay daria un numero que PARECE comparable
        # y no lo es: es el caso que obligo a ampliar la ventana a 36 h.
        serie = [1.0] * 12               # 12 h con paso de 60 min
        self.assertIsNone(m14c.media_movil_maxima(serie, paso_min=60.0))

    def test_un_paso_no_positivo_es_error(self) -> None:
        with self.assertRaises(ValueError):
            m14c.media_movil_maxima([1.0, 2.0], paso_min=0.0)


class PruebaPeriodosSostenidos(unittest.TestCase):

    PERIODOS = [2.33, 5, 10, 15, 25, 50, 100, 500]

    def test_recorta_lo_que_es_extrapolacion(self) -> None:
        # Con 29 anios y factor 2 se llega a Tr 58: 100 y 500 quedan fuera.
        sostenidos = m14c.periodos_sostenidos(29, self.PERIODOS, 2.0)
        self.assertEqual(sostenidos, [2.33, 5.0, 10.0, 15.0, 25.0, 50.0])

    def test_una_serie_corta_sostiene_muy_poco(self) -> None:
        self.assertEqual(m14c.periodos_sostenidos(15, self.PERIODOS, 2.0),
                         [2.33, 5.0, 10.0, 15.0, 25.0])

    def test_sin_registro_no_sostiene_nada(self) -> None:
        self.assertEqual(m14c.periodos_sostenidos(0, self.PERIODOS, 2.0), [])


class PruebaBanda(unittest.TestCase):

    def test_dentro_incluye_los_extremos(self) -> None:
        self.assertTrue(m14c.dentro_de_la_banda(5.0, 5.0, 9.0))
        self.assertTrue(m14c.dentro_de_la_banda(9.0, 5.0, 9.0))

    def test_fuera_por_arriba_y_por_abajo(self) -> None:
        self.assertFalse(m14c.dentro_de_la_banda(9.1, 5.0, 9.0))
        self.assertFalse(m14c.dentro_de_la_banda(4.9, 5.0, 9.0))


class PruebaEmparejamiento(unittest.TestCase):
    """
    Se busca la union mas proxima, no se declara una lista fija.

    Una lista serviria a un estudio y a ninguno mas. Sobre el estudio real la
    busqueda encontro TRES estaciones, una mas de las identificadas a mano.
    """

    UNIONES = {"J1": (0.0, 0.0), "J2": (1000.0, 0.0), "J3": (5000.0, 5000.0)}

    def test_empareja_con_la_mas_proxima(self) -> None:
        parejas, sin = m14c.emparejar_con_uniones(
            [("A", "EST A", 1100.0, 0.0)], self.UNIONES, 500.0)
        self.assertEqual(len(parejas), 1)
        self.assertEqual(parejas[0].union, "J2")
        self.assertAlmostEqual(parejas[0].distancia_m, 100.0)
        self.assertEqual(sin, [])

    def test_no_fuerza_una_union_lejana(self) -> None:
        # Compararia el modelo en un sitio contra la medida de otro, con areas
        # drenadas distintas.
        parejas, sin = m14c.emparejar_con_uniones(
            [("B", "EST B", 3000.0, 0.0)], self.UNIONES, 500.0)
        self.assertEqual(parejas, [])
        self.assertEqual(len(sin), 1)
        self.assertEqual(sin[0]["union_mas_cercana"], "J2")
        self.assertGreater(sin[0]["distancia_m"], 500.0)

    def test_un_modelo_sin_uniones_se_reporta(self) -> None:
        parejas, sin = m14c.emparejar_con_uniones(
            [("C", "EST C", 0.0, 0.0)], {}, 500.0)
        self.assertEqual(parejas, [])
        self.assertIn("uniones", sin[0]["motivo"])


class PruebaMaximosAnualesDeCaudal(unittest.TestCase):

    def test_un_anio_sin_un_mes_seco_sigue_sirviendo(self) -> None:
        # La creciente anual no ocurre en un mes seco: descartar el anio por esa
        # ausencia tira registro utilizable. Medido en SIMAYA, la regla de doce
        # meses dejaba 5 anios y la de temporada humeda deja 9.
        humedos = {4: 3.0, 5: 9.0, 10: 4.0, 11: 2.0}
        sin_enero = {**humedos, **{m: 1.0 for m in (2, 3, 6, 7, 8, 9, 12)}}
        sin_abril = {m: 1.0 for m in range(1, 13) if m != 4}
        maximos = m14c.maximos_anuales_de_mensuales(
            {2019: sin_enero, 2020: sin_abril},
            meses_exigidos=(4, 5, 10, 11))
        self.assertEqual(maximos, {2019: 9.0})

    def test_exige_los_doce_meses(self) -> None:
        # Un anio al que le falta la temporada de lluvias daria un maximo que no
        # es comparable con los demas de la muestra.
        completo = {m: float(m) for m in range(1, 13)}
        incompleto = {m: 99.0 for m in range(1, 12)}
        maximos = m14c.maximos_anuales_de_mensuales(
            {2019: completo, 2020: incompleto})
        self.assertEqual(maximos, {2019: 12.0})


class PruebaBandaDeConfianza(unittest.TestCase):
    """El bootstrap que sustituye a treinta formulas analiticas."""

    @classmethod
    def setUpClass(cls) -> None:
        import numpy as np
        generador = np.random.default_rng(7)
        cls.datos = list(generador.gumbel(loc=20.0, scale=5.0, size=30))

    def _banda(self, periodos=(2.33, 25.0)):
        return fr.banda_confianza(self.datos, "gumbel_max", "momentos_l",
                                  periodos, repeticiones=200)

    def test_el_cuantil_cae_dentro_de_su_banda(self) -> None:
        for periodo, dato in self._banda().items():
            self.assertLessEqual(dato["inferior"], dato["cuantil"])
            self.assertLessEqual(dato["cuantil"], dato["superior"])

    def test_es_reproducible_entre_corridas(self) -> None:
        # Una banda que cambia sola entre corridas es indefendible ante una
        # revision, aunque la diferencia sea pequena.
        self.assertEqual(self._banda(), self._banda())

    def test_la_banda_se_ensancha_con_el_periodo(self) -> None:
        banda = self._banda((2.33, 100.0))
        estrecha = banda[2.33]["superior"] - banda[2.33]["inferior"]
        ancha = banda[100.0]["superior"] - banda[100.0]["inferior"]
        self.assertGreater(ancha, estrecha)

    def test_una_distribucion_que_no_ajusta_no_inventa_banda(self) -> None:
        self.assertEqual(
            fr.banda_confianza(self.datos, "no_existe", "momentos_l", [10.0]),
            {})

    def test_rechaza_una_confianza_imposible(self) -> None:
        with self.assertRaises(fr.ErrorFrecuencia):
            fr.banda_confianza(self.datos, "gumbel_max", "momentos_l", [10.0],
                               confianza=1.5)

    def test_rechaza_una_muestra_insuficiente(self) -> None:
        with self.assertRaises(fr.ErrorFrecuencia):
            fr.banda_confianza([1.0, 2.0], "gumbel_max", "momentos_l", [10.0])



class PruebaFlujoBase(unittest.TestCase):
    """
    El bloque de flujo base que se le escribe a HEC-HMS.

    La sintaxis sale de los proyectos de muestra de HEC-HMS 4.13, que es la
    unica fuente con autoridad: el formato del .basin no esta publicado.
    """

    DECLARADO = {"metodo": "recesion", "factor_recesion": 0.8,
                 "caudal_especifico_m3s_km2": 0.00455, "umbral_pico": 0.1}

    def test_sin_declarar_no_se_inventa_un_metodo(self) -> None:
        # Un 'Recession' sin parametros seria un metodo declarado y vacio.
        for vacio in (None, {}, {"metodo": "ninguno"}):
            grupo, valor, campos = m13.grupo_de_flujo_base(vacio)
            self.assertEqual((grupo, valor, campos), ("Baseflow", "None", ()))

    def test_escribe_los_cuatro_campos_de_la_recesion(self) -> None:
        grupo, valor, campos = m13.grupo_de_flujo_base(self.DECLARADO)
        self.assertEqual((grupo, valor), ("Baseflow", "Recession"))
        claves = dict(campos)
        self.assertEqual(claves["Recession Factor"], "0.800")
        self.assertEqual(claves["Threshold Flow to Peak Ratio"], "0.100")
        self.assertEqual(claves["Initial Variable"], "Combined Inflow")

    def test_el_caudal_especifico_no_se_redondea_a_cero(self) -> None:
        # Con dos decimales, 0,00455 m3/s/km2 se escribiria como 0,00 y el
        # modelo quedaria sin flujo base sin que nada lo dijera.
        claves = dict(m13.grupo_de_flujo_base(self.DECLARADO)[2])
        self.assertEqual(float(claves["Initial Flow/Area Ratio"]), 0.00455)



class PruebaConsistenciaInterna(unittest.TestCase):
    """
    Las comprobaciones que no necesitan estaciones de caudal.

    En este estudio las cuatro detectaron un error real antes de que ninguna
    estacion dijera nada, y son lo unico que un estudio sin limnimetria puede
    oponerle a los resultados del modelo.
    """

    def test_la_creciente_debe_crecer_mas_que_su_lluvia(self) -> None:
        # Cota FISICA por abajo: el coeficiente de escorrentia sube con la
        # magnitud del evento. Fue lo que delato a EL VERGEL como regulada.
        regulada = m14c.crecimiento_relativo(16.2, 36.1, 21.9, 49.9)
        self.assertLess(regulada, 1.05)
        natural = m14c.crecimiento_relativo(6.5, 30.9, 21.9, 49.9)
        self.assertGreater(natural, 1.5)

    def test_delata_el_umbral_de_perdidas(self) -> None:
        # Con Ia = 0,2*S el modelo crecia 13,6 veces contra 2,28 de la lluvia.
        con_umbral = m14c.crecimiento_relativo(6.6, 89.7, 21.9, 49.9)
        self.assertGreater(con_umbral, 3.0)

    def test_sin_datos_no_devuelve_un_cero_que_parece_medida(self) -> None:
        for caso in ((0.0, 10.0, 21.9, 49.9), (1.0, 2.0, 0.0, 49.9),
                     (None, 2.0, 21.9, 49.9)):
            with self.subTest(caso=caso):
                self.assertIsNone(m14c.crecimiento_relativo(*caso))

    def test_el_exponente_de_area_sale_de_dos_puntos_anidados(self) -> None:
        # Q proporcional a A^n: con n = 1 el caudal escala como el area.
        self.assertAlmostEqual(
            m14c.exponente_de_area(10.0, 50.0, 20.0, 100.0), 1.0, places=6)
        n = m14c.exponente_de_area(45.3, 81.31, 97.3, 220.60)
        self.assertGreater(n, 0.6)
        self.assertLess(n, 0.9)

    def test_un_area_que_no_crece_no_define_exponente(self) -> None:
        self.assertIsNone(m14c.exponente_de_area(10.0, 100.0, 20.0, 100.0))
        self.assertIsNone(m14c.exponente_de_area(0.0, 50.0, 20.0, 100.0))

    def test_la_banda_dice_por_que_lado_se_sale(self) -> None:
        self.assertEqual(m14c.fuera_de_banda(0.5, [1.0, 3.0]),
                         "por debajo de 1")
        self.assertEqual(m14c.fuera_de_banda(5.96, [1.0, 3.0]),
                         "por encima de 3")
        self.assertEqual(m14c.fuera_de_banda(2.32, [1.0, 3.0]), "")

    def test_la_ausencia_de_medida_no_es_incumplimiento(self) -> None:
        self.assertEqual(m14c.fuera_de_banda(None, [1.0, 3.0]), "")



class PruebaAreasAcumuladas(unittest.TestCase):
    """
    Area drenada por elemento y cuales llevan un embalse en su cuenca.

    LO QUE INVALIDA EL EXPONENTE es que el caudal del punto de aguas abajo
    llegue laminado. Ese es el de ABAJO, no el de arriba: en este estudio el
    embalse recorta 50 m3/s a 5,5, y una pareja que lo cruce mide el embalse.
    """

    MODELO = """Subbasin: SB1
     Area: 10.0
     Downstream: J1
End:

Subbasin: SB2
     Area: 30.0
     Downstream: E1
End:

Reservoir: E1
     Downstream: J1
End:

Junction: J1
     Downstream: Sink-1
End:

Sink: Sink-1
End:
"""

    def test_acumula_hacia_aguas_abajo(self) -> None:
        areas, _ = m14c.areas_acumuladas(self.MODELO)
        self.assertAlmostEqual(areas["J1"], 40.0)
        self.assertAlmostEqual(areas["E1"], 30.0)

    def test_afectado_es_el_que_tiene_el_embalse_encima(self) -> None:
        _, afectados = m14c.areas_acumuladas(self.MODELO)
        self.assertIn("J1", afectados)
        self.assertIn("Sink-1", afectados)
        # SB2 esta AGUAS ARRIBA del embalse: su caudal no llega laminado.
        self.assertNotIn("SB2", afectados)
        self.assertNotIn("SB1", afectados)

    def test_un_modelo_sin_embalses_no_aparta_nada(self) -> None:
        sin_embalse = self.MODELO.replace("Reservoir: E1", "Junction: E1")
        _, afectados = m14c.areas_acumuladas(sin_embalse)
        self.assertEqual(afectados, set())


class PruebaColumnaDeContraste(unittest.TestCase):
    """
    La condicion de cada pareja, en palabras, para la tabla del informe.

    POR QUE NO BASTA CON 'indicativa'. Una columna que diga 'True' obliga al
    lector a saber que significa, y la distincion no es menor: una pareja
    indicativa tiene menos anios de los que se exigen para verificar, NO cuenta
    para el veredicto y no sostiene por si sola un cambio de parametro, porque
    con pocos anios el ajuste de frecuencia lo decide un solo ano extremo.
    """

    def test_las_dos_columnas_dicen_lo_mismo(self) -> None:
        ruta = (_RAIZ_REPO.parent / "Estudios" / "refugio_del_valle" / "data"
                / "02_procesado" / "hidrologia" / "verificacion_crecientes.csv")
        if not ruta.is_file():
            self.skipTest("no hay productos de verificacion de un estudio")
        import csv

        with ruta.open(encoding="utf-8-sig", newline="") as manejador:
            filas = list(csv.DictReader(manejador, delimiter=";"))
        self.assertTrue(filas)
        for fila in filas:
            with self.subTest(estacion=fila["estacion"]):
                indicativa = fila["indicativa"].strip().lower() == "true"
                self.assertEqual(fila["contraste"] == "Indicativa", indicativa)
                self.assertIn(fila["contraste"], ("Indicativa", "Verificación"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

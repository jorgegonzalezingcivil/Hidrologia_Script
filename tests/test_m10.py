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
from comun.errores import ErrorHidrologia, ErrorRutas  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_raster import escribir_tiff  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)
_CUENCA = _RAIZ_REPO / "data" / "03_SIG" / "vector" / "area_influencia.shp"
HAY_CUENCA = _CUENCA.is_file()
_DEM = _RAIZ_REPO / _CFG.obtener("dem.delimitacion.salida_dem")
HAY_DEM = _DEM.is_file()


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
        evaluadas = [{"formula": f"f{i}", "aplicable": True, "tc_horas": 2.0 + i}
                     for i in range(6)]
        self.assertTrue(m10.resumir_adopcion(evaluadas, 5)["procede_adoptar"])

    def test_aplicable_sin_calcular_no_basta(self) -> None:
        # Ser aplicable y haberse podido calcular son cosas distintas. Una
        # formula dentro de su rango a la que le falta una magnitud no aporta
        # nada a la mediana, y contarla daria un subconjunto ficticio.
        evaluadas = [{"formula": f"f{i}", "aplicable": True, "tc_horas": None}
                     for i in range(6)]
        resumen = m10.resumir_adopcion(evaluadas, 5)
        self.assertEqual(resumen["formulas_aplicables"], 6)
        self.assertEqual(resumen["formulas_adoptables"], 0)
        self.assertFalse(resumen["procede_adoptar"])

    def test_con_pocas_no_procede(self) -> None:
        evaluadas = [{"formula": f"f{i}", "aplicable": i < 3, "tc_horas": 2.0}
                     for i in range(6)]
        resumen = m10.resumir_adopcion(evaluadas, 5)
        self.assertFalse(resumen["procede_adoptar"])
        self.assertEqual(resumen["formulas_aplicables"], 3)

    def test_sin_ninguna_aplicable(self) -> None:
        evaluadas = [{"formula": "f", "aplicable": False, "tc_horas": 2.0}]
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


class PruebaRelieveSintetico(unittest.TestCase):
    """
    Un plano inclinado de pendiente conocida es el único caso en el que se sabe
    de antemano cuánto debe dar el cálculo. Si Horn no reproduce ahí la cifra
    exacta, ningún resultado sobre terreno real es defendible.
    """

    CELDA = 10.0
    ORIGEN = (1000.0, 2000.0)

    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _plano(self, alto: int, ancho: int, pendiente: float) -> Path:
        # La cota crece hacia el este; la fila no influye.
        valores = [[100.0 + pendiente * self.CELDA * i for i in range(ancho)]
                   for _ in range(alto)]
        return escribir_tiff(self.temporal / "plano.tif", valores,
                             celda=self.CELDA, origen=self.ORIGEN)

    def _cuadrado(self, celdas: int):
        """Polígono que encierra las celdas interiores del ráster."""
        x0 = self.ORIGEN[0] + self.CELDA
        y1 = self.ORIGEN[1] - self.CELDA
        lado = (celdas - 2) * self.CELDA
        return [[[(x0, y1 - lado), (x0, y1), (x0 + lado, y1),
                  (x0 + lado, y1 - lado)]]]

    def test_un_plano_inclinado_devuelve_su_pendiente(self) -> None:
        dem = self._plano(12, 12, 0.20)
        resultado = m10.estadisticas_de_relieve(
            dem, self._cuadrado(12), intervalo_m=5.0, escalas=(2,))
        self.assertAlmostEqual(
            resultado["pendiente_media_cuenca"], 0.20, places=5)
        # La mediana sale del histograma de pendiente, de modo que su
        # resolución es la de una casilla. Exigirle más sería exigirle una
        # precisión que el propio producto no declara.
        self.assertAlmostEqual(resultado["pendiente_mediana"], 0.20,
                               delta=m10.PASO_PENDIENTE)

    def test_una_superficie_horizontal_no_tiene_pendiente(self) -> None:
        dem = self._plano(12, 12, 0.0)
        resultado = m10.estadisticas_de_relieve(
            dem, self._cuadrado(12), intervalo_m=5.0, escalas=(2,))
        self.assertAlmostEqual(resultado["pendiente_media_cuenca"], 0.0)
        self.assertEqual(resultado["desnivel_altitudinal"], 0.0)

    def test_el_area_por_conteo_de_celdas_coincide(self) -> None:
        dem = self._plano(12, 12, 0.10)
        resultado = m10.estadisticas_de_relieve(
            dem, self._cuadrado(12), intervalo_m=5.0, escalas=(2,))
        # 10 x 10 celdas de 10 x 10 m.
        self.assertEqual(resultado["celdas_con_dato"], 100)
        self.assertAlmostEqual(resultado["area_por_dem_km2"], 0.01, places=6)

    def test_las_cotas_extremas_son_las_del_plano(self) -> None:
        dem = self._plano(12, 12, 0.10)
        resultado = m10.estadisticas_de_relieve(
            dem, self._cuadrado(12), intervalo_m=5.0, escalas=(2,))
        # Columnas 1 a 10: cota 100 + 1*10*0,10 hasta 100 + 10*10*0,10.
        self.assertAlmostEqual(resultado["cota_min"], 101.0, places=4)
        self.assertAlmostEqual(resultado["cota_max"], 110.0, places=4)
        self.assertAlmostEqual(resultado["cota_media"], 105.5, places=4)

    def test_las_celdas_sin_dato_no_entran(self) -> None:
        valores = [[100.0 + 0.5 * i for i in range(12)] for _ in range(12)]
        for j in range(12):
            valores[j][5] = -9999.0
        dem = escribir_tiff(self.temporal / "hueco.tif", valores,
                            celda=self.CELDA, origen=self.ORIGEN)
        resultado = m10.estadisticas_de_relieve(
            dem, self._cuadrado(12), intervalo_m=5.0, escalas=(2,))
        self.assertEqual(resultado["celdas_en_cuenca"], 100)
        self.assertEqual(resultado["celdas_con_dato"], 90)
        self.assertAlmostEqual(resultado["cobertura_dem_pct"], 90.0, places=3)
        # Y ninguna ventana que toque el hueco produce pendiente.
        self.assertLess(resultado["celdas_con_pendiente"], 90)

    def test_una_cuenca_fuera_del_raster_es_error(self) -> None:
        dem = self._plano(12, 12, 0.10)
        lejos = [[[(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)]]]
        with self.assertRaises(ErrorHidrologia) as contexto:
            m10.estadisticas_de_relieve(dem, lejos)
        self.assertIn("CRS", str(contexto.exception))

    def test_el_ruido_estimado_recupera_su_magnitud(self) -> None:
        """
        Se contamina un plano horizontal con ruido de desviación conocida y se
        comprueba que el estimador la recupera. Es lo que sostiene la
        advertencia sobre el DEM de radar en terreno llano.
        """
        import random

        random.seed(20260809)
        sigma = 1.5
        valores = [[100.0 + random.gauss(0.0, sigma) for _ in range(120)]
                   for _ in range(120)]
        dem = escribir_tiff(self.temporal / "ruido.tif", valores,
                            celda=self.CELDA, origen=self.ORIGEN)
        resultado = m10.estadisticas_de_relieve(
            dem, self._cuadrado(120), intervalo_m=1.0, escalas=(8,),
            pendiente_llana=0.05)
        estimado = resultado["ruido_vertical_estimado_m"]
        self.assertIsNotNone(estimado)
        self.assertAlmostEqual(estimado, sigma, delta=0.15 * sigma)


class PruebaCurvaHipsometrica(unittest.TestCase):
    def test_va_de_area_completa_a_cero(self) -> None:
        import numpy as np

        cuentas = np.array([10, 20, 30, 40])
        bordes = np.array([0.0, 25.0, 50.0, 75.0, 100.0])
        curva = m10.curva_hipsometrica(cuentas, bordes, 100.0)
        self.assertAlmostEqual(curva[0]["area_relativa"], 1.0)
        self.assertAlmostEqual(curva[-1]["area_relativa"], 0.0)
        self.assertAlmostEqual(curva[0]["cota_relativa"], 0.0)
        self.assertAlmostEqual(curva[-1]["cota_relativa"], 1.0)

    def test_decrece_siempre(self) -> None:
        import numpy as np

        curva = m10.curva_hipsometrica(
            np.array([5, 1, 9, 3]), np.array([0.0, 1.0, 2.0, 3.0, 4.0]), 1.0)
        areas = [p["area_relativa"] for p in curva]
        self.assertEqual(areas, sorted(areas, reverse=True))

    def test_sin_celdas_no_hay_curva(self) -> None:
        import numpy as np

        self.assertEqual(
            m10.curva_hipsometrica(np.zeros(3), np.arange(4.0), 1.0), [])


@unittest.skipUnless(HAY_CUENCA and HAY_DEM, "no hay cuenca o DEM")
class PruebaRelieveReal(unittest.TestCase):
    """
    Sobre el DEM del estudio. Son lentas (medio minuto) porque recorren
    456 MB de terreno, y no se sustituyen por una versión reducida: el
    contraste entre el área contada del ráster y la del polígono solo tiene
    valor si se hace sobre los insumos reales.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.relieve = m10.estadisticas_de_relieve(
            _DEM, shapefile.leer_geometrias(_CUENCA),
            intervalo_m=float(_CFG.obtener(
                "morfometria.relieve.intervalo_hipsometrico_m")),
            escalas=_CFG.obtener("morfometria.relieve.escalas_diagnostico"),
            pendiente_llana=float(_CFG.obtener(
                "morfometria.relieve.pendiente_llana_mm")))

    def test_el_area_del_raster_coincide_con_la_del_poligono(self) -> None:
        # Contrasta dos caminos independientes: el barrido del ráster y la
        # fórmula del área del polígono. Que coincidan valida los dos.
        del_poligono = shapefile.area_poligonos(_CUENCA) / 1e6
        self.assertAlmostEqual(
            self.relieve["area_por_dem_km2"], del_poligono,
            delta=0.001 * del_poligono)

    def test_las_cotas_son_coherentes_entre_si(self) -> None:
        r = self.relieve
        self.assertLess(r["cota_min"], r["cota_p1"])
        self.assertLess(r["cota_p1"], r["cota_mediana"])
        self.assertLess(r["cota_mediana"], r["cota_p99"])
        self.assertLess(r["cota_p99"], r["cota_max"])
        self.assertAlmostEqual(
            r["desnivel_altitudinal"], r["cota_max"] - r["cota_min"], places=2)

    def test_la_integral_hipsometrica_esta_en_su_rango(self) -> None:
        self.assertTrue(0.0 <= self.relieve["integral_hipsometrica"] <= 1.0)

    def test_la_pendiente_baja_al_agregar(self) -> None:
        # Es la propiedad que da sentido al diagnóstico: si no bajara, la
        # comparación entre resoluciones no diría nada.
        escalas = self.relieve["pendiente_por_escala"]
        medias = ([self.relieve["pendiente_media_cuenca"]]
                  + [e["pendiente_media_mm"] for e in escalas])
        self.assertEqual(medias, sorted(medias, reverse=True))

    def test_el_dem_esta_en_el_crs_de_calculo(self) -> None:
        self.assertEqual(self.relieve["crs_dem"], _CFG.obtener("crs.calculo"))


class PruebaRecorteDeColaAjena(unittest.TestCase):
    """
    El polígono de la unidad puede rebasar unos metros la confluencia con el
    cauce receptor, y entonces el recorrido más largo continúa por él. Medido
    sobre este estudio, el cauce terminaba con 0,22 km de Río Magdalena tras
    242,85 km de Río Bogotá.
    """

    def _tramos(self, nombres):
        return {i: {"nombre": n} for i, n in enumerate(nombres)}

    def test_recorta_la_cola_corta_de_otro_rio(self) -> None:
        # Proporciones del caso real: 0,22 km de cola tras 242,85 km de cauce.
        nombres = ["Rio A"] * 200 + ["Rio B"]
        longitudes = {i: 1000.0 for i in range(201)}
        camino, informe = m10.recortar_cola_ajena(
            list(range(201)), self._tramos(nombres), longitudes)
        self.assertTrue(informe["recortado"])
        self.assertEqual(len(camino), 200)
        self.assertEqual(informe["cola_ajena_nombre"], "Rio B")
        self.assertEqual(informe["cauce_adoptado"], "Rio A")

    def test_no_recorta_si_la_cola_es_grande(self) -> None:
        # Un trozo ajeno grande no es un rebase del poligono sino un problema
        # de delimitacion, y debe verse en el resultado en lugar de taparse.
        nombres = ["Rio A"] * 30 + ["Rio B"]
        longitudes = {i: 1000.0 for i in range(30)}
        longitudes[30] = 1200.0
        camino, informe = m10.recortar_cola_ajena(
            list(range(31)), self._tramos(nombres), longitudes)
        self.assertFalse(informe["recortado"])
        self.assertTrue(informe["excede_el_limite"])
        self.assertEqual(len(camino), 31)

    def test_un_cambio_de_nombre_sustancial_es_el_mismo_cauce(self) -> None:
        # Un rio que corre diez kilometros con un nombre y diez con otro no
        # tiene cola ajena: cambia de nombre, que es lo normal.
        nombres = ["Rio A"] * 10 + ["Rio B"] * 10
        longitudes = {i: 1000.0 for i in range(20)}
        camino, informe = m10.recortar_cola_ajena(
            list(range(20)), self._tramos(nombres), longitudes)
        self.assertFalse(informe["recortado"])
        self.assertEqual(len(camino), 20)

    def test_no_toca_un_cauce_de_un_solo_rio(self) -> None:
        longitudes = {i: 1000.0 for i in range(10)}
        camino, informe = m10.recortar_cola_ajena(
            list(range(10)), self._tramos(["Rio A"] * 10), longitudes)
        self.assertFalse(informe["recortado"])
        self.assertEqual(len(camino), 10)

    def test_los_afluentes_de_cabecera_no_se_confunden_con_cola(self) -> None:
        # El cauce empieza por quebradas con otro nombre y termina en el rio.
        # Eso es normal y no debe recortarse nada.
        nombres = ["Quebrada X", "Quebrada Y"] + ["Rio A"] * 18
        longitudes = {i: 1000.0 for i in range(20)}
        camino, informe = m10.recortar_cola_ajena(
            list(range(20)), self._tramos(nombres), longitudes)
        self.assertFalse(informe["recortado"])
        self.assertEqual(len(camino), 20)

    def test_un_camino_vacio_no_revienta(self) -> None:
        self.assertEqual(m10.recortar_cola_ajena([], {}, {})[0], [])


@unittest.skipUnless(HAY_CUENCA, "no hay capa de cuenca")
class PruebaDrenajeReal(unittest.TestCase):
    """Sobre la red del M02b, si existe."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ruta = _RAIZ_REPO / _CFG.obtener("red_topologica.salida_red")
        if not cls.ruta.is_file():
            raise unittest.SkipTest("no existe la red del M02b")
        muestra = m10._muestreador_de_cota(_DEM) if HAY_DEM else None
        try:
            cls.drenaje = m10.parametros_de_red(
                cls.ruta, shapefile.leer_geometrias(_CUENCA), 5925.891, muestra)
        finally:
            if muestra is not None:
                muestra.cerrar()

    def test_el_orden_y_las_corrientes_son_coherentes(self) -> None:
        d = self.drenaje
        self.assertGreaterEqual(d["orden_corrientes"], 1)
        self.assertEqual(max(d["corrientes_por_orden"]), d["orden_corrientes"])
        # En una red bien formada hay mas corrientes de orden bajo que alto.
        cuentas = [c for _, c in sorted(d["corrientes_por_orden"].items())]
        self.assertEqual(cuentas, sorted(cuentas, reverse=True))

    def test_el_cauce_principal_no_excede_la_red(self) -> None:
        self.assertLess(self.drenaje["long_cauce_principal_km"],
                        self.drenaje["long_cauces_km"])

    def test_la_sinuosidad_no_puede_ser_menor_que_uno(self) -> None:
        # Un cauce nunca es mas corto que la recta entre sus extremos.
        self.assertGreaterEqual(self.drenaje["indice_sinuosidad"], 1.0)

    def test_el_cauce_recorre_el_rio_de_la_cuenca(self) -> None:
        # El control barato que atrapa una red mal empalmada.
        self.assertIn("Bogotá", self.drenaje["nombres_del_cauce_principal"])

    @unittest.skipUnless(HAY_DEM, "no hay DEM")
    def test_el_cauce_desciende(self) -> None:
        self.assertGreater(self.drenaje["cota_nacimiento"],
                           self.drenaje["cota_cierre"])
        self.assertGreater(self.drenaje["pendiente_media_cauce"], 0.0)


class PruebaTiempoDeRezago(unittest.TestCase):
    """
    CLAUDE.md, sección 6: Δt es el INTERVALO DE CÁLCULO, no la duración de la
    tormenta. Con la tormenta de tres horas del estudio el término valdría 90
    minutos en lugar de 2,5, y el hidrograma saldría desplazado más de una hora.
    """

    def test_criterio_scs(self) -> None:
        rezago = m10.tiempo_de_rezago(2.0, "scs", 5.0)
        self.assertAlmostEqual(rezago["tlag_horas"], 1.2)
        self.assertAlmostEqual(rezago["tlag_minutos"], 72.0)

    def test_el_intervalo_no_afecta_al_criterio_scs(self) -> None:
        self.assertEqual(m10.tiempo_de_rezago(2.0, "scs", 5.0)["tlag_horas"],
                         m10.tiempo_de_rezago(2.0, "scs", 60.0)["tlag_horas"])

    def test_criterio_hechms_anade_medio_intervalo(self) -> None:
        rezago = m10.tiempo_de_rezago(2.0, "hechms", 5.0)
        # 5 min / 2 = 2,5 min = 0,041667 h, sobre 1,2 h.
        self.assertAlmostEqual(rezago["tlag_horas"], 1.2 + 2.5 / 60.0, places=4)
        self.assertEqual(rezago["intervalo_calculo_min"], 5.0)

    def test_sin_tc_no_hay_rezago(self) -> None:
        rezago = m10.tiempo_de_rezago(None, "scs", 5.0)
        self.assertIsNone(rezago["tlag_horas"])
        self.assertIn("concentración", rezago["motivo"])

    def test_un_criterio_desconocido_no_inventa_un_valor(self) -> None:
        rezago = m10.tiempo_de_rezago(2.0, "inventado", 5.0)
        self.assertIsNone(rezago["tlag_horas"])
        self.assertIn("no reconocido", rezago["motivo"])

    def test_el_criterio_declarado_esta_entre_los_admitidos(self) -> None:
        self.assertIn(_CFG.obtener("tiempo_rezago.criterio"), ("scs", "hechms"))


class PruebaAdopcionConValores(unittest.TestCase):
    """La adopción exige dos condiciones: número de fórmulas y dispersión."""

    def _evaluadas(self, valores, aplicables=None):
        aplicables = range(len(valores)) if aplicables is None else aplicables
        return [{"formula": f"f{i}", "aplicable": i in aplicables,
                 "tc_horas": v} for i, v in enumerate(valores)]

    def test_adopta_la_mediana_del_subconjunto(self) -> None:
        resumen = m10.resumir_adopcion(
            self._evaluadas([1.0, 2.0, 3.0, 4.0, 5.0]), 5, cv_maximo=1.0)
        self.assertTrue(resumen["procede_adoptar"])
        self.assertAlmostEqual(resumen["tc_horas"], 3.0)

    def test_la_dispersion_alta_impide_adoptar(self) -> None:
        resumen = m10.resumir_adopcion(
            self._evaluadas([1.0, 2.0, 3.0, 40.0, 100.0]), 5, cv_maximo=0.60)
        self.assertTrue(resumen["dispersion_excesiva"])
        self.assertFalse(resumen["procede_adoptar"])
        self.assertIsNone(resumen["tc_horas"])

    def test_solo_cuentan_las_aplicables(self) -> None:
        # Cinco formulas con valor, pero solo tres dentro de su rango.
        resumen = m10.resumir_adopcion(
            self._evaluadas([1.0, 2.0, 3.0, 4.0, 5.0], aplicables={0, 1, 2}),
            5, cv_maximo=1.0)
        self.assertEqual(resumen["formulas_aplicables"], 3)
        self.assertFalse(resumen["procede_adoptar"])

    def test_una_aplicable_sin_calcular_no_es_adoptable(self) -> None:
        evaluadas = self._evaluadas([1.0, 2.0, 3.0, 4.0, 5.0])
        evaluadas[0]["tc_horas"] = None
        resumen = m10.resumir_adopcion(evaluadas, 5, cv_maximo=1.0)
        self.assertEqual(resumen["formulas_aplicables"], 5)
        self.assertEqual(resumen["formulas_adoptables"], 4)


@unittest.skipUnless(HAY_CUENCA, "no hay capa de cuenca")
class PruebaGrupoHidrologico(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        directorio = Path(_CFG.obtener("referencia_nacional.directorio"))
        cls.ruta = directorio / str(_CFG.obtener("referencia_nacional.suelos_hsg"))
        if not cls.ruta.is_file():
            raise unittest.SkipTest("no está la capa nacional de suelos")
        cls.poligonos = shapefile.leer_geometrias(_CUENCA)

    def _muestreo(self, duales="no_drenado", paso=1000.0):
        return m10.grupos_hidrologicos(
            self.ruta, self.poligonos, _CFG.obtener("crs.calculo"),
            paso_m=paso, duales=duales)

    def test_el_reparto_suma_cien(self) -> None:
        suelos = self._muestreo()
        self.assertAlmostEqual(
            sum(r["porcentaje"] for r in suelos["reparto"]), 100.0, places=1)

    def test_todos_los_grupos_son_del_scs(self) -> None:
        for fila in self._muestreo()["reparto"]:
            self.assertIn(fila["grupo"], ("A", "B", "C", "D"))

    def test_el_criterio_de_duales_cambia_el_reparto(self) -> None:
        # Es la comprobacion de que la decision tiene consecuencias medibles.
        sin_drenar = {r["grupo"]: r["porcentaje"] for r in
                      self._muestreo("no_drenado")["reparto"]}
        drenado = {r["grupo"]: r["porcentaje"] for r in
                   self._muestreo("drenado")["reparto"]}
        self.assertGreater(sin_drenar.get("D", 0.0), drenado.get("D", 0.0))

    def test_la_cobertura_del_raster_se_declara(self) -> None:
        suelos = self._muestreo()
        self.assertGreater(suelos["cobertura_pct"], 90.0)
        self.assertEqual(
            suelos["muestras"],
            suelos["muestras_validas"] + suelos["muestras_sin_dato"]
            + suelos["muestras_fuera"])

    def test_un_paso_mas_fino_no_cambia_el_dominante(self) -> None:
        self.assertEqual(self._muestreo(paso=1000.0)["grupo_dominante"],
                         self._muestreo(paso=500.0)["grupo_dominante"])


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

# -*- coding: utf-8 -*-
"""
Pruebas del motor de análisis de frecuencia y del M07.

Se verifican contra series sintéticas de parámetros conocidos: es la única forma
de comprobar que un ajuste recupera lo que debe. Una serie Gumbel de ubicación
60 y escala 15 debe devolver esos parámetros, y si no lo hace el error está en
el estimador y no en el dato.

    python tests/test_frecuencia.py
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

from comun.config import cargar  # noqa: E402

try:
    import numpy as np
    import frecuencia as fr
    import M07_frecuencia as m07
    HAY_SCIPY = True
except ImportError:  # pragma: no cover
    HAY_SCIPY = False

_CFG = cargar(raiz=_RAIZ_REPO)


def _gumbel(n=60, ubicacion=60.0, escala=15.0, semilla=7):
    generador = np.random.default_rng(semilla)
    return ubicacion + escala * (-np.log(-np.log(generador.uniform(size=n))))


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaMomentosL(unittest.TestCase):
    """
    Su virtud es no elevar las desviaciones al cuadrado ni al cubo, de modo que
    un extremo no domina el resultado. En una serie de máximos ese extremo es el
    dato de diseño, no un error.
    """

    def test_el_primer_momento_es_la_media(self) -> None:
        datos = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertAlmostEqual(fr.momentos_l(datos)["l1"], 30.0)

    def test_una_serie_simetrica_no_tiene_sesgo(self) -> None:
        datos = list(np.linspace(0.0, 100.0, 101))
        self.assertAlmostEqual(fr.momentos_l(datos)["t3"], 0.0, places=6)

    def test_el_sesgo_positivo_se_reconoce(self) -> None:
        self.assertGreater(fr.momentos_l(_gumbel())["t3"], 0.0)

    def test_es_robusto_frente_a_un_extremo(self) -> None:
        # El momento ordinario de tercer orden se dispara; el de orden L no.
        base = list(np.linspace(10.0, 20.0, 40))
        con_extremo = base + [500.0]
        t3_base = fr.momentos_l(base)["t3"]
        t3_extremo = fr.momentos_l(con_extremo)["t3"]
        from scipy import stats as _st
        sesgo_base = abs(float(_st.skew(base, bias=False)))
        sesgo_extremo = abs(float(_st.skew(con_extremo, bias=False)))
        self.assertLess(abs(t3_extremo - t3_base),
                        sesgo_extremo - sesgo_base)

    def test_muestra_corta_es_error_explicito(self) -> None:
        with self.assertRaises(fr.ErrorFrecuencia):
            fr.momentos_l([1.0, 2.0, 3.0])


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaAjuste(unittest.TestCase):
    def setUp(self) -> None:
        self.datos = _gumbel(n=200)

    def test_gumbel_recupera_sus_parametros(self) -> None:
        for metodo in fr.METODOS:
            ajuste = fr.ajustar(self.datos, "gumbel_max", metodo)
            self.assertTrue(ajuste.valido, metodo)
            self.assertAlmostEqual(ajuste.parametros[0], 60.0, delta=4.0)
            self.assertAlmostEqual(ajuste.parametros[1], 15.0, delta=3.0)

    def test_la_normal_recupera_media_y_desviacion(self) -> None:
        generador = np.random.default_rng(3)
        datos = generador.normal(100.0, 20.0, 300)
        ajuste = fr.ajustar(datos, "normal", "momentos")
        self.assertAlmostEqual(ajuste.parametros[0], 100.0, delta=3.0)
        self.assertAlmostEqual(ajuste.parametros[1], 20.0, delta=3.0)

    def test_una_combinacion_sin_relacion_cerrada_se_declara(self) -> None:
        # Se prefiere declarar la ausencia a recurrir en silencio a otro método,
        # que produciría un resultado etiquetado como momentos-L sin serlo.
        ajuste = fr.ajustar(self.datos, "weibull", "momentos_l")
        self.assertFalse(ajuste.valido)
        self.assertIn("momentos-L", ajuste.error)

    def test_una_distribucion_inventada_no_revienta(self) -> None:
        ajuste = fr.ajustar(self.datos, "inventada", "momentos")
        self.assertFalse(ajuste.valido)
        self.assertIn("no reconocida", ajuste.error)

    def test_muestra_minima(self) -> None:
        self.assertFalse(fr.ajustar([1.0, 2.0, 3.0], "normal", "momentos").valido)

    def test_el_limite_normal_de_pearson3_no_desborda(self) -> None:
        # Con sesgo casi nulo, alfa se dispara y gamma(alfa) desbordaba.
        generador = np.random.default_rng(11)
        datos = generador.normal(100.0, 20.0, 200)
        ajuste = fr.ajustar(datos, "pearson3", "momentos_l")
        self.assertTrue(ajuste.valido)
        # No se exige igualdad exacta: el sesgo muestral no es cero, de modo
        # que el resultado debe quedar CERCA del límite normal, no encima.
        esperado = fr.momentos_l(datos)["l2"] * math.sqrt(math.pi)
        self.assertAlmostEqual(ajuste.parametros[2], esperado, delta=0.01)
        self.assertLess(abs(ajuste.parametros[0]), 0.05)

    def test_las_logaritmicas_exigen_valores_positivos(self) -> None:
        ajuste = fr.ajustar([0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
                            "lognormal2", "momentos")
        self.assertFalse(ajuste.valido)


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaCuantiles(unittest.TestCase):
    def setUp(self) -> None:
        self.datos = _gumbel(n=200)
        self.ajuste = fr.ajustar(self.datos, "gumbel_max", "momentos_l")

    def test_el_cuantil_crece_con_el_periodo(self) -> None:
        valores = fr.cuantiles(self.ajuste, [2.33, 5, 25, 100, 500])
        secuencia = [valores[p] for p in sorted(valores)]
        self.assertEqual(secuencia, sorted(secuencia))

    def test_el_periodo_unitario_se_descarta(self) -> None:
        # T=1 daría probabilidad cero de no excedencia, que no es un cuantil.
        self.assertNotIn(1.0, fr.cuantiles(self.ajuste, [1.0, 10.0]))

    def test_un_ajuste_fallido_no_da_cuantiles(self) -> None:
        fallido = fr.ajustar([1.0], "normal", "momentos")
        self.assertEqual(fr.cuantiles(fallido, [10.0]), {})

    def test_las_logaritmicas_deshacen_la_transformacion(self) -> None:
        # Si no se deshiciera, el cuantil saldría en el espacio del logaritmo y
        # sería de un orden de magnitud absurdo.
        ajuste = fr.ajustar(self.datos, "lognormal2", "momentos")
        valores = fr.cuantiles(ajuste, [100.0])
        self.assertGreater(valores[100.0], float(np.median(self.datos)))
        self.assertLess(valores[100.0], 10.0 * float(np.max(self.datos)))


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaBondad(unittest.TestCase):
    def test_la_distribucion_correcta_ajusta_mejor(self) -> None:
        datos = _gumbel(n=300)
        gumbel = fr.ajustar(datos, "gumbel_max", "maxima_verosimilitud")
        gumbel.bondad = fr.bondad_de_ajuste(gumbel, datos)
        normal = fr.ajustar(datos, "normal", "maxima_verosimilitud")
        normal.bondad = fr.bondad_de_ajuste(normal, datos)
        self.assertLess(gumbel.bondad["aic"], normal.bondad["aic"])
        self.assertLess(gumbel.bondad["anderson_darling"],
                        normal.bondad["anderson_darling"])

    def test_reporta_todas_las_pruebas(self) -> None:
        datos = _gumbel(n=100)
        ajuste = fr.ajustar(datos, "gumbel_max", "momentos_l")
        bondad = fr.bondad_de_ajuste(ajuste, datos)
        for clave in ("ks", "ks_p", "anderson_darling", "aic", "bic"):
            self.assertIn(clave, bondad)

    def test_el_bic_penaliza_mas_que_el_aic(self) -> None:
        datos = _gumbel(n=100)
        ajuste = fr.ajustar(datos, "gev", "momentos_l")
        bondad = fr.bondad_de_ajuste(ajuste, datos)
        self.assertGreater(bondad["bic"], bondad["aic"])


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaGrubbsBeck(unittest.TestCase):
    """
    Busca atípicos BAJOS. Los altos son el dato de diseño y no se tocan nunca.
    """

    def test_una_serie_limpia_no_tiene_atipicos_bajos(self) -> None:
        self.assertEqual(fr.grubbs_beck(_gumbel(n=50))["cuantos"], 0)

    def test_un_anio_anormalmente_seco_se_detecta(self) -> None:
        datos = list(_gumbel(n=50)) + [0.5]
        self.assertGreaterEqual(fr.grubbs_beck(datos)["cuantos"], 1)

    def test_un_extremo_alto_no_se_marca(self) -> None:
        datos = list(_gumbel(n=50)) + [900.0]
        self.assertNotIn(900.0, fr.grubbs_beck(datos)["atipicos_bajos"])

    def test_exige_valores_positivos(self) -> None:
        self.assertIn("error", fr.grubbs_beck([0.0] + list(_gumbel(n=20))))

    def test_muestra_corta_se_reporta(self) -> None:
        self.assertIn("error", fr.grubbs_beck([1.0, 2.0, 3.0]))


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaPosicionGrafica(unittest.TestCase):
    def test_weibull_esta_centrada(self) -> None:
        posiciones = fr.posicion_grafica(9, "weibull")
        self.assertAlmostEqual(float(posiciones[4]), 0.5)

    def test_todas_quedan_entre_cero_y_uno(self) -> None:
        for formula in ("weibull", "gringorten", "cunnane", "hazen"):
            posiciones = fr.posicion_grafica(30, formula)
            self.assertTrue(bool(np.all((posiciones > 0) & (posiciones < 1))),
                            formula)

    def test_formula_desconocida_es_error(self) -> None:
        with self.assertRaises(fr.ErrorFrecuencia):
            fr.posicion_grafica(10, "inventada")


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaMaximosAnuales(unittest.TestCase):
    """
    La completitud es estacional, no un total anual.

    Un año con 340 días puede tener abril entero vacío, y abril es temporada
    húmeda: su máximo sería el de un año seco.
    """

    def _anio(self, dias_por_mes, valor=10.0):
        return {m: [valor] * dias_por_mes for m in range(1, 13)}

    def test_un_anio_completo_aporta_su_maximo(self) -> None:
        anios = {2000: self._anio(30)}
        anios[2000][6] = [10.0] * 29 + [99.0]
        maximos, rechazados = m07.maximos_anuales(anios, 25)
        self.assertEqual(maximos[2000], 99.0)
        self.assertEqual(rechazados, 0)

    def test_un_mes_por_debajo_del_minimo_invalida_el_anio(self) -> None:
        anios = {2000: self._anio(30)}
        anios[2000][4] = [10.0] * 5
        maximos, rechazados = m07.maximos_anuales(anios, 25)
        self.assertEqual(maximos, {})
        self.assertEqual(rechazados, 1)

    def test_un_mes_ausente_invalida_el_anio(self) -> None:
        anios = {2000: self._anio(30)}
        del anios[2000][4]
        maximos, rechazados = m07.maximos_anuales(anios, 25)
        self.assertEqual(maximos, {})
        self.assertEqual(rechazados, 1)

    def test_el_criterio_estacional_es_mas_estricto_que_el_total(self) -> None:
        # 340 días en total, pero abril vacío: el total lo admitiría.
        anios = {2000: {m: [10.0] * 31 for m in range(1, 13)}}
        anios[2000][4] = []
        total = sum(len(v) for v in anios[2000].values())
        self.assertGreater(total, 330)
        self.assertEqual(m07.maximos_anuales(anios, 25)[0], {})


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaSeleccion(unittest.TestCase):
    def test_elige_el_menor_criterio(self) -> None:
        datos = _gumbel(n=100)
        ajustes = []
        for distribucion in ("normal", "gumbel_max", "gev"):
            ajuste = fr.ajustar(datos, distribucion, "maxima_verosimilitud")
            ajuste.bondad = fr.bondad_de_ajuste(ajuste, datos)
            ajustes.append(ajuste)
        mejor = m07.seleccionar(ajustes, "aic")
        self.assertEqual(mejor.bondad["aic"],
                         min(a.bondad["aic"] for a in ajustes))

    def test_sin_candidatos_devuelve_nada(self) -> None:
        self.assertIsNone(m07.seleccionar([], "aic"))



@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaRepertorioHydrognomon(unittest.TestCase):
    """
    CLAUDE.md, sección 4, declara Hydrognomon reemplazado por este análisis: el
    reemplazo debe ser al menos tan completo como lo reemplazado para máximos.
    """

    def test_estan_las_de_maximos_de_hydrognomon(self) -> None:
        for nombre in ("normal", "lognormal2", "lognormal3", "gumbel_max",
                       "gev", "pearson3", "logpearson3", "exponencial",
                       "gamma", "ev2_max", "gev_k_fijo", "pareto"):
            self.assertIn(nombre, fr.DISTRIBUCIONES, nombre)

    def test_frechet_ajusta_y_da_cola_mas_pesada_que_gumbel(self) -> None:
        datos = _gumbel(n=120)
        frechet = fr.ajustar(datos, "ev2_max", "maxima_verosimilitud")
        self.assertTrue(frechet.valido)
        self.assertGreater(fr.cuantiles(frechet, [500.0])[500.0], 0)

    def test_la_forma_fija_de_la_gev_se_respeta(self) -> None:
        datos = _gumbel(n=100)
        previo = fr.FORMA_GEV_FIJA
        try:
            fr.FORMA_GEV_FIJA = -0.15
            ajuste = fr.ajustar(datos, "gev_k_fijo", "maxima_verosimilitud")
            self.assertAlmostEqual(ajuste.parametros[0], -0.15, places=6)
        finally:
            fr.FORMA_GEV_FIJA = previo

    def test_el_signo_de_la_forma_cambia_el_cuantil_alto(self) -> None:
        # Hydrognomon usa el signo contrario: fijarlo al revés cambia por
        # completo el periodo de retorno alto.
        datos = _gumbel(n=100)
        previo = fr.FORMA_GEV_FIJA
        try:
            fr.FORMA_GEV_FIJA = -0.15
            pesada = fr.cuantiles(
                fr.ajustar(datos, "gev_k_fijo", "maxima_verosimilitud"),
                [100.0])[100.0]
            fr.FORMA_GEV_FIJA = 0.15
            acotada = fr.cuantiles(
                fr.ajustar(datos, "gev_k_fijo", "maxima_verosimilitud"),
                [100.0])[100.0]
        finally:
            fr.FORMA_GEV_FIJA = previo
        self.assertGreater(pesada, acotada)

    def test_la_densidad_se_evalua_en_el_espacio_de_los_datos(self) -> None:
        # Sin el jacobiano, la curva logarítmica no integraría uno y quedaría
        # por debajo de las demás sin que eso signifique peor ajuste.
        datos = _gumbel(n=100)
        ajuste = fr.ajustar(datos, "lognormal2", "momentos")
        malla = np.linspace(float(datos.min()), float(datos.max()), 400)
        curva = fr.densidad(ajuste, malla)
        area = float(np.trapezoid(curva, malla))
        self.assertGreater(area, 0.5)
        self.assertLess(area, 1.05)


@unittest.skipUnless(HAY_SCIPY, "scipy no está instalado")
class PruebaPlausibilidad(unittest.TestCase):
    """
    Un criterio de información compara verosimilitudes; no comprueba que el
    resultado tenga sentido. La Pareto degenerada ganaba por AIC con un pico
    sobre el borde.
    """

    def setUp(self) -> None:
        self.datos = list(_gumbel(n=40))

    def test_un_ajuste_razonable_pasa(self) -> None:
        ajuste = fr.ajustar(self.datos, "gumbel_max", "momentos_l")
        ajuste.cuantiles = fr.cuantiles(ajuste, [2.33, 25, 100, 500])
        self.assertTrue(m07.es_plausible(ajuste, self.datos))

    def test_un_cuantil_centenario_bajo_el_maximo_se_rechaza(self) -> None:
        ajuste = fr.Ajuste("inventada", "momentos", parametros=(1.0,))
        ajuste.cuantiles = {2.33: 10.0, 100.0: 20.0}
        self.assertFalse(m07.es_plausible(ajuste, [10.0, 500.0]))

    def test_cuantiles_no_monotonos_se_rechazan(self) -> None:
        ajuste = fr.Ajuste("inventada", "momentos", parametros=(1.0,))
        ajuste.cuantiles = {2.33: 100.0, 100.0: 50.0}
        self.assertFalse(m07.es_plausible(ajuste, [10.0]))

    def test_un_ajuste_fallido_no_es_plausible(self) -> None:
        self.assertFalse(m07.es_plausible(fr.Ajuste("x", "y"), [1.0]))

    def test_la_seleccion_respeta_las_excluidas(self) -> None:
        ajustes = []
        for distribucion in ("gumbel_max", "pareto"):
            ajuste = fr.ajustar(self.datos, distribucion,
                                "maxima_verosimilitud")
            if ajuste.valido:
                ajuste.bondad = fr.bondad_de_ajuste(ajuste, self.datos)
                ajuste.cuantiles = fr.cuantiles(ajuste, [2.33, 100, 500])
            ajustes.append(ajuste)
        elegido = m07.seleccionar(ajustes, "aic", self.datos,
                                  excluidas=["pareto"])
        self.assertIsNotNone(elegido)
        self.assertNotEqual(elegido.distribucion, "pareto")

    def test_la_pareto_esta_excluida_en_la_configuracion(self) -> None:
        excluidas = list(_CFG.obtener("frecuencia.excluidas_de_seleccion") or ())
        self.assertIn("pareto", excluidas)


class PruebaConfiguracion(unittest.TestCase):
    def test_los_periodos_de_retorno_son_los_declarados(self) -> None:
        periodos = list(_CFG.obtener("frecuencia.periodos_retorno"))
        self.assertEqual(periodos, [2.33, 5, 10, 15, 25, 50, 100, 500])

    def test_no_se_aplica_iqr_a_la_serie_de_maximos(self) -> None:
        # CLAUDE.md, sección 7: truncaría el dato de diseño.
        self.assertFalse(_CFG.obtener("anomalos.aplicar_a_serie_maximos"))

    def test_el_criterio_de_completitud_es_por_mes(self) -> None:
        dias = int(_CFG.obtener("frecuencia.min_dias_mes"))
        self.assertTrue(1 <= dias <= 31)




class PruebaCalificadoresDesdeLaDoctrina(unittest.TestCase):
    """
    La lista de calificadores que impiden sustentar un máximo es DOCTRINA y
    vive en config/perfiles_ideam.yaml, no en el código.

    Estaba embebida como constante, y eso contradice la sección 2: añadir un
    calificador nuevo, como los tres de pluviógrafo que aparecieron en 2021,
    exigía tocar el programa.
    """

    def setUp(self) -> None:
        import sys
        from pathlib import Path

        raiz = Path(__file__).resolve().parents[1]
        if str(raiz / "src") not in sys.path:
            sys.path.insert(0, str(raiz / "src"))
        import M07_frecuencia as m07
        from comun.config import cargar

        self.m07 = m07
        self.raiz = raiz
        self.cfg = cargar(raiz=raiz)

    def test_se_leen_del_perfil_y_no_del_codigo(self) -> None:
        marcas, procedencia = self.m07.calificadores_excluidos(
            self.cfg, self.raiz)
        self.assertIn("perfiles_ideam", procedencia)
        self.assertNotEqual(procedencia, "respaldo del código")

    def test_incluyen_el_acumulado(self) -> None:
        # CLAUDE.md, sección 7: es el que marca un registro que agrupa varios
        # días y que sin la marca se leería como un máximo de 24 h inexistente.
        marcas, _ = self.m07.calificadores_excluidos(self.cfg, self.raiz)
        self.assertIn("ACUMULADO", marcas)

    def test_incluyen_los_de_registro_parcial(self) -> None:
        marcas, _ = self.m07.calificadores_excluidos(self.cfg, self.raiz)
        for marca in ("SIN TRAZO", "INCOMPLETO"):
            with self.subTest(marca=marca):
                self.assertIn(marca, marcas)

    def test_arrastran_los_de_efecto_excluir_del_analisis(self) -> None:
        # DATO RECHAZADO no es cuestión de máximos: no sirve para nada, y el
        # perfil lo declara con ese efecto en lugar de repetirlo en la lista.
        marcas, _ = self.m07.calificadores_excluidos(self.cfg, self.raiz)
        self.assertIn("DATO RECHAZADO", marcas)

    def test_un_estudio_sin_perfil_propio_hereda_el_de_la_herramienta(self) -> None:
        """
        No cae al respaldo del código: cae a la DOCTRINA.

        'config/' es prefijo de código, de modo que un estudio que no trae su
        propio perfil usa el de la herramienta. Es lo correcto y es lo que
        mantiene la doctrina en un solo sitio.
        """
        import shutil
        import tempfile

        vacio = Path(tempfile.mkdtemp())
        try:
            marcas, procedencia = self.m07.calificadores_excluidos(
                self.cfg, vacio)
            self.assertIn("perfiles_ideam", procedencia)
            self.assertIn("ACUMULADO", marcas)
        finally:
            shutil.rmtree(vacio, ignore_errors=True)

    def test_sin_perfil_legible_hay_respaldo(self) -> None:
        """El respaldo solo entra si el perfil no se puede leer de ningún sitio."""
        import copy

        datos = self.cfg.como_dict()
        datos["ideam"]["dhime_zip"]["perfiles"] = "/no/existe/perfiles.yaml"
        del copy

        class _Falsa:
            def __init__(self, datos):
                self._datos = datos

            def obtener(self, clave, defecto=None):
                actual = self._datos
                for parte in clave.split("."):
                    actual = actual[parte]
                return actual

        marcas, procedencia = self.m07.calificadores_excluidos(
            _Falsa(datos), self.raiz)
        self.assertEqual(procedencia, "respaldo del código")
        self.assertIn("ACUMULADO", marcas)

if __name__ == "__main__":
    unittest.main(verbosity=2)

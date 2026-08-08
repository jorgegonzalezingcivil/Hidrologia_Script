# -*- coding: utf-8 -*-
"""
Pruebas del M04b: medidas de longitud de serie y matriz de sensibilidad.

Todo corre bajo el venv y sin librerías de terceros.

    python tests/test_m04b.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M04b_sensibilidad_series as m04b  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


class PruebaCompletitud(unittest.TestCase):
    def test_los_dias_esperados_siguen_al_bisiesto(self) -> None:
        self.assertEqual(m04b.registros_esperados("Diaria", 2020), 366)
        self.assertEqual(m04b.registros_esperados("Diaria", 2021), 365)

    def test_la_frecuencia_mensual_espera_doce(self) -> None:
        self.assertEqual(m04b.registros_esperados("Mensual", 2020), 12)

    def test_una_frecuencia_desconocida_no_inventa_denominador(self) -> None:
        self.assertIsNone(m04b.registros_esperados("Quincenal", 2020))
        self.assertIsNone(m04b.registros_esperados("", 2020))

    def test_la_completitud_no_pasa_de_uno(self) -> None:
        # Un año con más registros de los esperados no vale más que uno completo.
        self.assertEqual(m04b.completitud_anual(400, 365), 1.0)
        self.assertAlmostEqual(m04b.completitud_anual(6, 12), 0.5)

    def test_sin_denominador_la_completitud_es_indeterminada(self) -> None:
        self.assertIsNone(m04b.completitud_anual(10, None))


class PruebaAniosUtiles(unittest.TestCase):
    """
    Un año con tres días de dato no es un año de serie.

    Es el control que impide inflar la longitud: sin él, la matriz contaría
    igual un año completo y uno testimonial.
    """

    MENSUAL = {2000: 12, 2001: 12, 2002: 6, 2003: 12, 2010: 1}

    def test_solo_cuentan_los_anios_que_alcanzan_el_umbral(self) -> None:
        utiles = m04b.anios_utiles(self.MENSUAL, "Mensual", 0.80)
        self.assertEqual(utiles, {2000, 2001, 2003})

    def test_un_umbral_permisivo_admite_el_anio_a_medias(self) -> None:
        utiles = m04b.anios_utiles(self.MENSUAL, "Mensual", 0.50)
        self.assertEqual(utiles, {2000, 2001, 2002, 2003})

    def test_la_ventana_recorta(self) -> None:
        utiles = m04b.anios_utiles(self.MENSUAL, "Mensual", 0.80, (2001, 2005))
        self.assertEqual(utiles, {2001, 2003})

    def test_frecuencia_desconocida_cuenta_presencia(self) -> None:
        # Optimista a propósito; quien llama emite la advertencia.
        utiles = m04b.anios_utiles(self.MENSUAL, "Quincenal", 0.80)
        self.assertEqual(utiles, {2000, 2001, 2002, 2003, 2010})


class PruebaRacha(unittest.TestCase):
    """
    Treinta años en dos bloques no son treinta continuos.

    El M07 no es indiferente a esa diferencia, de modo que la matriz la reporta
    en columna aparte.
    """

    def test_racha_de_un_bloque(self) -> None:
        self.assertEqual(m04b.racha_maxima({2000, 2001, 2002, 2003}), 4)

    def test_el_hueco_parte_la_racha(self) -> None:
        self.assertEqual(m04b.racha_maxima({2000, 2001, 2010, 2011, 2012}), 3)

    def test_conjunto_vacio(self) -> None:
        self.assertEqual(m04b.racha_maxima(set()), 0)

    def test_un_solo_anio(self) -> None:
        self.assertEqual(m04b.racha_maxima({1999}), 1)

    def test_los_repetidos_no_alargan(self) -> None:
        self.assertEqual(m04b.racha_maxima([2000, 2000, 2001]), 2)


class PruebaVentanas(unittest.TestCase):
    def test_el_nulo_final_se_resuelve_al_anio_del_estudio(self) -> None:
        self.assertEqual(m04b.resolver_ventana([1990, None], 2026), (1990, 2026))

    def test_ventana_cerrada(self) -> None:
        self.assertEqual(m04b.resolver_ventana([1980, 2010], 2026), (1980, 2010))

    def test_la_etiqueta_es_legible(self) -> None:
        self.assertEqual(m04b.etiqueta_de_ventana([2000, None], 2026), "2000-2026")


class PruebaAnioDeFecha(unittest.TestCase):
    def test_fecha_iso(self) -> None:
        self.assertEqual(m04b.anio_de_fecha("1986-12-01"), 1986)

    def test_fecha_ilegible(self) -> None:
        for valor in ("", "  ", "sin fecha", "86-12-01"):
            self.assertIsNone(m04b.anio_de_fecha(valor), valor)


class PruebaSuspension(unittest.TestCase):
    """La suspensión clasifica, no elimina (CLAUDE.md, sección 6)."""

    def test_sin_fecha_es_vigente(self) -> None:
        self.assertEqual(m04b.estado_por_suspension("", 2026, 5), m04b.VIGENTE)

    def test_suspension_reciente(self) -> None:
        self.assertEqual(m04b.estado_por_suspension("2023-04-01", 2026, 5),
                         m04b.SUSPENDIDA_RECIENTE)

    def test_suspension_antigua(self) -> None:
        self.assertEqual(m04b.estado_por_suspension("1998-01-01", 2026, 5),
                         m04b.SUSPENDIDA_ANTIGUA)

    def test_el_borde_todavia_es_reciente(self) -> None:
        self.assertEqual(m04b.estado_por_suspension("2021-01-01", 2026, 5),
                         m04b.SUSPENDIDA_RECIENTE)
        self.assertEqual(m04b.estado_por_suspension("2020-01-01", 2026, 5),
                         m04b.SUSPENDIDA_ANTIGUA)


class PruebaResumenDeSerie(unittest.TestCase):
    """
    La amplitud no es la longitud.

    Una serie con registro en los extremos y nada en medio tiene amplitud
    grande y longitud útil mínima; confundirlas es el error que este módulo
    existe para evitar.
    """

    def setUp(self) -> None:
        self.serie = m04b.SerieEstacion("X", "PTPM_TT_M", "Mensual")
        for anio in (1970, 2020):
            for _ in range(12):
                self.serie.sumar(anio)

    def test_amplitud_frente_a_anios_utiles(self) -> None:
        resumen = m04b.resumir_serie(self.serie, 0.80, {})
        self.assertEqual(resumen["amplitud"], 51)
        self.assertEqual(resumen["anios_con_dato"], 2)
        self.assertEqual(resumen["anios_utiles"], 2)
        self.assertEqual(resumen["racha_max"], 1)

    def test_las_ventanas_se_reportan_aparte(self) -> None:
        ventanas = {"2000-2026": (2000, 2026), "1960-1980": (1960, 1980)}
        resumen = m04b.resumir_serie(self.serie, 0.80, ventanas)
        self.assertEqual(resumen["utiles_2000-2026"], 1)
        self.assertEqual(resumen["utiles_1960-1980"], 1)


class PruebaMatriz(unittest.TestCase):
    def setUp(self) -> None:
        self.ventanas = {"1980-2026": (1980, 2026), "2000-2026": (2000, 2026)}
        self.variable_de = {"PTPM_TT_M": "precipitacion", "Q_MEDIA_D": "caudal"}
        self.resumenes = [
            {"codigo": "A", "etiqueta": "PTPM_TT_M",
             "utiles_1980-2026": 30, "racha_1980-2026": 30,
             "utiles_2000-2026": 20, "racha_2000-2026": 20},
            {"codigo": "B", "etiqueta": "PTPM_TT_M",
             "utiles_1980-2026": 30, "racha_1980-2026": 12,
             "utiles_2000-2026": 5, "racha_2000-2026": 5},
            {"codigo": "C", "etiqueta": "Q_MEDIA_D",
             "utiles_1980-2026": 8, "racha_1980-2026": 8,
             "utiles_2000-2026": 8, "racha_2000-2026": 8},
        ]

    def _celda(self, matriz, variable, ventana, umbral):
        return next(c for c in matriz if c["variable"] == variable
                    and c["ventana"] == ventana and c["umbral_anios"] == umbral)

    def test_la_continuidad_se_cuenta_aparte(self) -> None:
        matriz = m04b.construir_matriz(
            self.resumenes, [25], self.ventanas, self.variable_de)
        celda = self._celda(matriz, "precipitacion", "1980-2026", 25)
        self.assertEqual(celda["estaciones"], 2)
        # B tiene 30 años útiles pero su mayor racha es de 12.
        self.assertEqual(celda["estaciones_continuas"], 1)

    def test_una_estacion_con_varias_etiquetas_cuenta_una_vez(self) -> None:
        resumenes = self.resumenes + [
            {"codigo": "A", "etiqueta": "PTPM_CON",
             "utiles_1980-2026": 30, "racha_1980-2026": 30,
             "utiles_2000-2026": 20, "racha_2000-2026": 20},
        ]
        variable_de = dict(self.variable_de, PTPM_CON="precipitacion")
        matriz = m04b.construir_matriz(
            resumenes, [25], self.ventanas, variable_de)
        self.assertEqual(
            self._celda(matriz, "precipitacion", "1980-2026", 25)["estaciones"], 2)

    def test_una_ventana_mas_corta_que_el_umbral_no_es_evaluable(self) -> None:
        matriz = m04b.construir_matriz(
            self.resumenes, [30], self.ventanas, self.variable_de)
        self.assertTrue(
            self._celda(matriz, "precipitacion", "1980-2026", 30)["evaluable"])
        # La ventana 2000-2026 tiene 27 años: pedir 30 es imposible.
        self.assertFalse(
            self._celda(matriz, "precipitacion", "2000-2026", 30)["evaluable"])

    def test_el_filtro_de_admitidos_restringe(self) -> None:
        matriz = m04b.construir_matriz(
            self.resumenes, [25], self.ventanas, self.variable_de,
            admitidos={"A"})
        self.assertEqual(
            self._celda(matriz, "precipitacion", "1980-2026", 25)["estaciones"], 1)

    def test_la_etiqueta_sin_declarar_no_se_pierde(self) -> None:
        matriz = m04b.construir_matriz(
            self.resumenes, [5], self.ventanas, {})
        self.assertTrue(any(c["variable"] == "sin declarar" for c in matriz))


class PruebaMapaVariables(unittest.TestCase):
    def test_invierte_la_declaracion_de_config(self) -> None:
        mapa = m04b.mapa_etiqueta_variable(
            _CFG.obtener("ideam.descarga.series_por_variable"))
        self.assertEqual(mapa.get("PTPM_CON"), "precipitacion")
        self.assertEqual(mapa.get("Q_MEDIA_D"), "caudal")

    def test_declaracion_vacia(self) -> None:
        self.assertEqual(m04b.mapa_etiqueta_variable({}), {})
        self.assertEqual(m04b.mapa_etiqueta_variable(None), {})


class PruebaAcumulado(unittest.TestCase):
    def test_agrupa_por_estacion_etiqueta_y_anio(self) -> None:
        filas = [
            {"codigo": "A", "etiqueta": "P", "frecuencia": "Mensual",
             "fecha": "2000-01-01"},
            {"codigo": "A", "etiqueta": "P", "frecuencia": "Mensual",
             "fecha": "2000-02-01"},
            {"codigo": "B", "etiqueta": "P", "frecuencia": "Mensual",
             "fecha": "2000-01-01"},
        ]
        acumulado, leidos, ilegibles = m04b.acumular_series(iter(filas))
        self.assertEqual((leidos, ilegibles), (3, 0))
        self.assertEqual(acumulado[("A", "P")].por_anio, {2000: 2})
        self.assertEqual(acumulado[("B", "P")].por_anio, {2000: 1})

    def test_las_fechas_ilegibles_se_cuentan_y_no_se_acumulan(self) -> None:
        filas = [{"codigo": "A", "etiqueta": "P", "frecuencia": "Mensual",
                  "fecha": "sin fecha"}]
        acumulado, leidos, ilegibles = m04b.acumular_series(iter(filas))
        self.assertEqual((leidos, ilegibles), (1, 1))
        self.assertEqual(acumulado, {})


class PruebaVerificacionContraM04(unittest.TestCase):
    """
    El control que existe por un fallo real.

    Una corrida del M04 con --solo-inventario reporta la estadística completa y
    termina 'CORRECTO' sin reescribir el CSV. Consumir ese archivo sin comprobar
    produce un resultado incorrecto en silencio, que es lo que CLAUDE.md,
    sección 2, prohíbe.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.reporte = self.tmp / "M04_ingesta.json"
        self.acumulado = {
            ("A", "PTPM_CON"): m04b.SerieEstacion("A", "PTPM_CON", "Diaria"),
            ("A", "Q_MEDIA_D"): m04b.SerieEstacion("A", "Q_MEDIA_D", "Diaria"),
        }
        self.acumulado[("A", "PTPM_CON")].por_anio = {2000: 10}
        self.acumulado[("A", "Q_MEDIA_D")].por_anio = {2000: 5}

    def _escribir(self, series, unicos) -> None:
        self.reporte.write_text(
            json.dumps({"series": series, "registros_unicos": unicos}),
            encoding="utf-8")

    def test_una_serie_ausente_es_bloqueante(self) -> None:
        self._escribir({"PTPM_CON": 10, "Q_MEDIA_D": 5, "Q_MX_D": 42}, 57)
        hallazgos = m04b.verificar_contra_m04(self.acumulado, self.reporte)
        self.assertEqual(hallazgos[0].severidad, BLOQUEANTE)
        self.assertIn("Q_MX_D", hallazgos[0].mensaje)

    def test_un_total_distinto_es_bloqueante(self) -> None:
        self._escribir({"PTPM_CON": 10, "Q_MEDIA_D": 5}, 99)
        hallazgos = m04b.verificar_contra_m04(self.acumulado, self.reporte)
        self.assertEqual(hallazgos[0].severidad, BLOQUEANTE)

    def test_coincidencia_limpia(self) -> None:
        self._escribir({"PTPM_CON": 10, "Q_MEDIA_D": 5}, 15)
        hallazgos = m04b.verificar_contra_m04(self.acumulado, self.reporte)
        self.assertTrue(all(h.severidad == INFORMATIVO for h in hallazgos))

    def test_una_etiqueta_sobrante_advierte_sin_bloquear(self) -> None:
        self._escribir({"PTPM_CON": 10}, 15)
        hallazgos = m04b.verificar_contra_m04(self.acumulado, self.reporte)
        self.assertTrue(any(h.severidad == ADVERTENCIA for h in hallazgos))
        self.assertFalse(any(h.severidad == BLOQUEANTE for h in hallazgos))

    def test_reporte_ausente_advierte(self) -> None:
        hallazgos = m04b.verificar_contra_m04(
            self.acumulado, self.tmp / "no_existe.json")
        self.assertEqual(hallazgos[0].severidad, ADVERTENCIA)



class PruebaUmbralPorVariable(unittest.TestCase):
    """
    Un umbral único no sirve para todas las variables.

    Medido en este estudio: 30 años dejan 42 estaciones de precipitación y CERO
    de evaporación. Sin excepciones por variable, la decisión habría desactivado
    el balance del M18 sin decirlo.
    """

    EXCEPCIONES = {"temperatura": 20, "evaporacion": 15}

    def test_una_variable_sin_excepcion_usa_el_general(self) -> None:
        self.assertEqual(
            m04b.umbral_de_variable("precipitacion", 30, self.EXCEPCIONES), 30)

    def test_la_excepcion_manda(self) -> None:
        self.assertEqual(
            m04b.umbral_de_variable("evaporacion", 30, self.EXCEPCIONES), 15)

    def test_sin_excepciones_declaradas(self) -> None:
        self.assertEqual(m04b.umbral_de_variable("caudal", 30, None), 30)
        self.assertEqual(m04b.umbral_de_variable("caudal", 30, {}), 30)

    def test_una_excepcion_nula_no_anula_el_general(self) -> None:
        self.assertEqual(
            m04b.umbral_de_variable("caudal", 30, {"caudal": None}), 30)

    def test_sin_umbral_general_no_hay_umbral(self) -> None:
        self.assertIsNone(m04b.umbral_de_variable("precipitacion", None, {}))

    def test_la_excepcion_rige_aunque_no_haya_general(self) -> None:
        self.assertEqual(
            m04b.umbral_de_variable("evaporacion", None, self.EXCEPCIONES), 15)


class PruebaCriterioDelUmbral(unittest.TestCase):
    def test_utiles_es_el_predeterminado(self) -> None:
        self.assertEqual(m04b.columna_de_criterio("utiles", "1980-2026"),
                         "utiles_1980-2026")
        self.assertEqual(m04b.columna_de_criterio("", "1980-2026"),
                         "utiles_1980-2026")

    def test_racha_selecciona_la_otra_columna(self) -> None:
        self.assertEqual(m04b.columna_de_criterio("racha", "1980-2026"),
                         "racha_1980-2026")

    def test_no_distingue_mayusculas(self) -> None:
        self.assertEqual(m04b.columna_de_criterio(" RACHA ", "1990-2026"),
                         "racha_1990-2026")


class PruebaDecisionAdoptada(unittest.TestCase):
    """La decisión declarada debe estar respaldada por la matriz."""

    def test_el_umbral_general_figura_entre_los_evaluados(self) -> None:
        adoptado = _CFG.obtener("sensibilidad_series.umbral_adoptado_anios")
        if adoptado is None:
            self.skipTest("umbral sin adoptar")
        self.assertIn(
            adoptado, list(_CFG.obtener("sensibilidad_series.umbrales_anios")))

    def test_las_excepciones_figuran_entre_los_evaluados(self) -> None:
        umbrales = list(_CFG.obtener("sensibilidad_series.umbrales_anios"))
        excepciones = _CFG.obtener(
            "sensibilidad_series.umbrales_por_variable") or {}
        for variable, valor in dict(excepciones).items():
            self.assertIn(valor, umbrales, variable)

    def test_la_ventana_adoptada_figura_entre_las_evaluadas(self) -> None:
        adoptada = _CFG.obtener("sensibilidad_series.ventana_adoptada")
        if adoptada is None:
            self.skipTest("ventana sin adoptar")
        anio = int(_CFG.obtener("proyecto.anio_estudio"))
        evaluadas = {m04b.etiqueta_de_ventana(v, anio)
                     for v in _CFG.obtener("sensibilidad_series.ventanas")}
        self.assertIn(m04b.etiqueta_de_ventana(adoptada, anio), evaluadas)

    def test_el_criterio_es_uno_de_los_dos_admitidos(self) -> None:
        self.assertIn(_CFG.obtener("sensibilidad_series.criterio_umbral"),
                      ("utiles", "racha"))


class PruebaConfiguracionReal(unittest.TestCase):
    def test_los_parametros_declarados_existen(self) -> None:
        umbrales = list(_CFG.obtener("sensibilidad_series.umbrales_anios"))
        self.assertEqual(umbrales, sorted(umbrales))
        minimo = _CFG.obtener("sensibilidad_series.completitud_anual_minima")
        self.assertTrue(0.0 < float(minimo) <= 1.0)

    def test_las_ventanas_se_resuelven(self) -> None:
        anio = int(_CFG.obtener("proyecto.anio_estudio"))
        for ventana in _CFG.obtener("sensibilidad_series.ventanas"):
            inicio, fin = m04b.resolver_ventana(ventana, anio)
            self.assertLess(inicio, fin)


if __name__ == "__main__":
    unittest.main(verbosity=2)

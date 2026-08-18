# -*- coding: utf-8 -*-
"""
Pruebas del M16: cartografía temática.

Solo las funciones puras. La composición y la exportación exigen el Python de
QGIS y no pueden verificarse desde el venv, pero el cálculo de la escala, el
encuadre y la lectura de la declaración sí, y son lo que decide si la plancha
sale utilizable o con la cuenca fuera del marco.

    python tests/test_m16.py
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

import M16_cartografia as m16  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion, ErrorFormato, ErrorHidrologia, ErrorRutas,
)

_CFG = cargar(raiz=_RAIZ_REPO)
_SERIE = _CFG.obtener("cartografia.serie_escalas")
_PLANCHA = _CFG.obtener("cartografia.plancha")


class PruebaMarco(unittest.TestCase):
    def test_descuenta_margenes_y_panel(self) -> None:
        x, y, ancho, alto = m16.marco_del_mapa(
            {"ancho_mm": 420, "alto_mm": 297, "margen_mm": 10,
             "ancho_panel_mm": 95})
        self.assertAlmostEqual(ancho, 420 - 10 - 10 - 95 - 10)
        self.assertAlmostEqual(alto, 297 - 20)
        self.assertAlmostEqual(x, 10)
        self.assertAlmostEqual(y, 10)

    def test_un_panel_que_no_cabe_es_error(self) -> None:
        # Se detiene en lugar de componer sobre un marco de ancho negativo.
        with self.assertRaises(ErrorConfiguracion):
            m16.marco_del_mapa({"ancho_mm": 100, "alto_mm": 297,
                                "margen_mm": 10, "ancho_panel_mm": 95})

    def test_una_plancha_incompleta_es_error(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            m16.marco_del_mapa({"ancho_mm": 420, "alto_mm": 297})


class PruebaEscala(unittest.TestCase):
    """
    SE REDONDEA HACIA ARRIBA. Una escala una posición por debajo deja la cuenca
    fuera del marco, y eso no se ve hasta imprimir.
    """

    def setUp(self) -> None:
        _, _, self.ancho, self.alto = m16.marco_del_mapa(_PLANCHA)

    def _escala(self, ancho_m, alto_m, margen=0.0):
        return m16.escala_normalizada(ancho_m, alto_m, self.ancho, self.alto,
                                      _SERIE, margen)

    def test_la_extension_siempre_cabe_en_el_marco(self) -> None:
        for ancho_m, alto_m in ((27100, 36500), (2200, 3100), (146200, 114600),
                                (900, 700), (95000, 12000)):
            escala = self._escala(ancho_m, alto_m)
            self.assertGreaterEqual(escala * (self.ancho / 1000.0), ancho_m)
            self.assertGreaterEqual(escala * (self.alto / 1000.0), alto_m)

    def test_la_escala_pertenece_a_la_serie(self) -> None:
        self.assertIn(self._escala(27100, 36500), _SERIE)

    def test_manda_el_lado_mas_exigente(self) -> None:
        # Una cuenca alargada en vertical se encuadra por su alto, no por su
        # ancho, aunque el marco sea apaisado.
        self.assertEqual(self._escala(1000, 36500), self._escala(27100, 36500))

    def test_el_margen_nunca_reduce_la_escala(self) -> None:
        sin_margen = self._escala(27100, 36500, 0.0)
        con_margen = self._escala(27100, 36500, 0.20)
        self.assertGreaterEqual(con_margen, sin_margen)

    def test_una_extension_degenerada_es_error(self) -> None:
        # Es el caso de encuadrar por una capa de un solo punto.
        for ancho_m, alto_m in ((0, 100), (100, 0), (-5, 100)):
            with self.assertRaises(ErrorHidrologia):
                self._escala(ancho_m, alto_m)

    def test_si_la_serie_no_alcanza_se_detiene(self) -> None:
        # No devuelve la mayor en silencio: eso recortaría el mapa.
        with self.assertRaises(ErrorHidrologia):
            m16.escala_normalizada(5_000_000, 100, self.ancho, self.alto,
                                   [1000, 5000], 0.0)

    def test_un_marco_sin_superficie_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m16.escala_normalizada(1000, 1000, 0, 100, _SERIE)


class PruebaEncuadre(unittest.TestCase):
    """
    La extensión se recalcula para que la escala impresa sea la anunciada. Un
    mapa cuyo rótulo dice 1:50.000 y mide 1:47.318 no es defendible.
    """

    def test_el_marco_queda_exactamente_a_la_escala(self) -> None:
        encuadrada = m16.extension_para_escala((0, 0, 27100, 36500), 150000,
                                               295.0, 277.0)
        self.assertAlmostEqual(encuadrada[2] - encuadrada[0],
                               150000 * 0.295, places=3)
        self.assertAlmostEqual(encuadrada[3] - encuadrada[1],
                               150000 * 0.277, places=3)

    def test_conserva_el_centro(self) -> None:
        original = (1000.0, 2000.0, 4000.0, 8000.0)
        encuadrada = m16.extension_para_escala(original, 50000, 295.0, 277.0)
        self.assertAlmostEqual((original[0] + original[2]) / 2,
                               (encuadrada[0] + encuadrada[2]) / 2, places=6)
        self.assertAlmostEqual((original[1] + original[3]) / 2,
                               (encuadrada[1] + encuadrada[3]) / 2, places=6)

    def test_nunca_recorta_la_extension_original(self) -> None:
        original = (0.0, 0.0, 27100.0, 36500.0)
        _, _, ancho, alto = m16.marco_del_mapa(_PLANCHA)
        escala = m16.escala_normalizada(27100, 36500, ancho, alto, _SERIE, 0.05)
        encuadrada = m16.extension_para_escala(original, escala, ancho, alto)
        self.assertLessEqual(encuadrada[0], original[0])
        self.assertLessEqual(encuadrada[1], original[1])
        self.assertGreaterEqual(encuadrada[2], original[2])
        self.assertGreaterEqual(encuadrada[3], original[3])


class PruebaGrilla(unittest.TestCase):
    def test_devuelve_una_cifra_legible(self) -> None:
        # 3.700 m sería exacto y también ilegible en el margen de una plancha.
        for ancho in (5000, 44250, 177000, 900):
            self.assertIn(m16.intervalo_de_grilla(ancho),
                          m16.INTERVALOS_GRILLA)

    def test_se_aproxima_al_numero_de_divisiones_pedido(self) -> None:
        intervalo = m16.intervalo_de_grilla(44250, 5)
        self.assertGreaterEqual(44250 / intervalo, 2.5)
        self.assertLessEqual(44250 / intervalo, 9.0)

    def test_nunca_devuelve_cero(self) -> None:
        for ancho, divisiones in ((0, 5), (-10, 5), (1000, 0)):
            self.assertGreater(m16.intervalo_de_grilla(ancho, divisiones), 0)


class PruebaEscalaComoTexto(unittest.TestCase):
    def test_usa_el_separador_de_miles_del_informe(self) -> None:
        self.assertEqual(m16.escala_como_texto(150000), "1:150.000")
        self.assertEqual(m16.escala_como_texto(1000000), "1:1.000.000")
        self.assertEqual(m16.escala_como_texto(500), "1:500")


class PruebaTokenDePatron(unittest.TestCase):
    def test_captura_lo_que_el_comodin_reemplaza(self) -> None:
        self.assertEqual(
            m16._token_del_patron("data/r/pmax_*.tif", "data/r/pmax_T100.tif"),
            "T100")

    def test_no_depende_del_separador_del_sistema(self) -> None:
        self.assertEqual(
            m16._token_del_patron("data/r/pmax_*.tif",
                                  "data\\r\\pmax_T2_33.tif"),
            "T2_33")

    def test_una_ruta_que_no_casa_no_da_token(self) -> None:
        self.assertIsNone(
            m16._token_del_patron("data/r/pmax_*.tif", "data/r/isoyetas.tif"))

    def test_un_patron_con_dos_comodines_no_es_utilizable(self) -> None:
        self.assertIsNone(
            m16._token_del_patron("a/*_*.tif", "a/x_y.tif"))


class PruebaDeclaracion(unittest.TestCase):
    """La declaración real del repositorio, no una inventada para la prueba."""

    def setUp(self) -> None:
        self.ruta = _RAIZ_REPO / "config" / "mapas.yaml"
        self.tmp = Path(tempfile.mkdtemp())

    def test_la_declaracion_del_repositorio_se_lee(self) -> None:
        planchas = m16.leer_declaracion(self.ruta, self.tmp, self.tmp)
        self.assertTrue(planchas)
        for plancha in planchas:
            self.assertTrue(plancha.identificador)
            self.assertTrue(plancha.titulo)
            self.assertTrue(plancha.capas)

    def test_toda_plancha_declara_su_encuadre(self) -> None:
        # Sin capa de encuadre no hay forma de fijar el marco.
        for plancha in m16.leer_declaracion(self.ruta, self.tmp, self.tmp):
            self.assertTrue(plancha.encuadre, plancha.identificador)

    def test_las_esenciales_estan_entre_sus_capas(self) -> None:
        for plancha in m16.leer_declaracion(self.ruta, self.tmp, self.tmp):
            declaradas = {c.identificador for c in plancha.capas}
            self.assertTrue(set(plancha.esenciales) <= declaradas,
                            plancha.identificador)

    def test_no_hay_identificadores_repetidos(self) -> None:
        identificadores = [p.identificador for p in
                           m16.leer_declaracion(self.ruta, self.tmp, self.tmp)]
        self.assertEqual(len(identificadores), len(set(identificadores)))

    def test_una_declaracion_ausente_es_error(self) -> None:
        with self.assertRaises(ErrorRutas):
            m16.leer_declaracion(self.tmp / "no_existe.yaml", self.tmp,
                                 self.tmp)

    def test_una_capa_no_declarada_es_error(self) -> None:
        # Se detiene en lugar de componer una plancha incompleta en silencio.
        ruta = self.tmp / "mapas.yaml"
        ruta.write_text(
            "capas:\n  a: {tipo: vector, ruta: 'a.shp'}\n"
            "mapas:\n  - id: m\n    encuadre: a\n    capas: [a, inexistente]\n",
            encoding="utf-8")
        with self.assertRaises(ErrorFormato):
            m16.leer_declaracion(ruta, self.tmp, self.tmp)

    def test_un_encuadre_no_declarado_es_error(self) -> None:
        ruta = self.tmp / "mapas.yaml"
        ruta.write_text(
            "capas:\n  a: {tipo: vector, ruta: 'a.shp'}\n"
            "mapas:\n  - id: m\n    encuadre: otra\n    capas: [a]\n",
            encoding="utf-8")
        with self.assertRaises(ErrorFormato):
            m16.leer_declaracion(ruta, self.tmp, self.tmp)

    def test_una_esencial_fuera_de_sus_capas_es_error(self) -> None:
        ruta = self.tmp / "mapas.yaml"
        ruta.write_text(
            "capas:\n  a: {tipo: vector, ruta: 'a.shp'}\n"
            "  b: {tipo: vector, ruta: 'b.shp'}\n"
            "mapas:\n  - id: m\n    encuadre: a\n    capas: [a]\n"
            "    esenciales: [b]\n",
            encoding="utf-8")
        with self.assertRaises(ErrorFormato):
            m16.leer_declaracion(ruta, self.tmp, self.tmp)

    def test_un_tipo_de_capa_desconocido_es_error(self) -> None:
        ruta = self.tmp / "mapas.yaml"
        ruta.write_text(
            "capas:\n  a: {tipo: malla, ruta: 'a.shp'}\n"
            "mapas:\n  - id: m\n    encuadre: a\n    capas: [a]\n",
            encoding="utf-8")
        with self.assertRaises(ErrorFormato):
            m16.leer_declaracion(ruta, self.tmp, self.tmp)


class PruebaSeries(unittest.TestCase):
    """
    Una plancha por archivo que exista, no por una lista escrita a mano: un
    estudio con otros periodos de retorno no exige editar la declaración.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "r").mkdir()
        for nombre in ("pmax_T10.tif", "pmax_T100.tif", "pmax_T2_33.tif",
                       "otra_cosa.tif"):
            (self.tmp / "r" / nombre).write_bytes(b"")

    def test_produce_una_entrada_por_archivo(self) -> None:
        expandidas = m16.expandir_series(
            [{"id": "p_{token}", "patron": "r/pmax_*.tif"}], self.tmp)
        self.assertEqual([t for _, t in expandidas],
                         ["T10", "T100", "T2_33"])

    def test_no_recoge_lo_que_no_casa(self) -> None:
        expandidas = m16.expandir_series(
            [{"id": "p_{token}", "patron": "r/pmax_*.tif"}], self.tmp)
        self.assertNotIn("otra_cosa", [t for _, t in expandidas])

    def test_un_patron_sin_coincidencias_no_produce_planchas(self) -> None:
        self.assertEqual(
            m16.expandir_series([{"id": "x", "patron": "r/nada_*.tif"}],
                                self.tmp), [])

    def test_una_serie_sin_patron_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            m16.expandir_series([{"id": "x"}], self.tmp)


class PruebaSimbologia(unittest.TestCase):
    """
    La convención cartográfica es doctrina y vive en data/referencia, no en el
    código.
    """

    def setUp(self) -> None:
        self.ruta = (_RAIZ_REPO / "data" / "referencia"
                     / "simbologia_cartografia.csv")
        self.tabla = m16.leer_simbologia(self.ruta)

    def test_la_tabla_del_repositorio_se_lee(self) -> None:
        self.assertTrue(self.tabla)

    def test_los_limites_van_sin_relleno(self) -> None:
        # Un área de influencia rellena tapa el mapa entero.
        for clave in ("area_influencia", "subzona_intersectada", "envolvente",
                      "subcuencas"):
            self.assertEqual(self.tabla[clave]["relleno"].strip(), "",
                             f"{clave} lleva relleno y taparía el contenido")

    def test_los_colores_son_hexadecimales(self) -> None:
        for clave, fila in self.tabla.items():
            for columna in ("relleno", "borde"):
                valor = (fila.get(columna) or "").strip()
                if valor:
                    self.assertRegex(valor, r"^#[0-9a-fA-F]{6}$",
                                     f"{clave}.{columna}")

    def test_todo_raster_declara_su_rampa(self) -> None:
        for clave, fila in self.tabla.items():
            if fila.get("geometria", "").strip() == "raster":
                self.assertTrue((fila.get("rampa") or "").strip(), clave)

    def test_la_opacidad_esta_entre_cero_y_uno(self) -> None:
        for clave, fila in self.tabla.items():
            valor = float(fila.get("opacidad") or 1.0)
            self.assertGreaterEqual(valor, 0.0, clave)
            self.assertLessEqual(valor, 1.0, clave)

    def test_una_tabla_ausente_no_es_error(self) -> None:
        # Significa que el estudio no la sobreescribe, no que falte algo.
        self.assertEqual(m16.leer_simbologia(Path("no_existe.csv")), {})

    def test_ninguna_capa_queda_sin_origen_de_simbologia(self) -> None:
        """
        Toda capa declarada tiene de dónde sacar su aspecto.

        SON TRES ORIGENES Y BASTA UNO: el .qml que el consultor guarde tras
        calibrar en QGIS, la simbología graduada o categorizada que la propia
        plancha declara, o la fila de la tabla de convenciones. Una capa sin
        ninguno sale con el color aleatorio de QGIS, y eso no es entregable.
        """
        tmp = Path(tempfile.mkdtemp())
        huerfanas = []
        for plancha in m16.leer_declaracion(
                _RAIZ_REPO / "config" / "mapas.yaml", tmp, tmp):
            for capa in plancha.capas:
                rol = Path(capa.estilo).stem if capa.estilo else ""
                if capa.simbologia or (rol and rol in self.tabla):
                    continue
                huerfanas.append(capa.identificador)
        self.assertEqual(sorted(set(huerfanas)), [])

    def test_toda_capa_declara_su_archivo_de_estilo(self) -> None:
        # Sin .qml declarado, lo que el consultor ajuste en QGIS no tiene dónde
        # guardarse y se pierde en la siguiente corrida de la cadena.
        tmp = Path(tempfile.mkdtemp())
        sin_estilo = [capa.identificador
                      for plancha in m16.leer_declaracion(
                          _RAIZ_REPO / "config" / "mapas.yaml", tmp, tmp)
                      for capa in plancha.capas if not capa.estilo]
        self.assertEqual(sorted(set(sin_estilo)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
Pruebas del M16: composición de planchas sobre la plantilla de QGIS.

    python tests/test_m16.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M16_cartografia as m16  # noqa: E402
from comun.errores import ErrorHidrologia  # noqa: E402



class PruebaEscalaNormalizada(unittest.TestCase):
    """
    HACIA ARRIBA Y NO A LA MAS PROXIMA. Hacia abajo la escala se hace mayor, el
    terreno abarcado se encoge y lo que se queria encuadrar deja de caber.
    """

    SERIE = (25000, 50000, 100000, 150000, 165000, 200000, 250000)

    def test_redondea_hacia_arriba(self) -> None:
        # 1:234.656 fue una escala real de la plantilla del consultor.
        self.assertEqual(m16.escala_normalizada(234656, self.SERIE), 250000)
        self.assertEqual(m16.escala_normalizada(101, self.SERIE), 25000)

    def test_un_valor_exacto_de_la_serie_se_respeta(self) -> None:
        self.assertEqual(m16.escala_normalizada(150000, self.SERIE), 150000)

    def test_por_encima_de_la_serie_devuelve_la_mayor(self) -> None:
        # Quien llame lo reporta: el encuadre se sale del juego de casa.
        self.assertEqual(m16.escala_normalizada(9e6, self.SERIE), 250000)


class PruebaEncuadre(unittest.TestCase):
    """La escala escrita y la medida sobre el papel tienen que coincidir."""

    SERIE = (100000, 150000, 200000, 250000)

    def test_la_extension_final_corresponde_a_la_escala(self) -> None:
        final, escala = m16.encuadrar(
            (0, 0, 30000, 35000), 190.0, 230.0, self.SERIE)
        ancho_m = final[2] - final[0]
        self.assertAlmostEqual(ancho_m * 1000.0 / 190.0, escala, places=3)

    def test_se_conserva_el_centro(self) -> None:
        # Recentrar sobre otro punto moveria el mapa sin decirlo.
        final, _ = m16.encuadrar((100, 200, 30100, 35200), 190.0, 230.0,
                                 self.SERIE)
        self.assertAlmostEqual((final[0] + final[2]) / 2, 15100, places=3)
        self.assertAlmostEqual((final[1] + final[3]) / 2, 17700, places=3)

    def test_manda_la_dimension_que_no_cabe(self) -> None:
        # Una extension apaisada en un marco vertical: manda el ancho.
        _final, escala = m16.encuadrar((0, 0, 40000, 1000), 190.0, 230.0,
                                       self.SERIE)
        self.assertGreaterEqual(escala * 190.0 / 1000.0, 40000)

    def test_un_marco_sin_medidas_es_error(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m16.encuadrar((0, 0, 100, 100), 0.0, 230.0, self.SERIE)


class PruebaTextosDelRotulo(unittest.TestCase):
    def test_no_se_repite_la_palabra_proyecto(self) -> None:
        # La plantilla lo tenia de las dos formas, debajo de un titulo que ya
        # dice 'PROYECTO:'.
        self.assertEqual(
            m16.texto_de_proyecto("PROYECTO Refugio del Valle", "agosto 2026"),
            "REFUGIO DEL VALLE AGOSTO 2026")
        self.assertEqual(
            m16.texto_de_proyecto("Refugio del Valle", "agosto 2026"),
            "REFUGIO DEL VALLE AGOSTO 2026")

    def test_el_contenido_lleva_titulo_y_subtitulo(self) -> None:
        self.assertEqual(m16.texto_de_contenido("A", "B"), "A\nB")
        self.assertEqual(m16.texto_de_contenido("A", ""), "A")

    def test_la_escala_se_escribe_con_punto_de_miles(self) -> None:
        # No con el separador de la maquina: una plancha no puede salir
        # distinta segun quien la genere.
        self.assertEqual(m16.formatear_escala(165000), "165.000")
        self.assertEqual(m16.formatear_escala(1000000), "1.000.000")


class PruebaCatalogoReal(unittest.TestCase):
    """El catalogo del repositorio contra la plantilla del repositorio."""

    def setUp(self) -> None:
        self.plantilla = _RAIZ_REPO / "templates" / "planchas.qgz"
        if not self.plantilla.is_file():
            self.skipTest("no esta la plantilla de planchas")
        self.catalogo = m16.leer_catalogo(
            _RAIZ_REPO / "config" / "planchas.yaml")
        raiz, _ = m16.abrir_proyecto(self.plantilla)
        self.layouts = m16.layouts_por_nombre(raiz)

    def test_cada_plancha_declarada_existe_en_la_plantilla(self) -> None:
        # Se emparejan por el nombre del layout: si no coincide, el modulo no
        # adivina y la plancha sale con el encuadre de la plantilla.
        for plancha in self.catalogo["planchas"]:
            self.assertIn(plancha["layout"], self.layouts)

    def test_cada_layout_de_la_plantilla_esta_declarado(self) -> None:
        declarados = {p["layout"] for p in self.catalogo["planchas"]}
        for nombre in self.layouts:
            if nombre.startswith("Figura"):
                self.assertIn(nombre, declarados)

    def test_toda_plancha_declara_titulo_y_capa_que_enmarca(self) -> None:
        for plancha in self.catalogo["planchas"]:
            self.assertTrue(plancha.get("titulo"), plancha["layout"])
            self.assertTrue(plancha.get("enmarca"), plancha["layout"])

    def test_cada_plancha_tiene_un_marco_de_mapa(self) -> None:
        for plancha in self.catalogo["planchas"]:
            items = m16.items_por_id(self.layouts[plancha["layout"]])
            self.assertTrue(items.get(m16.ID_MAPA), plancha["layout"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
Pruebas de la jerarquía de la red de drenaje.

Solo cubren las funciones de GRAFO, que no dependen de QGIS y por eso corren
en el venv. El resto de red_drenaje (recorte, eje de polígonos, orientación)
usa primitivas de QGIS y se prueba desde ese entorno.

    python tests/test_red_drenaje.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import red_drenaje as red  # noqa: E402


# =============================================================================
# Red de referencia
# =============================================================================
# Cuenca de juguete con jerarquía conocida. El diccionario es "quién desemboca
# en quién", que es la forma en que construir_adyacencia entrega la red.
#
#   1  2   3  4        1 y 2 nacen y confluyen en 10  -> 10 es de orden 2
#    \/     \/         3 y 4 nacen y confluyen en 11  -> 11 es de orden 2
#    10     11         10 y 11 confluyen en 20        -> 20 es de orden 3
#      \   /           5 nace y entra en 20, que ya
#       20 <- 5          es mayor, de modo que 20 no sube
#        |
#       21              21 continúa a 20: hereda el orden 3
#
RED = {
    10: [1, 2],
    11: [3, 4],
    20: [10, 11, 5],
    21: [20],
}
TRAMOS = [1, 2, 3, 4, 5, 10, 11, 20, 21]


class PruebaOrdenStrahler(unittest.TestCase):
    def setUp(self) -> None:
        self.orden, self.ciclos = red.orden_strahler(RED, TRAMOS)

    def test_las_cabeceras_son_de_orden_uno(self) -> None:
        for cabecera in (1, 2, 3, 4, 5):
            self.assertEqual(self.orden[cabecera], 1, f"tramo {cabecera}")

    def test_dos_del_mismo_orden_suben(self) -> None:
        self.assertEqual(self.orden[10], 2)
        self.assertEqual(self.orden[11], 2)

    def test_dos_de_orden_dos_suben_a_tres(self) -> None:
        self.assertEqual(self.orden[20], 3)

    def test_un_afluente_menor_no_sube_el_orden(self) -> None:
        # El tramo 5, de orden 1, entra en el 20 junto a dos de orden 2. Con la
        # regla de Shreve el orden sumaría; con la de Strahler no.
        self.assertEqual(self.orden[20], 3)

    def test_la_continuacion_hereda_el_orden(self) -> None:
        self.assertEqual(self.orden[21], 3)

    def test_no_hay_ciclos_en_una_red_sana(self) -> None:
        self.assertEqual(self.ciclos, [])

    def test_todos_los_tramos_reciben_orden(self) -> None:
        self.assertEqual(set(self.orden), set(TRAMOS))

    def test_un_tramo_suelto_es_de_orden_uno(self) -> None:
        orden, _ = red.orden_strahler({}, [7])
        self.assertEqual(orden[7], 1)

    def test_una_cadena_larga_no_agota_la_pila(self) -> None:
        # Con recursión, una red encadenada de miles de tramos revienta el
        # intérprete. La red del estudio tiene ocho mil.
        cadena = {i: [i + 1] for i in range(5000)}
        orden, ciclos = red.orden_strahler(cadena, list(range(5001)))
        self.assertEqual(ciclos, [])
        self.assertEqual(set(orden.values()), {1})

    def test_un_ciclo_se_reporta_en_lugar_de_colgar(self) -> None:
        # La adyacencia se resuelve por proximidad, no por topología
        # declarada: dos tramos pueden quedar señalándose el uno al otro.
        orden, ciclos = red.orden_strahler({1: [2], 2: [1]}, [1, 2])
        self.assertTrue(ciclos)
        self.assertEqual(set(orden), {1, 2})


class PruebaConteoDeCorrientes(unittest.TestCase):
    """
    Contar corrientes no es contar tramos. La cartografía parte un mismo río en
    decenas de piezas por razones de dibujo.
    """

    def setUp(self) -> None:
        self.orden, _ = red.orden_strahler(RED, TRAMOS)
        self.corrientes = red.contar_corrientes(self.orden, RED)

    def test_cinco_corrientes_de_orden_uno(self) -> None:
        self.assertEqual(self.corrientes[1], 5)

    def test_dos_corrientes_de_orden_dos(self) -> None:
        self.assertEqual(self.corrientes[2], 2)

    def test_una_sola_de_orden_tres_pese_a_ser_dos_tramos(self) -> None:
        # Los tramos 20 y 21 son ambos de orden 3 y consecutivos: son UNA
        # corriente. Contar tramos daría dos y falsearía la bifurcación.
        self.assertEqual(self.corrientes[3], 1)

    def test_una_cadena_de_orden_uno_cuenta_como_una(self) -> None:
        cadena = {10: [11], 11: [12]}
        orden, _ = red.orden_strahler(cadena, [10, 11, 12])
        self.assertEqual(red.contar_corrientes(orden, cadena), {1: 1})


class PruebaRazonDeBifurcacion(unittest.TestCase):
    def test_pares_consecutivos_y_media(self) -> None:
        resultado = red.razon_bifurcacion({1: 40, 2: 10, 3: 3})
        self.assertEqual([p["orden"] for p in resultado["pares"]],
                         ["1/2", "2/3"])
        self.assertAlmostEqual(resultado["pares"][0]["razon"], 4.0)
        self.assertAlmostEqual(resultado["media_simple"],
                               round((4.0 + 10 / 3) / 2, 3), places=3)

    def test_la_ponderada_no_deja_que_el_par_alto_mande(self) -> None:
        """
        En el ultimo par el denominador vale 1 por definicion en una cuenca de
        una sola salida, de modo que el cociente es grande siempre. Medido
        sobre la red de este estudio, la media simple da 10,01 y la ponderada
        6,53: la diferencia entera la aporta el par 4/5, calculado sobre veinte
        corrientes frente a las 7.404 del par 1/2.
        """
        resultado = red.razon_bifurcacion({1: 6414, 2: 990, 3: 155, 4: 19, 5: 1})
        self.assertAlmostEqual(resultado["media_simple"], 10.006, places=2)
        self.assertAlmostEqual(resultado["media_ponderada"], 6.529, places=2)
        self.assertEqual(resultado["adoptada"], resultado["media_ponderada"])

    def test_el_peso_de_cada_par_son_sus_corrientes(self) -> None:
        resultado = red.razon_bifurcacion({1: 40, 2: 10, 3: 3})
        self.assertEqual(resultado["pares"][0]["peso"], 50)
        self.assertEqual(resultado["pares"][1]["peso"], 13)

    def test_el_rango_natural_se_juzga_sobre_la_ponderada(self) -> None:
        self.assertTrue(
            red.razon_bifurcacion({1: 40, 2: 10, 3: 3})["dentro_del_rango_natural"])
        self.assertFalse(
            red.razon_bifurcacion({1: 100, 2: 10, 3: 1})["dentro_del_rango_natural"])

    def test_un_solo_orden_no_da_razon(self) -> None:
        resultado = red.razon_bifurcacion({1: 10})
        self.assertEqual(resultado["pares"], [])
        self.assertIsNone(resultado["media_ponderada"])
        self.assertIsNone(resultado["adoptada"])

    def test_un_orden_ausente_no_inventa_el_par(self) -> None:
        # Si falta el orden 2, no se puede formar 1/2 ni 2/3.
        self.assertEqual(red.razon_bifurcacion({1: 10, 3: 2})["pares"], [])


class PruebaCaminoMasLargo(unittest.TestCase):
    """
    El cauce principal es el recorrido más largo hasta la salida, no el río con
    nombre ni el de mayor orden.
    """

    def test_elige_por_longitud_y_no_por_orden(self) -> None:
        # El 11 es de orden 2 y el 5 de orden 1, pero el 5 es mucho más largo.
        longitudes = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 100.0,
                      10: 2.0, 11: 2.0, 20: 5.0, 21: 5.0}
        camino = red.camino_mas_largo(RED, longitudes, 21)
        self.assertEqual(camino[-1], 21)
        self.assertIn(5, camino)
        self.assertNotIn(11, camino)

    def test_la_longitud_acumulada_es_la_maxima(self) -> None:
        longitudes = {i: 1.0 for i in TRAMOS}
        camino = red.camino_mas_largo(RED, longitudes, 21)
        # Cualquier recorrido desde una cabecera hasta el 21 tiene cuatro
        # tramos: cabecera, confluencia, 20 y 21.
        self.assertEqual(len(camino), 4)

    def test_el_camino_va_de_la_cabecera_a_la_salida(self) -> None:
        longitudes = {i: 1.0 for i in TRAMOS}
        camino = red.camino_mas_largo(RED, longitudes, 21)
        self.assertEqual(camino[-1], 21)
        self.assertEqual(RED.get(camino[0], []), [])

    def test_una_salida_sin_afluentes_es_ella_sola(self) -> None:
        self.assertEqual(red.camino_mas_largo({}, {9: 3.0}, 9), [9])

    def test_un_ciclo_no_cuelga(self) -> None:
        camino = red.camino_mas_largo({1: [2], 2: [1]}, {1: 1.0, 2: 1.0}, 1)
        self.assertTrue(camino)


if __name__ == "__main__":
    unittest.main(verbosity=2)

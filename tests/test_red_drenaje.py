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


class PruebaEngancheDelPunto(unittest.TestCase):
    """
    El punto NO se engancha al tramo más cercano.

    Es el defecto que este proyecto destapó: el eje derivado por adelgazamiento
    deja hebras paralelas y muñones de una o dos celdas donde el cauce se
    ensancha. Sobre el punto real, el tramo más cercano estaba a 26,5 m y no
    arrastraba nada; el que lleva la red estaba a 62,6 m y arrastraba 275 km.
    """

    # Un muñón corto justo al lado del punto, y la hebra buena un poco más
    # lejos, con una cadena colgando de ella.
    VERTICES = {
        1: [(0.0, 0.0), (10.0, 0.0)],          # muñón, a 5 m del punto
        2: [(0.0, 20.0), (40.0, 20.0)],        # hebra buena, a 15 m
        3: [(0.0, 60.0), (0.0, 20.0)],         # cuelga de la 2
        4: [(-40.0, 90.0), (0.0, 60.0)],       # cuelga de la 3
        9: [(200.0, 200.0), (260.0, 200.0)],   # lejos, fuera de tolerancia
    }
    AFLUENTES = {2: [3], 3: [4]}
    LONGITUDES = {1: 10.0, 2: 40.0, 3: 40.0, 4: 50.0, 9: 60.0}
    PUNTO = (5.0, 5.0)

    def _enganchar(self, tolerancia=50.0):
        return red.enganchar_punto(
            self.AFLUENTES, self.LONGITUDES, self.VERTICES,
            self.PUNTO[0], self.PUNTO[1], tolerancia)

    def test_adopta_la_hebra_que_arrastra_la_red(self) -> None:
        resultado = self._enganchar()
        self.assertEqual(resultado["tramo"], 2)
        self.assertEqual(resultado["tramos_arriba"], 3)

    def test_declara_que_descarto_el_mas_cercano(self) -> None:
        # Sin esta declaración, el informe no podría explicar por qué el área
        # no se apoya en el tramo que cualquiera habría elegido.
        resultado = self._enganchar()
        self.assertTrue(resultado["descartado_el_mas_cercano"])
        self.assertEqual(resultado["mas_cercano"], 1)
        self.assertEqual(resultado["red_del_mas_cercano_km"], 0.01)

    def test_la_envolvente_cubre_lo_trazado(self) -> None:
        xmin, ymin, xmax, ymax = self._enganchar()["envolvente"]
        self.assertLessEqual(xmin, -40.0)
        self.assertGreaterEqual(xmax, 40.0)
        self.assertGreaterEqual(ymax, 90.0)

    def test_lo_que_esta_fuera_de_tolerancia_no_compite(self) -> None:
        claves = {c["tramo"] for c in self._enganchar()["candidatos"]}
        self.assertNotIn(9, claves)

    def test_sin_red_cerca_lo_dice(self) -> None:
        resultado = self._enganchar(tolerancia=1.0)
        self.assertIsNone(resultado["tramo"])
        self.assertIn("ningún tramo", resultado["motivo"])

    def test_con_un_solo_candidato_no_hay_descarte(self) -> None:
        resultado = red.enganchar_punto(
            self.AFLUENTES, self.LONGITUDES, self.VERTICES,
            5.0, 5.0, 6.0)
        self.assertEqual(resultado["tramo"], 1)
        self.assertFalse(resultado["descartado_el_mas_cercano"])

    def test_a_igualdad_de_red_manda_la_distancia(self) -> None:
        vertices = {1: [(0.0, 0.0), (10.0, 0.0)],
                    2: [(0.0, 30.0), (10.0, 30.0)]}
        resultado = red.enganchar_punto(
            {}, {1: 10.0, 2: 10.0}, vertices, 5.0, 5.0, 50.0)
        self.assertEqual(resultado["tramo"], 1)

    def test_la_distancia_es_al_segmento_y_no_a_sus_extremos(self) -> None:
        # Un punto frente al centro de un segmento largo está cerca de él,
        # aunque sus vértices queden lejos.
        vertices = {1: [(-500.0, 10.0), (500.0, 10.0)]}
        resultado = red.enganchar_punto({}, {1: 1000.0}, vertices,
                                        0.0, 0.0, 20.0)
        self.assertEqual(resultado["tramo"], 1)
        self.assertAlmostEqual(resultado["distancia_m"], 10.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PruebaPuenteSobreEmbalses(unittest.TestCase):
    """
    El IGAC dibuja los embalses como polígono aparte y la red queda cortada en
    cada uno. Se tratan como NODO y no como tramo: entra agua por varios
    afluentes y sale por uno.

    No se les deriva eje. Se intentó rasterizarlos junto con los cauces dobles
    y la cadena resultante subía de 2.789 a 3.097 m sobre el Embalse San
    Rafael: el esqueleto recorre los brazos laterales del polígono, no un
    cauce, y la dirección de flujo dentro de un embalse no existe.
    """

    # Embalse cuadrado de 100 m de lado, centrado en el origen.
    EMBALSE = [[(-50.0, -50.0), (-50.0, 50.0), (50.0, 50.0), (50.0, -50.0)]]

    def _tramos(self):
        # Dos entradas que mueren en la orilla norte y este, y una salida que
        # arranca de la orilla sur.
        return [
            (1, (-200.0, 300.0), (-20.0, 50.0)),    # entra por el norte
            (2, (300.0, 200.0), (50.0, 20.0)),      # entra por el este
            (3, (0.0, -50.0), (0.0, -400.0)),       # sale por el sur
            (9, (900.0, 900.0), (950.0, 950.0)),    # lejos, no toca
        ]

    def test_las_entradas_desembocan_en_la_salida(self) -> None:
        afluentes, informe = red.puentear_embalses(
            self._tramos(), {}, [("San Rafael", self.EMBALSE)], 5.0)
        self.assertEqual(sorted(afluentes[3]), [1, 2])
        self.assertEqual(informe[0]["puenteados"], 2)
        self.assertEqual(informe[0]["salida_adoptada"], 3)

    def test_lo_que_no_toca_el_embalse_no_se_conecta(self) -> None:
        afluentes, _ = red.puentear_embalses(
            self._tramos(), {}, [("San Rafael", self.EMBALSE)], 5.0)
        self.assertNotIn(9, afluentes.get(3, []))

    def test_no_se_duplica_una_arista_ya_existente(self) -> None:
        # Si la red ya estaba conectada por otra via, repetir la arista
        # crearia un ciclo y el orden de Strahler quedaria indefinido.
        afluentes, informe = red.puentear_embalses(
            self._tramos(), {3: [1]}, [("San Rafael", self.EMBALSE)], 5.0)
        self.assertEqual(sorted(afluentes[3]), [1, 2])
        self.assertEqual(informe[0]["puenteados"], 1)

    def test_sin_salida_se_declara_sumidero(self) -> None:
        tramos = [(1, (-200.0, 300.0), (-20.0, 50.0))]
        afluentes, informe = red.puentear_embalses(
            tramos, {}, [("Sin desague", self.EMBALSE)], 5.0)
        self.assertEqual(afluentes, {})
        self.assertIn("sumidero", informe[0]["motivo"])

    def test_sin_entradas_no_hace_nada(self) -> None:
        tramos = [(3, (0.0, -50.0), (0.0, -400.0))]
        _, informe = red.puentear_embalses(
            tramos, {}, [("Solo salida", self.EMBALSE)], 5.0)
        self.assertEqual(informe[0]["puenteados"], 0)
        self.assertIn("sin entradas", informe[0]["motivo"])

    def test_con_dos_salidas_manda_la_cota(self) -> None:
        # Un embalse con dos desagues es posible, pero tambien es la senal de
        # una cartografia con un brazo mal cerrado. Se adopta la de menor cota
        # y se declara.
        tramos = self._tramos() + [(4, (-50.0, 0.0), (-400.0, 0.0))]
        cotas = {(0.0, -50.0): 2900.0, (-50.0, 0.0): 2850.0}
        afluentes, informe = red.puentear_embalses(
            tramos, {}, [("Dos desagues", self.EMBALSE)], 5.0,
            cota=lambda x, y: cotas.get((x, y), float("nan")))
        self.assertEqual(informe[0]["salidas"], 2)
        self.assertEqual(informe[0]["salida_adoptada"], 4)
        self.assertIn("menor cota", informe[0]["motivo"])

    def test_la_tolerancia_decide_que_toca(self) -> None:
        # Un tramo que muere a 30 m de la orilla no toca con tolerancia de 5.
        tramos = [(1, (-200.0, 300.0), (-20.0, 80.0)),
                  (3, (0.0, -50.0), (0.0, -400.0))]
        _, estrecha = red.puentear_embalses(
            tramos, {}, [("E", self.EMBALSE)], 5.0)
        _, amplia = red.puentear_embalses(
            tramos, {}, [("E", self.EMBALSE)], 40.0)
        self.assertEqual(estrecha[0]["puenteados"], 0)
        self.assertEqual(amplia[0]["puenteados"], 1)

    def test_la_distancia_al_poligono_es_al_borde(self) -> None:
        # Dentro del polígono la distancia al BORDE no es cero.
        self.assertAlmostEqual(
            red.distancia_a_poligono(0.0, 0.0, self.EMBALSE), 50.0, places=6)
        self.assertAlmostEqual(
            red.distancia_a_poligono(-50.0, 0.0, self.EMBALSE), 0.0, places=6)
        self.assertAlmostEqual(
            red.distancia_a_poligono(-70.0, 0.0, self.EMBALSE), 20.0, places=6)

# -*- coding: utf-8 -*-
"""
Pruebas del módulo compartido de graficación.

No comprueban que las figuras sean bonitas, cosa que ninguna prueba puede hacer.
Comprueban lo que sí es verificable y sí se rompe en silencio: que el estilo
salga de config.yaml y no del código, que se escriban todos los formatos
declarados, que la rampa ordinal esté ordenada, y que las figuras se cierren.

    python tests/test_graficos.py
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

from comun.config import cargar  # noqa: E402

try:
    import graficos
    import matplotlib.pyplot as plt
    HAY_MATPLOTLIB = True
except ImportError:  # pragma: no cover
    HAY_MATPLOTLIB = False

_CFG = cargar(raiz=_RAIZ_REPO)


@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaEstilo(unittest.TestCase):
    def test_el_estilo_sale_de_la_configuracion(self) -> None:
        estilo = graficos.Estilo.desde_config(_CFG)
        self.assertEqual(estilo.dpi, int(_CFG.obtener("graficos.dpi")))
        self.assertEqual(list(estilo.formatos),
                         list(_CFG.obtener("graficos.formatos")))
        self.assertTrue(estilo.paleta)

    def test_el_backend_no_es_interactivo(self) -> None:
        # Un backend con ventana rompería una ejecución desatendida.
        import matplotlib
        self.assertEqual(matplotlib.get_backend().lower(), "agg")

    def test_la_paleta_da_la_vuelta(self) -> None:
        estilo = graficos.Estilo(paleta=("#000000", "#ffffff"))
        self.assertEqual(estilo.color(0), estilo.color(2))
        self.assertEqual(estilo.color(1), estilo.color(3))

    def test_paleta_vacia_no_revienta(self) -> None:
        self.assertEqual(graficos.Estilo(paleta=()).color(0),
                         graficos.GRIS_CONTEXTO)

    def test_el_tamano_se_convierte_a_pulgadas(self) -> None:
        estilo = graficos.Estilo(ancho_cm=25.4, alto_cm=12.7)
        self.assertAlmostEqual(estilo.tamano_pulgadas[0], 10.0)
        self.assertAlmostEqual(estilo.tamano_pulgadas[1], 5.0)


@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaRampa(unittest.TestCase):
    """
    La rampa existe porque la paleta categórica no ordena.

    Aplicada a umbrales, que sí están ordenados, hacía que el primer y el último
    grupo salieran en tonos de azul parecidos pese a ser los extremos opuestos.
    """

    def setUp(self) -> None:
        self.estilo = graficos.Estilo.desde_config(_CFG)

    def test_devuelve_tantos_colores_como_se_piden(self) -> None:
        for cuantos in (1, 2, 6, 12):
            self.assertEqual(len(graficos.rampa(cuantos, self.estilo)), cuantos)

    def test_cero_colores(self) -> None:
        self.assertEqual(graficos.rampa(0, self.estilo), [])

    def test_invertir_da_la_vuelta(self) -> None:
        directa = graficos.rampa(5, self.estilo)
        inversa = graficos.rampa(5, self.estilo, invertir=True)
        self.assertEqual(directa, list(reversed(inversa)))

    def test_los_colores_son_distintos_entre_si(self) -> None:
        colores = graficos.rampa(6, self.estilo)
        self.assertEqual(len(set(colores)), 6)

    def test_ningun_color_es_casi_blanco(self) -> None:
        # Por debajo del recorte, los puntos desaparecen sobre fondo blanco.
        for color in graficos.rampa(6, self.estilo):
            canales = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
            self.assertLess(sum(canales) / 3, 235, color)


@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaTramos(unittest.TestCase):
    def test_un_bloque_continuo(self) -> None:
        self.assertEqual(graficos.tramos_consecutivos([2000, 2001, 2002]),
                         [(2000.0, 3.0)])

    def test_el_hueco_parte_el_tramo(self) -> None:
        self.assertEqual(
            graficos.tramos_consecutivos([2000, 2001, 2010, 2011]),
            [(2000.0, 2.0), (2010.0, 2.0)])

    def test_conjunto_vacio(self) -> None:
        self.assertEqual(graficos.tramos_consecutivos([]), [])

    def test_los_repetidos_no_alargan(self) -> None:
        self.assertEqual(graficos.tramos_consecutivos([1999, 1999, 2000]),
                         [(1999.0, 2.0)])

    def test_el_desorden_no_importa(self) -> None:
        self.assertEqual(graficos.tramos_consecutivos([2002, 2000, 2001]),
                         [(2000.0, 3.0)])


@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaEscritura(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.estilo = graficos.Estilo(formatos=("png", "svg"), dpi=72)

    def test_escribe_todos_los_formatos_declarados(self) -> None:
        with graficos.figura(self.estilo, titulo="prueba") as (fig, ax):
            ax.plot([0, 1], [0, 1])
            escritas = graficos.guardar(fig, self.tmp / "figura", self.estilo)
        self.assertEqual([r.suffix for r in escritas], [".png", ".svg"])
        for ruta in escritas:
            self.assertGreater(ruta.stat().st_size, 0)

    def test_crea_el_directorio_si_no_existe(self) -> None:
        destino = self.tmp / "sub" / "otro" / "figura"
        with graficos.figura(self.estilo) as (fig, _):
            graficos.guardar(fig, destino, self.estilo)
        self.assertTrue(destino.with_suffix(".png").is_file())

    def test_la_figura_se_cierra_al_salir(self) -> None:
        # Decenas de figuras sin cerrar agotan la memoria del proceso, que es el
        # defecto que arrastraba la rutina heredada.
        antes = len(plt.get_fignums())
        for _ in range(5):
            with graficos.figura(self.estilo) as (fig, ax):
                ax.plot([0, 1], [1, 0])
        self.assertEqual(len(plt.get_fignums()), antes)

    def test_se_cierra_aunque_el_dibujo_falle(self) -> None:
        antes = len(plt.get_fignums())
        with self.assertRaises(ValueError):
            with graficos.figura(self.estilo) as (fig, ax):
                raise ValueError("fallo al dibujar")
        self.assertEqual(len(plt.get_fignums()), antes)

    def test_varias_celdas_entregan_el_arreglo(self) -> None:
        with graficos.figura(self.estilo, filas=3, columnas=1) as (fig, ejes):
            self.assertEqual(len(ejes), 3)
            ejes[0][0].plot([0, 1], [0, 1])


@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaAltoDeBarras(unittest.TestCase):
    def test_crece_con_las_filas(self) -> None:
        estilo = graficos.Estilo()
        self.assertLess(graficos.alto_para_filas(10, estilo),
                        graficos.alto_para_filas(100, estilo))

    def test_respeta_un_minimo(self) -> None:
        estilo = graficos.Estilo()
        self.assertGreaterEqual(graficos.alto_para_filas(1, estilo), 6.0)



@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaReproyeccion(unittest.TestCase):
    """
    Las figuras geográficas se rotulan en coordenadas planas.

    El informe de referencia presenta sus mapas en MAGNA Ciudad Bogotá, no en
    grados. Además, la figura se dibuja a escala equivalente, y en grados eso
    deforma las distancias con la latitud.
    """

    def test_mismo_sistema_es_la_identidad(self) -> None:
        conv = graficos.transformador("EPSG:3116", "EPSG:3116")
        self.assertEqual(conv(1000.0, 2000.0), (1000.0, 2000.0))

    def test_destino_vacio_es_la_identidad(self) -> None:
        conv = graficos.transformador("EPSG:4686", "")
        self.assertEqual(conv(-74.0, 4.6), (-74.0, 4.6))

    def test_reproyecta_a_magna_bogota(self) -> None:
        conv = graficos.transformador("EPSG:4686", "EPSG:3116")
        este, norte = conv(-74.03111111, 4.81316667)
        # El punto de descarga del estudio ronda (1003512, 1025918).
        self.assertTrue(9.5e5 < este < 1.1e6, este)
        self.assertTrue(9.5e5 < norte < 1.1e6, norte)

    def test_el_orden_es_este_norte(self) -> None:
        # Sin always_xy algunos sistemas devuelven la latitud primero y la
        # figura sale girada noventa grados.
        conv = graficos.transformador("EPSG:4686", "EPSG:9377")
        este, norte = conv(-74.0, 4.6)
        self.assertGreater(este, norte)

    def test_ida_y_vuelta(self) -> None:
        ida = graficos.transformador("EPSG:4686", "EPSG:3116")
        vuelta = graficos.transformador("EPSG:3116", "EPSG:4686")
        lon, lat = vuelta(*ida(-74.1, 4.7))
        self.assertAlmostEqual(lon, -74.1, places=6)
        self.assertAlmostEqual(lat, 4.7, places=6)

    def test_el_crs_de_figuras_por_defecto_es_el_de_ingreso(self) -> None:
        declarado = _CFG.obtener("graficos.crs_figuras")
        efectivo = declarado or _CFG.obtener("punto_descarga.crs")
        self.assertTrue(str(efectivo).upper().startswith("EPSG:"))


@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaBarrasDeRango(unittest.TestCase):
    """La longitud teórica del catálogo y el dato real, superpuestos."""

    def setUp(self) -> None:
        self.estilo = graficos.Estilo(formatos=("png",), dpi=72)

    def test_dibuja_rango_y_tramos(self) -> None:
        with graficos.figura(self.estilo) as (fig, ax):
            graficos.barras_de_rango(
                ax, ["A", "B"],
                [(1970.0, 2020.0), (1990.0, 2000.0)],
                [[(1970.0, 10.0), (1990.0, 5.0)], []],
                self.estilo,
            )
            self.assertEqual(len(ax.get_yticklabels()), 2)

    def test_una_estacion_sin_rango_no_revienta(self) -> None:
        with graficos.figura(self.estilo) as (fig, ax):
            graficos.barras_de_rango(
                ax, ["A"], [None], [[(2000.0, 3.0)]], self.estilo)

    def test_el_eje_queda_invertido(self) -> None:
        # La primera fila debe quedar arriba, como en el gráfico de referencia.
        with graficos.figura(self.estilo) as (fig, ax):
            graficos.barras_de_rango(
                ax, ["A", "B"], [(1, 2), (3, 4)], [[], []], self.estilo)
            inferior, superior = ax.get_ylim()
            self.assertGreater(inferior, superior)


@unittest.skipUnless(HAY_MATPLOTLIB, "matplotlib no está instalado")
class PruebaAislamientoDeEntornos(unittest.TestCase):
    """
    src/comun no puede depender de matplotlib.

    Ese paquete lo comparte el Python de QGIS, que no tiene por qué disponer de
    la librería. Romper esta separación rompe el esquema de doble entorno de
    CLAUDE.md, sección 3, y el fallo aparecería lejos, en un módulo SIG.
    """

    def test_comun_no_importa_matplotlib_ni_numpy(self) -> None:
        prohibidas = ("matplotlib", "numpy", "scipy", "pandas", "pyproj")
        for archivo in (_DIRECTORIO_SRC / "comun").glob("*.py"):
            texto = archivo.read_text(encoding="utf-8")
            for linea in texto.splitlines():
                limpia = linea.strip()
                if not (limpia.startswith("import ")
                        or limpia.startswith("from ")):
                    continue
                for libreria in prohibidas:
                    self.assertNotIn(
                        f" {libreria}", f" {limpia}",
                        f"{archivo.name} importa {libreria}: "
                        "rompe el doble entorno",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)

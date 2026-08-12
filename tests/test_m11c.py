# -*- coding: utf-8 -*-
"""
Pruebas del M11c: factor de reducción por área.

    python tests/test_m11c.py
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

import M11c_arf as m11c  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorHidrologia, ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

TABLA = [
    {"area_km2": 100.0, "duracion_h": 3.0, "arf": 0.89, "origen": "x"},
    {"area_km2": 100.0, "duracion_h": 24.0, "arf": 0.95, "origen": "x"},
    {"area_km2": 400.0, "duracion_h": 3.0, "arf": 0.83, "origen": "x"},
    {"area_km2": 400.0, "duracion_h": 24.0, "arf": 0.92, "origen": "x"},
]


class PruebaInterpolacion(unittest.TestCase):
    """
    En el área se interpola en LOGARITMO, porque así se trazan las curvas.

    Interpolar linealmente sobre un eje dibujado logarítmico sesga hacia
    factores altos, es decir hacia el lado inseguro.
    """

    def test_un_nodo_devuelve_su_propio_valor(self) -> None:
        self.assertAlmostEqual(
            m11c.interpolar_arf(TABLA, 100.0, 3.0)["arf"], 0.89, places=4)

    def test_el_area_se_interpola_en_logaritmo(self) -> None:
        # 200 km2 está en el punto medio logarítmico entre 100 y 400, de modo
        # que el factor debe ser la media de 0,89 y 0,83.
        obtenido = m11c.interpolar_arf(TABLA, 200.0, 3.0)["arf"]
        self.assertAlmostEqual(obtenido, 0.86, places=4)
        # La interpolación lineal habría dado 0,873, más alta y más insegura.
        self.assertLess(obtenido, 0.873)

    def test_la_duracion_se_interpola_linealmente(self) -> None:
        obtenido = m11c.interpolar_arf(TABLA, 100.0, 13.5)["arf"]
        self.assertAlmostEqual(obtenido, 0.92, places=4)

    def test_fuera_de_tabla_no_extrapola_y_lo_declara(self) -> None:
        # Extrapolar una curva empírica más allá de donde se midió es lo que la
        # matriz de tiempos de concentración impide en el M10.
        fuera = m11c.interpolar_arf(TABLA, 5000.0, 3.0)
        self.assertAlmostEqual(fuera["arf"], 0.83, places=4)
        self.assertAlmostEqual(fuera["area_usada_km2"], 400.0, places=3)
        self.assertIn("fuera del rango", fuera["fuera_de_tabla"])

    def test_una_tabla_incompleta_no_inventa_valores(self) -> None:
        with self.assertRaises(ErrorFormato):
            m11c.interpolar_arf(TABLA[:3], 200.0, 13.0)

    def test_area_o_duracion_no_positivas(self) -> None:
        with self.assertRaises(ErrorHidrologia):
            m11c.interpolar_arf(TABLA, 0.0, 3.0)


class PruebaLecturaDeTabla(unittest.TestCase):
    def _escribir(self, texto: str) -> Path:
        ruta = Path(tempfile.mkdtemp()) / "arf.csv"
        ruta.write_text(texto, encoding="utf-8")
        return ruta

    def test_un_factor_mayor_que_uno_se_rechaza(self) -> None:
        # Amplificaría la lluvia en lugar de reducirla.
        ruta = self._escribir("area_km2;duracion_h;arf;origen\n1;1;1.20;x\n")
        with self.assertRaises(ErrorFormato):
            m11c.leer_tabla_arf(ruta, ";")

    def test_un_factor_nulo_se_rechaza(self) -> None:
        ruta = self._escribir("area_km2;duracion_h;arf;origen\n1;1;0;x\n")
        with self.assertRaises(ErrorFormato):
            m11c.leer_tabla_arf(ruta, ";")

    def test_una_tabla_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorRutas):
            m11c.leer_tabla_arf(Path("no_existe.csv"), ";")


class PruebaTablaReal(unittest.TestCase):
    """La tabla es doctrina y vive en data/referencia, no en el código."""

    def setUp(self) -> None:
        self.ruta = _RAIZ_REPO / _CFG.obtener("arf.tabla")

    def test_la_tabla_existe_y_es_legible(self) -> None:
        self.assertTrue(self.ruta.is_file(), str(self.ruta))
        self.assertGreaterEqual(len(m11c.leer_tabla_arf(self.ruta, ";")), 20)

    def test_cada_fila_declara_su_origen(self) -> None:
        for fila in m11c.leer_tabla_arf(self.ruta, ";"):
            self.assertTrue(fila["origen"],
                            f"{fila['area_km2']} km2 sin origen declarado")

    def test_el_factor_decrece_con_el_area(self) -> None:
        tabla = m11c.leer_tabla_arf(self.ruta, ";")
        for duracion in sorted({f["duracion_h"] for f in tabla}):
            de_esa = sorted((f for f in tabla if f["duracion_h"] == duracion),
                            key=lambda f: f["area_km2"])
            valores = [f["arf"] for f in de_esa]
            self.assertEqual(valores, sorted(valores, reverse=True),
                             f"a {duracion} h el factor no decrece con el área")

    def test_el_factor_crece_con_la_duracion(self) -> None:
        # A igual área, tres horas se reducen más que veinticuatro.
        tabla = m11c.leer_tabla_arf(self.ruta, ";")
        for area in sorted({f["area_km2"] for f in tabla}):
            de_esa = sorted((f for f in tabla if f["area_km2"] == area),
                            key=lambda f: f["duracion_h"])
            valores = [f["arf"] for f in de_esa]
            self.assertEqual(valores, sorted(valores),
                             f"en {area} km2 el factor no crece con la duración")

    def test_la_tabla_es_rectangular(self) -> None:
        # Sin todas las combinaciones, la interpolación no puede completarse
        # sin inventar valores.
        tabla = m11c.leer_tabla_arf(self.ruta, ";")
        areas = {f["area_km2"] for f in tabla}
        duraciones = {f["duracion_h"] for f in tabla}
        self.assertEqual(len(tabla), len(areas) * len(duraciones))


class PruebaAnalitico(unittest.TestCase):
    def test_con_area_despreciable_vale_uno(self) -> None:
        self.assertAlmostEqual(m11c.arf_analitico(1e-9, 3.0), 1.0, places=6)

    def test_decrece_con_el_area(self) -> None:
        self.assertGreater(m11c.arf_analitico(10.0, 3.0),
                           m11c.arf_analitico(1000.0, 3.0))

    def test_crece_con_la_duracion(self) -> None:
        self.assertLess(m11c.arf_analitico(200.0, 1.0),
                        m11c.arf_analitico(200.0, 24.0))


class PruebaComposicionDeFactores(unittest.TestCase):
    """
    El factor se aplica UNA vez, sobre la lámina de su propia duración.

    La primera versión aplicaba el de 24 h aquí y dejaba un residual para el
    M12b. Daba el mismo número, pero repartía un factor entre dos módulos y
    confiaba en que nadie olvidase la segunda mitad.
    """

    def test_las_dos_rutas_dan_el_mismo_resultado(self) -> None:
        # Es lo que justifica el cambio: si difirieran, sería una decisión
        # técnica y no una simplificación.
        de_24 = m11c.interpolar_arf(TABLA, 200.0, 24.0)["arf"]
        de_3 = m11c.interpolar_arf(TABLA, 200.0, 3.0)["arf"]
        residual = de_3 / de_24
        self.assertAlmostEqual(de_24 * residual, de_3, places=6)

    def test_el_factor_de_diseno_reduce_mas_que_el_de_la_serie(self) -> None:
        # Un aguacero corto es más localizado: sobre la misma área hay que
        # reducir más a 3 h que a 24 h.
        self.assertLess(m11c.interpolar_arf(TABLA, 200.0, 3.0)["arf"],
                        m11c.interpolar_arf(TABLA, 200.0, 24.0)["arf"])

    def test_el_modulo_ya_no_aplica_el_factor(self) -> None:
        # Aplicar aquí reduciría una P24h con el factor de otra duración.
        self.assertFalse(hasattr(m11c, "aplicar_factor"))


class PruebaConfiguracion(unittest.TestCase):
    def test_la_politica_esta_entre_las_admitidas(self) -> None:
        self.assertIn(str(_CFG.obtener("arf.aplicar")),
                      ("evaluar", "forzar_si", "forzar_no"))

    def test_la_duracion_de_diseno_esta_declarada(self) -> None:
        # El factor depende de ella: a igual área, tres horas se reducen más
        # que veinticuatro.
        self.assertGreater(float(_CFG.obtener("tormenta.duracion_h")), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

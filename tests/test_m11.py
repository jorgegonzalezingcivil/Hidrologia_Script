# -*- coding: utf-8 -*-
"""
Pruebas del M11: zonificación pluviométrica y precipitación por subcuenca.

    python tests/test_m11.py
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

import M11_zonificacion as m11  # noqa: E402
from comun import geometria  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


class PruebaZonificacion(unittest.TestCase):
    """
    Se agrupan subcuencas que no difieren más del umbral declarado.

    El criterio lo fija el consultor y el resultado tiene que poder explicarse
    en una frase ante interventoría.
    """

    def _sub(self, nombre, valor, area=1.0):
        return {"subcuenca": nombre, "p_mm": valor, "area_km2": area}

    def test_valores_proximos_caen_en_una_zona(self) -> None:
        zonas = m11.zonificar(
            [self._sub("A", 100.0), self._sub("B", 102.0),
             self._sub("C", 104.0)], "p_mm", 5.0)
        self.assertEqual(len(zonas), 1)
        self.assertEqual(zonas[0]["subcuencas"], 3)

    def test_un_salto_por_encima_del_umbral_abre_zona(self) -> None:
        zonas = m11.zonificar(
            [self._sub("A", 100.0), self._sub("B", 130.0)], "p_mm", 5.0)
        self.assertEqual(len(zonas), 2)

    def test_la_media_de_la_zona_pondera_por_area(self) -> None:
        # Una subcuenca de diez kilómetros cuadrados no puede pesar lo mismo
        # que una de uno (CLAUDE.md, sección 6).
        zonas = m11.zonificar(
            [self._sub("A", 100.0, area=9.0), self._sub("B", 104.0, area=1.0)],
            "p_mm", 5.0)
        self.assertEqual(len(zonas), 1)
        self.assertAlmostEqual(zonas[0]["precipitacion_media_mm"], 100.4,
                               places=2)

    def test_las_subcuencas_sin_valor_no_entran(self) -> None:
        zonas = m11.zonificar(
            [self._sub("A", 100.0), {"subcuenca": "B", "p_mm": None,
                                     "area_km2": 1.0}], "p_mm", 5.0)
        self.assertEqual(zonas[0]["miembros"], ["A"])

    def test_sin_ningun_valor_no_inventa_zonas(self) -> None:
        self.assertEqual(m11.zonificar([], "p_mm", 5.0), [])


class PruebaGradienteMedido(unittest.TestCase):
    """
    El gradiente se lee de quien lo midió, no se vuelve a calcular.

    Duplicar el ajuste abriría la puerta a que dos partes del estudio
    declarasen gradientes distintos sobre las mismas estaciones.
    """

    REPORTE = {
        "por_periodo": {
            "2.33": {"gradiente_altitudinal": {
                "r2": 0.058, "pendiente_mm_por_m": 0.0045,
                "altitud_min_m": 1900.0, "altitud_max_m": 3195.0}},
            "100": {"gradiente_altitudinal": {
                "r2": 0.141, "pendiente_mm_por_m": 0.0121,
                "altitud_min_m": 1900.0, "altitud_max_m": 3195.0}},
        }
    }

    def _escribir(self, contenido):
        temporal = Path(tempfile.mkdtemp()) / "M08_isoyetas_pmax.json"
        temporal.write_text(json.dumps(contenido), encoding="utf-8")
        return temporal

    def test_recupera_el_r2_y_el_rango_de_cotas(self) -> None:
        medido = m11.leer_gradiente_medido(self._escribir(self.REPORTE))
        self.assertAlmostEqual(medido["r2_maximo"], 0.141)
        self.assertAlmostEqual(medido["altitud_min_m"], 1900.0)
        self.assertAlmostEqual(medido["altitud_max_m"], 3195.0)

    def test_un_reporte_ausente_no_inventa_gradiente(self) -> None:
        self.assertEqual(m11.leer_gradiente_medido(Path("no_existe.json")), {})

    def test_un_reporte_sin_ajuste_devuelve_vacio(self) -> None:
        self.assertEqual(
            m11.leer_gradiente_medido(self._escribir({"por_periodo": {}})), {})


class PruebaCentroide(unittest.TestCase):
    """
    El respaldo por centroide existe para las subcuencas más pequeñas que la
    celda del campo interpolado: no es ausencia de lluvia, es de muestreo.
    """

    CUADRADO = [[(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0),
                 (0.0, 0.0)]]

    def test_el_centroide_de_un_cuadrado_es_su_centro(self) -> None:
        x, y = geometria.centroide(self.CUADRADO)
        self.assertAlmostEqual(x, 5.0, places=6)
        self.assertAlmostEqual(y, 5.0, places=6)

    def test_un_hueco_desplaza_el_centroide(self) -> None:
        # Hueco en la mitad derecha: el centroide se corre a la izquierda.
        hueco = [(6.0, 2.0), (8.0, 2.0), (8.0, 8.0), (6.0, 8.0), (6.0, 2.0)]
        x, _ = geometria.centroide([self.CUADRADO[0], hueco])
        self.assertLess(x, 5.0)

    def test_una_forma_en_ele_no_da_el_centro_de_la_envolvente(self) -> None:
        ele = [[(0.0, 0.0), (0.0, 10.0), (2.0, 10.0), (2.0, 2.0),
                (10.0, 2.0), (10.0, 0.0), (0.0, 0.0)]]
        x, y = geometria.centroide(ele)
        envolvente = geometria.envolvente([ele])
        centro_caja = ((envolvente[0] + envolvente[2]) / 2,
                       (envolvente[1] + envolvente[3]) / 2)
        self.assertNotAlmostEqual(x, centro_caja[0], places=3)
        self.assertNotAlmostEqual(y, centro_caja[1], places=3)

    def test_un_poligono_degenerado_no_revienta(self) -> None:
        recta = [[(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (0.0, 0.0)]]
        x, y = geometria.centroide(recta)
        self.assertTrue(0.0 <= x <= 10.0)
        self.assertAlmostEqual(y, 0.0, places=6)

    def test_sin_vertices_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorFormato):
            geometria.centroide([])


class PruebaConfiguracion(unittest.TestCase):
    def test_el_r2_minimo_esta_declarado(self) -> None:
        minimo = float(_CFG.obtener(
            "zonificacion_pluviometrica.r2_minimo_gradiente"))
        self.assertGreater(minimo, 0.0)
        self.assertLessEqual(minimo, 1.0)

    def test_el_periodo_de_referencia_es_uno_de_los_calculados(self) -> None:
        # Zonificar con un periodo que el M07 no calcula dejaría al M11 sin
        # campo con el que agrupar.
        referencia = str(_CFG.obtener(
            "zonificacion_pluviometrica.periodo_referencia"))
        periodos = [str(p) for p in
                    _CFG.obtener("frecuencia.periodos_retorno")]
        self.assertIn(referencia, periodos)

    def test_el_umbral_de_zonificacion_es_razonable(self) -> None:
        umbral = float(_CFG.obtener(
            "zonificacion_pluviometrica.diferencia_maxima_pct"))
        self.assertGreater(umbral, 0.0)
        self.assertLess(umbral, 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

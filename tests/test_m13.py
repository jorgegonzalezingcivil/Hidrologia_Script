# -*- coding: utf-8 -*-
"""
Pruebas del M13: actualización del proyecto de HEC-HMS.

    python tests/test_m13.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M13_hec_hms as m13  # noqa: E402
from comun.config import cargar  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

MODELO = """Basin: Basin 1
     Unit System: Metric
End:

Subbasin: SB1
     Canvas X: 1000.0
     Area: 1.5
     Downstream: J1

     LossRate: Initial+Constant
     Percent Impervious Area: 0.0

     Transform: Clark
     Clark Method: Specified
     Time Area Method: Default

     Baseflow: Recession
End:

Reach: R1
     Canvas X: 2000.0
     Downstream: J2

     Route: Lag
     Channel Loss: None
End:
"""


class PruebaSeparacionDeBloques(unittest.TestCase):
    """
    Las líneas en blanco separan bloques y no deben ocultarlos.

    La primera versión condicionaba el encabezado a que el buffer estuviera
    vacío: la línea en blanco previa lo llenaba, ninguna subcuenca se
    clasificaba y el módulo actualizaba cero sin decir por qué.
    """

    def test_reconoce_cada_tipo(self) -> None:
        bloques = m13.separar_bloques(MODELO)
        tipos = [b[0] for b in bloques]
        self.assertIn("Subbasin", tipos)
        self.assertIn("Reach", tipos)
        self.assertIn("Basin", tipos)

    def test_conserva_el_texto_integro(self) -> None:
        # Un reescritor que solo emitiera lo que entiende borraría en silencio
        # lo que el paso manual dejó.
        bloques = m13.separar_bloques(MODELO)
        self.assertEqual("".join(b[2] for b in bloques), MODELO)

    def test_recupera_el_nombre(self) -> None:
        nombres = {b[0]: b[1] for b in m13.separar_bloques(MODELO)}
        self.assertEqual(nombres["Subbasin"], "SB1")
        self.assertEqual(nombres["Reach"], "R1")


class PruebaCampos(unittest.TestCase):
    BLOQUE = "Subbasin: SB1\n     Area: 1.5\n     Transform: Clark\nEnd:\n"

    def test_sustituye_conservando_la_sangria(self) -> None:
        salida = m13.fijar_campo(self.BLOQUE, "Transform", "SCS")
        self.assertIn("     Transform: SCS\n", salida)
        self.assertNotIn("Clark", salida)

    def test_anade_el_campo_que_falta(self) -> None:
        salida = m13.fijar_campo(self.BLOQUE, "Curve Number", "74.0")
        self.assertIn("     Curve Number: 74.0\n", salida)
        self.assertTrue(salida.rstrip().endswith("End:"))

    def test_quita_los_campos_del_metodo_anterior(self) -> None:
        salida = m13.quitar_campos(self.BLOQUE, ("Transform",))
        self.assertNotIn("Transform", salida)
        self.assertIn("Area: 1.5", salida)


class PruebaActualizacion(unittest.TestCase):
    def _subcuenca(self) -> str:
        return [b[2] for b in m13.separar_bloques(MODELO)
                if b[0] == "Subbasin"][0]

    def _tramo(self) -> str:
        return [b[2] for b in m13.separar_bloques(MODELO)
                if b[0] == "Reach"][0]

    def test_la_subcuenca_pasa_a_scs_con_sus_parametros(self) -> None:
        salida, motivo = m13.actualizar_subcuenca(
            self._subcuenca(), {"cn": 74.0, "tlag_min": 28.14})
        self.assertEqual(motivo, "")
        self.assertIn("LossRate: SCS", salida)
        self.assertIn("Transform: SCS", salida)
        self.assertIn("Curve Number: 74.0", salida)
        self.assertIn("Lag: 28.14", salida)
        self.assertNotIn("Clark Method", salida)

    def test_sin_rezago_no_se_toca_y_se_reporta(self) -> None:
        # Rellenarla con un valor por defecto produciría un modelo que corre y
        # miente.
        original = self._subcuenca()
        salida, motivo = m13.actualizar_subcuenca(original, {"cn": 74.0})
        self.assertEqual(salida, original)
        self.assertIn("tlag_min", motivo)

    def test_la_topologia_se_conserva(self) -> None:
        salida, _ = m13.actualizar_subcuenca(
            self._subcuenca(), {"cn": 74.0, "tlag_min": 28.14})
        self.assertIn("Downstream: J1", salida)
        self.assertIn("Canvas X: 1000.0", salida)
        self.assertIn("Area: 1.5", salida)

    def test_el_tramo_pasa_a_muskingum_cunge(self) -> None:
        salida, motivo = m13.actualizar_tramo(
            self._tramo(), {"longitud_m": 809.21, "pendiente": 0.0123},
            0.04, 3.0, 2.0)
        self.assertEqual(motivo, "")
        self.assertIn("Route: Muskingum Cunge", salida)
        self.assertIn("Length: 809.21", salida)
        self.assertIn("Energy Slope: 0.012300", salida)
        self.assertIn("Manning n: 0.040", salida)
        self.assertIn("Downstream: J2", salida)

    def test_un_tramo_sin_pendiente_no_se_toca(self) -> None:
        original = self._tramo()
        salida, motivo = m13.actualizar_tramo(
            original, {"longitud_m": 100.0, "pendiente": 0.0}, 0.04, 3.0, 2.0)
        self.assertEqual(salida, original)
        self.assertIn("pendiente", motivo)

    def test_un_tramo_sin_geometria_no_se_toca(self) -> None:
        original = self._tramo()
        salida, motivo = m13.actualizar_tramo(original, {}, 0.04, 3.0, 2.0)
        self.assertEqual(salida, original)


class PruebaConfiguracion(unittest.TestCase):
    def test_la_plantilla_no_lleva_la_ruta_de_un_estudio(self) -> None:
        # Es un dato de proyecto: heredarlo haría que el siguiente estudio
        # escribiera sobre el modelo de este.
        self.assertEqual(
            str(_CFG.obtener("hec_hms.proyecto.directorio", "")).strip(), "")

    def test_la_copia_de_seguridad_esta_declarada(self) -> None:
        self.assertIsInstance(
            _CFG.obtener("hec_hms.proyecto.copia_de_seguridad"), bool)

    def test_el_n_de_manning_es_razonable(self) -> None:
        n = _CFG.obtener("hec_hms.transito.muskingum_cunge.n_manning")
        if n is None:
            self.skipTest("sin declarar en la plantilla")
        self.assertGreater(float(n), 0.01)
        self.assertLess(float(n), 0.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

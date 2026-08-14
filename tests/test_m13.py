# -*- coding: utf-8 -*-
"""
Pruebas del M13: actualización del proyecto de HEC-HMS.

    python tests/test_m13.py
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

import M13_hec_hms as m13  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402

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
        # 'Mannings n' con ese: es la etiqueta que HEC-HMS escribe, leida de un
        # tramo que el propio programa configuro.
        self.assertIn("Mannings n: 0.040", salida)
        self.assertIn("Downstream: J2", salida)

    def test_el_end_del_bloque_no_se_pierde(self) -> None:
        # Cuando el metodo es el ultimo grupo no hay linea en blanco tras el, y
        # detenerse solo en la linea en blanco se comia el 'End:': el bloque
        # quedaba sin cerrar y se fusionaba con el siguiente. Se perdian la
        # mitad de las subcuencas.
        salida, _ = m13.actualizar_tramo(
            self._tramo(), {"longitud_m": 100.0, "pendiente": 0.01},
            0.04, 3.0, 2.0)
        self.assertEqual(salida.count("End:"), 1)
        self.assertTrue(salida.rstrip().endswith("End:"))

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


TOPOLOGIA = """Basin: Basin 1
End:

Subbasin: SB1
     Area: 10.0
     Downstream: R1
End:

Subbasin: SB2
     Area: 5.0
     Downstream: R1
End:

Reach: R1
     Downstream: J1
End:

Subbasin: SB3
     Area: 3.0
     Downstream: J1
End:

Junction: J1
     Downstream: Sink-1
End:

Sink: Sink-1
End:
"""


class PruebaAreasAcumuladas(unittest.TestCase):
    """
    El ancho de cada tramo sale de SU área de drenaje, no de un valor único.
    La suma en el cierre es la comprobación de que la red no tiene ramas sueltas.
    """

    def test_cada_tramo_reune_lo_que_tiene_aguas_arriba(self) -> None:
        acumuladas = m13.areas_acumuladas(TOPOLOGIA)
        self.assertAlmostEqual(acumuladas["R1"], 15.0)
        self.assertAlmostEqual(acumuladas["J1"], 18.0)

    def test_el_cierre_reune_la_cuenca_entera(self) -> None:
        acumuladas = m13.areas_acumuladas(TOPOLOGIA)
        self.assertAlmostEqual(acumuladas["Sink-1"], 18.0)

    def test_una_subcuenca_se_cuenta_a_si_misma(self) -> None:
        self.assertAlmostEqual(m13.areas_acumuladas(TOPOLOGIA)["SB1"], 10.0)

    def test_un_enlace_circular_no_cuelga_el_modulo(self) -> None:
        # La delimitación no debería producirlo, pero tampoco lo impide, y un
        # recorrido sin control de visitados no terminaría nunca.
        circular = ("Subbasin: A\n     Area: 1.0\n     Downstream: B\nEnd:\n\n"
                    "Junction: B\n     Downstream: C\nEnd:\n\n"
                    "Junction: C\n     Downstream: B\nEnd:\n")
        acumuladas = m13.areas_acumuladas(circular)
        self.assertAlmostEqual(acumuladas["B"], 1.0)
        self.assertAlmostEqual(acumuladas["C"], 1.0)


class PruebaAnchoDeFondo(unittest.TestCase):
    def test_crece_con_la_raiz_del_area(self) -> None:
        # w = 2*A^0.5: cuadruplicar el área duplica el ancho.
        uno = m13.ancho_por_geometria_hidraulica(25.0, 2.0, 0.5)
        otro = m13.ancho_por_geometria_hidraulica(100.0, 2.0, 0.5)
        self.assertAlmostEqual(uno, 10.0)
        self.assertAlmostEqual(otro, 20.0)

    def test_nunca_baja_del_minimo(self) -> None:
        # Un área nula solo puede venir de una topología rota; un ancho cero
        # daría una sección sin área y HEC-HMS no lo resolvería.
        self.assertEqual(
            m13.ancho_por_geometria_hidraulica(0.0, 2.0, 0.5, minimo_m=1.0), 1.0)

    def test_la_relacion_se_lee_de_la_tabla_de_doctrina(self) -> None:
        ruta = _RAIZ_REPO / _CFG.obtener(
            "hec_hms.transito.muskingum_cunge.tabla_geometria")
        relacion = m13.leer_geometria_hidraulica(ruta, ";")
        self.assertGreater(relacion["coeficiente"], 0.0)
        self.assertGreater(relacion["exponente"], 0.0)
        self.assertLess(relacion["exponente"], 1.0)
        self.assertTrue(relacion["fuente"])

    def test_una_variable_ausente_es_error_explicito(self) -> None:
        ruta = _RAIZ_REPO / _CFG.obtener(
            "hec_hms.transito.muskingum_cunge.tabla_geometria")
        with self.assertRaises(ErrorFormato):
            m13.leer_geometria_hidraulica(ruta, ";", variable="no_existe")

    def test_tabla_ausente(self) -> None:
        with self.assertRaises(ErrorRutas):
            m13.leer_geometria_hidraulica(Path("no_existe.csv"), ";")


class PruebaMeteorologia(unittest.TestCase):
    """
    El vocabulario del .met está leído de uno que HEC-HMS reescribió al guardar,
    y su log lo confirma: 'Found no parameter problems in meteorologic model'.
    """

    ASIGNACION = [
        {"subcuenca": "SB1", "pluviometro": "Z1"},
        {"subcuenca": "SB2", "pluviometro": "Z2"},
        {"subcuenca": "SB3", "pluviometro": "Z1"},
    ]

    def _escribir(self) -> str:
        destino = Path(tempfile.mkdtemp()) / "T10.met"
        m13.escribir_met(destino, "T10", "10", self.ASIGNACION,
                         lambda zona, periodo: f"{zona}_T{periodo}", "Basin 1")
        return destino.read_text(encoding="utf-8")

    def test_el_metodo_es_el_nombre_del_archivo_no_el_de_la_interfaz(self) -> None:
        texto = self._escribir()
        self.assertIn("     Precipitation Method: Specified Average\n", texto)
        self.assertNotIn("Specified Hyetograph", texto)

    def test_cada_subcuenca_lleva_su_pluviometro_dentro_del_bloque(self) -> None:
        # Es la línea que engancha la serie. Sin ella HEC-HMS abre el modelo
        # meteorológico vacío y la simulación aborta sin lluvia.
        texto = self._escribir()
        self.assertIn("Subbasin: SB1\n     Gage: Z1_T10\nEnd:\n", texto)
        self.assertIn("Subbasin: SB2\n     Gage: Z2_T10\nEnd:\n", texto)
        self.assertEqual(texto.count("\n     Gage: "), len(self.ASIGNACION))

    def test_no_se_listan_los_pluviometros_aparte(self) -> None:
        # Una versión previa los declaraba al principio del .met; HEC-HMS los
        # borró al guardar. Viven en el .gage.
        texto = self._escribir()
        self.assertNotIn("\nGage:", texto)
        self.assertNotIn("Type: Recording", texto)

    def test_declara_el_modelo_de_cuenca_y_todos_los_metodos(self) -> None:
        texto = self._escribir()
        self.assertIn("     Use Basin Model: Basin 1\n", texto)
        for metodo in ("Air Temperature", "Snowmelt", "Wind Speed"):
            self.assertIn(f"     {metodo} Method: None\n", texto)


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

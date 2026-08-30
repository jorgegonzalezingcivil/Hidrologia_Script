# -*- coding: utf-8 -*-
"""
Pruebas de migrar_estudio.py: poner al día la configuración de un estudio.

    python tests/test_migracion.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
if str(_RAIZ_REPO) not in sys.path:
    sys.path.insert(0, str(_RAIZ_REPO))

import migrar_estudio as mig  # noqa: E402

_PLANTILLA = """\
# Cabecera del archivo.
esquema_version: 2

estaciones:
  buffer_adicional_km: 5.0

  # Comentario que JUSTIFICA la clave nueva y debe viajar con ella.
  # Segunda linea de la justificacion.
  ampliacion:
    paso_km: 1.0
    tope_km: 15.0

  catalogo: "data/catalogo.shp"

dem:
  delimitacion:
    salida_area_influencia: "data/vector/area_influencia_preliminar.shp"
""".splitlines()


class PruebaLocalizacionDeClaves(unittest.TestCase):

    def test_encuentra_una_clave_anidada(self) -> None:
        rutas = {r for _, r, _ in mig.recorrer_claves(_PLANTILLA)}
        self.assertIn("estaciones.ampliacion.tope_km", rutas)
        self.assertIn("dem.delimitacion.salida_area_influencia", rutas)

    def test_el_bloque_arrastra_los_comentarios_de_encima(self) -> None:
        inicio, fin = mig.bloque_de_clave(_PLANTILLA, "estaciones.ampliacion")
        bloque = _PLANTILLA[inicio:fin]
        self.assertTrue(bloque[0].strip().startswith("#"))
        self.assertIn("    tope_km: 15.0", bloque)
        # No debe llevarse la clave siguiente.
        self.assertNotIn('  catalogo: "data/catalogo.shp"', bloque)

    def test_el_bloque_no_arrastra_el_comentario_de_la_siguiente(self) -> None:
        inicio, fin = mig.bloque_de_clave(
            _PLANTILLA, "estaciones.buffer_adicional_km")
        bloque = _PLANTILLA[inicio:fin]
        self.assertEqual([l for l in bloque if l.strip()],
                         ["  buffer_adicional_km: 5.0"])

    def test_una_clave_ausente_devuelve_nada(self) -> None:
        self.assertIsNone(mig.bloque_de_clave(_PLANTILLA, "no.existe"))


class PruebaValorEnLinea(unittest.TestCase):

    def test_quita_el_comentario_de_la_derecha(self) -> None:
        self.assertEqual(mig.valor_en_linea("  tope_km: 15.0  # por regimen"),
                         "15.0")

    def test_respeta_una_almohadilla_entre_comillas(self) -> None:
        # Cortar por el primer '#' romperia el valor sin avisar.
        self.assertEqual(mig.valor_en_linea('  nombre: "Sector #3"  # nota'),
                         "Sector #3")


class PruebaMigracionCompleta(unittest.TestCase):
    """La migración real declarada en config/migraciones.yaml."""

    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        (self.temporal / "config").mkdir()
        vector = self.temporal / "data" / "03_SIG" / "vector"
        vector.mkdir(parents=True)

        # Una configuracion de la version 1: sin esquema_version, con la ruta
        # antigua y sin el bloque de ampliacion.
        (self.temporal / "config" / "config.yaml").write_text(
            "proyecto:\n"
            '  nombre: "Prueba"\n'
            "\n"
            "estaciones:\n"
            "  buffer_adicional_km: 5.0\n"
            '  catalogo: "x.shp"\n'
            "\n"
            "hec_hms:\n"
            "  intercambio:\n"
            '    insumos: "a"\n'
            '    salida: "b"\n'
            "\n"
            "dem:\n"
            "  delimitacion:\n"
            '    salida_area_influencia: "data/03_SIG/vector/area_influencia.shp"\n',
            encoding="utf-8")
        for extension in (".shp", ".dbf", ".prj"):
            (vector / f"area_influencia{extension}").write_text("x")

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _config(self) -> str:
        return (self.temporal / "config" / "config.yaml").read_text(
            encoding="utf-8")

    def test_la_ausencia_de_version_significa_la_uno(self) -> None:
        self.assertEqual(mig.leer_version(self._config()), 1)

    def test_simular_no_escribe_nada(self) -> None:
        antes = self._config()
        resultado = mig.migrar(self.temporal, _RAIZ_REPO, simular=True)
        self.assertEqual(resultado.version_final, 2)
        self.assertEqual(self._config(), antes)
        vector = self.temporal / "data" / "03_SIG" / "vector"
        self.assertTrue((vector / "area_influencia.shp").is_file())

    def test_migra_claves_valor_y_archivos(self) -> None:
        resultado = mig.migrar(self.temporal, _RAIZ_REPO)
        self.assertEqual((resultado.version_inicial, resultado.version_final),
                         (1, 2))

        texto = self._config()
        self.assertIn("esquema_version: 2", texto)
        self.assertIn("area_influencia_preliminar.shp", texto)
        self.assertIn("ampliacion:", texto)
        self.assertIn("buffer_area_km", texto)

        vector = self.temporal / "data" / "03_SIG" / "vector"
        for extension in (".shp", ".dbf", ".prj"):
            self.assertTrue(
                (vector / f"area_influencia_preliminar{extension}").is_file())
            self.assertFalse((vector / f"area_influencia{extension}").is_file())

    def test_deja_respaldo_del_archivo_anterior(self) -> None:
        resultado = mig.migrar(self.temporal, _RAIZ_REPO)
        self.assertIsNotNone(resultado.respaldo)
        self.assertTrue(resultado.respaldo.is_file())
        self.assertIn("area_influencia.shp",
                      resultado.respaldo.read_text(encoding="utf-8"))

    def test_es_idempotente(self) -> None:
        mig.migrar(self.temporal, _RAIZ_REPO)
        primera = self._config()
        segunda_pasada = mig.migrar(self.temporal, _RAIZ_REPO)
        self.assertEqual(segunda_pasada.version_inicial, 2)
        self.assertEqual(segunda_pasada.version_final, 2)
        self.assertEqual(self._config(), primera)

    def test_no_pisa_un_valor_que_el_consultor_cambio(self) -> None:
        ruta = self.temporal / "config" / "config.yaml"
        ruta.write_text(
            ruta.read_text(encoding="utf-8").replace(
                "data/03_SIG/vector/area_influencia.shp",
                "data/03_SIG/vector/mi_area_propia.shp"),
            encoding="utf-8")

        resultado = mig.migrar(self.temporal, _RAIZ_REPO)
        omitidos = [c for c in resultado.requieren_decision
                    if c.clase == "revalorada"]
        self.assertEqual(len(omitidos), 1)
        self.assertIn("decisión propia", omitidos[0].motivo_omision)
        self.assertIn("mi_area_propia.shp", self._config())


if __name__ == "__main__":
    unittest.main(verbosity=2)

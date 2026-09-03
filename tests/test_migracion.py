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

# La versión a la que se debe llegar es la que declara la herramienta. Escribir
# aquí una cifra obliga a tocar la prueba cada vez que se añade una migración,
# y una prueba que hay que retocar en cada cambio deja de vigilar nada.
_OBJETIVO = mig.leer_version(
    (_RAIZ_REPO / "config" / "config.yaml").read_text(encoding="utf-8"))

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
            # Un estudio de la version 1 ya traia estos bloques: son
            # anteriores a la cadena de migraciones y ninguna receta los
            # introduce, de modo que tienen que estar aqui para que las que
            # cuelgan de ellos encuentren su ancla.
            "proyecto:\n"
            '  nombre: "Prueba"\n'
            '  responsable: ""\n'
            "\n"
            "caudal_ambiental:\n"
            '  metodo_adoptado: "qirh"\n'
            "\n"
            "estaciones:\n"
            "  buffer_adicional_km: 5.0\n"
            '  catalogo: "x.shp"\n'
            "\n"
            "tormenta:\n"
            "  duracion_h: 3.0\n"
            "\n"
            # Un estudio de la version 1 ya declaraba el factor de cambio
            # climatico: el bloque es anterior a la cadena de migraciones y
            # ninguna receta lo introduce, de modo que tiene que estar aqui
            # para que las que cuelgan de el encuentren su ancla.
            "cambio_climatico:\n"
            "  aplicar: true\n"
            "\n"
            "numero_curva:\n"
            '  condicion_humedad: "II"\n'
            "\n"
            "hec_hms:\n"
            "  intercambio:\n"
            '    insumos: "a"\n'
            '    salida: "b"\n'
            '  baseflow: "none"\n'
            "  proyecto:\n"
            '    modelo_cuenca: "Basin_1"\n'
            "  transito:\n"
            "    muskingum:\n"
            "      celeridad_ms: null\n"
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

    def test_una_receta_a_medias_no_sube_la_version(self) -> None:
        """
        El faltante tiene que seguir viendose en la siguiente corrida.

        Si la version subiera igual, el estudio diria que esta al dia sin
        tener la clave y nadie volveria a intentarlo: el modulo que la lea
        caeria en su valor por omision, en silencio.
        """
        ruta = self.temporal / "config" / "config.yaml"
        # Se quita el ancla que necesita la receta de la 3 a la 4.
        ruta.write_text(self._config().replace("  duracion_h: 3.0\n", ""),
                        encoding="utf-8")

        resultado = mig.migrar(self.temporal, _RAIZ_REPO)

        self.assertEqual(resultado.version_final, 3)
        self.assertIn("esquema_version: 3", self._config())
        self.assertTrue(resultado.requieren_decision)
        # Y la siguiente pasada vuelve a intentarlo, en lugar de darlo por hecho.
        self.assertEqual(mig.migrar(self.temporal, _RAIZ_REPO).version_inicial, 3)

    def test_una_clave_ya_presente_no_detiene_la_migracion(self) -> None:
        """
        Que ya este hecho no es un fallo.

        Un estudio que arranca en la version 1 recibe bloques enteros de la
        herramienta, y esos bloques ya traen claves que una receta POSTERIOR
        vuelve a pedir. Si eso cortara la migracion, ningun estudio antiguo
        llegaria nunca a la ultima version.
        """
        resultado = mig.migrar(self.temporal, _RAIZ_REPO)
        self.assertEqual(resultado.version_final, _OBJETIVO)
        ya_estaban = [c for c in resultado.cambios
                      if c.motivo_omision == "ya estaba presente"]
        self.assertTrue(ya_estaban, "el caso que se quiere vigilar no ocurrio")
        for cambio in ya_estaban:
            self.assertFalse(cambio.bloquea)
        self.assertEqual(resultado.requieren_decision, [])

    def test_la_ausencia_de_version_significa_la_uno(self) -> None:
        self.assertEqual(mig.leer_version(self._config()), 1)

    def test_simular_no_escribe_nada(self) -> None:
        antes = self._config()
        resultado = mig.migrar(self.temporal, _RAIZ_REPO, simular=True)
        self.assertEqual(resultado.version_final, _OBJETIVO)
        self.assertEqual(self._config(), antes)
        vector = self.temporal / "data" / "03_SIG" / "vector"
        self.assertTrue((vector / "area_influencia.shp").is_file())

    def test_migra_claves_valor_y_archivos(self) -> None:
        resultado = mig.migrar(self.temporal, _RAIZ_REPO)
        self.assertEqual((resultado.version_inicial, resultado.version_final),
                         (1, _OBJETIVO))

        texto = self._config()
        self.assertIn(f"esquema_version: {_OBJETIVO}", texto)
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
        self.assertEqual(segunda_pasada.version_inicial, _OBJETIVO)
        self.assertEqual(segunda_pasada.version_final, _OBJETIVO)
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

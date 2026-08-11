# -*- coding: utf-8 -*-
"""
Pruebas del orquestador de la cadena.

    python tests/test_cadena.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
for candidato in (str(_RAIZ_REPO), str(_DIRECTORIO_SRC)):
    if candidato not in sys.path:
        sys.path.insert(0, candidato)

import ejecutar_cadena as cadena  # noqa: E402
from comun.config import cargar, leer_yaml  # noqa: E402
from comun.errores import ErrorConfiguracion  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


class PruebaDeclaracionReal(unittest.TestCase):
    """
    La cadena es doctrina y vive en config/, no en el código.

    Estas pruebas la validan como dato: que sea legible, que no se contradiga
    con lo que hay en src/ y que los identificadores sirvan para --desde y
    --hasta.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.pasos = cadena.leer_cadena(_RAIZ_REPO)

    def test_se_lee_y_tiene_pasos(self) -> None:
        self.assertGreater(len(self.pasos), 15)

    def test_los_identificadores_no_se_repiten(self) -> None:
        # Se usan en --desde, --hasta y --solo: repetirlos haría ambiguo el
        # recorte de la cadena.
        claves = [p.modulo for p in self.pasos]
        self.assertEqual(len(claves), len(set(claves)))

    def test_todo_paso_disponible_tiene_su_script(self) -> None:
        faltan = [p.modulo for p in self.pasos
                  if p.disponible and not p.manual
                  and not (_RAIZ_REPO / p.script).is_file()]
        self.assertEqual(faltan, [],
                         "declarados disponibles pero sin archivo")

    def test_ningun_paso_pendiente_tiene_ya_su_script(self) -> None:
        # Si el módulo ya existe y la cadena lo declara pendiente, la cadena
        # se detendría antes de un módulo que sí se puede correr.
        sobran = [p.modulo for p in self.pasos
                  if not p.disponible and (_RAIZ_REPO / p.script).is_file()]
        self.assertEqual(sobran, [],
                         "programados pero declarados pendientes")

    def test_todo_modulo_de_src_esta_en_la_cadena(self) -> None:
        # Un módulo programado que nadie encadena no se ejecutaría nunca por
        # esta vía, y el olvido no daría ninguna señal.
        declarados = {(_RAIZ_REPO / p.script).name
                      for p in self.pasos if p.script}
        en_disco = {f.name for f in (_RAIZ_REPO / "src").glob("M*.py")}
        self.assertEqual(en_disco - declarados, set())

    def test_los_entornos_son_los_declarados_en_claude_md(self) -> None:
        for paso in self.pasos:
            with self.subTest(modulo=paso.modulo):
                self.assertIn(paso.entorno, ("venv", "qgis", "manual"))

    def test_los_modulos_sig_corren_con_qgis(self) -> None:
        # CLAUDE.md, sección 3: los módulos SIG no importan librerías del venv
        # y al revés. Lanzarlos con el intérprete equivocado da un ImportError
        # que no explica nada, y es el error más fácil de cometer a mano.
        for paso in self.pasos:
            if not paso.script or not (_RAIZ_REPO / paso.script).is_file():
                continue
            texto = (_RAIZ_REPO / paso.script).read_text(encoding="utf-8")
            usa_qgis = "from qgis.core import" in texto or "import qgis" in texto
            with self.subTest(modulo=paso.modulo):
                self.assertEqual(usa_qgis, paso.entorno == "qgis")

    def test_el_paso_manual_es_unico_y_esta_declarado(self) -> None:
        # CLAUDE.md, sección 4: la delimitación asistida de HEC-HMS es el
        # único paso con intervención manual obligatoria.
        manuales = [p for p in self.pasos if p.manual]
        self.assertEqual(len(manuales), 1)
        self.assertGreater(len(manuales[0].nombre_largo.strip()), 60,
                           "un paso manual sin instrucciones no sirve")

    def test_la_segunda_fase_del_m02_va_despues_de_la_red(self) -> None:
        # El área definitiva necesita la red, y la red necesita el DEM. Si el
        # orden se invirtiera, el M02 no encontraría la red y conservaría la
        # subzona sin que nadie se enterara.
        claves = [p.modulo for p in self.pasos]
        self.assertLess(claves.index("M02a"), claves.index("M02b"))
        self.assertLess(claves.index("M02b"), claves.index("M02c"))

    def test_el_modo_general_no_pasa_por_hec_hms(self) -> None:
        for paso in self.pasos:
            if paso.modulo in ("M09a", "M09b", "HEC", "M13", "M14", "M14b"):
                with self.subTest(modulo=paso.modulo):
                    self.assertFalse(paso.aplica_a("general"))
                    self.assertTrue(paso.aplica_a("detallado"))


class PruebaAcotado(unittest.TestCase):
    def setUp(self) -> None:
        self.pasos = cadena.leer_cadena(_RAIZ_REPO)

    def test_desde_recorta_por_delante(self) -> None:
        recortada = cadena.acotar(self.pasos, "M03", "", "")
        self.assertEqual(recortada[0].modulo, "M03")
        self.assertEqual(recortada[-1].modulo, self.pasos[-1].modulo)

    def test_hasta_recorta_por_detras(self) -> None:
        recortada = cadena.acotar(self.pasos, "", "M03", "")
        self.assertEqual(recortada[0].modulo, self.pasos[0].modulo)
        self.assertEqual(recortada[-1].modulo, "M03")

    def test_solo_toma_los_pedidos_en_el_orden_de_la_cadena(self) -> None:
        recortada = cadena.acotar(self.pasos, "", "", "M07,M03")
        self.assertEqual([p.modulo for p in recortada], ["M03", "M07"])

    def test_un_modulo_inexistente_se_declara(self) -> None:
        with self.assertRaises(ErrorConfiguracion) as contexto:
            cadena.acotar(self.pasos, "M99", "", "")
        self.assertIn("M99", str(contexto.exception))

    def test_un_tramo_invertido_se_rechaza(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            cadena.acotar(self.pasos, "M07", "M03", "")


class PruebaDeclaracionMalFormada(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        (self.temporal / "config").mkdir()
        (self.temporal / "config" / "config.yaml").write_text(
            "proyecto:\n  nombre: x\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _escribir(self, contenido: str):
        (self.temporal / "config" / "cadena.yaml").write_text(
            contenido, encoding="utf-8")
        return cadena.leer_cadena(self.temporal)

    def test_sin_pasos_es_error(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            self._escribir("pasos: []\n")

    def test_un_paso_sin_modulo_es_error(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            self._escribir("pasos:\n  - nombre: sin identificador\n")

    def test_un_identificador_repetido_es_error(self) -> None:
        with self.assertRaises(ErrorConfiguracion) as contexto:
            self._escribir(
                "pasos:\n"
                "  - modulo: M00\n    script: a.py\n"
                "  - modulo: M00\n    script: b.py\n")
        self.assertIn("dos veces", str(contexto.exception))

    def test_un_entorno_desconocido_es_error(self) -> None:
        with self.assertRaises(ErrorConfiguracion) as contexto:
            self._escribir(
                "pasos:\n  - modulo: M00\n    script: a.py\n"
                "    entorno: conda\n")
        self.assertIn("conda", str(contexto.exception))

    def test_disponible_sin_script_es_error(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            self._escribir("pasos:\n  - modulo: M00\n    estado: disponible\n")

    def test_pendiente_sin_script_se_admite(self) -> None:
        pasos = self._escribir(
            "pasos:\n  - modulo: M99\n    estado: pendiente\n")
        self.assertEqual(len(pasos), 1)
        self.assertFalse(pasos[0].disponible)


class PruebaFiltroPorModo(unittest.TestCase):
    def test_sin_modos_declarados_aplica_a_todos(self) -> None:
        paso = cadena.Paso(modulo="M10")
        self.assertTrue(paso.aplica_a("general"))
        self.assertTrue(paso.aplica_a("detallado"))

    def test_con_modos_declarados_filtra(self) -> None:
        paso = cadena.Paso(modulo="M13", modos=["detallado"])
        self.assertFalse(paso.aplica_a("general"))
        self.assertTrue(paso.aplica_a("detallado"))


class PruebaInterpretes(unittest.TestCase):
    def test_el_venv_se_resuelve_contra_el_codigo(self) -> None:
        # Un estudio no tiene entorno virtual propio: el intérprete es de la
        # instalación. Resolverlo contra el estudio daría una ruta inexistente.
        temporal = Path(tempfile.mkdtemp())
        try:
            encontrados = cadena.interpretes(_CFG, temporal)
            self.assertTrue(
                encontrados["venv"].is_relative_to(_RAIZ_REPO),
                str(encontrados["venv"]))
        finally:
            shutil.rmtree(temporal, ignore_errors=True)

    def test_una_ruta_absoluta_se_respeta(self) -> None:
        encontrados = cadena.interpretes(_CFG, _RAIZ_REPO)
        self.assertTrue(encontrados["qgis"].is_absolute())


if __name__ == "__main__":
    unittest.main(verbosity=2)

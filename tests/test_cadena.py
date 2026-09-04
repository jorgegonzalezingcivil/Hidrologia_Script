# -*- coding: utf-8 -*-
"""
Pruebas del orquestador de la cadena.

    python tests/test_cadena.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import json
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


class PruebaEmpaquetadoDeEntrega(unittest.TestCase):
    """
    El comprimido de entrega se verifica antes de armarse.

    UN .ZIP LO HACE EL EXPLORADOR DE ARCHIVOS. Lo que este paso aporta es
    comprobar que el entregable esta completo antes de cerrarlo: un informe se
    escribe igual con una tabla sin llenar o con prosa de otro estudio, y el
    archivo esta ahi y pesa lo mismo.
    """

    def setUp(self) -> None:
        import importlib.util

        ruta = _RAIZ_REPO / "tools" / "empaquetar_entrega.py"
        if not ruta.is_file():
            self.skipTest("no esta la herramienta de empaquetado")
        especificacion = importlib.util.spec_from_file_location(
            "empaquetar_entrega", ruta)
        self.modulo = importlib.util.module_from_spec(especificacion)
        especificacion.loader.exec_module(self.modulo)
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_sin_informe_no_se_arma(self) -> None:
        faltan = self.modulo.verificar(
            self.temporal, self.temporal / "no_existe.docx",
            self.temporal / "anexos")
        self.assertTrue(any("informe" in m for m in faltan))

    def test_sin_acta_de_entrega_no_se_arma(self) -> None:
        """
        El acta lista la huella de cada anexo.

        Sin ella no hay forma de comprobar meses despues que el anexo
        entregado es el que el estudio produjo, que es justo lo que una
        revision reclama.
        """
        informe = self.temporal / "informe.docx"
        informe.write_text("x", encoding="utf-8")
        anexos = self.temporal / "anexos"
        anexos.mkdir()
        faltan = self.modulo.verificar(self.temporal, informe, anexos)
        self.assertTrue(any("ACTA_DE_ENTREGA" in m for m in faltan))

    def test_un_bloqueante_del_informe_detiene_la_entrega(self) -> None:
        # El archivo existe y pesa lo mismo con una tabla sin llenar: lo que
        # dice si esta completo es el reporte del modulo.
        informe = self.temporal / "informe.docx"
        informe.write_text("x", encoding="utf-8")
        anexos = self.temporal / "anexos"
        anexos.mkdir()
        (anexos / "ACTA_DE_ENTREGA.md").write_text("x", encoding="utf-8")
        procesado = self.temporal / "data" / "02_procesado"
        procesado.mkdir(parents=True)
        for nombre in ("M15_informe", "M17_anexos"):
            (procesado / f"{nombre}.json").write_text(
                json.dumps({"hallazgos": [
                    {"severidad": "BLOQUEANTE", "clave": "informe.prueba"}]}),
                encoding="utf-8")
        faltan = self.modulo.verificar(self.temporal, informe, anexos)
        self.assertTrue(any("bloqueante" in m for m in faltan))


class PruebaDescargaOptativa(unittest.TestCase):
    """
    La ingesta del IDEAM se hace UNA VEZ, no en cada corrida.

    POR QUE. La descarga NO ES IDEMPOTENTE: un registro hoy Preliminar puede
    ser Definitivo manana, de modo que repetirla en cada pasada cambiaria la
    serie bajo un informe ya redactado. Y ademas costaba tiempo sin traer nada:
    con el estudio ya descargado, una pasada tardaba cuarenta y tres minutos
    preguntando por series que no existen y saltando las que ya estaban.
    """

    def _pasos(self):
        import yaml

        declaracion = _RAIZ_REPO / "config" / "cadena.yaml"
        if not declaracion.is_file():
            self.skipTest("no esta la declaracion de la cadena")
        with declaracion.open(encoding="utf-8") as manejador:
            return (yaml.safe_load(manejador) or {}).get("pasos") or []

    def test_ningun_paso_descarga_de_forma_ordinaria(self) -> None:
        # '--descargar' no puede estar en 'argumentos', que se pasan siempre.
        for paso in self._pasos():
            with self.subTest(modulo=paso.get("modulo")):
                self.assertNotIn(
                    "--descargar",
                    [str(a) for a in (paso.get("argumentos") or ())])

    def test_la_ingesta_declara_su_argumento_de_descarga(self) -> None:
        # Y tiene que seguir pudiendo descargar cuando se pide.
        ingesta = next((p for p in self._pasos()
                        if str(p.get("modulo")) == "M04"), None)
        self.assertIsNotNone(ingesta, "no esta declarado el paso M04")
        self.assertIn("--descargar",
                      [str(a) for a in
                       (ingesta.get("argumentos_de_descarga") or ())])


class PruebaBanderaSilenciosa(unittest.TestCase):
    """
    Todo modulo de la cadena tiene que admitir '--silencioso'.

    ES EL FALLO QUE HUBO. El corredor anade esa bandera al comando de CADA
    modulo, y doce de ellos no la declaraban: argparse abortaba con codigo 2
    antes de escribir una linea de log, y la cadena no podia pasar del M11.
    Nadie lo habia notado porque el estudio siempre se corrio modulo a modulo.

    La prueba lee la declaracion y no una lista escrita aqui: un modulo nuevo
    queda cubierto sin tocarla.
    """

    def test_cada_script_declarado_la_admite(self) -> None:
        import yaml

        declaracion = _RAIZ_REPO / "config" / "cadena.yaml"
        if not declaracion.is_file():
            self.skipTest("no esta la declaracion de la cadena")
        with declaracion.open(encoding="utf-8") as manejador:
            datos = yaml.safe_load(manejador) or {}
        pasos = [p for p in datos.get("pasos") or []
                 if str(p.get("estado", "")) == "disponible"
                 and str(p.get("script", "")).strip()]
        self.assertTrue(pasos, "la declaracion no trae pasos disponibles")
        for paso in pasos:
            ruta = _RAIZ_REPO / str(paso["script"])
            with self.subTest(modulo=paso.get("modulo")):
                if not ruta.is_file():
                    continue
                self.assertIn('"--silencioso"',
                              ruta.read_text(encoding="utf-8"),
                              f"{ruta.name} no declara --silencioso y la cadena "
                              "se lo pasa: argparse aborta con codigo 2")



if __name__ == "__main__":
    unittest.main(verbosity=2)

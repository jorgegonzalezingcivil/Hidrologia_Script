# -*- coding: utf-8 -*-
"""
Pruebas del M15: resolución de las instrucciones de la plantilla.

Se prueban las funciones puras y la declaración real del repositorio. La
edición del documento exige python-docx y una plantilla de 3,7 MB, de modo que
se verifica sobre la del estudio en las pruebas de integración y aquí solo lo
que decide qué se hace con cada instrucción.

    python tests/test_m15.py
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

import M15_informe as m15  # noqa: E402
from comun.errores import ErrorRutas  # noqa: E402


class PruebaClasificacion(unittest.TestCase):
    """
    SE DECIDE POR EL TEXTO Y NO POR EL COLOR. El sombreado se pierde al copiar y
    pegar un párrafo, y entonces la instrucción quedaría muda sin que nada lo
    señalara.
    """

    def test_reconoce_una_figura_y_extrae_su_archivo(self) -> None:
        tipo, argumento = m15.clasificar("Colocar Figura: M10_mapa_cn.png")
        self.assertEqual(tipo, "figura")
        self.assertEqual(argumento, "M10_mapa_cn.png")

    def test_extrae_el_archivo_aunque_la_instruccion_diga_la_carpeta(self) -> None:
        # La plantilla a veces añade 'de la carpeta individuales/isoyetas_fase'.
        tipo, argumento = m15.clasificar(
            "Colocar Figura: compuesto.png de la carpeta individuales/isoyetas_fase")
        self.assertEqual(tipo, "figura")
        self.assertEqual(argumento, "compuesto.png")

    def test_reconoce_una_tabla_y_su_numero(self) -> None:
        tipo, argumento = m15.clasificar(
            "Completar la Tabla 2-1 con datos según estaciones identificadas")
        self.assertEqual(tipo, "tabla")
        self.assertEqual(m15._normalizar_numero(argumento), "21")

    def test_reconoce_el_analisis_y_no_lo_resuelve(self) -> None:
        tipo, _argumento = m15.clasificar("Analizar Ilustración 3-5 y 3-6.")
        self.assertEqual(tipo, "analisis")

    def test_un_parrafo_corriente_no_es_instruccion(self) -> None:
        for texto in ("En la Tabla 2-1 se presentan las características",
                      "Fuente: IDEAM, 2024.", "", "   ",
                      "Ilustración 22. Localización Estaciones"):
            self.assertEqual(m15.clasificar(texto)[0], "")

    def test_no_distingue_mayusculas(self) -> None:
        self.assertEqual(m15.clasificar("colocar figura: x.png")[0], "figura")
        self.assertEqual(m15.clasificar("COMPLETAR LA TABLA 3-1")[0], "tabla")


class PruebaNumeroDeTabla(unittest.TestCase):
    """
    Word compone la leyenda con campos y el guion se pierde al extraer su texto,
    o aparece como guion corto, largo o de no separación según cómo se escribió.
    """

    def test_los_tres_guiones_dan_el_mismo_numero(self) -> None:
        self.assertEqual(m15._normalizar_numero("2-1"), "21")
        self.assertEqual(m15._normalizar_numero("2‑1"), "21")
        self.assertEqual(m15._normalizar_numero("2—1"), "21")
        self.assertEqual(m15._normalizar_numero("21"), "21")

    def test_distingue_numeros_distintos(self) -> None:
        # 3-1 y 3-10 no pueden colapsar en el mismo, o una tabla se llenaria
        # con los datos de la otra.
        self.assertNotEqual(m15._normalizar_numero("3-1"),
                            m15._normalizar_numero("3-10"))


class PruebaBusquedaDeFiguras(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "individuales" / "enso").mkdir(parents=True)
        (self.tmp / "M10_mapa_cn.png").write_bytes(b"x")
        (self.tmp / "individuales" / "enso" / "guasca.png").write_bytes(b"x")

    def test_encuentra_en_la_raiz(self) -> None:
        self.assertIsNotNone(m15.buscar_figura("M10_mapa_cn.png", [self.tmp]))

    def test_encuentra_en_una_subcarpeta(self) -> None:
        # El consultor no tiene por que saber en que tema la dejo cada modulo.
        encontrada = m15.buscar_figura("guasca.png", [self.tmp])
        self.assertIsNotNone(encontrada)
        self.assertEqual(encontrada.name, "guasca.png")

    def test_lo_que_no_existe_devuelve_nada(self) -> None:
        self.assertIsNone(m15.buscar_figura("no_existe.png", [self.tmp]))

    def test_una_raiz_ausente_no_es_error(self) -> None:
        self.assertIsNone(
            m15.buscar_figura("x.png", [self.tmp / "no_esta"]))


class PruebaDeclaracionDeTablas(unittest.TestCase):
    """La declaración real del repositorio, no una inventada para la prueba."""

    def setUp(self) -> None:
        self.declaracion = m15.leer_declaracion_tablas(
            _RAIZ_REPO / "config" / "informe_tablas.yaml")

    def test_la_declaracion_del_repositorio_se_lee(self) -> None:
        self.assertTrue(self.declaracion)

    def test_toda_tabla_declara_fuente_y_columnas(self) -> None:
        for numero, entrada in self.declaracion.items():
            self.assertTrue(entrada.get("fuente"), numero)
            self.assertTrue(entrada.get("columnas"), numero)

    def test_los_numeros_no_colisionan(self) -> None:
        # Dos tablas con el mismo numero normalizado harian que una se llenara
        # con los datos de la otra sin ninguna senal.
        crudos = [str(e.get("numero")) for e in self.declaracion.values()]
        self.assertEqual(len(crudos), len(set(crudos)))

    def test_los_encabezados_son_al_menos_uno(self) -> None:
        # Con cero, la primera fila de titulos se sobreescribiria con datos.
        for numero, entrada in self.declaracion.items():
            self.assertGreaterEqual(int(entrada.get("encabezados", 1)), 1,
                                    numero)

    def test_una_declaracion_ausente_no_es_error(self) -> None:
        # Significa que aun no se declaro ninguna tabla, no que falte algo.
        self.assertEqual(
            m15.leer_declaracion_tablas(Path("no_existe.yaml")), {})


class PruebaLecturaDeTabla(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ruta = self.tmp / "datos.csv"
        self.ruta.write_text("a;b\n1;2\n3;4\n", encoding="utf-8-sig")

    def test_lee_las_filas(self) -> None:
        filas = m15.leer_tabla_csv(self.ruta, ";")
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[0]["a"], "1")

    def test_una_tabla_ausente_es_error(self) -> None:
        # Se detiene en lugar de dejar la tabla del estudio anterior en pie.
        with self.assertRaises(ErrorRutas):
            m15.leer_tabla_csv(self.tmp / "no_existe.csv", ";")


if __name__ == "__main__":
    unittest.main(verbosity=2)

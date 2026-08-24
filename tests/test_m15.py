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

    def test_toda_tabla_declara_fuente_y_como_se_llena(self) -> None:
        # 'columnas' para una lista, 'matriz' para una matriz, y una de las dos
        # siempre: sin ninguna, la tabla se quedaria con lo que la plantilla
        # traia y el modulo diria que la lleno.
        for clave, entrada in self.declaracion.items():
            self.assertTrue(entrada.get("fuente"), clave)
            self.assertTrue(entrada.get("columnas") or entrada.get("matriz"),
                            clave)

    def test_se_indexa_por_leyenda_y_no_por_numero(self) -> None:
        # El numero de la instruccion es el del informe del que se copio el
        # apartado: en esta plantilla hay dos que dicen 5-1 delante de tablas
        # distintas. La clave tiene que ser la leyenda.
        for clave, entrada in self.declaracion.items():
            self.assertEqual(clave,
                             m15._normalizar_leyenda(str(entrada["leyenda"])))

    def test_las_leyendas_no_colisionan(self) -> None:
        # Dos tablas con la misma leyenda normalizada harian que una se llenara
        # con los datos de la otra sin ninguna senal.
        leyendas = [str(e.get("leyenda")) for e in self.declaracion.values()]
        self.assertEqual(len(leyendas),
                         len({m15._normalizar_leyenda(l) for l in leyendas}))

    def test_toda_leyenda_declarada_existe_en_la_plantilla(self) -> None:
        # Una leyenda mal escrita no emparejaria con nada y la tabla quedaria
        # sin llenar, que es justo lo que no se nota al revisar el informe.
        plantilla = _RAIZ_REPO / "templates" / "informe_base.docx"
        if not plantilla.is_file():
            self.skipTest("la plantilla saneada no esta en el repositorio")
        import docx_plantilla as dp
        documento = dp.abrir(plantilla)
        presentes = {m15._normalizar_leyenda(p.text)
                     for p in documento.paragraphs if p.text.strip()}
        for clave in self.declaracion:
            self.assertIn(clave, presentes)

    def test_los_encabezados_son_al_menos_uno(self) -> None:
        # Con cero, la primera fila de titulos se sobreescribiria con datos.
        for clave, entrada in self.declaracion.items():
            self.assertGreaterEqual(int(entrada.get("encabezados", 1)), 1,
                                    clave)

    def test_una_declaracion_ausente_no_es_error(self) -> None:
        # Significa que aun no se declaro ninguna tabla, no que falte algo.
        self.assertEqual(
            m15.leer_declaracion_tablas(Path("no_existe.yaml")), {})


class PruebaNormalizacionDeLeyenda(unittest.TestCase):
    """
    Las leyendas de la plantilla dicen 'Tabla -.' porque sus campos SEQ no
    tienen resultado en cache. El prefijo es justamente lo que no se compara.
    """

    def test_el_prefijo_numerado_no_cuenta(self) -> None:
        esperado = m15._normalizar_leyenda("Perímetro microcuencas")
        for texto in ("Tabla -. Perímetro microcuencas",
                      "Tabla 3-2. Perímetro microcuencas",
                      "Tabla 5-10. Perímetro microcuencas"):
            self.assertEqual(m15._normalizar_leyenda(texto), esperado)

    def test_las_tildes_y_las_mayusculas_no_cuentan(self) -> None:
        # La declaracion la escribe el consultor a mano.
        self.assertEqual(m15._normalizar_leyenda("Área  microcuencas"),
                         m15._normalizar_leyenda("area microcuencas"))

    def test_distingue_tablas_distintas(self) -> None:
        self.assertNotEqual(
            m15._normalizar_leyenda("Tabla -. Coeficiente de forma microcuencas"),
            m15._normalizar_leyenda("Tabla -. Coeficiente de compacidad microcuencas"))

class PruebaCorrecciones(unittest.TestCase):
    """
    La plantilla pide figuras equivocadas y el original no se toca: la
    correccion se declara, y tiene que emparejar con UNA instruccion.
    """

    def setUp(self) -> None:
        self.plantilla = _RAIZ_REPO / "templates" / "informe_base.docx"
        if not self.plantilla.is_file():
            self.skipTest("la plantilla saneada no esta en el repositorio")
        import docx_plantilla as dp
        self.documento = dp.abrir(self.plantilla)
        self.correcciones = m15.leer_correcciones(
            _RAIZ_REPO / "config" / "informe_correcciones.yaml")

    def test_la_declaracion_del_repositorio_se_lee(self) -> None:
        self.assertTrue(self.correcciones)

    def test_toda_correccion_declara_archivo_y_motivo(self) -> None:
        for clave, entrada in self.correcciones.items():
            self.assertTrue(entrada.get("archivo"), clave)
            self.assertTrue(entrada.get("motivo"), clave)

    def test_ninguna_correccion_queda_ambigua_ni_sin_uso(self) -> None:
        # Una que empareja con varias sustituiria figuras que estaban bien; una
        # que no empareja con ninguna no hace nada y nadie lo nota.
        _plan, ambiguas, sin_uso = m15.planear_correcciones(
            self.documento, self.correcciones)
        self.assertEqual(ambiguas, [])
        self.assertEqual(sin_uso, [])

    def test_cada_correccion_apunta_a_una_sola_instruccion(self) -> None:
        plan, _a, _s = m15.planear_correcciones(
            self.documento, self.correcciones)
        self.assertEqual(len(plan), len(self.correcciones))

    def test_sin_desempate_la_leyenda_repetida_es_ambigua(self) -> None:
        # 'Areas microcuencas' encabeza tres instrucciones de esta plantilla.
        suelta = {m15._normalizar_leyenda("Áreas microcuencas"): {
            "leyenda": "Áreas microcuencas", "archivo": "x.png"}}
        plan, ambiguas, _s = m15.planear_correcciones(self.documento, suelta)
        self.assertEqual(plan, {})
        self.assertEqual(len(ambiguas), 1)

    def test_una_declaracion_ausente_no_es_error(self) -> None:
        self.assertEqual(m15.leer_correcciones(Path("no_existe.yaml")), {})


class PruebaFormatoDeCifras(unittest.TestCase):
    """
    El consultor pidio dos decimales en toda cifra del informe. La regla no
    puede alcanzar a lo que no es una medida.
    """

    def test_una_medida_queda_con_dos_decimales(self) -> None:
        self.assertEqual(m15.formatear_numero("45.2", 2), "45.20")
        self.assertEqual(m15.formatear_numero("117.2345", 2), "117.23")

    def test_un_anio_o_un_codigo_no_se_tocan(self) -> None:
        # Redondear un entero lo convertiria en '1983.00' y en
        # '21201230.00', que no son ni un anio ni un codigo de estacion.
        for texto in ("1983", "21201230", "20011115", "12"):
            self.assertEqual(m15.formatear_numero(texto, 2), texto)

    def test_lo_que_no_es_numero_pasa_intacto(self) -> None:
        for texto in ("lognormal2", "CO", "", "Q. NN", "2.33 anios"):
            self.assertEqual(m15.formatear_numero(texto, 2), texto)

    def test_una_cifra_pequena_no_se_convierte_en_cero(self) -> None:
        # Un caudal de 0.0004 m3/s es un dato; '0.00' es una perdida muda.
        self.assertEqual(m15.formatear_numero("0.0004", 2), "0.0004")
        self.assertEqual(m15.formatear_numero("0.0", 2), "0.00")


class PruebaSustitucionDeNombres(unittest.TestCase):
    """El catalogo entrega la razon social; el informe usa la sigla."""

    def setUp(self) -> None:
        self.reglas = m15.leer_sustituciones(
            _RAIZ_REPO / "config" / "informe_correcciones.yaml")

    def test_la_declaracion_del_repositorio_se_lee(self) -> None:
        self.assertTrue(self.reglas)

    def test_toda_regla_declara_que_busca_y_por_que(self) -> None:
        for regla in self.reglas:
            self.assertTrue(regla.get("busca"))
            self.assertTrue(regla.get("pone"))
            self.assertTrue(regla.get("motivo"))

    def test_el_nombre_largo_del_ideam_queda_en_sigla(self) -> None:
        largo = "INSTITUTO DE HIDROLOGÍA METEOROLOGÍA Y ESTUDIOS AMBIENTALES"
        self.assertEqual(m15.aplicar_sustituciones(largo, self.reglas), "IDEAM")

    def test_lo_que_no_coincide_pasa_intacto(self) -> None:
        self.assertEqual(
            m15.aplicar_sustituciones("EAAB", self.reglas), "EAAB")

    def test_sin_reglas_no_cambia_nada(self) -> None:
        self.assertEqual(m15.aplicar_sustituciones("IDEAM", []), "IDEAM")


class PruebaAnchoDeclarado(unittest.TestCase):
    """
    EL ANCHO DECLARADO TIENE QUE SER EL DE LA TABLA. Si sobran columnas en la
    tabla, se quedan con lo que la plantilla traia y el informe mezcla dos
    estudios sin que nada lo advierta.
    """

    def setUp(self) -> None:
        plantilla = _RAIZ_REPO / "templates" / "informe_base.docx"
        if not plantilla.is_file():
            self.skipTest("la plantilla saneada no esta en el repositorio")
        import docx_plantilla as dp
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        documento = dp.abrir(plantilla)
        elementos = []
        for hijo in documento.element.body.iterchildren():
            if hijo.tag.endswith("}p"):
                elementos.append(("p", Paragraph(hijo, documento)))
            elif hijo.tag.endswith("}tbl"):
                elementos.append(("t", Table(hijo, documento)))
        self.tablas = {}
        for i, (clase, obj) in enumerate(elementos):
            if clase != "p" or m15.clasificar(obj.text)[0] != "tabla":
                continue
            leyenda, tabla = "", None
            for j in range(i + 1, min(len(elementos), i + 8)):
                if elementos[j][0] == "t":
                    tabla = elementos[j][1]
                    break
                texto = elementos[j][1].text.strip()
                if texto and not leyenda:
                    leyenda = texto
            if tabla is not None:
                self.tablas[m15._normalizar_leyenda(leyenda)] = tabla
        self.declaracion = m15.leer_declaracion_tablas(
            _RAIZ_REPO / "config" / "informe_tablas.yaml")

    def test_cada_declaracion_cuadra_con_su_tabla(self) -> None:
        for clave, entrada in self.declaracion.items():
            tabla = self.tablas.get(clave)
            self.assertIsNotNone(tabla, clave)
            matriz = entrada.get("matriz")
            if matriz:
                ancho = 1 + len(matriz.get("orden") or [])
            else:
                ancho = len(entrada.get("columnas") or [])
            self.assertEqual(ancho, len(tabla.columns), clave)

    def test_los_encabezados_dejan_al_menos_una_fila_de_datos(self) -> None:
        for clave, entrada in self.declaracion.items():
            tabla = self.tablas[clave]
            self.assertGreater(len(tabla.rows),
                               int(entrada.get("encabezados", 1)), clave)


class PruebaDeclaracionDeMatrices(unittest.TestCase):
    """Doce tablas del informe son matrices y no listas."""

    def setUp(self) -> None:
        self.declaracion = m15.leer_declaracion_tablas(
            _RAIZ_REPO / "config" / "informe_tablas.yaml")
        self.matrices = {c: e for c, e in self.declaracion.items()
                         if e.get("matriz")}

    def test_hay_matrices_declaradas(self) -> None:
        self.assertTrue(self.matrices)

    def test_toda_matriz_declara_sus_tres_campos_y_el_orden(self) -> None:
        for clave, entrada in self.matrices.items():
            matriz = entrada["matriz"]
            for campo in ("fila", "columna", "valor"):
                self.assertTrue(matriz.get(campo), f"{clave}: {campo}")
            self.assertTrue(matriz.get("orden"), clave)

    def test_el_orden_no_repite_valores(self) -> None:
        # Una columna repetida escribiria el mismo dato dos veces y dejaria
        # otra sin escribir.
        for clave, entrada in self.matrices.items():
            orden = [str(v) for v in entrada["matriz"]["orden"]]
            self.assertEqual(len(orden), len(set(orden)), clave)

    def test_una_matriz_no_declara_tambien_columnas(self) -> None:
        # Declarar las dos cosas deja en duda cual manda.
        for clave, entrada in self.matrices.items():
            self.assertFalse(entrada.get("columnas"), clave)


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

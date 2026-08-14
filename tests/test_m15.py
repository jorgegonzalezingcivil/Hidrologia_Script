# -*- coding: utf-8 -*-
"""
Pruebas del M15: redacción del informe en Word.

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

import docx_plantilla  # noqa: E402
import M15_informe as m15  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)


class PruebaSustitucion(unittest.TestCase):
    """
    Un valor que la cadena no resolvió NO se inventa. Un hueco señalado se ve al
    hojear el documento; una cifra plausible y falsa no la detecta nadie.
    """

    def test_rellena_lo_que_hay(self) -> None:
        texto, faltan = m15.sustituir(
            "El area es de {area_km2} km2.", {"area_km2": "220,31"})
        self.assertEqual(texto, "El area es de 220,31 km2.")
        self.assertEqual(faltan, [])

    def test_marca_lo_que_falta_y_lo_reporta(self) -> None:
        texto, faltan = m15.sustituir("Area {area_km2}, cota {cota_max}.",
                                      {"area_km2": "220,31"})
        self.assertIn(m15.MARCA_SIN_VALOR, texto)
        self.assertNotIn("{cota_max}", texto)
        self.assertEqual(faltan, ["cota_max"])

    def test_un_valor_vacio_cuenta_como_ausente(self) -> None:
        # Una cadena vacía dejaría la frase coja sin que nada lo señalara.
        _, faltan = m15.sustituir("Area {area_km2}.", {"area_km2": ""})
        self.assertEqual(faltan, ["area_km2"])

    def test_una_llave_sin_cerrar_no_rompe_el_texto(self) -> None:
        texto, faltan = m15.sustituir("Area {area_km2 sin cerrar", {})
        self.assertEqual(texto, "Area {area_km2 sin cerrar")
        self.assertEqual(faltan, [])

    def test_un_texto_sin_llaves_pasa_intacto(self) -> None:
        texto, faltan = m15.sustituir("Sin valores.", {"area_km2": "1"})
        self.assertEqual(texto, "Sin valores.")
        self.assertEqual(faltan, [])


class PruebaFormato(unittest.TestCase):
    """
    En un documento en español el separador decimal es la coma. Escribir 220.60
    obliga al lector a decidir si son doscientos veinte o veintidós mil.
    """

    def test_usa_coma_decimal(self) -> None:
        self.assertEqual(m15.formatear(220.31), "220,31")

    def test_usa_punto_para_los_miles(self) -> None:
        self.assertEqual(m15.formatear(3687.0), "3.687")

    def test_respeta_los_decimales_pedidos(self) -> None:
        self.assertEqual(m15.formatear(0.5946, 3), "0,595")

    def test_un_texto_no_se_toca(self) -> None:
        self.assertEqual(m15.formatear("gumbel_max"), "gumbel_max")

    def test_un_vacio_no_produce_cero(self) -> None:
        # Un cero afirmaría un valor; el vacío deja ver que no lo hay.
        self.assertEqual(m15.formatear(None), "")
        self.assertEqual(m15.formatear(""), "")


class PruebaTabla(unittest.TestCase):
    CABECERA = "elemento;tipo;q_T100_m3s;area_km2\n"

    def _escribir(self, filas: str) -> Path:
        ruta = Path(tempfile.mkdtemp()) / "tabla.csv"
        ruta.write_text(self.CABECERA + filas, encoding="utf-8")
        return ruta

    FILAS = ("Sink-1;Sink;184.5;220.31\nSB1;Subbasin;1.2;1.5\n"
             "R1;Reach;90.0;50.0\n")

    def test_solo_salen_las_columnas_declaradas_y_con_su_nombre(self) -> None:
        # Las tablas de la cadena llevan nombres de trabajo; un informe con
        # 'q_T100_m3s' de encabezado obliga a descifrarlo.
        datos = m15.leer_tabla(self._escribir(self.FILAS), ";",
                               {"elemento": "Elemento", "q_T100_m3s": "T = 100 años"})
        self.assertEqual(datos["encabezados"], ["Elemento", "T = 100 años"])
        self.assertEqual(len(datos["filas"][0]), 2)

    def test_el_filtro_deja_solo_lo_pedido(self) -> None:
        datos = m15.leer_tabla(self._escribir(self.FILAS), ";",
                               {"elemento": "Elemento"},
                               filtro={"columna": "tipo", "valor": "Sink"})
        self.assertEqual(datos["filas"], [["Sink-1"]])

    def test_los_numeros_salen_con_coma_decimal(self) -> None:
        datos = m15.leer_tabla(self._escribir(self.FILAS), ";",
                               {"q_T100_m3s": "Caudal"},
                               filtro={"columna": "tipo", "valor": "Sink"})
        self.assertEqual(datos["filas"], [["184,50"]])

    def test_el_corte_de_filas_se_reporta(self) -> None:
        # Cortar en silencio dejaría una tabla incompleta con aspecto completo.
        datos = m15.leer_tabla(self._escribir(self.FILAS), ";",
                               {"elemento": "Elemento"}, filas_max=2)
        self.assertEqual(len(datos["filas"]), 2)
        self.assertEqual(datos["omitidas"], 1)
        self.assertEqual(datos["total"], 3)

    def test_una_columna_declarada_que_no_existe_se_reporta(self) -> None:
        datos = m15.leer_tabla(self._escribir(self.FILAS), ";",
                               {"elemento": "Elemento", "no_existe": "X"})
        self.assertEqual(datos["columnas_ausentes"], ["no_existe"])

    def test_sin_ninguna_columna_declarada_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            m15.leer_tabla(self._escribir(self.FILAS), ";", {"no_existe": "X"})

    def test_tabla_ausente(self) -> None:
        with self.assertRaises(ErrorRutas):
            m15.leer_tabla(Path("no_existe.csv"), ";", {"a": "A"})


class PruebaPlantilla(unittest.TestCase):
    """
    La plantilla se deriva del informe de referencia del consultor, que es su
    propiedad y define el formato de entrega (CLAUDE.md, sección 10).
    """

    def test_la_plantilla_existe_y_abre(self) -> None:
        ruta = _RAIZ_REPO / _CFG.obtener("informe.plantilla")
        if not ruta.is_file():
            self.skipTest("la plantilla aún no se ha derivado")
        documento = docx_plantilla.abrir(ruta)
        self.assertEqual(len(documento.paragraphs), 0)
        self.assertEqual(len(documento.tables), 0)

    def test_conserva_los_estilos_que_el_modulo_usa(self) -> None:
        ruta = _RAIZ_REPO / _CFG.obtener("informe.plantilla")
        if not ruta.is_file():
            self.skipTest("la plantilla aún no se ha derivado")
        identificadores = {e.style_id for e in docx_plantilla.abrir(ruta).styles}
        for estilo in (list(m15.ESTILO_TITULO.values())
                       + [m15.ESTILO_TEXTO, m15.ESTILO_LEYENDA,
                          m15.ESTILO_FUENTE, m15.ESTILO_TABLA]):
            self.assertIn(estilo, identificadores)

    def test_conserva_el_tamano_de_pagina(self) -> None:
        # Sin el sectPr final el documento sale en A4 con márgenes por defecto,
        # y el informe de referencia es carta.
        ruta = _RAIZ_REPO / _CFG.obtener("informe.plantilla")
        if not ruta.is_file():
            self.skipTest("la plantilla aún no se ha derivado")
        seccion = docx_plantilla.abrir(ruta).sections[0]
        self.assertEqual(seccion.page_width.twips, 12240)
        self.assertEqual(seccion.page_height.twips, 15840)

    def test_no_arrastra_las_imagenes_del_informe(self) -> None:
        # De 197 imágenes solo sobreviven las de membrete: la plantilla pesa
        # cientos de kilobytes en lugar de decenas de megabytes.
        ruta = _RAIZ_REPO / _CFG.obtener("informe.plantilla")
        if not ruta.is_file():
            self.skipTest("la plantilla aún no se ha derivado")
        self.assertLess(ruta.stat().st_size, 2_000_000)

    def test_un_origen_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(docx_plantilla.ErrorPlantilla):
            docx_plantilla.extraer_plantilla(
                Path("no_existe.docx"), Path(tempfile.mkdtemp()) / "x.dotx")

    def test_una_plantilla_ausente_es_error_explicito(self) -> None:
        with self.assertRaises(docx_plantilla.ErrorPlantilla):
            docx_plantilla.abrir(Path("no_existe.dotx"))


class PruebaDeclaraciones(unittest.TestCase):
    def _cargar(self, clave):
        import yaml
        ruta = _RAIZ_REPO / _CFG.obtener(clave)
        return yaml.safe_load(ruta.read_text(encoding="utf-8"))

    def test_la_estructura_declara_capitulos(self) -> None:
        capitulos = self._cargar("informe.estructura").get("capitulos", [])
        self.assertGreater(len(capitulos), 5)
        for nodo in capitulos:
            self.assertTrue(str(nodo.get("titulo", "")).strip())
            self.assertEqual(int(nodo.get("nivel", 0)), 1)

    def test_cada_texto_declarado_existe_en_la_narrativa(self) -> None:
        # Una clave mal escrita produciría un apartado mudo y ninguna señal.
        narrativa = self._cargar("informe.texto")
        pendientes = [self._cargar("informe.estructura").get("capitulos", [])]
        claves = []
        while pendientes:
            for nodo in pendientes.pop():
                if nodo.get("texto"):
                    claves.append(nodo["texto"])
                if nodo.get("hijos"):
                    pendientes.append(nodo["hijos"])
        self.assertTrue(claves)
        for clave in claves:
            self.assertIn(clave, narrativa, f"falta el texto {clave!r}")

    def test_los_estilos_de_titulo_cubren_los_niveles_declarados(self) -> None:
        pendientes = [self._cargar("informe.estructura").get("capitulos", [])]
        while pendientes:
            for nodo in pendientes.pop():
                self.assertIn(int(nodo.get("nivel", 1)), m15.ESTILO_TITULO)
                if nodo.get("hijos"):
                    pendientes.append(nodo["hijos"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

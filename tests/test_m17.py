# -*- coding: utf-8 -*-
"""
Pruebas del M17: ensamble y verificacion de anexos.

    python tests/test_m17.py
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

import M17_anexos as m17  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402


class PruebaEstructuraReal(unittest.TestCase):
    """La estructura del repositorio, no una inventada para la prueba."""

    def setUp(self) -> None:
        self.estructura = m17.leer_estructura(
            _RAIZ_REPO / "config" / "anexos.yaml")
        self.piezas = m17.aplanar(self.estructura)

    def test_la_estructura_se_lee(self) -> None:
        self.assertTrue(self.piezas)

    def test_toda_pieza_declara_numero_titulo_y_origen(self) -> None:
        for pieza in self.piezas:
            self.assertTrue(pieza.numero)
            self.assertTrue(pieza.titulo, pieza.numero)
            self.assertTrue(pieza.origen, pieza.numero)

    def test_los_numeros_no_se_repiten(self) -> None:
        # Dos anexos con el mismo numero dejarian uno sin carpeta propia.
        numeros = [p.numero for p in self.piezas]
        self.assertEqual(len(numeros), len(set(numeros)))

    def test_toda_pieza_declara_de_donde_sale(self) -> None:
        # El acta de entrega distingue lo que produjo la cadena de lo que
        # aporto el consultor: sin eso, un anexo ausente no se sabe a quien
        # reclamarselo.
        for pieza in self.piezas:
            self.assertIn(pieza.fuente, ("cadena", "usuario", "doctrina"),
                          pieza.numero)

    def test_un_anexo_con_hijos_no_es_pieza(self) -> None:
        # Tratarlo como pieza produciria una carpeta vacia con su numero, que
        # al revisar se lee como un anexo que se perdio.
        numeros = {p.numero for p in self.piezas}
        self.assertNotIn("1", numeros)
        self.assertIn("1.1", numeros)

    def test_una_estructura_ausente_es_error(self) -> None:
        with self.assertRaises(ErrorRutas):
            m17.leer_estructura(Path("no_existe.yaml"))


class PruebaBusquedaYHuella(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "sub").mkdir()
        (self.tmp / "uno.csv").write_text("a", encoding="utf-8")
        (self.tmp / "sub" / "dos.csv").write_text("b", encoding="utf-8")
        (self.tmp / "basura.lock").write_text("x", encoding="utf-8")

    def test_una_carpeta_entrega_lo_que_tenga(self) -> None:
        # Si un modulo anade un producto, entra solo.
        hallados = m17.buscar_archivos(self.tmp, self.tmp, ["*.lock"])
        self.assertEqual(len(hallados), 2)

    def test_se_recorre_hacia_dentro(self) -> None:
        nombres = {p.name for p in m17.buscar_archivos(self.tmp, self.tmp, [])}
        self.assertIn("dos.csv", nombres)

    def test_lo_excluido_no_entra(self) -> None:
        nombres = {p.name
                   for p in m17.buscar_archivos(self.tmp, self.tmp, ["*.lock"])}
        self.assertNotIn("basura.lock", nombres)

    def test_un_comodin_selecciona(self) -> None:
        hallados = m17.buscar_archivos(self.tmp / "*.csv", self.tmp, [])
        self.assertEqual(len(hallados), 1)

    def test_un_origen_que_no_existe_no_es_error(self) -> None:
        # Es un anexo sin contenido, y quien llame decide si eso bloquea.
        self.assertEqual(
            m17.buscar_archivos(self.tmp / "no_esta", self.tmp, []), [])

    def test_la_huella_distingue_dos_archivos(self) -> None:
        uno = m17.huella(self.tmp / "uno.csv")
        dos = m17.huella(self.tmp / "sub" / "dos.csv")
        self.assertNotEqual(uno, dos)
        self.assertEqual(len(uno), 64)

    def test_la_huella_del_mismo_archivo_no_cambia(self) -> None:
        self.assertEqual(m17.huella(self.tmp / "uno.csv"),
                         m17.huella(self.tmp / "uno.csv"))


class PruebaNombreDeCarpeta(unittest.TestCase):
    def test_lleva_el_numero_como_el_informe_lo_cita(self) -> None:
        pieza = m17.Pieza("3.1", "Analisis de Consistencia", "x", "cadena", True)
        self.assertEqual(m17.carpeta_de(pieza), "3.1. Analisis de Consistencia")

    def test_se_limpian_los_caracteres_que_windows_no_admite(self) -> None:
        # Un titulo con dos puntos o barra rompe la escritura y el paquete
        # queda a medias.
        pieza = m17.Pieza("6", "Crecientes: HEC/HMS", "x", "cadena", True)
        for malo in '<>:"/\|?*':
            self.assertNotIn(malo, m17.carpeta_de(pieza))


class PruebaOrigenVacio(unittest.TestCase):
    """
    Un anexo sin 'origen' es una declaración incompleta, no 'todo'.

    LO QUE PASO. Se declaro un anexo con subanexos usando una clave que el
    modulo no lee, de modo que el padre se trato como pieza con origen vacio.
    Ese origen se resolvia contra la raiz del estudio y el recorrido copio el
    estudio ENTERO dentro del propio paquete de anexos, que ademas cuelga de
    el: 9,4 GB y un fallo de ruta demasiado larga.
    """

    def test_no_devuelve_nada(self) -> None:
        self.assertEqual(m17.buscar_archivos("", Path("."), []), [])
        self.assertEqual(m17.buscar_archivos("   ", Path("."), []), [])


class PruebaCarpetaDelAnexo(unittest.TestCase):
    """
    Un anexo puede imponer la carpeta de todos sus subanexos.

    LO NECESITA EL QUE ENTREGA UN PROYECTO DE QGIS. Sus capas se referencian
    por ruta relativa, y repartidas en carpetas hermanas el proyecto abre sin
    una sola capa: comprobado sobre este estudio, 51 fuentes en el de planchas
    y todas relativas.
    """

    def _pieza(self, **cambios):
        argumentos = dict(numero="10.1", titulo="Proyecto de trabajo",
                          origen="x", fuente="cadena", obligatorio=False)
        argumentos.update(cambios)
        return m17.Pieza(**argumentos)

    def test_sin_imponerla_usa_su_numero_y_titulo(self) -> None:
        self.assertEqual(m17.carpeta_de(self._pieza()),
                         "10.1. Proyecto de trabajo")

    def test_impuesta_manda_sobre_el_numero(self) -> None:
        self.assertEqual(
            m17.carpeta_de(self._pieza(carpeta="10. Proyecto SIG editable")),
            "10. Proyecto SIG editable")

    def test_carpeta_unica_la_reparte_a_los_subanexos(self) -> None:
        piezas = m17.aplanar({"anexos": [{
            "numero": "10", "titulo": "Proyecto SIG editable",
            "carpeta_unica": True,
            "subanexos": [
                {"numero": "10.1", "titulo": "Proyecto", "origen": "a"},
                {"numero": "10.2", "titulo": "Capas", "origen": "b"},
            ]}]})
        self.assertEqual({m17.carpeta_de(p) for p in piezas},
                         {"10. Proyecto SIG editable"})

    def test_sin_carpeta_unica_cada_subanexo_va_al_suyo(self) -> None:
        piezas = m17.aplanar({"anexos": [{
            "numero": "1", "titulo": "Información",
            "subanexos": [
                {"numero": "1.1", "titulo": "Crudos", "origen": "a"},
                {"numero": "1.2", "titulo": "ENA", "origen": "b"},
            ]}]})
        self.assertEqual(len({m17.carpeta_de(p) for p in piezas}), 2)

    def test_el_destino_interno_se_lee(self) -> None:
        # Es lo que conserva el arbol que el proyecto de QGIS espera.
        piezas = m17.aplanar({"anexos": [{
            "numero": "10.1", "titulo": "Proyecto", "origen": "a",
            "destino_interno": "data/03_SIG/proyecto"}]})
        self.assertEqual(piezas[0].destino_interno, "data/03_SIG/proyecto")


if __name__ == "__main__":
    unittest.main(verbosity=2)

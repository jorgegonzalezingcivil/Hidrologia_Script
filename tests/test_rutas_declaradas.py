# -*- coding: utf-8 -*-
"""
Ninguna SALIDA declarada puede nombrarse a mano en el código.

Es la generalización de un fallo que apareció TRES veces el mismo día, al
separar el área de influencia preliminar de la definitiva:

    M02  recortaba la pendiente contra 'area_influencia.shp' escrito a mano,
         mientras la configuración había movido esa salida a otra ruta. El
         módulo se detenía por 'no se encuentra', sin explicar por qué.
    M09  leía esa misma ruta para escribir la cota superior del INSTRUCTIVO,
         que es el número contra el que el ingeniero comprueba su delimitación.
    M09  buscaba el .prj de referencia solo entre capas que al importar pueden
         no existir todavía.

Y quedaban otros dieciocho sitios con el mismo patrón, latentes: coincidían con
la ruta declarada solo mientras nadie la moviera.

LA REGLA. Si config.yaml declara una ruta de salida, ningún módulo puede
referirse a ese archivo por su nombre literal: debe resolver la clave. Así una
ruta que el estudio declare distinta llega a todos los que la consumen, en vez
de partir la cadena en dos mitades que buscan en sitios distintos.

    python tests/test_rutas_declaradas.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

_RAIZ_REPO = Path(__file__).resolve().parents[1]


def _rutas_de_salida(nodo, prefijo: str = "", salida: dict | None = None):
    """Claves de configuración que declaran un archivo de SALIDA."""
    salida = {} if salida is None else salida
    if isinstance(nodo, dict):
        for clave, valor in nodo.items():
            _rutas_de_salida(valor, f"{prefijo}{clave}.", salida)
    elif isinstance(nodo, str) and "/" in nodo and "." in Path(nodo).name:
        clave = prefijo.rstrip(".")
        # 'salida_dem', 'salida_recorte_doble', 'salida'... siempre el último
        # segmento, para no arrastrar claves de entrada que citen una carpeta.
        if "salida" in clave.rsplit(".", 1)[-1]:
            salida[Path(nodo).name] = clave
    return salida


class PruebaRutasDeSalida(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        configuracion = yaml.safe_load(
            (_RAIZ_REPO / "config" / "config.yaml").read_text(encoding="utf-8"))
        cls.salidas = _rutas_de_salida(configuracion)
        cls.fuentes = sorted((_RAIZ_REPO / "src").rglob("*.py"))

    def test_hay_salidas_que_vigilar(self) -> None:
        # Si esto falla, el detector dejó de encontrar nada y la prueba de
        # abajo pasaría siempre sin comprobar nada.
        self.assertGreater(len(self.salidas), 10)

    def test_ninguna_salida_se_nombra_a_mano(self) -> None:
        infracciones: list[str] = []

        for nombre, clave in sorted(self.salidas.items()):
            patron = re.compile(r"""["']""" + re.escape(nombre) + r"""["']""")
            for fuente in self.fuentes:
                texto = fuente.read_text(encoding="utf-8", errors="replace")
                for numero, linea in enumerate(texto.splitlines(), start=1):
                    if linea.strip().startswith("#"):
                        continue
                    if patron.search(linea):
                        infracciones.append(
                            f"{fuente.name}:{numero} fija '{nombre}'; "
                            f"debe resolver la clave '{clave}'")

        self.assertEqual(
            infracciones, [],
            "Rutas de salida fijadas a mano:\n  " + "\n  ".join(infracciones))


if __name__ == "__main__":
    unittest.main(verbosity=2)

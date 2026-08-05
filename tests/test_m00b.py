# -*- coding: utf-8 -*-
"""
Pruebas del M00b: declaración del proyecto QGIS y su construcción.

Las pruebas de validación y expansión son puras y corren bajo cualquiera de los
dos intérpretes. Las que construyen el .qgz se omiten de forma automática si la
API de QGIS no está disponible, de modo que la suite completa sigue siendo
ejecutable desde el venv:

    python tests/test_m00b.py
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" tests/test_m00b.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M00b_proyecto_qgis as m00b  # noqa: E402
from comun import esquema  # noqa: E402
from comun.config import cargar, leer_yaml  # noqa: E402

try:
    import qgis.core  # noqa: F401

    HAY_QGIS = True
except Exception:
    HAY_QGIS = False


def tearDownModule() -> None:
    """
    Cierra la aplicación QGIS una sola vez, al terminar toda la suite.

    QGIS no admite reinicializarse dentro del mismo proceso: cerrarla entre
    pruebas produce una violación de acceso que mata el intérprete sin traza.
    """
    if HAY_QGIS:
        m00b.finalizar_qgis()


def _declaracion_repositorio() -> dict:
    return leer_yaml(_RAIZ_REPO / "config" / "proyecto_qgis.yaml")


def _declaracion_minima() -> dict:
    return {
        "version": 1,
        "grupos": [
            {
                "nombre": "Grupo",
                "expandido": True,
                "visible": True,
                "capas": [
                    {
                        "id": "capa_a",
                        "nombre": "Capa A",
                        "tipo": "vector",
                        "ruta": "data/03_SIG/vector/capa_a.shp",
                        "modulo": "M02",
                        "visible": True,
                        "estilo": "capa_a.qml",
                    }
                ],
            }
        ],
    }


class PruebaValidacionDeclaracion(unittest.TestCase):
    def _claves(self, datos, severidad: str) -> set[str]:
        return {
            h.clave for h in m00b.validar_declaracion(datos)
            if h.severidad == severidad
        }

    def test_la_declaracion_del_repositorio_es_valida(self) -> None:
        hallazgos = m00b.validar_declaracion(_declaracion_repositorio())
        bloqueantes = [h for h in hallazgos if h.es_bloqueante]
        self.assertEqual(bloqueantes, [],
                         "\n".join(str(h) for h in bloqueantes))

    def test_rechaza_version_no_soportada(self) -> None:
        datos = _declaracion_minima()
        datos["version"] = 2
        self.assertIn("version", self._claves(datos, esquema.BLOQUEANTE))

    def test_rechaza_declaracion_sin_grupos(self) -> None:
        self.assertIn("grupos",
                      self._claves({"version": 1, "grupos": []}, esquema.BLOQUEANTE))

    def test_rechaza_contenido_que_no_es_bloque(self) -> None:
        self.assertIn("<raiz>", self._claves(["uno"], esquema.BLOQUEANTE))

    def test_rechaza_capa_sin_identificador(self) -> None:
        datos = _declaracion_minima()
        del datos["grupos"][0]["capas"][0]["id"]
        self.assertIn("grupos[0].capas[0].id",
                      self._claves(datos, esquema.BLOQUEANTE))

    def test_rechaza_tipo_de_capa_no_admitido(self) -> None:
        datos = _declaracion_minima()
        datos["grupos"][0]["capas"][0]["tipo"] = "malla"
        self.assertIn("grupos[0].capas[0].tipo",
                      self._claves(datos, esquema.BLOQUEANTE))

    def test_rechaza_visible_no_booleano(self) -> None:
        datos = _declaracion_minima()
        datos["grupos"][0]["capas"][0]["visible"] = "si"
        self.assertIn("grupos[0].capas[0].visible",
                      self._claves(datos, esquema.BLOQUEANTE))

    def test_rechaza_estilo_con_directorio(self) -> None:
        datos = _declaracion_minima()
        datos["grupos"][0]["capas"][0]["estilo"] = "sub/capa_a.qml"
        self.assertIn("grupos[0].capas[0].estilo",
                      self._claves(datos, esquema.BLOQUEANTE))

    def test_advierte_estilo_sin_extension_qml(self) -> None:
        datos = _declaracion_minima()
        datos["grupos"][0]["capas"][0]["estilo"] = "capa_a.sld"
        self.assertIn("grupos[0].capas[0].estilo",
                      self._claves(datos, esquema.ADVERTENCIA))

    def test_rechaza_identificadores_repetidos(self) -> None:
        datos = _declaracion_minima()
        gemela = deepcopy(datos["grupos"][0]["capas"][0])
        datos["grupos"][0]["capas"].append(gemela)
        self.assertIn("grupos[0].capas[1].id",
                      self._claves(datos, esquema.BLOQUEANTE))

    def test_detecta_identificador_repetido_entre_grupos_anidados(self) -> None:
        datos = _declaracion_minima()
        datos["grupos"][0]["grupos"] = [{
            "nombre": "Anidado",
            "capas": [deepcopy(datos["grupos"][0]["capas"][0])],
        }]
        bloqueantes = self._claves(datos, esquema.BLOQUEANTE)
        self.assertTrue(any("grupos[0].grupos[0].capas[0].id" == c
                            for c in bloqueantes), bloqueantes)

    def test_advierte_clave_no_reconocida(self) -> None:
        datos = _declaracion_minima()
        datos["grupos"][0]["capas"][0]["opacidad"] = 0.5
        self.assertIn("grupos[0].capas[0].opacidad",
                      self._claves(datos, esquema.ADVERTENCIA))

    def test_rechaza_patron_sin_clave_patron(self) -> None:
        datos = _declaracion_minima()
        datos["grupos"][0]["patrones"] = [{
            "id": "p", "nombre": "P", "tipo": "raster",
        }]
        self.assertIn("grupos[0].patrones[0].patron",
                      self._claves(datos, esquema.BLOQUEANTE))


class PruebaExpansion(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        (self.temporal / "data" / "03_SIG" / "raster").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _crear(self, *nombres: str) -> None:
        for nombre in nombres:
            (self.temporal / "data" / "03_SIG" / "raster" / nombre).write_text(
                "x", encoding="utf-8"
            )

    def test_expande_capas_declaradas(self) -> None:
        grupos, hallazgos = m00b.expandir_grupos(
            _declaracion_minima()["grupos"], self.temporal
        )
        self.assertEqual(len(grupos), 1)
        self.assertEqual(len(grupos[0].capas), 1)
        self.assertEqual(grupos[0].capas[0].grupo, ("Grupo",))
        self.assertFalse(grupos[0].capas[0].existe)
        self.assertEqual(hallazgos, [])

    def test_expande_patrones_en_orden_alfabetico(self) -> None:
        self._crear("isoyetas_c.tif", "isoyetas_a.tif", "isoyetas_b.tif")
        declaracion = [{
            "nombre": "Isoyetas",
            "patrones": [{
                "id": "iso", "nombre": "Isoyetas", "tipo": "raster",
                "patron": "data/03_SIG/raster/isoyetas_*.tif",
                "modulo": "M06", "visible": False, "estilo": "iso.qml",
            }],
        }]
        grupos, _ = m00b.expandir_grupos(declaracion, self.temporal)
        nombres = [capa.nombre for capa in grupos[0].capas]
        self.assertEqual(nombres, ["isoyetas_a", "isoyetas_b", "isoyetas_c"])
        self.assertTrue(all(capa.existe for capa in grupos[0].capas))
        self.assertEqual(grupos[0].capas[0].id, "iso__isoyetas_a")

    def test_patron_sin_coincidencias_es_informativo(self) -> None:
        declaracion = [{
            "nombre": "Isoyetas",
            "patrones": [{
                "id": "iso", "nombre": "Isoyetas", "tipo": "raster",
                "patron": "data/03_SIG/raster/nada_*.tif", "modulo": "M06",
            }],
        }]
        grupos, hallazgos = m00b.expandir_grupos(declaracion, self.temporal)
        self.assertEqual(grupos[0].capas, ())
        self.assertEqual(len(hallazgos), 1)
        self.assertEqual(hallazgos[0].severidad, esquema.INFORMATIVO)

    def test_recorre_subgrupos_conservando_el_orden(self) -> None:
        declaracion = [{
            "nombre": "Padre",
            "capas": [{
                "id": "a", "nombre": "A", "tipo": "vector", "ruta": "a.shp",
            }],
            "grupos": [{
                "nombre": "Hijo",
                "capas": [{
                    "id": "b", "nombre": "B", "tipo": "vector", "ruta": "b.shp",
                }],
            }],
        }]
        grupos, _ = m00b.expandir_grupos(declaracion, self.temporal)
        capas = m00b.recorrer_capas(grupos)
        self.assertEqual([capa.id for capa in capas], ["a", "b"])
        self.assertEqual(capas[1].grupo, ("Padre", "Hijo"))

    def test_estilo_por_defecto_toma_el_identificador(self) -> None:
        declaracion = [{
            "nombre": "G",
            "capas": [{
                "id": "sin_estilo", "nombre": "S", "tipo": "vector", "ruta": "s.shp",
            }],
        }]
        grupos, _ = m00b.expandir_grupos(declaracion, self.temporal)
        self.assertEqual(grupos[0].capas[0].estilo, "sin_estilo.qml")

    def test_disponibilidad_informativa_por_defecto(self) -> None:
        grupos, _ = m00b.expandir_grupos(
            _declaracion_minima()["grupos"], self.temporal
        )
        hallazgos = m00b.revisar_disponibilidad(m00b.recorrer_capas(grupos), False)
        self.assertEqual(len(hallazgos), 1)
        self.assertEqual(hallazgos[0].severidad, esquema.INFORMATIVO)

    def test_disponibilidad_bloqueante_si_se_exige(self) -> None:
        grupos, _ = m00b.expandir_grupos(
            _declaracion_minima()["grupos"], self.temporal
        )
        hallazgos = m00b.revisar_disponibilidad(m00b.recorrer_capas(grupos), True)
        self.assertEqual(hallazgos[0].severidad, esquema.BLOQUEANTE)


class PruebaCoherenciaConLaConfiguracion(unittest.TestCase):
    """La declaración y el config.yaml deben referirse a lo mismo."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = cargar(raiz=_RAIZ_REPO)

    def test_la_declaracion_declarada_en_config_existe(self) -> None:
        self.assertTrue(self.cfg.ruta_de("proyecto_qgis.declaracion").is_file())

    def test_el_destino_del_proyecto_esta_bajo_data_03_sig(self) -> None:
        destino = self.cfg.ruta_de("proyecto_qgis.archivo")
        self.assertEqual(destino.suffix, ".qgz")
        self.assertIn("03_SIG", destino.parts)

    def test_las_rutas_declaradas_apuntan_dentro_del_repositorio(self) -> None:
        grupos, _ = m00b.expandir_grupos(
            _declaracion_repositorio()["grupos"], _RAIZ_REPO
        )
        for capa in m00b.recorrer_capas(grupos):
            self.assertTrue(
                str(capa.ruta).startswith(str(_RAIZ_REPO)),
                f"{capa.id} apunta fuera del repositorio: {capa.ruta}",
            )


@unittest.skipUnless(HAY_QGIS, "requiere el intérprete de QGIS")
class PruebaConstruccion(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.destino = self.temporal / "proyecto" / "prueba.qgz"
        self.estilos = self.temporal / "proyecto" / "estilos"
        self.cfg = cargar(raiz=_RAIZ_REPO)
        self.prefix = self.cfg.obtener("entornos.qgis.prefix_path")
        self.crs = self.cfg.obtener("crs.calculo")

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _raster_de_prueba(self, nombre: str = "sonda.asc") -> Path:
        """
        Escribe un ráster ASCII Grid mínimo con su .prj.

        Se usa AAIGrid porque es texto plano: la prueba no depende de ninguna
        API de escritura de GDAL que pueda cambiar entre versiones de QGIS.
        """
        from qgis.core import QgsCoordinateReferenceSystem

        destino = self.temporal / nombre
        destino.write_text(
            "ncols 2\nnrows 2\nxllcorner 4600000\nyllcorner 1700000\n"
            "cellsize 100\nNODATA_value -9999\n1 2\n3 4\n",
            encoding="utf-8",
        )
        destino.with_suffix(".prj").write_text(
            QgsCoordinateReferenceSystem(self.crs).toWkt(), encoding="utf-8"
        )
        return destino

    def _construir(self, grupos):
        return m00b.construir_proyecto(
            grupos=grupos,
            destino=self.destino,
            directorio_estilos=self.estilos,
            crs_calculo=self.crs,
            titulo="Prueba M00b",
            prefix_path=self.prefix,
        )

    def test_escribe_el_proyecto_aunque_no_haya_capas(self) -> None:
        grupos, _ = m00b.expandir_grupos(
            _declaracion_repositorio()["grupos"], self.temporal
        )
        resultado = self._construir(grupos)
        self.assertIsNotNone(resultado.archivo)
        self.assertTrue(self.destino.is_file())
        self.assertGreater(self.destino.stat().st_size, 0)
        self.assertEqual(resultado.capas_cargadas, 0)
        self.assertGreater(resultado.capas_ausentes, 0)

    def test_carga_una_capa_real_y_crea_su_estilo(self) -> None:
        raster = self._raster_de_prueba()
        declaracion = [{
            "nombre": "Terreno",
            "expandido": True,
            "visible": True,
            "capas": [{
                "id": "sonda", "nombre": "Sonda", "tipo": "raster",
                "ruta": raster.name, "modulo": "prueba",
                "visible": True, "estilo": "sonda.qml",
            }],
        }]
        grupos, _ = m00b.expandir_grupos(declaracion, self.temporal)
        resultado = self._construir(grupos)

        self.assertEqual(resultado.capas_cargadas, 1)
        self.assertEqual(resultado.estilos_creados, 1)
        self.assertTrue((self.estilos / "sonda.qml").is_file())
        self.assertTrue(self.destino.is_file())
        bloqueantes = [h for h in resultado.hallazgos if h.es_bloqueante]
        self.assertEqual(bloqueantes, [], "\n".join(str(h) for h in bloqueantes))

    def test_no_sobrescribe_un_estilo_existente(self) -> None:
        raster = self._raster_de_prueba()
        declaracion = [{
            "nombre": "Terreno",
            "capas": [{
                "id": "sonda", "nombre": "Sonda", "tipo": "raster",
                "ruta": raster.name, "estilo": "sonda.qml",
            }],
        }]
        grupos, _ = m00b.expandir_grupos(declaracion, self.temporal)

        primera = self._construir(grupos)
        self.assertEqual(primera.estilos_creados, 1)
        marca = (self.estilos / "sonda.qml").read_text(encoding="utf-8")

        segunda = self._construir(grupos)
        self.assertEqual(segunda.estilos_creados, 0)
        self.assertEqual(segunda.estilos_aplicados, 1)
        self.assertEqual(
            (self.estilos / "sonda.qml").read_text(encoding="utf-8"), marca
        )

    def test_una_capa_ilegible_es_bloqueante(self) -> None:
        roto = self.temporal / "roto.asc"
        roto.write_text("esto no es un raster", encoding="utf-8")
        declaracion = [{
            "nombre": "G",
            "capas": [{
                "id": "roto", "nombre": "Roto", "tipo": "raster",
                "ruta": roto.name,
            }],
        }]
        grupos, _ = m00b.expandir_grupos(declaracion, self.temporal)
        resultado = self._construir(grupos)
        self.assertTrue(any(h.es_bloqueante for h in resultado.hallazgos))
        self.assertIsNone(resultado.archivo)
        self.assertFalse(self.destino.exists())

    def test_crs_invalido_no_escribe_el_proyecto(self) -> None:
        resultado = m00b.construir_proyecto(
            grupos=(), destino=self.destino, directorio_estilos=self.estilos,
            crs_calculo="EPSG:999999", titulo="X", prefix_path=self.prefix,
        )
        self.assertTrue(any(h.es_bloqueante for h in resultado.hallazgos))
        self.assertFalse(self.destino.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

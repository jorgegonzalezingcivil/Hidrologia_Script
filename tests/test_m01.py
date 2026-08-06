# -*- coding: utf-8 -*-
"""
Pruebas del M01: declaración de campos y ubicación del punto de descarga.

Las pruebas de campos y de funciones puras corren bajo cualquier intérprete. Las
que ubican el punto requieren QGIS y la capa de subzonas, y se omiten de forma
automática si falta alguno de los dos.

    python tests/test_m01.py
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" tests/test_m01.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M01_punto_descarga as m01  # noqa: E402
from comun import campos as mod_campos  # noqa: E402
from comun import esquema  # noqa: E402
from comun.campos import CampoSalida  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato  # noqa: E402

try:
    import qgis.core  # noqa: F401

    HAY_QGIS = True
except Exception:
    HAY_QGIS = False

_CFG = cargar(raiz=_RAIZ_REPO)
_RUTA_SUBZONAS = _CFG.ruta_de("subzonas_hidrograficas.archivo")
HAY_CAPA = _RUTA_SUBZONAS.is_file()


def tearDownModule() -> None:
    if HAY_QGIS:
        m01.finalizar_qgis()


# =============================================================================
# Declaración de campos
# =============================================================================
class PruebaCampos(unittest.TestCase):
    def test_los_campos_del_m01_son_escribibles(self) -> None:
        mod_campos.validar_campos(m01.CAMPOS_PUNTO)
        mod_campos.validar_campos(m01.CAMPOS_SUBZONA)

    def test_rechaza_nombre_de_mas_de_diez_caracteres(self) -> None:
        with self.assertRaises(ErrorFormato) as contexto:
            mod_campos.validar_campos([
                CampoSalida("precipitacion_media", "Precipitación", "decimal")
            ])
        self.assertIn("trunca", str(contexto.exception))

    def test_rechaza_nombres_que_colisionan_al_ignorar_mayusculas(self) -> None:
        with self.assertRaises(ErrorFormato):
            mod_campos.validar_campos([
                CampoSalida("cod_szh", "Código", "texto"),
                CampoSalida("COD_SZH", "Código otra vez", "texto"),
            ])

    def test_rechaza_acentos_y_espacios(self) -> None:
        with self.assertRaises(ErrorFormato):
            mod_campos.validar_campos([CampoSalida("área", "Área", "decimal")])
        with self.assertRaises(ErrorFormato):
            mod_campos.validar_campos([CampoSalida("cod szh", "Código", "texto")])

    def test_rechaza_tipo_no_admitido(self) -> None:
        with self.assertRaises(ErrorFormato):
            mod_campos.validar_campos([CampoSalida("x", "X", "booleano")])

    def test_escribe_el_diccionario(self) -> None:
        temporal = Path(tempfile.mkdtemp())
        try:
            destino = mod_campos.escribir_diccionario(
                m01.CAMPOS_PUNTO, temporal / "punto_campos.csv", "punto"
            )
            contenido = destino.read_text(encoding="utf-8-sig")
            self.assertIn("campo_corto;campo_descriptivo", contenido)
            self.assertIn("cod_szh;Código de la subzona hidrográfica", contenido)
            for campo in m01.CAMPOS_PUNTO:
                self.assertIn(campo.corto, contenido)
        finally:
            shutil.rmtree(temporal, ignore_errors=True)


# =============================================================================
# Funciones puras
# =============================================================================
class PruebaFuncionesPuras(unittest.TestCase):
    def test_dentro_de_colombia(self) -> None:
        self.assertTrue(m01.dentro_de_colombia(-74.045844, 4.830574))
        self.assertFalse(m01.dentro_de_colombia(-3.0, 40.0))
        self.assertFalse(m01.dentro_de_colombia(4.60, -74.08))

    def test_normalizar_codigo(self) -> None:
        self.assertEqual(m01.normalizar_codigo(2120), "2120")
        self.assertEqual(m01.normalizar_codigo(2120.0), "2120")
        self.assertEqual(m01.normalizar_codigo(" 2120 "), "2120")
        self.assertEqual(m01.normalizar_codigo(None), "")

    def test_verificar_mapeo_campos_detecta_ausentes(self) -> None:
        hallazgos = m01.verificar_mapeo_campos(
            ["COD_SZH", "NOM_SZH"],
            {"codigo_szh": "COD_SZH", "nombre_zh": "NOM_ZH"},
        )
        self.assertEqual(len(hallazgos), 1)
        self.assertTrue(hallazgos[0].es_bloqueante)
        self.assertIn("NOM_ZH", hallazgos[0].mensaje)

    def test_verificar_mapeo_campos_ignora_mayusculas(self) -> None:
        self.assertEqual(
            m01.verificar_mapeo_campos(["cod_szh"], {"codigo_szh": "COD_SZH"}), []
        )


# =============================================================================
# Coherencia entre config y la capa declarada
# =============================================================================
@unittest.skipUnless(HAY_CAPA, "requiere la capa de subzonas")
class PruebaCoherencia(unittest.TestCase):
    def test_los_campos_declarados_existen_en_la_capa(self) -> None:
        from comun import shapefile

        info = shapefile.leer_shapefile(_RUTA_SUBZONAS)
        declarados = dict(_CFG.obtener("subzonas_hidrograficas.campos"))
        self.assertEqual(
            m01.verificar_mapeo_campos(info.nombres_campos, declarados), []
        )

    def test_la_capa_del_m00b_apunta_al_mismo_archivo(self) -> None:
        from comun.config import leer_yaml

        declaracion = leer_yaml(_RAIZ_REPO / "config" / "proyecto_qgis.yaml")
        rutas_declaradas = [
            capa["ruta"]
            for grupo in declaracion["grupos"]
            for capa in grupo.get("capas", [])
            if capa["id"] == "subzonas_hidrograficas"
        ]
        self.assertEqual(len(rutas_declaradas), 1)
        self.assertEqual(
            (_RAIZ_REPO / rutas_declaradas[0]).resolve(), _RUTA_SUBZONAS
        )


# =============================================================================
# Ubicación del punto
# =============================================================================
@unittest.skipUnless(HAY_QGIS and HAY_CAPA, "requiere QGIS y la capa de subzonas")
class PruebaUbicacion(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        m01.iniciar_qgis(_CFG.obtener("entornos.qgis.prefix_path"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _config_con(self, **cambios):
        """Config del repositorio con el punto de descarga sustituido."""
        from comun.config import Config

        datos = _CFG.como_dict()
        datos["punto_descarga"].update(cambios)
        return Config(datos, _CFG.ruta, _CFG.raiz, _CFG.sha256)

    def test_ubica_el_punto_declarado_en_el_rio_bogota(self) -> None:
        ubicacion, hallazgos = m01.ubicar_punto(_CFG, _RUTA_SUBZONAS)
        self.assertIsNotNone(ubicacion)
        self.assertEqual([h for h in hallazgos if h.es_bloqueante], [])
        self.assertEqual(ubicacion.metodo, m01.METODO_CONTIENE)
        self.assertEqual(ubicacion.atributos["cod_szh"], "2120")
        self.assertEqual(ubicacion.atributos["cod_zh"], "21")
        self.assertEqual(ubicacion.atributos["cod_ah"], "2")
        self.assertAlmostEqual(ubicacion.longitud, -74.045844, places=4)
        self.assertAlmostEqual(ubicacion.latitud, 4.830574, places=4)
        self.assertGreater(ubicacion.area_km2, 0)

    def test_el_mismo_punto_en_otro_crs_da_la_misma_subzona(self) -> None:
        """La reproyección explícita debe ser indiferente al CRS de entrada."""
        configuracion = self._config_con(
            crs="EPSG:4326", x=-74.045844, y=4.830574
        )
        ubicacion, _ = m01.ubicar_punto(configuracion, _RUTA_SUBZONAS)
        self.assertIsNotNone(ubicacion)
        self.assertEqual(ubicacion.atributos["cod_szh"], "2120")

    def test_punto_fuera_de_colombia_es_bloqueante(self) -> None:
        configuracion = self._config_con(crs="EPSG:4326", x=-3.7, y=40.4)
        ubicacion, hallazgos = m01.ubicar_punto(configuracion, _RUTA_SUBZONAS)
        self.assertIsNone(ubicacion)
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))

    def test_coordenadas_intercambiadas_son_bloqueantes(self) -> None:
        configuracion = self._config_con(crs="EPSG:4326", x=4.830574, y=-74.045844)
        ubicacion, hallazgos = m01.ubicar_punto(configuracion, _RUTA_SUBZONAS)
        self.assertIsNone(ubicacion)
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))

    def test_crs_invalido_es_bloqueante(self) -> None:
        configuracion = self._config_con(crs="EPSG:999999")
        ubicacion, hallazgos = m01.ubicar_punto(configuracion, _RUTA_SUBZONAS)
        self.assertIsNone(ubicacion)
        self.assertTrue(any(h.clave == "punto_descarga.crs" and h.es_bloqueante
                            for h in hallazgos))

    def test_punto_en_el_mar_fuera_de_tolerancia_es_bloqueante(self) -> None:
        """Un punto mar adentro del Pacífico no pertenece a ninguna subzona."""
        configuracion = self._config_con(crs="EPSG:4326", x=-79.5, y=3.0)
        ubicacion, hallazgos = m01.ubicar_punto(configuracion, _RUTA_SUBZONAS)
        self.assertIsNone(ubicacion)
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))

    def test_campo_mal_declarado_es_bloqueante(self) -> None:
        from comun.config import Config

        datos = _CFG.como_dict()
        datos["subzonas_hidrograficas"]["campos"]["codigo_szh"] = "NO_EXISTE"
        configuracion = Config(datos, _CFG.ruta, _CFG.raiz, _CFG.sha256)
        ubicacion, hallazgos = m01.ubicar_punto(configuracion, _RUTA_SUBZONAS)
        self.assertIsNone(ubicacion)
        self.assertTrue(any("NO_EXISTE" in h.mensaje for h in hallazgos))


@unittest.skipUnless(HAY_QGIS and HAY_CAPA, "requiere QGIS y la capa de subzonas")
class PruebaEjecucionCompleta(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_escribe_las_capas_con_prj_y_diccionario(self) -> None:
        import json

        destino_json = self.temporal / "M01.json"
        codigo, hallazgos = m01.ejecutar(
            raiz=_RAIZ_REPO, ruta_json=destino_json, consola=False
        )
        self.assertEqual(codigo, m01.SALIDA_CORRECTA,
                         "\n".join(str(h) for h in hallazgos if h.es_bloqueante))

        punto = _CFG.ruta_de("subzonas_hidrograficas.salida_punto")
        subzona = _CFG.ruta_de("subzonas_hidrograficas.salida_subzona")
        for capa in (punto, subzona):
            self.assertTrue(capa.is_file(), capa)
            self.assertTrue(capa.with_suffix(".prj").is_file(), capa)
            self.assertTrue(
                capa.with_name(f"{capa.stem}_campos.csv").is_file(), capa
            )

        reporte = json.loads(destino_json.read_text(encoding="utf-8"))
        self.assertTrue(reporte["conforme"])
        self.assertEqual(reporte["resultado"]["jerarquia"]["cod_szh"], "2120")

    def test_las_capas_escritas_estan_en_el_crs_de_calculo(self) -> None:
        from comun import shapefile

        m01.ejecutar(raiz=_RAIZ_REPO, ruta_json=self.temporal / "x.json",
                     consola=False)
        punto = _CFG.ruta_de("subzonas_hidrograficas.salida_punto")
        info = shapefile.leer_shapefile(punto)
        self.assertEqual(info.crs_epsg, _CFG.obtener("crs.calculo"))
        self.assertEqual(info.n_registros, 1)
        self.assertIn("cod_szh", [c.lower() for c in info.nombres_campos])


if __name__ == "__main__":
    unittest.main(verbosity=2)

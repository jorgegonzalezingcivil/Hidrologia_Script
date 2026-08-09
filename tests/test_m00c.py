# -*- coding: utf-8 -*-
"""
Pruebas del M00c: adaptador de shapefile y verificación de insumos.

Las pruebas construyen shapefiles sintéticos byte a byte. Eso permite verificar
el adaptador sin GDAL, sin QGIS y sin depender de ningún insumo real:

    python tests/test_m00c.py
"""

from __future__ import annotations

import shutil
import struct
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M00c_insumos as m00c  # noqa: E402
from comun import esquema, shapefile  # noqa: E402
from comun.config import cargar, leer_yaml  # noqa: E402
from comun.errores import ErrorFormato  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

_WKT_9377 = (
    'PROJCS["MAGNA-SIRGAS 2018 / Origen-Nacional",'
    'GEOGCS["MAGNA-SIRGAS 2018",DATUM["D",SPHEROID["GRS 1980",6378137,298.257222101,'
    'AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","1035"]],'
    'AUTHORITY["EPSG","9377"]]'
)


# =============================================================================
# Constructores de shapefiles sintéticos
# =============================================================================
def escribir_shapefile_poligono(
    base: Path,
    campo: str = "CLASE",
    valores: tuple[str, ...] = ("UCS-1",),
    lado: float = 100.0,
    wkt: str | None = _WKT_9377,
) -> Path:
    """
    Escribe un shapefile con un cuadrado por registro, todos superpuestos.

    El cuadrado tiene el lado indicado, de modo que el área total conocida es
    lado * lado * numero de registros.
    """
    n = len(valores)
    puntos = [(0.0, 0.0), (0.0, lado), (lado, lado), (lado, 0.0), (0.0, 0.0)]

    contenido = struct.pack("<i", 5)
    contenido += struct.pack("<4d", 0.0, 0.0, lado, lado)
    contenido += struct.pack("<i", 1) + struct.pack("<i", len(puntos))
    contenido += struct.pack("<i", 0)
    for x, y in puntos:
        contenido += struct.pack("<2d", x, y)

    registros = b""
    for numero in range(1, n + 1):
        registros += struct.pack(">i", numero)
        registros += struct.pack(">i", len(contenido) // 2)
        registros += contenido

    longitud_palabras = (100 + len(registros)) // 2
    cabecera = struct.pack(">i", 9994) + b"\x00" * 20
    cabecera += struct.pack(">i", longitud_palabras)
    cabecera += struct.pack("<i", 1000)
    cabecera += struct.pack("<i", 5)
    cabecera += struct.pack("<4d", 0.0, 0.0, lado, lado)
    cabecera += struct.pack("<4d", 0.0, 0.0, 0.0, 0.0)

    ruta_shp = base.with_suffix(".shp")
    ruta_shp.write_bytes(cabecera + registros)

    # El .shx no lo usa el adaptador, pero su ausencia se reporta.
    base.with_suffix(".shx").write_bytes(cabecera)

    escribir_dbf(base.with_suffix(".dbf"), campo, valores)

    if wkt is not None:
        base.with_suffix(".prj").write_text(wkt, encoding="utf-8")

    return ruta_shp


def escribir_dbf(destino: Path, campo: str, valores: tuple[str, ...]) -> None:
    """Escribe una tabla dBase III con un solo campo de texto."""
    longitud_campo = max(10, max((len(v) for v in valores), default=1))
    longitud_registro = 1 + longitud_campo
    longitud_cabecera = 32 + 32 + 1

    cabecera = bytes([0x03, 126, 1, 1])
    cabecera += struct.pack("<I", len(valores))
    cabecera += struct.pack("<H", longitud_cabecera)
    cabecera += struct.pack("<H", longitud_registro)
    cabecera += b"\x00" * 20

    descriptor = campo.encode("ascii")[:10].ljust(11, b"\x00")
    descriptor += b"C"
    descriptor += b"\x00" * 4
    descriptor += bytes([longitud_campo, 0])
    descriptor += b"\x00" * 14

    cuerpo = b""
    for valor in valores:
        cuerpo += b" " + valor.encode("utf-8").ljust(longitud_campo, b" ")

    destino.write_bytes(cabecera + descriptor + b"\x0d" + cuerpo + b"\x1a")


# =============================================================================
# Adaptador de shapefile
# =============================================================================
class PruebaAdaptadorShapefile(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.base = self.temporal / "suelos"

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_lee_cabecera_campos_y_crs(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("A", "B", "C"))
        info = shapefile.leer_shapefile(self.base.with_suffix(".shp"))

        self.assertEqual(info.tipo_geometria, "Polígono")
        self.assertEqual(info.n_registros, 3)
        self.assertEqual(info.nombres_campos, ("UCS",))
        self.assertEqual(info.crs_epsg, "EPSG:9377")
        self.assertEqual(info.extension, (0.0, 0.0, 100.0, 100.0))
        self.assertEqual(info.componentes_faltantes, ())

    def test_detecta_componentes_faltantes(self) -> None:
        escribir_shapefile_poligono(self.base, wkt=None)
        info = shapefile.leer_shapefile(self.base.with_suffix(".shp"))
        self.assertIn(".prj", info.componentes_faltantes)
        self.assertIsNone(info.crs_epsg)

    def test_valores_unicos_ordenados_y_sin_repeticion(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("C", "A", "B", "A"))
        self.assertEqual(
            shapefile.valores_unicos(self.base.with_suffix(".shp"), "UCS"),
            ["A", "B", "C"],
        )

    def test_valores_unicos_ignora_mayusculas_del_campo(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("A",))
        self.assertEqual(
            shapefile.valores_unicos(self.base.with_suffix(".shp"), "ucs"), ["A"]
        )

    def test_campo_inexistente_es_error(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("A",))
        with self.assertRaises(ErrorFormato) as contexto:
            shapefile.valores_unicos(self.base.with_suffix(".shp"), "TEXTURA")
        self.assertIn("UCS", str(contexto.exception))

    def test_area_de_poligonos(self) -> None:
        escribir_shapefile_poligono(self.base, valores=("A", "B"), lado=100.0)
        area = shapefile.area_poligonos(self.base.with_suffix(".shp"))
        self.assertAlmostEqual(area, 2 * 100.0 * 100.0, places=6)

    def test_archivo_truncado_es_error(self) -> None:
        destino = self.base.with_suffix(".shp")
        destino.write_bytes(struct.pack(">i", 9994) + b"\x00" * 10)
        escribir_dbf(self.base.with_suffix(".dbf"), "UCS", ("A",))
        with self.assertRaises(ErrorFormato):
            shapefile.leer_shapefile(destino)

    def test_archivo_que_no_es_shapefile_es_error(self) -> None:
        destino = self.base.with_suffix(".shp")
        destino.write_bytes(b"X" * 200)
        escribir_dbf(self.base.with_suffix(".dbf"), "UCS", ("A",))
        with self.assertRaises(ErrorFormato):
            shapefile.leer_shapefile(destino)

    def test_epsg_de_wkt(self) -> None:
        self.assertEqual(shapefile.epsg_de_wkt(_WKT_9377), "EPSG:9377")
        self.assertEqual(
            shapefile.epsg_de_wkt('PROJCS["x",ID["EPSG",3116]]'), "EPSG:3116"
        )
        self.assertIsNone(shapefile.epsg_de_wkt('PROJCS["sin autoridad"]'))
        self.assertIsNone(shapefile.epsg_de_wkt(None))


# =============================================================================
# Validación del manifiesto
# =============================================================================
def _manifiesto_valido() -> dict:
    return {
        "suelos": {
            "aportado": True, "archivo": "suelos/ucs.shp", "tipo": "shapefile",
            "perfil": "C", "campo_clave": "UCS", "fuente": "IGAC",
            "fecha": "2020-01-01", "escala": "1:25000",
            "crs_declarado": "EPSG:9377", "observaciones": "",
        },
        "cobertura": {"aportado": False},
        "caudales": {"aportado": False},
        "homologacion": {
            "suelos": {"archivo": "homologacion/suelos.csv",
                       "diligenciada": False, "fecha": "", "responsable": ""},
            "cobertura": {"archivo": "homologacion/cobertura.csv",
                          "diligenciada": False, "fecha": "", "responsable": ""},
        },
        "decisiones": [],
    }


class PruebaValidacionManifiesto(unittest.TestCase):
    def _claves(self, datos, severidad: str) -> set[str]:
        return {h.clave for h in m00c.validar_manifiesto(datos)
                if h.severidad == severidad}

    def test_el_manifiesto_del_repositorio_es_estructuralmente_valido(self) -> None:
        datos = leer_yaml(
            _RAIZ_REPO / "data" / "00_insumos_usuario" / "MANIFIESTO.yaml"
        )
        bloqueantes = [h for h in m00c.validar_manifiesto(datos) if h.es_bloqueante]
        self.assertEqual(bloqueantes, [], "\n".join(str(h) for h in bloqueantes))

    def test_manifiesto_minimo_valido(self) -> None:
        self.assertEqual(
            self._claves(_manifiesto_valido(), esquema.BLOQUEANTE), set()
        )

    def test_rechaza_bloque_ausente(self) -> None:
        datos = _manifiesto_valido()
        del datos["homologacion"]
        self.assertIn("homologacion", self._claves(datos, esquema.BLOQUEANTE))

    def test_rechaza_tipo_no_admitido(self) -> None:
        datos = _manifiesto_valido()
        datos["suelos"]["tipo"] = "geopackage"
        self.assertIn("suelos.tipo", self._claves(datos, esquema.BLOQUEANTE))

    def test_rechaza_perfil_no_admitido(self) -> None:
        datos = _manifiesto_valido()
        datos["suelos"]["perfil"] = "Z"
        self.assertIn("suelos.perfil", self._claves(datos, esquema.BLOQUEANTE))

    def test_perfil_d_exige_raster(self) -> None:
        datos = _manifiesto_valido()
        datos["suelos"]["perfil"] = "D"
        self.assertIn("suelos.perfil", self._claves(datos, esquema.BLOQUEANTE))

    def test_perfil_vectorial_no_admite_raster(self) -> None:
        datos = _manifiesto_valido()
        datos["suelos"]["tipo"] = "raster"
        datos["suelos"]["perfil"] = "B"
        self.assertIn("suelos.perfil", self._claves(datos, esquema.BLOQUEANTE))

    def test_shapefile_exige_campo_clave(self) -> None:
        datos = _manifiesto_valido()
        datos["suelos"]["campo_clave"] = ""
        self.assertIn("suelos.campo_clave", self._claves(datos, esquema.BLOQUEANTE))

    def test_advierte_metadatos_sin_diligenciar(self) -> None:
        datos = _manifiesto_valido()
        datos["suelos"]["fuente"] = ""
        self.assertIn("suelos.fuente", self._claves(datos, esquema.ADVERTENCIA))

    def test_caudal_sin_seccion_es_bloqueante(self) -> None:
        datos = _manifiesto_valido()
        datos["caudales"] = {
            "aportado": True, "origen": "ideam", "archivos": ["q.csv"],
            "estaciones": [{
                "codigo": "2120", "nombre": "X", "latitud": 4.6, "longitud": -74.1,
                "tipo_dato": "caudal", "periodo_inicio": "1990",
                "periodo_fin": "2020", "seccion_aforo": "", "fuente": "",
            }],
        }
        self.assertIn("caudales.estaciones[0].seccion_aforo",
                      self._claves(datos, esquema.BLOQUEANTE))

    def test_caudal_sin_ubicacion_es_bloqueante(self) -> None:
        datos = _manifiesto_valido()
        datos["caudales"] = {
            "aportado": True, "origen": "ideam", "archivos": ["q.csv"],
            "estaciones": [{
                "codigo": "2120", "latitud": None, "longitud": None,
                "tipo_dato": "caudal", "seccion_aforo": "puente",
            }],
        }
        self.assertIn("caudales.estaciones[0]",
                      self._claves(datos, esquema.BLOQUEANTE))

    def test_advierte_decision_sin_justificacion(self) -> None:
        datos = _manifiesto_valido()
        datos["decisiones"] = [{
            "tema": "versión de QGIS", "valor": "4.2.0",
            "justificacion": "", "fecha": "2026-08-05", "responsable": "",
        }]
        advertencias = self._claves(datos, esquema.ADVERTENCIA)
        self.assertIn("decisiones[0].justificacion", advertencias)
        self.assertIn("decisiones[0].responsable", advertencias)


# =============================================================================
# Doctrina de obligatoriedad
# =============================================================================
class PruebaObligatoriedad(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = cargar(raiz=_RAIZ_REPO)

    def test_suelos_ausente_es_bloqueante(self) -> None:
        datos = _manifiesto_valido()
        datos["suelos"]["aportado"] = False
        hallazgos = m00c.verificar_obligatoriedad(datos, self.cfg)
        self.assertTrue(any(h.es_bloqueante and h.clave == "suelos.aportado"
                            for h in hallazgos))

    def test_cobertura_ausente_es_advertencia(self) -> None:
        hallazgos = m00c.verificar_obligatoriedad(_manifiesto_valido(), self.cfg)
        self.assertTrue(any(h.severidad == esquema.ADVERTENCIA
                            and h.clave == "cobertura.aportado"
                            for h in hallazgos))

    def test_caudales_ausentes_son_informativos(self) -> None:
        hallazgos = m00c.verificar_obligatoriedad(_manifiesto_valido(), self.cfg)
        self.assertTrue(any(h.severidad == esquema.INFORMATIVO
                            and h.clave == "caudales.aportado"
                            for h in hallazgos))


# =============================================================================
# Verificación de archivos y homologación
# =============================================================================
class PruebaVerificacionInsumo(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        (self.temporal / "suelos").mkdir()
        (self.temporal / "homologacion").mkdir()
        self.base = self.temporal / "suelos" / "ucs"

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _bloque(self, **cambios) -> dict:
        bloque = deepcopy(_manifiesto_valido()["suelos"])
        bloque["archivo"] = "suelos/ucs.shp"
        bloque.update(cambios)
        return bloque

    def test_insumo_correcto_no_produce_bloqueantes(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("A", "B"))
        hallazgos, info = m00c.verificar_insumo(
            self._bloque(), "suelos", self.temporal, "EPSG:9377"
        )
        self.assertIsNotNone(info)
        self.assertEqual([h for h in hallazgos if h.es_bloqueante], [])

    def test_archivo_inexistente_es_bloqueante(self) -> None:
        hallazgos, info = m00c.verificar_insumo(
            self._bloque(), "suelos", self.temporal, "EPSG:9377"
        )
        self.assertIsNone(info)
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))

    def test_campo_clave_inexistente_es_bloqueante(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("A",))
        hallazgos, _ = m00c.verificar_insumo(
            self._bloque(campo_clave="TEXTURA"), "suelos", self.temporal,
            "EPSG:9377",
        )
        self.assertTrue(any(h.es_bloqueante and h.clave == "suelos.campo_clave"
                            for h in hallazgos))

    def test_crs_distinto_del_de_calculo_es_advertencia(self) -> None:
        escribir_shapefile_poligono(
            self.base, "UCS", ("A",),
            wkt='PROJCS["Bogota",AUTHORITY["EPSG","3116"]]',
        )
        hallazgos, _ = m00c.verificar_insumo(
            self._bloque(crs_declarado="EPSG:3116"), "suelos", self.temporal,
            "EPSG:9377",
        )
        self.assertTrue(any(h.severidad == esquema.ADVERTENCIA for h in hallazgos))

    def test_crs_declarado_que_no_coincide_con_el_prj(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("A",))
        hallazgos, _ = m00c.verificar_insumo(
            self._bloque(crs_declarado="EPSG:3116"), "suelos", self.temporal,
            "EPSG:9377",
        )
        self.assertTrue(any(h.clave == "suelos.crs_declarado" for h in hallazgos))

    def test_perfil_a_con_valores_ajenos_es_advertencia(self) -> None:
        escribir_shapefile_poligono(self.base, "UCS", ("A", "ZZ"))
        hallazgos, _ = m00c.verificar_insumo(
            self._bloque(perfil="A"), "suelos", self.temporal, "EPSG:9377"
        )
        self.assertTrue(any(h.severidad == esquema.ADVERTENCIA
                            and h.clave == "suelos.perfil" for h in hallazgos))

    def test_raster_advierte_que_no_se_puede_inspeccionar(self) -> None:
        destino = self.temporal / "suelos" / "suelos.tif"
        destino.write_bytes(b"II*\x00")
        hallazgos, info = m00c.verificar_insumo(
            self._bloque(archivo="suelos/suelos.tif", tipo="raster", perfil="D"),
            "suelos", self.temporal, "EPSG:9377",
        )
        self.assertIsNone(info)
        self.assertTrue(any(h.severidad == esquema.ADVERTENCIA for h in hallazgos))


class PruebaHomologacion(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        (self.temporal / "suelos").mkdir()
        (self.temporal / "homologacion").mkdir()
        self.base = self.temporal / "suelos" / "ucs"
        escribir_shapefile_poligono(self.base, "UCS", ("MQC", "RQV"))
        self.info = shapefile.leer_shapefile(self.base.with_suffix(".shp"))
        self.tabla = self.temporal / "homologacion" / "suelos.csv"
        self.resultado = m00c.ResultadoVerificacion()

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _gestionar(self, diligenciada: bool = False, generar: bool = True):
        return m00c.gestionar_homologacion(
            nombre="suelos",
            bloque_insumo={
                "aportado": True, "archivo": "suelos/ucs.shp",
                "tipo": "shapefile", "campo_clave": "UCS",
            },
            bloque_homologacion={
                "archivo": "homologacion/suelos.csv",
                "diligenciada": diligenciada,
            },
            info=self.info,
            directorio_manifiesto=self.temporal,
            delimitador=";",
            generar=generar,
            maximo_valores=500,
            resultado=self.resultado,
        )

    def _escribir_tabla(self, filas: str) -> None:
        self.tabla.write_text(
            "valor_origen;grupo_hidrologico;observaciones\n" + filas,
            encoding="utf-8-sig",
        )

    def test_genera_la_tabla_y_detiene(self) -> None:
        hallazgos = self._gestionar()
        self.assertTrue(self.tabla.is_file())
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))
        contenido = self.tabla.read_text(encoding="utf-8-sig")
        self.assertIn("MQC", contenido)
        self.assertIn("RQV", contenido)
        self.assertIn("grupo_hidrologico", contenido)
        self.assertEqual(len(self.resultado.tablas_generadas), 1)

    def test_tabla_diligenciada_no_produce_bloqueantes(self) -> None:
        self._escribir_tabla("MQC;C;\nRQV;B;\n")
        hallazgos = self._gestionar(diligenciada=True)
        self.assertEqual([h for h in hallazgos if h.es_bloqueante], [])

    def test_valores_sin_homologar_detienen(self) -> None:
        self._escribir_tabla("MQC;C;\nRQV;;\n")
        hallazgos = self._gestionar()
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))

    def test_valor_nuevo_en_el_insumo_detiene(self) -> None:
        self._escribir_tabla("MQC;C;\n")
        hallazgos = self._gestionar()
        self.assertTrue(any(h.es_bloqueante and "RQV" in h.mensaje
                            for h in hallazgos))

    def test_valor_obsoleto_es_advertencia(self) -> None:
        self._escribir_tabla("MQC;C;\nRQV;B;\nXXX;A;\n")
        hallazgos = self._gestionar(diligenciada=True)
        self.assertTrue(any(h.severidad == esquema.ADVERTENCIA
                            and "XXX" in h.mensaje for h in hallazgos))

    def test_grupo_hidrologico_invalido_detiene(self) -> None:
        self._escribir_tabla("MQC;E;\nRQV;B;\n")
        hallazgos = self._gestionar(diligenciada=True)
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))

    def test_columna_faltante_detiene(self) -> None:
        self.tabla.write_text("valor;grupo\nMQC;C\n", encoding="utf-8-sig")
        hallazgos = self._gestionar()
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))

    def test_incoherencia_con_la_marca_diligenciada(self) -> None:
        self._escribir_tabla("MQC;C;\nRQV;B;\n")
        hallazgos = self._gestionar(diligenciada=False)
        self.assertTrue(any(h.clave == "homologacion.suelos.diligenciada"
                            for h in hallazgos))

    def test_sin_generar_y_sin_tabla_detiene(self) -> None:
        hallazgos = self._gestionar(generar=False)
        self.assertFalse(self.tabla.exists())
        self.assertTrue(any(h.es_bloqueante for h in hallazgos))


# =============================================================================
# Escala y área
# =============================================================================
class PruebaEscala(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.cuenca = self.temporal / "cuenca_preliminar"
        # Cuadrado de 3000 m de lado: 9 km2.
        escribir_shapefile_poligono(self.cuenca, "ID", ("1",), lado=3000.0)
        self.tabla = _RAIZ_REPO / "data" / "referencia" / "escala_area_suelos.csv"

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_denominador_escala(self) -> None:
        self.assertEqual(m00c.denominador_escala("1:25000"), 25000)
        self.assertEqual(m00c.denominador_escala("1:25.000"), 25000)
        self.assertEqual(m00c.denominador_escala("100000"), 100000)
        self.assertIsNone(m00c.denominador_escala(""))
        self.assertIsNone(m00c.denominador_escala("detallada"))

    def test_escala_demasiado_gruesa_para_el_area(self) -> None:
        hallazgos = m00c.verificar_escala(
            {"aportado": True, "escala": "1:100000"},
            self.tabla, self.cuenca.with_suffix(".shp"), "EPSG:9377",
        )
        self.assertTrue(any(h.severidad == esquema.ADVERTENCIA
                            and "km2" in h.mensaje for h in hallazgos))

    def test_escala_compatible(self) -> None:
        hallazgos = m00c.verificar_escala(
            {"aportado": True, "escala": "1:10000"},
            self.tabla, self.cuenca.with_suffix(".shp"), "EPSG:9377",
        )
        self.assertTrue(any(h.severidad == esquema.INFORMATIVO
                            and "compatible" in h.mensaje for h in hallazgos))

    def test_advierte_que_la_tabla_es_orientativa(self) -> None:
        hallazgos = m00c.verificar_escala(
            {"aportado": True, "escala": "1:10000"},
            self.tabla, self.cuenca.with_suffix(".shp"), "EPSG:9377",
        )
        self.assertTrue(any(h.clave == "insumos_usuario.tabla_escala_area"
                            for h in hallazgos))

    def test_sin_cuenca_la_verificacion_queda_diferida(self) -> None:
        hallazgos = m00c.verificar_escala(
            {"aportado": True, "escala": "1:25000"},
            self.tabla, self.temporal / "no_existe.shp", "EPSG:9377",
        )
        self.assertTrue(any(h.severidad == esquema.INFORMATIVO
                            and "diferida" in h.mensaje for h in hallazgos))

    def test_sin_escala_declarada_es_advertencia(self) -> None:
        hallazgos = m00c.verificar_escala(
            {"aportado": True, "escala": ""},
            self.tabla, self.cuenca.with_suffix(".shp"), "EPSG:9377",
        )
        self.assertTrue(any(h.severidad == esquema.ADVERTENCIA for h in hallazgos))

    def test_cuenca_en_otro_crs_omite_la_verificacion(self) -> None:
        otra = self.temporal / "otra"
        escribir_shapefile_poligono(
            otra, "ID", ("1",), lado=1.0,
            wkt='GEOGCS["WGS 84",AUTHORITY["EPSG","4326"]]',
        )
        hallazgos = m00c.verificar_escala(
            {"aportado": True, "escala": "1:25000"},
            self.tabla, otra.with_suffix(".shp"), "EPSG:9377",
        )
        self.assertTrue(any("no sería métrica" in h.mensaje for h in hallazgos))


class PruebaModuloCompleto(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_ejecucion_sobre_el_repositorio(self) -> None:
        """
        El manifiesto declara CAPA DE BASE para suelos, de modo que no aportar
        información propia ya no detiene el módulo.

        No aportarla deja de ser bloqueante porque existe una fuente que sirve
        siempre, pero SÍ se advierte: un número de curva derivado de una capa
        global no vale lo mismo que uno derivado de un estudio de suelos del
        proyecto, y el informe debe poder distinguirlos.
        """
        destino_json = self.temporal / "reporte.json"
        codigo, hallazgos = m00c.ejecutar(
            raiz=_RAIZ_REPO, ruta_json=destino_json, consola=False
        )
        self.assertEqual(codigo, m00c.SALIDA_CORRECTA)
        base = [h for h in hallazgos if h.clave == "suelos.usa_capa_base"]
        self.assertTrue(base, "debe advertir que usa la capa de base")
        self.assertFalse(base[0].es_bloqueante)
        # Y no debe quedar el bloqueante antiguo.
        self.assertFalse(any(h.clave == "suelos.aportado" and h.es_bloqueante
                             for h in hallazgos))
        self.assertTrue(destino_json.is_file())

    def test_sin_capa_de_base_vuelve_a_ser_bloqueante(self) -> None:
        """
        La capa de base solo exime si EXISTE. Declararla y no tenerla en disco
        deja el estudio sin grupo hidrológico, y eso sí detiene.
        """
        hallazgos = m00c.verificar_obligatoriedad(
            {"suelos": {"aportado": False, "usa_capa_base": True,
                        "base_archivo": "suelos/no_existe.tif"}},
            _CFG,
        )
        bloqueantes = [h for h in hallazgos if h.es_bloqueante]
        self.assertTrue(bloqueantes)
        self.assertIn("NO se encuentra", bloqueantes[0].mensaje)


if __name__ == "__main__":
    unittest.main(verbosity=2)

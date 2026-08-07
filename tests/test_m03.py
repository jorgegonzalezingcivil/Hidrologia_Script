# -*- coding: utf-8 -*-
"""
Pruebas del M03 y de las piezas nuevas de comun: geometría, lectura completa de
registros y escritura de shapefiles de puntos.

Todo corre bajo el venv, sin QGIS: es justamente lo que el M03 debe demostrar.

    python tests/test_m03.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M03_seleccion_estaciones as m03  # noqa: E402
from comun import esquema, geometria, shapefile  # noqa: E402
from comun.campos import CampoSalida  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)
_CATALOGO = _CFG.ruta_de("estaciones.catalogo")
HAY_CATALOGO = _CATALOGO.is_file()

_CUADRADO = "POLYGON((-75 4,-73 4,-73 6,-75 6,-75 4))"
_CON_HUECO = (
    "POLYGON((-75 4,-73 4,-73 6,-75 6,-75 4),"
    "(-74.5 4.5,-73.5 4.5,-73.5 5.5,-74.5 5.5,-74.5 4.5))"
)


# =============================================================================
# Geometría
# =============================================================================
class PruebaGeometria(unittest.TestCase):
    def test_lee_un_poligono_simple(self) -> None:
        poligonos = geometria.poligonos_de_wkt(_CUADRADO)
        self.assertEqual(len(poligonos), 1)
        self.assertEqual(len(poligonos[0]), 1)
        self.assertEqual(len(poligonos[0][0]), 5)

    def test_lee_un_poligono_con_hueco(self) -> None:
        poligonos = geometria.poligonos_de_wkt(_CON_HUECO)
        self.assertEqual(len(poligonos[0]), 2)

    def test_lee_un_multipoligono(self) -> None:
        wkt = ("MULTIPOLYGON(((-75 4,-74 4,-74 5,-75 5,-75 4)),"
               "((-73 4,-72 4,-72 5,-73 5,-73 4)))")
        poligonos = geometria.poligonos_de_wkt(wkt)
        self.assertEqual(len(poligonos), 2)
        self.assertEqual(len(poligonos[0]), 1)
        self.assertEqual(len(poligonos[1]), 1)

    def test_rechaza_un_tipo_que_no_es_poligonal(self) -> None:
        with self.assertRaises(ErrorFormato):
            geometria.poligonos_de_wkt("POINT(-74 4.8)")

    def test_rechaza_wkt_vacio(self) -> None:
        with self.assertRaises(ErrorFormato):
            geometria.poligonos_de_wkt("")

    def test_punto_dentro_y_fuera(self) -> None:
        poligono = geometria.poligonos_de_wkt(_CUADRADO)[0]
        self.assertTrue(geometria.punto_en_poligono(-74.0, 5.0, poligono))
        self.assertFalse(geometria.punto_en_poligono(-72.0, 5.0, poligono))
        self.assertFalse(geometria.punto_en_poligono(-74.0, 3.0, poligono))

    def test_el_hueco_excluye(self) -> None:
        poligono = geometria.poligonos_de_wkt(_CON_HUECO)[0]
        self.assertTrue(geometria.punto_en_poligono(-74.8, 4.2, poligono))
        self.assertFalse(geometria.punto_en_poligono(-74.0, 5.0, poligono))

    def test_punto_en_alguno(self) -> None:
        wkt = ("MULTIPOLYGON(((-75 4,-74 4,-74 5,-75 5,-75 4)),"
               "((-73 4,-72 4,-72 5,-73 5,-73 4)))")
        poligonos = geometria.poligonos_de_wkt(wkt)
        self.assertTrue(geometria.punto_en_alguno(-72.5, 4.5, poligonos))
        self.assertFalse(geometria.punto_en_alguno(-73.5, 4.5, poligonos))

    def test_envolvente(self) -> None:
        poligonos = geometria.poligonos_de_wkt(_CUADRADO)
        self.assertEqual(geometria.envolvente(poligonos), (-75.0, 4.0, -73.0, 6.0))


# =============================================================================
# Escritura y relectura de shapefiles de puntos
# =============================================================================
class PruebaEscrituraPuntos(unittest.TestCase):
    CAMPOS = (
        CampoSalida("codigo", "Código", "texto", 12),
        CampoSalida("nombre", "Nombre", "texto", 40),
        CampoSalida("altitud", "Altitud", "decimal", 12, 2, "m"),
        CampoSalida("orden", "Orden", "entero", 6),
    )

    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _escribir(self, puntos, valores):
        return shapefile.escribir_puntos(
            destino=self.temporal / "estaciones.shp",
            puntos=puntos, campos=self.CAMPOS, valores=valores,
            wkt_crs='GEOGCS["WGS 84",AUTHORITY["EPSG","4326"]]',
        )

    def test_ida_y_vuelta(self) -> None:
        puntos = [(-74.05, 4.83), (-74.10, 4.90)]
        valores = [
            {"codigo": "2120001", "nombre": "Bogotá", "altitud": 2600.5,
             "orden": 1},
            {"codigo": "2120002", "nombre": "Chía", "altitud": 2560.25,
             "orden": 2},
        ]
        destino = self._escribir(puntos, valores)

        info = shapefile.leer_shapefile(destino)
        self.assertEqual(info.tipo_geometria, "Punto")
        self.assertEqual(info.n_registros, 2)
        self.assertEqual(info.crs_epsg, "EPSG:4326")
        self.assertEqual(info.componentes_faltantes, ())
        self.assertEqual(
            info.nombres_campos, ("codigo", "nombre", "altitud", "orden")
        )

        leidos = list(shapefile.leer_registros(destino))
        self.assertEqual(leidos[0]["codigo"], "2120001")
        self.assertEqual(leidos[1]["nombre"], "Chía")
        self.assertAlmostEqual(float(leidos[0]["altitud"]), 2600.5, places=2)
        self.assertEqual(leidos[1]["orden"], "2")

    def test_la_extension_corresponde_a_los_puntos(self) -> None:
        destino = self._escribir(
            [(-74.05, 4.83), (-73.90, 5.10)],
            [{"codigo": "a"}, {"codigo": "b"}],
        )
        x_min, y_min, x_max, y_max = shapefile.leer_shapefile(destino).extension
        self.assertAlmostEqual(x_min, -74.05, places=6)
        self.assertAlmostEqual(y_min, 4.83, places=6)
        self.assertAlmostEqual(x_max, -73.90, places=6)
        self.assertAlmostEqual(y_max, 5.10, places=6)

    def test_capa_vacia(self) -> None:
        destino = self._escribir([], [])
        info = shapefile.leer_shapefile(destino)
        self.assertEqual(info.n_registros, 0)
        self.assertEqual(list(shapefile.leer_registros(destino)), [])

    def test_listas_desparejas_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            self._escribir([(-74.0, 4.0)], [])

    def test_campo_demasiado_largo_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            shapefile.escribir_puntos(
                destino=self.temporal / "x.shp", puntos=[(-74.0, 4.0)],
                campos=[CampoSalida("identificador_largo", "X", "texto", 10)],
                valores=[{"identificador_largo": "a"}], wkt_crs="",
            )

    def test_el_texto_se_trunca_y_el_numero_desbordado_se_marca(self) -> None:
        destino = shapefile.escribir_puntos(
            destino=self.temporal / "y.shp", puntos=[(-74.0, 4.0)],
            campos=[CampoSalida("corto", "Corto", "texto", 5),
                    CampoSalida("num", "Num", "entero", 3)],
            valores=[{"corto": "abcdefghij", "num": 1234567}],
            wkt_crs="",
        )
        fila = list(shapefile.leer_registros(destino))[0]
        self.assertEqual(fila["corto"], "abcde")
        self.assertEqual(fila["num"], "***")


# =============================================================================
# Funciones puras del M03
# =============================================================================
class PruebaFuncionesM03(unittest.TestCase):
    CATEGORIAS = {
        "precipitacion": ["AM", "CO", "CP", "PG", "PM", "SP", "SS"],
        "temperatura": ["AM", "CO", "CP", "SP", "SS"],
        "caudal": ["LG", "LM"],
    }

    def test_a_decimal(self) -> None:
        self.assertEqual(m03.a_decimal("2600.5"), 2600.5)
        self.assertEqual(m03.a_decimal("2600,5"), 2600.5)
        self.assertEqual(m03.a_decimal("  0 "), 0.0)
        self.assertIsNone(m03.a_decimal(""))
        self.assertIsNone(m03.a_decimal("sin dato"))
        self.assertIsNone(m03.a_decimal(None))

    def test_una_categoria_puede_servir_a_varias_variables(self) -> None:
        self.assertEqual(
            m03.variables_de_categoria("CP", self.CATEGORIAS),
            ("precipitacion", "temperatura"),
        )

    def test_categoria_de_una_sola_variable(self) -> None:
        self.assertEqual(
            m03.variables_de_categoria("LG", self.CATEGORIAS), ("caudal",)
        )

    def test_categoria_sin_variable(self) -> None:
        self.assertEqual(m03.variables_de_categoria("RS", self.CATEGORIAS), ())
        self.assertEqual(m03.variables_de_categoria("", self.CATEGORIAS), ())

    def test_categoria_sin_distinguir_mayusculas(self) -> None:
        self.assertEqual(
            m03.variables_de_categoria("cp", self.CATEGORIAS),
            ("precipitacion", "temperatura"),
        )

    def test_advierte_categorias_ausentes_del_catalogo(self) -> None:
        hallazgos = m03.verificar_categorias(
            {"caudal": ["LG", "LM", "RM"]}, {"LG", "LM"}
        )
        self.assertEqual(len(hallazgos), 1)
        self.assertEqual(hallazgos[0].severidad, esquema.ADVERTENCIA)
        self.assertIn("RM", hallazgos[0].mensaje)

    def test_no_advierte_si_todas_existen(self) -> None:
        self.assertEqual(
            m03.verificar_categorias({"caudal": ["LG"]}, {"LG", "LM"}), []
        )


class PruebaClasificacion(unittest.TestCase):
    MAPA = {
        "codigo": "CODIGO", "nombre": "nombre", "categoria": "CATEGORIA",
        "categoria_desc": "d_CATEGORI", "estado": "d_ESTADO",
        "entidad": "ENTIDAD", "altitud": "altitud", "latitud": "latitud",
        "longitud": "longitud", "corriente": "d_CORRIENT",
        "departamento": "d_DEPARTAM", "municipio": "d_MUNICIPI",
        "subzona": "d_SUBZONA_", "fecha_instalacion": "FECHA_INST",
        "fecha_suspension": "FECHA_SUSP",
    }
    CATEGORIAS = {"precipitacion": ["PM", "CO"], "caudal": ["LG"]}

    def _registro(self, codigo, categoria, lat, lon, estado="Activa"):
        base = {campo: "" for campo in self.MAPA.values()}
        base.update({
            "CODIGO": codigo, "CATEGORIA": categoria, "d_ESTADO": estado,
            "latitud": str(lat), "longitud": str(lon), "nombre": f"E{codigo}",
        })
        return base

    def _clasificar(self, registros):
        resultado = m03.ResultadoM03()
        m03.clasificar(
            registros=registros, mapa_campos=self.MAPA,
            categorias_por_variable=self.CATEGORIAS,
            poligonos=geometria.poligonos_de_wkt(_CUADRADO),
            resultado=resultado,
        )
        return resultado

    def test_selecciona_dentro_del_area_con_categoria_aplicable(self) -> None:
        r = self._clasificar([self._registro("1", "PM", 5.0, -74.0)])
        self.assertEqual(len(r.seleccionadas), 1)
        self.assertEqual(r.seleccionadas[0].variables, ("precipitacion",))
        self.assertEqual(r.por_variable["precipitacion"], 1)

    def test_descarta_fuera_del_area_solo_si_esta_en_la_envolvente(self) -> None:
        r = self._clasificar([self._registro("2", "PM", 40.0, -3.0)])
        self.assertEqual(r.seleccionadas, [])
        self.assertEqual(r.descartadas, [])  # lejos: no ensucia el inventario
        self.assertEqual(r.en_area, 0)

    def test_descarta_por_categoria_dentro_del_area(self) -> None:
        r = self._clasificar([self._registro("3", "RS", 5.0, -74.0)])
        self.assertEqual(r.seleccionadas, [])
        self.assertEqual(len(r.descartadas), 1)
        self.assertEqual(r.descartadas[0].motivo, m03.MOTIVO_CATEGORIA)
        self.assertEqual(r.en_area, 1)

    def test_descarta_sin_coordenadas(self) -> None:
        r = self._clasificar([self._registro("4", "PM", "", "")])
        self.assertEqual(len(r.descartadas), 1)
        self.assertEqual(r.descartadas[0].motivo, m03.MOTIVO_SIN_COORDENADAS)
        self.assertEqual(r.con_coordenadas, 0)

    def test_las_suspendidas_se_conservan(self) -> None:
        r = self._clasificar([
            self._registro("5", "PM", 5.0, -74.0, estado="Suspendida")
        ])
        self.assertEqual(len(r.seleccionadas), 1)
        self.assertEqual(r.por_estado["Suspendida"], 1)

    def test_conteos_globales(self) -> None:
        r = self._clasificar([
            self._registro("1", "PM", 5.0, -74.0),
            self._registro("2", "LG", 5.1, -74.1),
            self._registro("3", "RS", 5.2, -74.2),
            self._registro("4", "PM", 40.0, -3.0),
        ])
        self.assertEqual(r.total_catalogo, 4)
        self.assertEqual(r.con_coordenadas, 4)
        self.assertEqual(r.en_area, 3)
        self.assertEqual(len(r.seleccionadas), 2)
        self.assertEqual(r.por_variable, {"precipitacion": 1, "caudal": 1})


class PruebaAptitudCalibracion(unittest.TestCase):
    """
    La cota no decide por si sola en ninguno de los dos sentidos.

    Regresion medida en el proyecto: la estacion 2120700146 aparecia 15 m por
    DEBAJO del punto de descarga y estaba 6,26 km AGUAS ARRIBA sobre el mismo
    cauce del Rio Bogota. El criterio anterior la descartaba sin apelacion. En
    terreno plano el desnivel real del rio es menor que la incertidumbre
    vertical de un DEM de radar, de modo que el signo de la diferencia es ruido.
    """

    # Punto de descarga del estudio: 2568 m, sobre el Rio Bogota en la sabana.
    COTA_PUNTO = 2568.0
    LON, LAT = -74.15, 4.75

    def _evaluar(self, altitud, categoria="LG", banda=30.0):
        estacion = m03.Estacion(
            valores={"categoria": categoria, "altitud": altitud},
            latitud=self.LAT + 0.01, longitud=self.LON + 0.01,
        )
        return m03.evaluar_aptitud_calibracion(
            estacion, self.LON, self.LAT, self.COTA_PUNTO, banda)

    def test_el_caso_real_cae_dentro_de_la_banda(self) -> None:
        # 2553 m frente a 2568: 15 m por debajo, y aguas arriba en realidad.
        dif, _, apta = self._evaluar(2553.0)
        self.assertAlmostEqual(dif, -15.0)
        self.assertEqual(apta, m03.DUDOSA_BANDA)
        self.assertNotEqual(apta, m03.NO_APTA_COTA)

    def test_por_encima_se_remite_a_la_red(self) -> None:
        _, _, apta = self._evaluar(2598.0)
        self.assertEqual(apta, m03.DUDOSA)

    def test_muy_por_debajo_se_descarta(self) -> None:
        # 614 m: bajo el Salto del Tequendama, sin ambiguedad posible.
        _, _, apta = self._evaluar(614.0)
        self.assertEqual(apta, m03.NO_APTA_COTA)

    def test_el_borde_de_la_banda_todavia_no_descarta(self) -> None:
        self.assertEqual(self._evaluar(self.COTA_PUNTO - 30.0)[2],
                         m03.DUDOSA_BANDA)
        self.assertEqual(self._evaluar(self.COTA_PUNTO - 30.001)[2],
                         m03.NO_APTA_COTA)

    def test_una_banda_nula_recupera_el_criterio_estricto(self) -> None:
        self.assertEqual(self._evaluar(2553.0, banda=0.0)[2], m03.NO_APTA_COTA)

    def test_la_banda_negativa_no_invierte_el_criterio(self) -> None:
        # Un valor mal declarado no debe ampliar el descarte en silencio.
        self.assertEqual(self._evaluar(2553.0, banda=-30.0)[2],
                         m03.DUDOSA_BANDA)

    def test_la_categoria_manda_sobre_la_cota(self) -> None:
        # Una pluviometrica no calibra aunque este aguas arriba.
        _, _, apta = self._evaluar(2600.0, categoria="PM")
        self.assertEqual(apta, m03.NO_APTA_TIPO)

    def test_sin_cota_del_punto_no_se_descarta_nada(self) -> None:
        estacion = m03.Estacion(
            valores={"categoria": "LG", "altitud": 2553.0},
            latitud=self.LAT, longitud=self.LON,
        )
        _, _, apta = m03.evaluar_aptitud_calibracion(
            estacion, self.LON, self.LAT, None, 30.0)
        self.assertNotEqual(apta, m03.NO_APTA_COTA)

    def test_la_banda_configurada_es_utilizable(self) -> None:
        banda = _CFG.obtener("estaciones.banda_cota_m")
        self.assertIsInstance(banda, (int, float))
        self.assertGreater(banda, 0)


class PruebaLecturaArea(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.reporte = self.temporal / "M02_delimitacion.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_lee_la_geometria_pedida(self) -> None:
        self.reporte.write_text(json.dumps({
            "geometrias_epsg4326": {"area_estaciones": _CUADRADO}
        }), encoding="utf-8")
        poligonos = m03.leer_area(self.reporte, "area_estaciones")
        self.assertEqual(len(poligonos), 1)

    def test_reporte_ausente_es_error(self) -> None:
        with self.assertRaises(ErrorFormato) as ctx:
            m03.leer_area(self.temporal / "no_existe.json")
        self.assertIn("M02", str(ctx.exception))

    def test_reporte_sin_geometrias_es_error(self) -> None:
        self.reporte.write_text(json.dumps({"modulo": "M02"}), encoding="utf-8")
        with self.assertRaises(ErrorFormato):
            m03.leer_area(self.reporte)

    def test_geometria_inexistente_lista_las_disponibles(self) -> None:
        self.reporte.write_text(json.dumps({
            "geometrias_epsg4326": {"cuenca_preliminar": _CUADRADO}
        }), encoding="utf-8")
        with self.assertRaises(ErrorFormato) as ctx:
            m03.leer_area(self.reporte, "area_estaciones")
        self.assertIn("cuenca_preliminar", str(ctx.exception))


@unittest.skipUnless(HAY_CATALOGO, "requiere el catálogo CNE")
class PruebaCatalogoReal(unittest.TestCase):
    def test_los_campos_declarados_existen(self) -> None:
        info = shapefile.leer_shapefile(_CATALOGO)
        declarados = dict(_CFG.obtener("estaciones.campos"))
        faltantes = [n for n in declarados.values() if not info.tiene_campo(n)]
        self.assertEqual(faltantes, [])

    def test_el_catalogo_tiene_coordenadas_utilizables(self) -> None:
        mapa = dict(_CFG.obtener("estaciones.campos"))
        registros = shapefile.leer_registros(
            _CATALOGO, [mapa["latitud"], mapa["longitud"]]
        )
        con_coordenadas = 0
        for indice, fila in enumerate(registros):
            if indice >= 200:
                break
            if m03.a_decimal(fila[mapa["latitud"]]) is not None:
                con_coordenadas += 1
        self.assertGreater(con_coordenadas, 150)


if __name__ == "__main__":
    unittest.main(verbosity=2)

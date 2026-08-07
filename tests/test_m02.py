# -*- coding: utf-8 -*-
"""
Pruebas del M02: adaptador de ASF, selección de cobertura y delimitación.

Las que requieren QGIS y GRASS se omiten de forma automática bajo el venv. La
cadena hidrológica se verifica sobre un DEM sintético, sin descargar nada.

    python tests/test_m02.py
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" tests/test_m02.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import M02_dem_delimitacion as m02  # noqa: E402
from comun import asf  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato  # noqa: E402

try:
    import qgis.core  # noqa: F401

    HAY_QGIS = True
except Exception:
    HAY_QGIS = False

_CFG = cargar(raiz=_RAIZ_REPO)


def tearDownModule() -> None:
    if HAY_QGIS:
        import sig

        sig.finalizar_qgis()


def _escena(orbita, marco, wkt, mb=250.0, proceso="2015-06-19T00:00:00Z"):
    return asf.EscenaASF(
        identificador=f"S{orbita}_{marco}",
        nombre_archivo=f"S{orbita}_{marco}.zip",
        url=f"https://ejemplo/{orbita}_{marco}.zip",
        md5="", tamano_mb=mb, huella_wkt=wkt,
        orbita_relativa=orbita, marco=marco, modo_haz="FBD",
        nivel="RTC_HI_RES", fecha_escena="2010-01-01T00:00:00Z",
        fecha_proceso=proceso,
    )


def _cuadrado(x0, y0, lado=1.0) -> str:
    return (f"POLYGON(({x0} {y0},{x0 + lado} {y0},{x0 + lado} {y0 + lado},"
            f"{x0} {y0 + lado},{x0} {y0}))")


# =============================================================================
# Adaptador de ASF
# =============================================================================
class PruebaAdaptadorASF(unittest.TestCase):
    def test_deduplica_por_huella_conservando_el_proceso_mas_reciente(self) -> None:
        escenas = [
            _escena(142, 100, _cuadrado(0, 0), proceso="2014-01-01T00:00:00Z"),
            _escena(142, 100, _cuadrado(0, 0), proceso="2015-06-19T00:00:00Z"),
            _escena(142, 110, _cuadrado(1, 0)),
            _escena(150, 100, _cuadrado(0, 1)),
        ]
        unicas = asf.deduplicar_por_huella(escenas)
        self.assertEqual(len(unicas), 3)
        primera = [e for e in unicas if e.huella == (142, 100)][0]
        self.assertEqual(primera.fecha_proceso, "2015-06-19T00:00:00Z")

    def test_la_deduplicacion_es_determinista(self) -> None:
        escenas = [_escena(o, m, _cuadrado(o, m))
                   for o in (150, 142) for m in (110, 100)]
        primera = [e.huella for e in asf.deduplicar_por_huella(escenas)]
        segunda = [e.huella for e in asf.deduplicar_por_huella(list(reversed(escenas)))]
        self.assertEqual(primera, segunda)

    def test_resumen_de_descarga(self) -> None:
        resumen = asf.resumen_descarga([
            _escena(1, 1, _cuadrado(0, 0), mb=1024.0),
            _escena(2, 2, _cuadrado(1, 1), mb=1024.0),
        ])
        self.assertEqual(resumen["escenas"], 2.0)
        self.assertAlmostEqual(resumen["volumen_gb"], 2.0, places=6)

    def test_nivel_no_admitido(self) -> None:
        with self.assertRaises(ValueError):
            asf.buscar(_cuadrado(0, 0), nivel="L1.5")

    def test_credenciales_detectadas_sin_exponer_la_clave(self) -> None:
        """
        Con un netrc real, el motivo reportado no puede contener el secreto.

        La comprobación es sobre el valor, no sobre la palabra 'password': el
        mensaje de ayuda cita el formato del archivo de forma legítima.
        """
        temporal = Path(tempfile.mkdtemp())
        try:
            falso = temporal / ".netrc"
            falso.write_text(
                f"machine {asf.SERVIDOR_EARTHDATA} login usuario_prueba "
                "password CLAVE_SECRETA_DE_PRUEBA\n",
                encoding="utf-8",
            )
            disponibles, motivo = asf.credenciales_disponibles(
                ruta_declarada=falso
            )
            self.assertTrue(disponibles)
            self.assertNotIn("CLAVE_SECRETA_DE_PRUEBA", motivo)
            self.assertNotIn("usuario_prueba", motivo)
        finally:
            shutil.rmtree(temporal, ignore_errors=True)

    def test_credenciales_ausentes_se_reportan_con_instrucciones(self) -> None:
        temporal = Path(tempfile.mkdtemp())
        try:
            disponibles, motivo = asf.credenciales_disponibles(
                ruta_declarada=temporal / "no_existe.netrc"
            )
            self.assertFalse(disponibles)
            self.assertIn("no_existe.netrc", motivo)
        finally:
            shutil.rmtree(temporal, ignore_errors=True)

    def test_la_ruta_declarada_no_cae_al_perfil_del_usuario(self) -> None:
        """
        Declarar una ruta inexistente no debe usar ~/.netrc en su lugar.

        Caer al archivo del perfil sería peor que fallar: el estudio se
        ejecutaría con las credenciales de otra cuenta sin que nadie lo note.
        """
        temporal = Path(tempfile.mkdtemp())
        try:
            self.assertIsNone(
                asf._ruta_netrc(temporal / "ausente.netrc")
            )
        finally:
            shutil.rmtree(temporal, ignore_errors=True)


class PruebaUbicacionCredenciales(unittest.TestCase):
    """La ruta declarada no puede caer dentro del repositorio."""

    def _hallazgos(self, valor):
        from comun import esquema as mod_esquema

        datos = _CFG.como_dict()
        datos["dem"]["earthdata"]["ruta_netrc"] = valor
        return mod_esquema.validar_rutas(datos, _RAIZ_REPO)

    def test_ruta_dentro_del_repositorio_es_bloqueante(self) -> None:
        dentro = str(_RAIZ_REPO / "config" / ".netrc")
        hallazgos = self._hallazgos(dentro)
        self.assertTrue(any(h.es_bloqueante
                            and h.clave == "dem.earthdata.ruta_netrc"
                            for h in hallazgos), hallazgos)

    def test_ruta_relativa_es_bloqueante(self) -> None:
        hallazgos = self._hallazgos("config/.netrc")
        self.assertTrue(any(h.es_bloqueante
                            and h.clave == "dem.earthdata.ruta_netrc"
                            for h in hallazgos))

    def test_ruta_fuera_del_repositorio_se_admite(self) -> None:
        fuera = str(Path(tempfile.gettempdir()).resolve() / "credenciales" / ".netrc")
        hallazgos = self._hallazgos(fuera)
        self.assertEqual(
            [h for h in hallazgos
             if h.clave == "dem.earthdata.ruta_netrc" and h.es_bloqueante], []
        )

    def test_null_mantiene_el_comportamiento_estandar(self) -> None:
        hallazgos = self._hallazgos(None)
        self.assertEqual(
            [h for h in hallazgos if h.clave == "dem.earthdata.ruta_netrc"], []
        )


# =============================================================================
# Extracción del DEM
# =============================================================================
class PruebaExtraccion(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _zip(self, nombres) -> Path:
        destino = self.temporal / "escena.zip"
        with zipfile.ZipFile(destino, "w") as comprimido:
            for nombre in nombres:
                comprimido.writestr(nombre, b"contenido")
        return destino

    def test_extrae_solo_el_modelo_de_elevacion(self) -> None:
        origen = self._zip([
            "AP_1/AP_1.dem.tif", "AP_1/AP_1_HH.tif",
            "AP_1/AP_1.inc_map.tif", "AP_1/AP_1.README.txt",
        ])
        extraidos = m02.extraer_dem(origen, self.temporal / "elev", ".dem.tif")
        self.assertEqual([p.name for p in extraidos], ["AP_1.dem.tif"])
        self.assertTrue((self.temporal / "elev" / "AP_1.dem.tif").is_file())
        self.assertFalse((self.temporal / "elev" / "AP_1_HH.tif").exists())

    def test_no_reextrae_si_ya_existe(self) -> None:
        origen = self._zip(["AP_1/AP_1.dem.tif"])
        destino = self.temporal / "elev"
        m02.extraer_dem(origen, destino, ".dem.tif")
        marca = (destino / "AP_1.dem.tif").stat().st_mtime_ns
        m02.extraer_dem(origen, destino, ".dem.tif")
        self.assertEqual((destino / "AP_1.dem.tif").stat().st_mtime_ns, marca)

    def test_zip_ilegible_es_error(self) -> None:
        roto = self.temporal / "roto.zip"
        roto.write_bytes(b"no soy un zip")
        with self.assertRaises(ErrorFormato):
            m02.extraer_dem(roto, self.temporal / "elev")


# =============================================================================
# Selección de cobertura
# =============================================================================
@unittest.skipUnless(HAY_QGIS, "requiere QGIS")
class PruebaCobertura(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import sig

        sig.iniciar_qgis(_CFG.obtener("entornos.qgis.prefix_path"))

    def test_elige_el_minimo_que_cubre_el_objetivo(self) -> None:
        from qgis.core import QgsGeometry

        objetivo = QgsGeometry.fromWkt(_cuadrado(0, 0, 2.0))
        escenas = [
            _escena(1, 1, _cuadrado(0, 0, 2.0)),      # cubre todo
            _escena(2, 2, _cuadrado(0, 0, 1.0)),      # cubre un cuarto
            _escena(3, 3, _cuadrado(1, 1, 1.0)),      # cubre otro cuarto
        ]
        seleccionadas, cobertura = m02.seleccionar_cobertura(
            escenas, objetivo, QgsGeometry
        )
        self.assertEqual(len(seleccionadas), 1)
        self.assertEqual(seleccionadas[0].identificador, "S1_1")
        self.assertAlmostEqual(cobertura, 100.0, places=6)

    def test_combina_escenas_cuando_ninguna_basta(self) -> None:
        from qgis.core import QgsGeometry

        objetivo = QgsGeometry.fromWkt(_cuadrado(0, 0, 2.0))
        escenas = [
            _escena(1, 1, "POLYGON((0 0,1 0,1 2,0 2,0 0))"),
            _escena(2, 2, "POLYGON((1 0,2 0,2 2,1 2,1 0))"),
        ]
        seleccionadas, cobertura = m02.seleccionar_cobertura(
            escenas, objetivo, QgsGeometry
        )
        self.assertEqual(len(seleccionadas), 2)
        self.assertAlmostEqual(cobertura, 100.0, places=6)

    def test_reporta_cobertura_parcial(self) -> None:
        from qgis.core import QgsGeometry

        objetivo = QgsGeometry.fromWkt(_cuadrado(0, 0, 2.0))
        escenas = [_escena(1, 1, _cuadrado(0, 0, 1.0))]
        _, cobertura = m02.seleccionar_cobertura(escenas, objetivo, QgsGeometry)
        self.assertAlmostEqual(cobertura, 25.0, places=6)

    def test_sin_escenas_no_hay_cobertura(self) -> None:
        from qgis.core import QgsGeometry

        objetivo = QgsGeometry.fromWkt(_cuadrado(0, 0, 2.0))
        seleccionadas, cobertura = m02.seleccionar_cobertura(
            [], objetivo, QgsGeometry
        )
        self.assertEqual(seleccionadas, [])
        self.assertAlmostEqual(cobertura, 0.0, places=6)


# =============================================================================
# Cadena hidrológica sobre un DEM sintético
# =============================================================================
@unittest.skipUnless(HAY_QGIS, "requiere QGIS y GRASS")
class PruebaDelimitacion(unittest.TestCase):
    """
    Cono invertido de 80x80 celdas de 30 m. El mínimo está desplazado del centro
    para que el drenaje converja a un punto conocido.
    """

    PASO = 30.0
    N = 80
    X0 = 4884000.0
    Y0 = 2091000.0
    COL_MIN, FILA_MIN = 20, 55

    @classmethod
    def setUpClass(cls) -> None:
        import sig

        prefijo = _CFG.obtener("entornos.qgis.prefix_path")
        sig.iniciar_qgis(prefijo)
        sig.inicializar_processing(prefijo)

    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.dem = self._crear_dem()

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _crear_dem(self) -> Path:
        import numpy as np
        from osgeo import gdal, osr

        filas, columnas = np.mgrid[0:self.N, 0:self.N]
        altura = 100.0 + 0.6 * (
            (columnas - self.COL_MIN) ** 2 + (filas - self.FILA_MIN) ** 2
        ) ** 0.5

        destino = self.temporal / "dem.tif"
        conjunto = gdal.GetDriverByName("GTiff").Create(
            str(destino), self.N, self.N, 1, gdal.GDT_Float32
        )
        conjunto.SetGeoTransform(
            (self.X0, self.PASO, 0, self.Y0 + self.N * self.PASO, 0, -self.PASO)
        )
        referencia = osr.SpatialReference()
        referencia.ImportFromEPSG(9377)
        conjunto.SetProjection(referencia.ExportToWkt())
        conjunto.GetRasterBand(1).WriteArray(altura.astype("float32"))
        conjunto.FlushCache()
        return destino

    def _coordenadas_del_minimo(self) -> tuple[float, float]:
        este = self.X0 + (self.COL_MIN + 0.5) * self.PASO
        norte = self.Y0 + (self.N - self.FILA_MIN - 0.5) * self.PASO
        return este, norte

    def test_estadisticas_del_raster(self) -> None:
        cotas = m02.estadisticas_raster(self.dem)
        self.assertLess(cotas["minimo"], cotas["media"])
        self.assertLess(cotas["media"], cotas["maximo"])
        self.assertAlmostEqual(cotas["minimo"], 100.0, places=3)

    def test_ajuste_al_cauce_mueve_el_punto(self) -> None:
        import processing

        relleno = self.temporal / "relleno.tif"
        processing.run("native:fillsinkswangliu", {
            "INPUT": str(self.dem), "BAND": 1, "MIN_SLOPE": 0.01,
            "OUTPUT_FILLED_DEM": str(relleno),
        })
        acumulacion = self.temporal / "acc.tif"
        processing.run("grass:r.watershed", {
            "elevation": str(relleno), "threshold": 200, "-s": True,
            "accumulation": str(acumulacion),
            "drainage": str(self.temporal / "dir.tif"),
        })

        este, norte = self._coordenadas_del_minimo()
        desviado_e, desviado_n = este + 4 * self.PASO, norte + 4 * self.PASO
        radio = 250.0
        ajustado_e, ajustado_n, desplazamiento = m02.ajustar_a_cauce(
            acumulacion, desviado_e, desviado_n, radio
        )

        # El contrato de la función es llevar el punto a la celda de mayor
        # acumulación dentro del radio, no acercarlo al mínimo topográfico.
        # Se verifica exactamente eso.
        self.assertGreater(desplazamiento, 0.0)
        # La ventana es cuadrada, de modo que la diagonal admite hasta raiz(2).
        self.assertLessEqual(desplazamiento, radio * 1.5)
        self.assertGreaterEqual(
            self._acumulacion_en(acumulacion, ajustado_e, ajustado_n),
            self._acumulacion_en(acumulacion, desviado_e, desviado_n),
        )

    def _acumulacion_en(self, ruta: Path, este: float, norte: float) -> float:
        import numpy as np
        from osgeo import gdal

        conjunto = gdal.Open(str(ruta))
        origen_x, paso_x, _, origen_y, _, paso_y = conjunto.GetGeoTransform()
        arreglo = conjunto.GetRasterBand(1).ReadAsArray().astype("float64")
        conjunto = None
        columna = int((este - origen_x) / paso_x)
        fila = int((norte - origen_y) / paso_y)
        return float(np.abs(arreglo[fila, columna]))

    def test_ajuste_fuera_del_raster_es_error(self) -> None:
        import processing

        acumulacion = self.temporal / "acc.tif"
        processing.run("grass:r.watershed", {
            "elevation": str(self.dem), "threshold": 200, "-s": True,
            "accumulation": str(acumulacion),
            "drainage": str(self.temporal / "dir.tif"),
        })
        with self.assertRaises(ErrorFormato):
            m02.ajustar_a_cauce(acumulacion, 0.0, 0.0, 250.0)

    def test_cadena_completa_produce_una_cuenca(self) -> None:
        import processing

        relleno = self.temporal / "relleno.tif"
        processing.run("native:fillsinkswangliu", {
            "INPUT": str(self.dem), "BAND": 1, "MIN_SLOPE": 0.01,
            "OUTPUT_FILLED_DEM": str(relleno),
        })
        direccion = self.temporal / "dir.tif"
        processing.run("grass:r.watershed", {
            "elevation": str(relleno), "threshold": 200, "-s": True,
            "drainage": str(direccion),
            "accumulation": str(self.temporal / "acc.tif"),
        })

        este, norte = self._coordenadas_del_minimo()
        cuenca = self.temporal / "cuenca.tif"
        processing.run("grass:r.water.outlet", {
            "input": str(direccion), "coordinates": f"{este},{norte}",
            "output": str(cuenca),
        })
        self.assertTrue(cuenca.is_file())

        import numpy as np
        from osgeo import gdal

        conjunto = gdal.Open(str(cuenca))
        arreglo = conjunto.GetRasterBand(1).ReadAsArray()
        conjunto = None
        celdas = int(np.sum(arreglo == 1))
        self.assertGreater(celdas, 100)

    def test_detecta_cuenca_que_toca_el_borde(self) -> None:
        import numpy as np
        from osgeo import gdal, osr

        for tocando in (True, False):
            arreglo = np.zeros((20, 20), dtype="int32")
            if tocando:
                arreglo[0:5, 0:5] = 1      # pegado a la esquina
            else:
                arreglo[8:12, 8:12] = 1    # en el centro

            destino = self.temporal / f"cuenca_{tocando}.tif"
            conjunto = gdal.GetDriverByName("GTiff").Create(
                str(destino), 20, 20, 1, gdal.GDT_Int32
            )
            conjunto.SetGeoTransform((0, 30, 0, 600, 0, -30))
            referencia = osr.SpatialReference()
            referencia.ImportFromEPSG(9377)
            conjunto.SetProjection(referencia.ExportToWkt())
            banda = conjunto.GetRasterBand(1)
            banda.SetNoDataValue(0)
            banda.WriteArray(arreglo)
            conjunto.FlushCache()
            conjunto = None

            self.assertEqual(m02.toca_borde(destino), tocando,
                             f"caso tocando={tocando}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

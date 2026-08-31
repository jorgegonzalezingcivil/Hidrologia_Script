# -*- coding: utf-8 -*-
"""
Pruebas del adaptador de ingesta de la CAR.

    python tests/test_ingesta_car.py
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

import ingesta_car as car  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402

_PERFIL_REAL = _RAIZ_REPO / "config" / "perfiles_car.yaml"

_PERFIL_YAML = """
version: 1
fuente: "car"
entidad: "CAR"
catalogo: "x.shp"
catalogo_crs: "EPSG:4326"
contenedor: {tipo: "xlsx", hoja: 0}
columnas: [CODIGO, ESTACION, PARAMETRO, ESCALA, TIPO, FECHA, DATO, UNIDADES]
campos:
  codigo: "CODIGO"
  nombre: "ESTACION"
  fecha: "FECHA"
  valor: "DATO"
  unidades: "UNIDADES"
  escala: "ESCALA"
  parametro: "PARAMETRO"
  tipo: "TIPO"
formato_fecha: "%Y-%m-%d %H:%M:%S"
series:
  - {parametro: "CAUDALES", tipo: "MEDIOS", etiqueta: "Q_MEDIA_M",
     unidad_esperada: "m3/s"}
series_sin_consumidor:
  - {parametro: "EVAPORACIÓN", tipo: "TOTALES", etiqueta: "EV_TT_M",
     unidad_esperada: "mm"}
descartes:
  - {parametro: "NIVELES", motivo: "sin curva de gasto"}
controles:
  escala_esperada: "MENSUAL"
  exigir_en_catalogo: true
"""


def _fila(**cambios):
    base = {"CODIGO": "2120742", "ESTACION": "BALSA LA",
            "PARAMETRO": "CAUDALES", "ESCALA": "MENSUAL", "TIPO": "MEDIOS",
            "FECHA": "2010-01-01 00:00:00", "DATO": "5.463", "UNIDADES": "m3/s"}
    base.update(cambios)
    return base


class PruebaCargaDelPerfil(unittest.TestCase):

    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.ruta = self.temporal / "perfiles_car.yaml"
        self.ruta.write_text(_PERFIL_YAML, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_lee_las_equivalencias(self) -> None:
        perfil = car.cargar_perfil(self.ruta)
        self.assertEqual(perfil.serie_de("CAUDALES", "MEDIOS").etiqueta,
                         "Q_MEDIA_M")

    def test_marca_las_que_no_tienen_consumidor(self) -> None:
        perfil = car.cargar_perfil(self.ruta)
        self.assertTrue(perfil.serie_de("CAUDALES", "MEDIOS").con_consumidor)
        self.assertFalse(
            perfil.serie_de("EVAPORACIÓN", "TOTALES").con_consumidor)

    def test_recoge_los_descartes(self) -> None:
        self.assertIn("NIVELES", car.cargar_perfil(self.ruta).parametros_descartados)

    def test_un_perfil_inexistente_es_error_explicito(self) -> None:
        with self.assertRaises(ErrorRutas):
            car.cargar_perfil(self.temporal / "no_existe.yaml")

    def test_una_equivalencia_incompleta_detiene(self) -> None:
        self.ruta.write_text(
            _PERFIL_YAML.replace('etiqueta: "Q_MEDIA_M"', 'etiqueta: ""'),
            encoding="utf-8")
        with self.assertRaises(ErrorFormato):
            car.cargar_perfil(self.ruta)


class PruebaNormalizacion(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        temporal = Path(tempfile.mkdtemp())
        ruta = temporal / "p.yaml"
        ruta.write_text(_PERFIL_YAML, encoding="utf-8")
        cls.perfil = car.cargar_perfil(ruta)
        cls.temporal = temporal
        cls.catalogo = {"2120742": {"nombre": "BALSA LA", "categoria": "LM",
                                    "altitud": "2568", "latitud": 4.8295,
                                    "longitud": -74.0708}}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temporal, ignore_errors=True)

    def test_traduce_al_esquema_interno(self) -> None:
        reg, motivo = car.normalizar(_fila(), self.perfil, self.catalogo)
        self.assertEqual(motivo, "")
        self.assertEqual(reg["etiqueta"], "Q_MEDIA_M")
        self.assertEqual(reg["fecha"], "2010-01-01")
        self.assertAlmostEqual(reg["valor"], 5.463)

    def test_situa_la_estacion_con_el_catalogo(self) -> None:
        # El libro NO trae ubicación: sin el cruce la estación no se puede
        # situar y no entraría a ninguna interpolación.
        reg, _ = car.normalizar(_fila(), self.perfil, self.catalogo)
        self.assertAlmostEqual(reg["latitud"], 4.8295)
        self.assertEqual(reg["categoria"], "LM")

    def test_una_unidad_distinta_se_rechaza(self) -> None:
        # La columna DATO mezcla cm, m3/s, mm y grados. Una fila de nivel leída
        # como caudal pasaría cualquier control numérico.
        _, motivo = car.normalizar(
            _fila(UNIDADES="cm"), self.perfil, self.catalogo)
        self.assertEqual(motivo, car.MOTIVO_UNIDAD)

    def test_un_parametro_descartado_no_entra(self) -> None:
        _, motivo = car.normalizar(
            _fila(PARAMETRO="NIVELES", UNIDADES="cm"), self.perfil, self.catalogo)
        self.assertEqual(motivo, car.MOTIVO_DESCARTADO)

    def test_una_escala_distinta_se_rechaza(self) -> None:
        # Si una entrega futura trajera series diarias, hay que verlas antes de
        # tratarlas como mensuales.
        _, motivo = car.normalizar(
            _fila(ESCALA="DIARIA"), self.perfil, self.catalogo)
        self.assertEqual(motivo, car.MOTIVO_ESCALA)

    def test_sin_equivalencia_declarada_no_entra(self) -> None:
        _, motivo = car.normalizar(
            _fila(TIPO="MAXIMOS ABSOLUTOS"), self.perfil, self.catalogo)
        self.assertEqual(motivo, car.MOTIVO_SIN_EQUIVALENCIA)

    def test_una_estacion_fuera_del_catalogo_no_entra(self) -> None:
        _, motivo = car.normalizar(_fila(CODIGO="9999999"), self.perfil,
                                   self.catalogo)
        self.assertEqual(motivo, car.MOTIVO_SIN_CATALOGO)

    def test_un_valor_vacio_no_entra(self) -> None:
        _, motivo = car.normalizar(_fila(DATO=None), self.perfil, self.catalogo)
        self.assertEqual(motivo, car.MOTIVO_VALOR)

    def test_una_fecha_ilegible_no_entra(self) -> None:
        # La fecha forma parte de la clave de deduplicación: sin ella los
        # registros colapsarían en una sola clave y se descartarían entre sí.
        _, motivo = car.normalizar(_fila(FECHA="ayer"), self.perfil,
                                   self.catalogo)
        self.assertEqual(motivo, car.MOTIVO_FECHA)

    def test_el_calificador_queda_vacio_y_no_en_cero(self) -> None:
        # Un cero se leería como un nivel de aprobación declarado.
        reg, _ = car.normalizar(_fila(), self.perfil, self.catalogo)
        self.assertEqual(reg["calificador"], "")
        self.assertEqual(reg["nivel_aprobacion"], "")

    def test_admite_coma_decimal(self) -> None:
        reg, _ = car.normalizar(_fila(DATO="5,463"), self.perfil, self.catalogo)
        self.assertAlmostEqual(reg["valor"], 5.463)


class PruebaPerfilRealDelRepositorio(unittest.TestCase):
    """El perfil que se versiona debe describir lo que la CAR entrega."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.perfil = car.cargar_perfil(_PERFIL_REAL)

    def test_las_etiquetas_son_las_que_la_cadena_ya_usa(self) -> None:
        etiquetas = {s.etiqueta for s in self.perfil.series.values()}
        for esperada in ("PTPM_TT_M", "PTPM_MX_TT_M",
                         "Q_MEDIA_M", "Q_MX_M", "Q_MN_M"):
            self.assertIn(esperada, etiquetas)

    def test_la_temperatura_declara_la_unidad_QUE_LLEGA(self) -> None:
        # El libro trae el indicador ordinal femenino (U+00AA) y no el signo de
        # grado (U+00B0). Declarar el correcto rechazaría las 9.071 filas de
        # temperatura, que es lo que ocurrio al probarlo contra el archivo real.
        serie = self.perfil.serie_de("TEMPERATURA AMBIENTE", "MEDIOS")
        self.assertIsNotNone(serie)
        self.assertEqual(serie.unidad_esperada, "ªC")

    def test_los_niveles_estan_descartados(self) -> None:
        self.assertIn("NIVELES", self.perfil.parametros_descartados)

    def test_toda_serie_con_consumidor_declara_su_unidad(self) -> None:
        for serie in self.perfil.series.values():
            if serie.con_consumidor:
                self.assertTrue(serie.unidad_esperada,
                                f"{serie.etiqueta} sin unidad esperada")


if __name__ == "__main__":
    unittest.main(verbosity=2)

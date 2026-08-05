# -*- coding: utf-8 -*-
"""
Pruebas del M00: configuración, rutas, esquema y logging.

Se ejecutan con la librería estándar, sin pytest, para que también corran bajo
el Python de QGIS:

    python tests/test_m00.py
    python -m unittest discover -s tests
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

import M00_configuracion as m00  # noqa: E402
from comun import config as mod_config  # noqa: E402
from comun import esquema, registro, rutas  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorClaveInexistente,
    ErrorConfiguracion,
    ErrorRutas,
    ErrorValidacion,
)


def _config_valida() -> dict:
    """
    Configuración mínima que satisface el esquema completo.

    Se construye a partir del config.yaml del repositorio: si el archivo real
    deja de ser conforme, estas pruebas lo detectan.
    """
    return mod_config.leer_yaml(_RAIZ_REPO / "config" / "config.yaml")


class PruebaRutas(unittest.TestCase):
    def test_detecta_la_raiz_del_repositorio(self) -> None:
        self.assertEqual(rutas.raiz_proyecto(), _RAIZ_REPO)

    def test_directorio_logico_conocido(self) -> None:
        destino = rutas.directorio("procesado_series", _RAIZ_REPO)
        self.assertEqual(destino, _RAIZ_REPO / "data" / "02_procesado" / "series")

    def test_directorio_logico_desconocido(self) -> None:
        with self.assertRaises(ErrorRutas):
            rutas.directorio("inexistente", _RAIZ_REPO)

    def test_resolver_conserva_las_rutas_absolutas(self) -> None:
        absoluta = Path(tempfile.gettempdir()).resolve() / "insumo.shp"
        self.assertEqual(rutas.resolver(absoluta, _RAIZ_REPO), absoluta)

    def test_resolver_desde_usa_el_directorio_del_manifiesto(self) -> None:
        base = _RAIZ_REPO / "data" / "00_insumos_usuario" / "MANIFIESTO.yaml"
        obtenida = rutas.resolver_desde(base, "suelos/ucs.shp")
        esperada = (_RAIZ_REPO / "data" / "00_insumos_usuario" / "suelos" / "ucs.shp")
        self.assertEqual(obtenida, esperada.resolve())

    def test_todos_los_directorios_declarados_existen(self) -> None:
        faltantes = [
            relativa for relativa in rutas.SUBDIRECTORIOS.values()
            if not (_RAIZ_REPO / relativa).is_dir()
        ]
        self.assertEqual(faltantes, [], "Ejecutar setup_estructura.py")


class PruebaLecturaYaml(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _escribir(self, contenido: str) -> Path:
        destino = self.temporal / "prueba.yaml"
        destino.write_text(contenido, encoding="utf-8")
        return destino

    def test_rechaza_claves_duplicadas(self) -> None:
        destino = self._escribir("bloque:\n  valor: 1\n  valor: 2\n")
        with self.assertRaises(ErrorConfiguracion) as contexto:
            mod_config.leer_yaml(destino)
        self.assertIn("duplicada", str(contexto.exception))

    def test_rechaza_archivo_vacio(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            mod_config.leer_yaml(self._escribir(""))

    def test_rechaza_contenido_que_no_es_bloque(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            mod_config.leer_yaml(self._escribir("- uno\n- dos\n"))

    def test_rechaza_archivo_inexistente(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            mod_config.leer_yaml(self.temporal / "ausente.yaml")


class PruebaConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = mod_config.cargar(raiz=_RAIZ_REPO)

    def test_el_config_del_repositorio_es_conforme(self) -> None:
        bloqueantes = [h for h in self.cfg.hallazgos if h.es_bloqueante]
        self.assertEqual(
            bloqueantes, [],
            "config/config.yaml tiene hallazgos bloqueantes:\n"
            + "\n".join(str(h) for h in bloqueantes),
        )

    def test_acceso_por_ruta_con_puntos(self) -> None:
        self.assertEqual(self.cfg.obtener("tormenta.huff.cuartil"), 2)

    def test_clave_inexistente_sin_defecto(self) -> None:
        with self.assertRaises(ErrorClaveInexistente):
            self.cfg.obtener("tormenta.inexistente")

    def test_clave_inexistente_con_defecto(self) -> None:
        self.assertEqual(self.cfg.obtener("tormenta.inexistente", 7), 7)

    def test_requerir_falla_si_el_valor_es_nulo(self) -> None:
        with self.assertRaises(ErrorConfiguracion):
            self.cfg.requerir("tormenta.hipotesis_adoptada")

    def test_las_listas_se_entregan_congeladas(self) -> None:
        periodos = self.cfg.obtener("frecuencia.periodos_retorno")
        self.assertIsInstance(periodos, tuple)
        with self.assertRaises(AttributeError):
            periodos.append(1000)  # type: ignore[attr-defined]

    def test_seccion_devuelve_copia_mutable_e_independiente(self) -> None:
        seccion = self.cfg.seccion("tormenta")
        seccion["duracion_h"] = 99
        self.assertNotEqual(self.cfg.obtener("tormenta.duracion_h"), 99)

    def test_ruta_de_resuelve_contra_la_raiz(self) -> None:
        destino = self.cfg.ruta_de("arf.tabla")
        self.assertEqual(destino, (_RAIZ_REPO / "data/referencia/arf_invias.csv"))

    def test_parametros_extrae_el_subconjunto_pedido(self) -> None:
        extraidos = self.cfg.parametros(("tormenta.duracion_h", "anomalos.metodo"))
        self.assertEqual(set(extraidos), {"tormenta.duracion_h", "anomalos.metodo"})

    def test_la_huella_es_estable(self) -> None:
        otra = mod_config.cargar(raiz=_RAIZ_REPO)
        self.assertEqual(self.cfg.sha256, otra.sha256)


class PruebaEsquemaEstructura(unittest.TestCase):
    def setUp(self) -> None:
        self.datos = _config_valida()

    def _validar(self):
        return esquema.validar(self.datos, raiz=_RAIZ_REPO, verificar_rutas=False)

    def _claves_por_severidad(self, severidad: str) -> set[str]:
        return {h.clave for h in self._validar() if h.severidad == severidad}

    def test_la_configuracion_de_referencia_no_tiene_bloqueantes(self) -> None:
        self.assertEqual(self._claves_por_severidad(esquema.BLOQUEANTE), set())

    def test_detecta_clave_ausente(self) -> None:
        del self.datos["tormenta"]["duracion_h"]
        self.assertIn("tormenta.duracion_h",
                      self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_detecta_tipo_incorrecto(self) -> None:
        self.datos["tormenta"]["duracion_h"] = "tres"
        self.assertIn("tormenta.duracion_h",
                      self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_un_booleano_no_pasa_por_numero(self) -> None:
        self.datos["tormenta"]["duracion_h"] = True
        self.assertIn("tormenta.duracion_h",
                      self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_detecta_valor_fuera_de_rango(self) -> None:
        self.datos["tormenta"]["huff"]["cuartil"] = 9
        self.assertIn("tormenta.huff.cuartil",
                      self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_detecta_opcion_no_admitida(self) -> None:
        self.datos["anomalos"]["metodo"] = "MAD"
        self.assertIn("anomalos.metodo",
                      self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_detecta_nulo_no_admitido(self) -> None:
        self.datos["tormenta"]["duracion_h"] = None
        self.assertIn("tormenta.duracion_h",
                      self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_admite_nulo_en_decisiones_pendientes(self) -> None:
        self.assertNotIn("complemento.metodo_adoptado",
                         self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_detecta_lista_no_creciente(self) -> None:
        self.datos["frecuencia"]["periodos_retorno"] = [10, 5, 100]
        self.assertIn("frecuencia.periodos_retorno",
                      self._claves_por_severidad(esquema.BLOQUEANTE))

    def test_detecta_elemento_invalido_dentro_de_lista(self) -> None:
        self.datos["frecuencia"]["distribuciones"].append("beta")
        claves = self._claves_por_severidad(esquema.BLOQUEANTE)
        self.assertTrue(any(c.startswith("frecuencia.distribuciones[") for c in claves))

    def test_advierte_sobre_claves_no_reconocidas(self) -> None:
        self.datos["tormenta"]["duracion_hs"] = 3.0
        self.assertIn("tormenta.duracion_hs",
                      self._claves_por_severidad(esquema.ADVERTENCIA))

    def test_detecta_bloque_reemplazado_por_valor_simple(self) -> None:
        self.datos["tormenta"]["huff"] = 2
        self.assertIn("tormenta.huff",
                      self._claves_por_severidad(esquema.BLOQUEANTE))


class PruebaEsquemaInvariantes(unittest.TestCase):
    def setUp(self) -> None:
        self.datos = _config_valida()

    def _claves(self, severidad: str) -> set[str]:
        hallazgos = esquema.validar_invariantes(self.datos)
        return {h.clave for h in hallazgos if h.severidad == severidad}

    def test_anomalos_no_puede_aplicarse_a_la_serie_de_maximos(self) -> None:
        self.datos["anomalos"]["aplicar_a_serie_maximos"] = True
        self.assertIn("anomalos.aplicar_a_serie_maximos",
                      self._claves(esquema.BLOQUEANTE))

    def test_enso_no_puede_eliminar_registros(self) -> None:
        self.datos["enso"]["elimina_registros"] = True
        self.assertIn("enso.elimina_registros", self._claves(esquema.BLOQUEANTE))

    def test_cuartiles_invertidos(self) -> None:
        self.datos["anomalos"]["q1"] = 0.9
        self.assertIn("anomalos.q1", self._claves(esquema.BLOQUEANTE))

    def test_intervalo_de_calculo_debe_coincidir_con_hec_hms(self) -> None:
        self.datos["hec_hms"]["control"]["intervalo_min"] = 15
        self.assertIn("hec_hms.control.intervalo_min",
                      self._claves(esquema.BLOQUEANTE))

    def test_duracion_no_multiplo_del_intervalo(self) -> None:
        self.datos["tormenta"]["intervalo_calculo_min"] = 7
        self.datos["hec_hms"]["control"]["intervalo_min"] = 7
        self.assertIn("tormenta.duracion_h", self._claves(esquema.BLOQUEANTE))

    def test_h3_factor_exige_coeficiente_y_fuente(self) -> None:
        self.datos["tormenta"]["hipotesis_adoptada"] = "h3_factor"
        bloqueantes = self._claves(esquema.BLOQUEANTE)
        self.assertIn("tormenta.coeficiente_desagregacion.valor", bloqueantes)
        self.assertIn("tormenta.coeficiente_desagregacion.fuente", bloqueantes)

    def test_hipotesis_adoptada_debe_haber_sido_evaluada(self) -> None:
        self.datos["tormenta"]["hipotesis_p24_a_pd"] = ["h1_directa"]
        self.datos["tormenta"]["hipotesis_adoptada"] = "h2_idf"
        self.assertIn("tormenta.hipotesis_adoptada", self._claves(esquema.BLOQUEANTE))

    def test_distribucion_adoptada_debe_haber_sido_ajustada(self) -> None:
        self.datos["frecuencia"]["distribucion_adoptada"] = "gamma"
        self.datos["frecuencia"]["distribuciones"] = ["normal", "gumbel_max"]
        self.assertIn("frecuencia.distribucion_adoptada",
                      self._claves(esquema.BLOQUEANTE))

    def test_precedencia_de_aprobacion_invertida(self) -> None:
        self.datos["ideam"]["deduplicacion"]["precedencia_aprobacion"] = [
            "Preliminar", "Definitivo",
        ]
        self.assertIn("ideam.deduplicacion.precedencia_aprobacion",
                      self._claves(esquema.BLOQUEANTE))

    def test_advierte_si_el_umbral_de_completitud_mensual_es_laxo(self) -> None:
        self.datos["ideam"]["agregacion_diaria_a_mensual"]["max_dias_faltantes"] = 10
        self.assertIn("ideam.agregacion_diaria_a_mensual.max_dias_faltantes",
                      self._claves(esquema.ADVERTENCIA))

    def test_advierte_si_el_transform_no_consume_rezago(self) -> None:
        self.datos["hec_hms"]["transform"] = "clark"
        self.assertIn("hec_hms.transform", self._claves(esquema.ADVERTENCIA))

    def test_advierte_si_se_admiten_menos_de_cinco_formulas_de_tc(self) -> None:
        self.datos["tiempo_concentracion"]["min_formulas_aplicables"] = 3
        self.assertIn("tiempo_concentracion.min_formulas_aplicables",
                      self._claves(esquema.ADVERTENCIA))

    def test_advierte_si_el_factor_de_cambio_climatico_admite_disminucion(self) -> None:
        self.datos["cambio_climatico"]["solo_si_incremento"] = False
        self.assertIn("cambio_climatico.solo_si_incremento",
                      self._claves(esquema.ADVERTENCIA))

    def test_advierte_si_el_crs_de_calculo_no_es_ctm12(self) -> None:
        self.datos["crs"]["calculo"] = "EPSG:3116"
        self.assertIn("crs.calculo", self._claves(esquema.ADVERTENCIA))

    def test_advierte_si_el_punto_de_descarga_esta_fuera_de_colombia(self) -> None:
        self.datos["punto_descarga"]["latitud"] = 40.0
        self.datos["punto_descarga"]["longitud"] = -3.0
        self.assertIn("punto_descarga", self._claves(esquema.ADVERTENCIA))

    def test_advierte_si_el_punto_de_descarga_esta_sin_definir(self) -> None:
        self.assertIn("punto_descarga", self._claves(esquema.ADVERTENCIA))

    def test_detecta_ventana_temporal_mal_formada(self) -> None:
        self.datos["sensibilidad_series"]["ventanas"] = [[2000]]
        self.assertIn("sensibilidad_series.ventanas[0]",
                      self._claves(esquema.BLOQUEANTE))

    def test_detecta_ventana_temporal_invertida(self) -> None:
        self.datos["sensibilidad_series"]["ventanas"] = [[2010, 1990]]
        self.assertIn("sensibilidad_series.ventanas[0]",
                      self._claves(esquema.BLOQUEANTE))

    def test_advierte_categorias_de_estacion_desconocidas(self) -> None:
        self.datos["estaciones"]["categorias_por_variable"]["precipitacion"] = ["ZZ"]
        self.assertIn("estaciones.categorias_por_variable.precipitacion",
                      self._claves(esquema.ADVERTENCIA))

    def test_advierte_si_la_interpolacion_es_mas_fina_que_el_dem(self) -> None:
        self.datos["interpolacion"]["resolucion_raster_m"] = 5.0
        self.assertIn("interpolacion.resolucion_raster_m",
                      self._claves(esquema.ADVERTENCIA))


class PruebaCargaEstricta(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_cargar_falla_ante_un_bloqueante(self) -> None:
        datos = _config_valida()
        datos["enso"]["elimina_registros"] = True
        destino = self.temporal / "config.yaml"
        import yaml

        destino.write_text(
            yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8"
        )
        with self.assertRaises(ErrorValidacion) as contexto:
            mod_config.cargar(ruta=destino, raiz=_RAIZ_REPO)
        self.assertIn("enso.elimina_registros", str(contexto.exception))


class PruebaRegistro(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        (self.temporal / "logs").mkdir()
        for marcador in rutas.MARCADORES_RAIZ:
            (self.temporal / marcador).write_text("marcador", encoding="utf-8")

    def tearDown(self) -> None:
        import logging

        for nombre in ("PRUEBA", "PRUEBA_BLOQUE"):
            logger = logging.getLogger(nombre)
            for manejador in list(logger.handlers):
                logger.removeHandler(manejador)
                manejador.close()
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_escribe_el_archivo_de_log_con_la_cabecera(self) -> None:
        logger = registro.configurar(
            "PRUEBA", raiz=self.temporal, consola=False, marca_tiempo="prueba"
        )
        registro.registrar_cabecera(logger, "PRUEBA", "prueba de trazabilidad")
        destino = registro.ruta_log(logger)
        self.assertIsNotNone(destino)
        contenido = destino.read_text(encoding="utf-8")
        self.assertIn("MÓDULO PRUEBA", contenido)
        self.assertIn("Python", contenido)
        self.assertIn("Fecha de ejecución", contenido)

    def test_no_duplica_manejadores_al_reconfigurar(self) -> None:
        primero = registro.configurar(
            "PRUEBA", raiz=self.temporal, consola=False, marca_tiempo="a"
        )
        segundo = registro.configurar(
            "PRUEBA", raiz=self.temporal, consola=False, marca_tiempo="b"
        )
        self.assertIs(primero, segundo)
        self.assertEqual(len(segundo.handlers), 1)

    def test_el_bloque_registra_la_excepcion_y_la_propaga(self) -> None:
        logger = registro.configurar(
            "PRUEBA_BLOQUE", raiz=self.temporal, consola=False, marca_tiempo="c"
        )
        with self.assertRaises(ValueError):
            with registro.bloque(logger, "etapa que falla"):
                raise ValueError("fallo deliberado")
        contenido = registro.ruta_log(logger).read_text(encoding="utf-8")
        self.assertIn("interrumpido", contenido)
        self.assertIn("fallo deliberado", contenido)


class PruebaModuloM00(unittest.TestCase):
    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.temporal, ignore_errors=True)

    def test_ejecucion_sobre_el_repositorio(self) -> None:
        destino_json = self.temporal / "reporte.json"
        codigo, hallazgos = m00.ejecutar(
            raiz=_RAIZ_REPO, ruta_json=destino_json, consola=False
        )
        self.assertEqual(codigo, m00.SALIDA_CORRECTA,
                         "\n".join(str(h) for h in hallazgos if h.es_bloqueante))
        reporte = json.loads(destino_json.read_text(encoding="utf-8"))
        self.assertTrue(reporte["conforme"])
        self.assertEqual(reporte["modulo"], "M00")
        self.assertEqual(len(reporte["hallazgos"]), len(hallazgos))

    def test_el_modo_estricto_detiene_ante_advertencias(self) -> None:
        codigo, hallazgos = m00.ejecutar(
            raiz=_RAIZ_REPO, estricto=True, consola=False
        )
        advertencias = [h for h in hallazgos if h.severidad == esquema.ADVERTENCIA]
        esperado = m00.SALIDA_ESTRICTA if advertencias else m00.SALIDA_CORRECTA
        self.assertEqual(codigo, esperado)

    def test_verificar_estructura_detecta_directorios_faltantes(self) -> None:
        hallazgos = m00.verificar_estructura(self.temporal)
        self.assertEqual(len(hallazgos), len(rutas.SUBDIRECTORIOS))
        self.assertTrue(all(h.severidad == esquema.ADVERTENCIA for h in hallazgos))

    def test_determinar_salida(self) -> None:
        bloqueante = esquema.Hallazgo(esquema.BLOQUEANTE, "x", "y")
        advertencia = esquema.Hallazgo(esquema.ADVERTENCIA, "x", "y")
        self.assertEqual(m00.determinar_salida([], False), m00.SALIDA_CORRECTA)
        self.assertEqual(
            m00.determinar_salida([bloqueante], False), m00.SALIDA_BLOQUEANTE
        )
        self.assertEqual(
            m00.determinar_salida([advertencia], False), m00.SALIDA_CORRECTA
        )
        self.assertEqual(
            m00.determinar_salida([advertencia], True), m00.SALIDA_ESTRICTA
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

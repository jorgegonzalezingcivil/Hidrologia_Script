# -*- coding: utf-8 -*-
"""
Pruebas del M13: actualización del proyecto de HEC-HMS.

    python tests/test_m13.py
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

import M13_hec_hms as m13  # noqa: E402
from comun.errores import ErrorHidrologia  # noqa: E402
from comun.esquema import BLOQUEANTE, INFORMATIVO  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato, ErrorRutas  # noqa: E402

_CFG = cargar(raiz=_RAIZ_REPO)

MODELO = """Basin: Basin 1
     Unit System: Metric
End:

Subbasin: SB1
     Canvas X: 1000.0
     Area: 1.5
     Downstream: J1

     LossRate: Initial+Constant
     Percent Impervious Area: 0.0

     Transform: Clark
     Clark Method: Specified
     Time Area Method: Default

     Baseflow: Recession
End:

Reach: R1
     Canvas X: 2000.0
     Downstream: J2

     Route: Lag
     Channel Loss: None
End:
"""


class PruebaSeparacionDeBloques(unittest.TestCase):
    """
    Las líneas en blanco separan bloques y no deben ocultarlos.

    La primera versión condicionaba el encabezado a que el buffer estuviera
    vacío: la línea en blanco previa lo llenaba, ninguna subcuenca se
    clasificaba y el módulo actualizaba cero sin decir por qué.
    """

    def test_reconoce_cada_tipo(self) -> None:
        bloques = m13.separar_bloques(MODELO)
        tipos = [b[0] for b in bloques]
        self.assertIn("Subbasin", tipos)
        self.assertIn("Reach", tipos)
        self.assertIn("Basin", tipos)

    def test_conserva_el_texto_integro(self) -> None:
        # Un reescritor que solo emitiera lo que entiende borraría en silencio
        # lo que el paso manual dejó.
        bloques = m13.separar_bloques(MODELO)
        self.assertEqual("".join(b[2] for b in bloques), MODELO)

    def test_recupera_el_nombre(self) -> None:
        nombres = {b[0]: b[1] for b in m13.separar_bloques(MODELO)}
        self.assertEqual(nombres["Subbasin"], "SB1")
        self.assertEqual(nombres["Reach"], "R1")


class PruebaCampos(unittest.TestCase):
    BLOQUE = "Subbasin: SB1\n     Area: 1.5\n     Transform: Clark\nEnd:\n"

    def test_sustituye_conservando_la_sangria(self) -> None:
        salida = m13.fijar_campo(self.BLOQUE, "Transform", "SCS")
        self.assertIn("     Transform: SCS\n", salida)
        self.assertNotIn("Clark", salida)

    def test_anade_el_campo_que_falta(self) -> None:
        salida = m13.fijar_campo(self.BLOQUE, "Curve Number", "74.0")
        self.assertIn("     Curve Number: 74.0\n", salida)
        self.assertTrue(salida.rstrip().endswith("End:"))

    def test_quita_los_campos_del_metodo_anterior(self) -> None:
        salida = m13.quitar_campos(self.BLOQUE, ("Transform",))
        self.assertNotIn("Transform", salida)
        self.assertIn("Area: 1.5", salida)


class PruebaActualizacion(unittest.TestCase):
    def _subcuenca(self) -> str:
        return [b[2] for b in m13.separar_bloques(MODELO)
                if b[0] == "Subbasin"][0]

    def _tramo(self) -> str:
        return [b[2] for b in m13.separar_bloques(MODELO)
                if b[0] == "Reach"][0]

    def test_la_subcuenca_pasa_a_scs_con_sus_parametros(self) -> None:
        salida, motivo = m13.actualizar_subcuenca(
            self._subcuenca(), {"cn": 74.0, "tlag_min": 28.14})
        self.assertEqual(motivo, "")
        self.assertIn("LossRate: SCS", salida)
        self.assertIn("Transform: SCS", salida)
        self.assertIn("Curve Number: 74.0", salida)
        self.assertIn("Lag: 28.14", salida)
        self.assertNotIn("Clark Method", salida)

    def test_sin_rezago_no_se_toca_y_se_reporta(self) -> None:
        # Rellenarla con un valor por defecto produciría un modelo que corre y
        # miente.
        original = self._subcuenca()
        salida, motivo = m13.actualizar_subcuenca(original, {"cn": 74.0})
        self.assertEqual(salida, original)
        self.assertIn("tlag_min", motivo)

    def test_la_topologia_se_conserva(self) -> None:
        salida, _ = m13.actualizar_subcuenca(
            self._subcuenca(), {"cn": 74.0, "tlag_min": 28.14})
        self.assertIn("Downstream: J1", salida)
        self.assertIn("Canvas X: 1000.0", salida)
        self.assertIn("Area: 1.5", salida)

    def test_el_tramo_pasa_a_muskingum_cunge(self) -> None:
        salida, motivo = m13.actualizar_tramo(
            self._tramo(), {"longitud_m": 809.21, "pendiente": 0.0123},
            0.04, 3.0, 2.0)
        self.assertEqual(motivo, "")
        self.assertIn("Route: Muskingum Cunge", salida)
        self.assertIn("Length: 809.21", salida)
        self.assertIn("Energy Slope: 0.012300", salida)
        # 'Mannings n' con ese: es la etiqueta que HEC-HMS escribe, leida de un
        # tramo que el propio programa configuro.
        self.assertIn("Mannings n: 0.040", salida)
        self.assertIn("Downstream: J2", salida)

    def test_el_end_del_bloque_no_se_pierde(self) -> None:
        # Cuando el metodo es el ultimo grupo no hay linea en blanco tras el, y
        # detenerse solo en la linea en blanco se comia el 'End:': el bloque
        # quedaba sin cerrar y se fusionaba con el siguiente. Se perdian la
        # mitad de las subcuencas.
        salida, _ = m13.actualizar_tramo(
            self._tramo(), {"longitud_m": 100.0, "pendiente": 0.01},
            0.04, 3.0, 2.0)
        self.assertEqual(salida.count("End:"), 1)
        self.assertTrue(salida.rstrip().endswith("End:"))

    def test_un_tramo_sin_pendiente_no_se_toca(self) -> None:
        original = self._tramo()
        salida, motivo = m13.actualizar_tramo(
            original, {"longitud_m": 100.0, "pendiente": 0.0}, 0.04, 3.0, 2.0)
        self.assertEqual(salida, original)
        self.assertIn("pendiente", motivo)

    def test_un_tramo_sin_geometria_no_se_toca(self) -> None:
        original = self._tramo()
        salida, motivo = m13.actualizar_tramo(original, {}, 0.04, 3.0, 2.0)
        self.assertEqual(salida, original)


TOPOLOGIA = """Basin: Basin 1
End:

Subbasin: SB1
     Area: 10.0
     Downstream: R1
End:

Subbasin: SB2
     Area: 5.0
     Downstream: R1
End:

Reach: R1
     Downstream: J1
End:

Subbasin: SB3
     Area: 3.0
     Downstream: J1
End:

Junction: J1
     Downstream: Sink-1
End:

Sink: Sink-1
End:
"""


class PruebaAreasAcumuladas(unittest.TestCase):
    """
    El ancho de cada tramo sale de SU área de drenaje, no de un valor único.
    La suma en el cierre es la comprobación de que la red no tiene ramas sueltas.
    """

    def test_cada_tramo_reune_lo_que_tiene_aguas_arriba(self) -> None:
        acumuladas = m13.areas_acumuladas(TOPOLOGIA)
        self.assertAlmostEqual(acumuladas["R1"], 15.0)
        self.assertAlmostEqual(acumuladas["J1"], 18.0)

    def test_el_cierre_reune_la_cuenca_entera(self) -> None:
        acumuladas = m13.areas_acumuladas(TOPOLOGIA)
        self.assertAlmostEqual(acumuladas["Sink-1"], 18.0)

    def test_una_subcuenca_se_cuenta_a_si_misma(self) -> None:
        self.assertAlmostEqual(m13.areas_acumuladas(TOPOLOGIA)["SB1"], 10.0)

    def test_un_enlace_circular_no_cuelga_el_modulo(self) -> None:
        # La delimitación no debería producirlo, pero tampoco lo impide, y un
        # recorrido sin control de visitados no terminaría nunca.
        circular = ("Subbasin: A\n     Area: 1.0\n     Downstream: B\nEnd:\n\n"
                    "Junction: B\n     Downstream: C\nEnd:\n\n"
                    "Junction: C\n     Downstream: B\nEnd:\n")
        acumuladas = m13.areas_acumuladas(circular)
        self.assertAlmostEqual(acumuladas["B"], 1.0)
        self.assertAlmostEqual(acumuladas["C"], 1.0)


class PruebaAnchoDeFondo(unittest.TestCase):
    def test_crece_con_la_raiz_del_area(self) -> None:
        # w = 2*A^0.5: cuadruplicar el área duplica el ancho.
        uno = m13.ancho_por_geometria_hidraulica(25.0, 2.0, 0.5)
        otro = m13.ancho_por_geometria_hidraulica(100.0, 2.0, 0.5)
        self.assertAlmostEqual(uno, 10.0)
        self.assertAlmostEqual(otro, 20.0)

    def test_nunca_baja_del_minimo(self) -> None:
        # Un área nula solo puede venir de una topología rota; un ancho cero
        # daría una sección sin área y HEC-HMS no lo resolvería.
        self.assertEqual(
            m13.ancho_por_geometria_hidraulica(0.0, 2.0, 0.5, minimo_m=1.0), 1.0)

    def test_la_relacion_se_lee_de_la_tabla_de_doctrina(self) -> None:
        ruta = _RAIZ_REPO / _CFG.obtener(
            "hec_hms.transito.muskingum_cunge.tabla_geometria")
        relacion = m13.leer_geometria_hidraulica(ruta, ";")
        self.assertGreater(relacion["coeficiente"], 0.0)
        self.assertGreater(relacion["exponente"], 0.0)
        self.assertLess(relacion["exponente"], 1.0)
        self.assertTrue(relacion["fuente"])

    def test_una_variable_ausente_es_error_explicito(self) -> None:
        ruta = _RAIZ_REPO / _CFG.obtener(
            "hec_hms.transito.muskingum_cunge.tabla_geometria")
        with self.assertRaises(ErrorFormato):
            m13.leer_geometria_hidraulica(ruta, ";", variable="no_existe")

    def test_tabla_ausente(self) -> None:
        with self.assertRaises(ErrorRutas):
            m13.leer_geometria_hidraulica(Path("no_existe.csv"), ";")


class PruebaMeteorologia(unittest.TestCase):
    """
    El vocabulario del .met está leído de uno que HEC-HMS reescribió al guardar,
    y su log lo confirma: 'Found no parameter problems in meteorologic model'.
    """

    ASIGNACION = [
        {"subcuenca": "SB1", "pluviometro": "Z1"},
        {"subcuenca": "SB2", "pluviometro": "Z2"},
        {"subcuenca": "SB3", "pluviometro": "Z1"},
    ]

    def _escribir(self) -> str:
        destino = Path(tempfile.mkdtemp()) / "T10.met"
        m13.escribir_met(destino, "T10", "10", self.ASIGNACION,
                         lambda zona, periodo: f"{zona}_T{periodo}", "Basin 1")
        return destino.read_text(encoding="utf-8")

    def test_el_metodo_es_el_nombre_del_archivo_no_el_de_la_interfaz(self) -> None:
        texto = self._escribir()
        self.assertIn("     Precipitation Method: Specified Average\n", texto)
        self.assertNotIn("Specified Hyetograph", texto)

    def test_cada_subcuenca_lleva_su_pluviometro_dentro_del_bloque(self) -> None:
        # Es la línea que engancha la serie. Sin ella HEC-HMS abre el modelo
        # meteorológico vacío y la simulación aborta sin lluvia.
        texto = self._escribir()
        self.assertIn("Subbasin: SB1\n     Gage: Z1_T10\nEnd:\n", texto)
        self.assertIn("Subbasin: SB2\n     Gage: Z2_T10\nEnd:\n", texto)
        self.assertEqual(texto.count("\n     Gage: "), len(self.ASIGNACION))

    def test_no_se_listan_los_pluviometros_aparte(self) -> None:
        # Una versión previa los declaraba al principio del .met; HEC-HMS los
        # borró al guardar. Viven en el .gage.
        texto = self._escribir()
        self.assertNotIn("\nGage:", texto)
        self.assertNotIn("Type: Recording", texto)

    def test_declara_el_modelo_de_cuenca_y_todos_los_metodos(self) -> None:
        texto = self._escribir()
        self.assertIn("     Use Basin Model: Basin 1\n", texto)
        for metodo in ("Air Temperature", "Snowmelt", "Wind Speed"):
            self.assertIn(f"     {metodo} Method: None\n", texto)


class PruebaConfiguracion(unittest.TestCase):
    def test_la_plantilla_no_lleva_la_ruta_de_un_estudio(self) -> None:
        # Es un dato de proyecto: heredarlo haría que el siguiente estudio
        # escribiera sobre el modelo de este.
        self.assertEqual(
            str(_CFG.obtener("hec_hms.proyecto.directorio", "")).strip(), "")

    def test_la_copia_de_seguridad_esta_declarada(self) -> None:
        self.assertIsInstance(
            _CFG.obtener("hec_hms.proyecto.copia_de_seguridad"), bool)

    def test_el_n_de_manning_es_razonable(self) -> None:
        n = _CFG.obtener("hec_hms.transito.muskingum_cunge.n_manning")
        if n is None:
            self.skipTest("sin declarar en la plantilla")
        self.assertGreater(float(n), 0.01)
        self.assertLess(float(n), 0.2)



class PruebaParametrosDeTransito(unittest.TestCase):
    """
    La K de Muskingum que el informe tabula. HEC-HMS la resuelve dentro de
    Muskingum-Cunge y hasta ahora solo existia en el archivo del modelo.
    """

    def setUp(self) -> None:
        self.tipo = {"SB1": "Subbasin", "SB2": "Subbasin", "J1": "Junction",
                     "R1": "Reach", "J2": "Junction", "R2": "Reach",
                     "Sink-1": "Sink"}
        self.aguas_abajo = {"SB1": "J1", "SB2": "J1", "J1": "R1",
                            "R1": "J2", "J2": "R2", "R2": "Sink-1"}
        self.geometrias = {"R1": {"longitud_m": 1800.0, "pendiente": 0.0087},
                           "R2": {"longitud_m": 600.0, "pendiente": 0.0054}}

    def _filas(self):
        return {f["tramo"]: f for f in m13.parametros_de_transito(
            self.geometrias, self.aguas_abajo, self.tipo, {}, 1.0)}

    def test_la_k_es_la_longitud_sobre_la_celeridad(self) -> None:
        fila = self._filas()["R1"]
        self.assertAlmostEqual(fila["k_s"], 1800.0)
        self.assertAlmostEqual(fila["k_min"], 30.0)
        self.assertAlmostEqual(fila["k_h"], 0.5)

    def test_la_subcuenca_llega_al_tramo_a_traves_de_la_union(self) -> None:
        # Ninguna subcuenca vierte directamente a un tramo: todas pasan por una
        # union. Buscando solo los vecinos inmediatos, los 125 tramos del
        # estudio salian sin microcuenca.
        self.assertEqual(self._filas()["R1"]["microcuenca"], "SB1+SB2")

    def test_el_tramo_recibe_solo_las_suyas_y_no_las_de_aguas_arriba(self) -> None:
        # R2 recibe el caudal YA transitado por R1: adjudicarle SB1 y SB2
        # contaria dos veces las mismas subcuencas.
        self.assertEqual(self._filas()["R2"]["microcuenca"], "")

    def test_la_pendiente_va_en_por_ciento(self) -> None:
        # La geometria la trae en m/m, que es lo que Muskingum-Cunge pide, y el
        # encabezado del informe dice 'Pendiente Cauce (%)'.
        self.assertAlmostEqual(self._filas()["R1"]["pendiente_pct"], 0.87)

    def test_lo_que_no_es_un_tramo_no_entra(self) -> None:
        # La geometria se lee del sqlite del proyecto, que trae tambien los
        # nombres de las subcuencas: 63 de las 125 entradas no son tramos.
        self.geometrias["SB1"] = {"longitud_m": 100.0, "pendiente": 0.01}
        self.assertNotIn("SB1", self._filas())

    def test_sin_celeridad_no_se_inventa_una_k(self) -> None:
        filas = m13.parametros_de_transito(
            self.geometrias, self.aguas_abajo, self.tipo, {}, 0.0)
        self.assertIsNone(filas[0]["k_s"])


class PruebaAbstraccionInicial(unittest.TestCase):
    """
    Ia = lambda * S, con la conversion de S que hace defendible el cambio.

    La relacion Ia = 0,2*S no es fisica: es un ajuste del SCS de los anios
    cincuenta. Woodward y otros (2003) hallaron la mediana cerca de 0,05 sobre
    unas 300 cuencas. Pero las tablas de CN estan calibradas CON 0,2, de modo
    que adoptar 0,05 obliga a convertir S; hacerlo sin convertir seria quedarse
    con el beneficio sin el costo.
    """

    def test_con_el_clasico_la_s_no_se_toca(self) -> None:
        r = m13.abstraccion_inicial(75.3, 0.20)
        self.assertAlmostEqual(r["s_adoptada_mm"], r["s_lambda_020_mm"])
        self.assertAlmostEqual(r["cn_equivalente"], 75.3, places=1)
        self.assertAlmostEqual(r["ia_mm"], 0.20 * r["s_adoptada_mm"], places=1)

    def test_con_el_revisado_la_s_crece_y_el_cn_baja(self) -> None:
        # Si S no creciera, la cuenca quedaria mucho mas reactiva de lo que los
        # datos respaldan: es el atajo que hay que evitar.
        r = m13.abstraccion_inicial(75.3, 0.05)
        self.assertGreater(r["s_adoptada_mm"], r["s_lambda_020_mm"])
        self.assertLess(r["cn_equivalente"], 75.3)
        self.assertAlmostEqual(r["ia_mm"], 0.05 * r["s_adoptada_mm"], places=1)

    def test_baja_el_umbral_sin_inflar_la_creciente_de_diseno(self) -> None:
        # Lo que hace defendible el cambio: multiplica la escorrentia del evento
        # frecuente y apenas mueve la del evento de diseno.
        def escorrentia(p, r, lam):
            ia = r["ia_mm"]
            s = r["s_adoptada_mm"]
            return 0.0 if p <= ia else (p - ia) ** 2 / (p - ia + s)

        clasico = m13.abstraccion_inicial(75.3, 0.20)
        revisado = m13.abstraccion_inicial(75.3, 0.05)
        frecuente = (escorrentia(21.9, revisado, 0.05)
                     / escorrentia(21.9, clasico, 0.20))
        diseno = (escorrentia(49.9, revisado, 0.05)
                  / escorrentia(49.9, clasico, 0.20))
        self.assertGreater(frecuente, 3.0)
        self.assertLess(diseno, 1.3)

    def test_un_lambda_sin_conversion_publicada_es_error(self) -> None:
        # Interpolar la relacion de Hawkins seria inventarla.
        with self.assertRaises(ErrorHidrologia):
            m13.abstraccion_inicial(75.3, 0.10)

    def test_un_cn_imposible_es_error(self) -> None:
        for cn in (0.0, -5.0, 101.0):
            with self.subTest(cn=cn):
                with self.assertRaises(ErrorHidrologia):
                    m13.abstraccion_inicial(cn, 0.20)



class PruebaEmbalse(unittest.TestCase):
    """
    El embalse que la configuracion declara y el M13 escribe.

    POR QUE ESTA EN LA CADENA Y NO A MANO. Editado a mano, el estudio no puede
    reproducir su propio caudal de diseno: la cadena lo habria calculado en
    185 m3/s en lugar de 82,5, sin ninguna senal de que faltaba la regulacion.
    """

    MODELO = """Junction: J25
     Downstream: R25
End:

Reach: R25
     Downstream: J24
End:

Junction: J24
     Downstream: Sink-1
End:
"""

    # Curva del operador, recortada: el vaso entre el nivel normal y dos
    # metros por debajo. El volumen va en hm3, que es como lo entrega el
    # operador, y sale de la funcion en miles de m3.
    CURVA = [(2770.0, 64.0), (2772.0, 70.0)]
    CRESTA = 2776.5

    def _curva(self, **cambios):
        argumentos = dict(elevacion_volumen=self.CURVA, cota_cresta=self.CRESTA,
                          longitud_cresta_m=405.0, coeficiente=2.0,
                          descarga_fondo_m3s=1.16, lamina_maxima_m=3.0,
                          pasos_sobre_cresta=6)
        argumentos.update(cambios)
        return m13.curva_de_embalse(**argumentos)

    def _bloque(self):
        return m13.bloque_de_embalse("San Rafael", "Volumen SR", "Tabla SR",
                                     70000.0, "R25")

    def test_la_cresta_esta_en_la_curva_y_alli_solo_descarga_el_fondo(self) -> None:
        # Sin el punto de la cresta HEC-HMS interpola entre el ultimo dato del
        # operador y la primera lamina, y da vertido donde todavia no lo hay.
        cotas, volumenes, descargas = self._curva()
        self.assertIn(self.CRESTA, cotas)
        self.assertAlmostEqual(descargas[cotas.index(self.CRESTA)], 1.16)
        self.assertEqual(len(cotas), len(volumenes))
        self.assertEqual(len(cotas), len(descargas))

    def test_el_almacenamiento_va_en_miles_de_metros_cubicos(self) -> None:
        # La unidad es THOU M3: 70 hm3 son 70.000, no 70 ni 70.000.000. Con
        # '1000 M3', que vale en las subcuencas, HEC-HMS rechaza la tabla.
        cotas, volumenes, _ = self._curva()
        self.assertAlmostEqual(volumenes[cotas.index(2772.0)], 70000.0)

    def test_por_encima_del_dato_se_prolonga_con_dv_dz_y_no_con_un_area(self) -> None:
        """
        Es el fallo que hubo, y valia un factor de mil.

        La curva del operador termina en el nivel normal y el vertido ocurre
        por encima. La ultima franja da dV/dz = 3.000 miles de m3 por metro; a
        4,5 m sobre ella el vaso son 70.000 + 13.500 = 83.500. Tomando esa
        pendiente por un area en m2 salian 70.000 + 13.500.000.
        """
        cotas, volumenes, _ = self._curva()
        self.assertAlmostEqual(volumenes[cotas.index(self.CRESTA)], 83500.0)

    def test_la_descarga_suma_las_dos_salidas(self) -> None:
        # El fondo opera de continuo y el vertedero solo por encima de la
        # cresta. Presentarlas como una sola curva escondia que el embalse
        # entrega caudal aunque no vierta.
        cotas, _, descargas = self._curva()
        cota = self.CRESTA + 3.0
        self.assertAlmostEqual(descargas[cotas.index(cota)],
                               1.16 + 2.0 * 405.0 * 3.0 ** 1.5)

    def test_una_curva_que_no_crece_es_error(self) -> None:
        # Un vaso que pierde volumen al subir el nivel no existe: es un dato
        # mal transcrito, y aceptarlo daria un transito sin sentido.
        with self.assertRaises(ErrorHidrologia):
            self._curva(elevacion_volumen=[(2770.0, 70.0), (2772.0, 64.0)])

    def test_una_geometria_imposible_es_error(self) -> None:
        for caso in ({"elevacion_volumen": [(2772.0, 70.0)]},
                     {"longitud_cresta_m": 0.0}, {"coeficiente": 0.0},
                     {"descarga_fondo_m3s": -1.0}):
            with self.subTest(caso=caso):
                with self.assertRaises(ErrorHidrologia):
                    self._curva(**caso)

    def test_el_bloque_lleva_el_token_del_archivo_y_no_el_de_pantalla(self) -> None:
        # 'Outflow Curve' es la etiqueta de la interfaz; con ella HEC-HMS aborta
        # al abrir el proyecto y NO escribe los logs de corrida.
        bloque = self._bloque()
        self.assertIn("     Route: Modified Puls", bloque)
        self.assertNotIn("Outflow Curve", bloque)
        self.assertIn("     Routing Curve: Storage-Outflow", bloque)
        self.assertIn("     Storage-Outflow Table: Tabla SR", bloque)
        # El estado de operacion del que se parte cambia la salida por un
        # factor de treinta entre el nivel normal y la cresta.
        self.assertIn("     Initial Storage: 70000.0", bloque)

    def test_se_inserta_y_reconecta_el_enlace(self) -> None:
        texto, motivo = m13.enlazar_embalse(
            self.MODELO, "San Rafael", "J25", "R25", self._bloque())
        self.assertEqual(motivo, "")
        self.assertIn("Reservoir: San Rafael", texto)
        # J25 pasa a descargar en el embalse, y el embalse en R25.
        self.assertRegex(texto, r"Junction: J25\s*\n     Downstream: San Rafael")
        self.assertRegex(texto, r"Reservoir: San Rafael[\s\S]*?Downstream: R25")

    def test_correrlo_dos_veces_no_duplica_nada(self) -> None:
        # El M13 se ejecuta muchas veces sobre el mismo modelo: dos elementos
        # con el mismo nombre producen un .basin que HEC-HMS rechaza.
        texto, _ = m13.enlazar_embalse(
            self.MODELO, "San Rafael", "J25", "R25", self._bloque())
        otra, motivo = m13.enlazar_embalse(
            texto, "San Rafael", "J25", "R25", self._bloque())
        self.assertEqual(motivo, "")
        self.assertEqual(otra.count("Reservoir: San Rafael"), 1)
        self.assertEqual(otra.count("Downstream: San Rafael"), 1)

    def test_una_topologia_distinta_de_la_declarada_se_reporta(self) -> None:
        # Reconectar a ciegas moveria el embalse a una rama que no es la suya y
        # laminaria area que no le corresponde.
        _, motivo = m13.enlazar_embalse(
            self.MODELO, "San Rafael", "J25", "R99", self._bloque())
        self.assertIn("no es la declarada", motivo)

    def test_un_nodo_que_no_existe_se_reporta(self) -> None:
        _, motivo = m13.enlazar_embalse(
            self.MODELO, "San Rafael", "J77", "R25", self._bloque())
        self.assertIn("no existe", motivo)



class PruebaFusionDeTransito(unittest.TestCase):
    """
    La tabla de transito la escriben DOS modulos y no se pueden pisar.

    El M13 pone la geometria y el M14 le anade despues la K, la X y la clase de
    pendiente, que son las que el propio M13 lee en la corrida siguiente para
    escribir el modelo. Sobrescribir el archivo entero borraba esas columnas: la
    corrida siguiente caia en la X por omision con celeridad de 1 m/s, y la
    tabla del informe quedaba diciendo una cosa y el modelo otra. No daba error
    en ninguna parte. Ocurrio de verdad y costo encontrarlo.
    """

    ANTERIOR = ("tramo;longitud_m;celeridad_ms;k_min;x;clase_pendiente\n"
                "R1;810;3.0;4.50;0.40;montana\n"
                "R2;500;1.2;6.94;0.20;pie de ladera\n")

    def setUp(self) -> None:
        self.temporal = Path(tempfile.mkdtemp())
        self.ruta = self.temporal / "transito.csv"
        self.ruta.write_text(self.ANTERIOR, encoding="utf-8-sig")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.temporal, ignore_errors=True)

    def _nuevas(self):
        # Lo que el M13 recalcula por su cuenta, con la celeridad unica.
        return [{"tramo": "R1", "longitud_m": 810, "celeridad_ms": 1.0,
                 "k_min": 13.5},
                {"tramo": "R2", "longitud_m": 500, "celeridad_ms": 1.0,
                 "k_min": 8.33}]

    def test_conserva_las_columnas_que_el_m13_no_escribe(self) -> None:
        filas = m13.fusionar_transito(self.ruta, self._nuevas(), ";")
        self.assertEqual(filas[0]["x"], "0.40")
        self.assertEqual(filas[0]["clase_pendiente"], "montana")

    def test_la_celeridad_del_m14_manda_sobre_la_recalculada(self) -> None:
        # Es el fallo que hubo: el M13 devolvia la celeridad a 1 m/s y la K con
        # ella, dejando la tabla y el modelo en desacuerdo.
        filas = m13.fusionar_transito(self.ruta, self._nuevas(), ";")
        self.assertEqual(filas[0]["celeridad_ms"], "3.0")
        self.assertEqual(filas[0]["k_min"], "4.50")

    def test_sin_clase_declarada_el_m13_conserva_lo_suyo(self) -> None:
        # En un estudio donde el M14 aun no ha pasado no hay clase, y entonces
        # la geometria del M13 es la unica que hay.
        self.ruta.write_text(
            "tramo;longitud_m;celeridad_ms;k_min\nR1;810;2.0;6.75\n",
            encoding="utf-8-sig")
        filas = m13.fusionar_transito(self.ruta, self._nuevas()[:1], ";")
        self.assertEqual(filas[0]["celeridad_ms"], 1.0)

    def test_sin_tabla_previa_no_inventa_nada(self) -> None:
        filas = m13.fusionar_transito(self.temporal / "no_existe.csv",
                                      self._nuevas(), ";")
        self.assertEqual(filas, self._nuevas())

    def test_un_tramo_nuevo_no_hereda_de_otro(self) -> None:
        nuevas = self._nuevas() + [{"tramo": "R9", "longitud_m": 100,
                                    "celeridad_ms": 1.0, "k_min": 1.67}]
        filas = m13.fusionar_transito(self.ruta, nuevas, ";")
        self.assertNotIn("clase_pendiente", filas[2])



class PruebaParametrosPorClase(unittest.TestCase):
    """
    El M13 saca la K y la X de la geometria, sin depender del M14.

    POR QUE IMPORTA EL ORDEN. En la cadena el M13 corre ANTES que el M14, y
    antes leia de la tabla que el M14 escribe: en una corrida limpia el modelo
    salia con los parametros de la corrida anterior mientras la tabla llevaba
    los nuevos. Medido sobre el estudio entregado, eso dejo el modelo con X de
    0,497, el valor de Cunge, en lugar del 0,250 de las clases, y un caudal de
    diseno un 26,5 % mas alto en los 251 elementos. Ninguno de los dos productos
    daba error: simplemente se contradecian.
    """

    CLASES = [
        {"nombre": "valle", "pendiente_min_pct": 0.0, "celeridad_ms": 0.7,
         "x": 0.15},
        {"nombre": "ladera", "pendiente_min_pct": 2.0, "celeridad_ms": 2.0,
         "x": 0.30},
        {"nombre": "montana", "pendiente_min_pct": 5.0, "celeridad_ms": 3.0,
         "x": 0.40},
    ]
    # La geometria trae la pendiente en m/m, no en por ciento.
    GEOMETRIAS = {
        "R1": {"longitud_m": 900.0, "pendiente": 0.08},
        "R2": {"longitud_m": 600.0, "pendiente": 0.001},
        "SB1": {"longitud_m": 400.0, "pendiente": 0.05},
    }
    TIPOS = {"R1": "Reach", "R2": "Reach", "SB1": "Subbasin"}

    def test_la_k_es_la_longitud_sobre_la_celeridad_de_su_clase(self) -> None:
        d = m13.parametros_por_clase(self.GEOMETRIAS, self.TIPOS, self.CLASES)
        # 900 m al 8 % son montana: 900 / 3,0 m/s = 300 s = 5 min.
        self.assertAlmostEqual(d["R1"]["k_min"], 5.0)
        self.assertAlmostEqual(d["R1"]["x"], 0.40)
        self.assertEqual(d["R1"]["clase"], "montana")

    def test_la_pendiente_se_lee_en_metro_por_metro(self) -> None:
        # 0,001 m/m son 0,1 %, es decir valle. Leerlo como 0,001 % daria igual,
        # pero leer 0,08 como 0,08 % pondria un canon de montana en el valle.
        d = m13.parametros_por_clase(self.GEOMETRIAS, self.TIPOS, self.CLASES)
        self.assertEqual(d["R2"]["clase"], "valle")
        self.assertAlmostEqual(d["R2"]["k_min"], 600.0 / 0.7 / 60.0)

    def test_solo_los_tramos(self) -> None:
        # La geometria del sqlite trae tambien las subcuencas; sin filtrar, la
        # tabla del informe listaba filas que no son tramos.
        d = m13.parametros_por_clase(self.GEOMETRIAS, self.TIPOS, self.CLASES)
        self.assertNotIn("SB1", d)
        self.assertEqual(set(d), {"R1", "R2"})

    def test_sin_clases_no_devuelve_nada(self) -> None:
        self.assertEqual(
            m13.parametros_por_clase(self.GEOMETRIAS, self.TIPOS, []), {})

    def test_una_longitud_nula_no_produce_una_k_absurda(self) -> None:
        d = m13.parametros_por_clase(
            {"R9": {"longitud_m": 0.0, "pendiente": 0.03}},
            {"R9": "Reach"}, self.CLASES)
        self.assertEqual(d, {})


class PruebaEscenarioSinFactor(unittest.TestCase):
    """
    El segundo escenario de cambio climatico, el de la lluvia registrada.

    POR QUE HACE FALTA. La configuracion declaraba el factor y lo aplicaba,
    pero el informe no podia decir cuanto del caudal venia de la proyeccion y
    cuanto del dato: habia que correr la cadena dos veces cambiando la clave a
    mano. Ahora los dos juegos salen de una sola corrida.
    """

    HIETOGRAMA = [
        {"pluviometro": "Z1", "zona": "Z1", "periodo_retorno": "100",
         "factor_cc": "1.1058", "lamina_mm": 10.0},
        {"pluviometro": "Z1", "zona": "Z1", "periodo_retorno": "100",
         "factor_cc": "1.1058", "lamina_mm": 20.0},
    ]

    def test_deshace_el_factor_dividiendo(self) -> None:
        # El factor es un multiplicador UNICO sobre la lamina: no toca la
        # distribucion de Huff ni el factor de reduccion por area, de modo que
        # dividir devuelve el hietograma que el M12b daria con la clave en
        # falso. Recalcularlo seria repetir la cadena entera para nada.
        copia = m13.duplicar_sin_factor(self.HIETOGRAMA, 1.1058)
        self.assertAlmostEqual(copia[0]["lamina_mm"], 10.0 / 1.1058)
        self.assertAlmostEqual(copia[1]["lamina_mm"], 20.0 / 1.1058)

    def test_los_pluviometros_llevan_sufijo(self) -> None:
        # Dos series con el mismo nombre en el DSS se pisan, y el escenario de
        # referencia acabaria alimentandose de la lluvia de diseno sin que
        # nada lo dijera.
        copia = m13.duplicar_sin_factor(self.HIETOGRAMA, 1.1058)
        for paso in copia:
            self.assertTrue(paso["pluviometro"].endswith(m13.SUFIJO_SIN_FACTOR))

    def test_no_toca_el_original(self) -> None:
        m13.duplicar_sin_factor(self.HIETOGRAMA, 1.1058)
        self.assertEqual(self.HIETOGRAMA[0]["lamina_mm"], 10.0)
        self.assertEqual(self.HIETOGRAMA[0]["pluviometro"], "Z1")

    def test_un_factor_imposible_no_produce_escenario(self) -> None:
        # Sin factor no hay nada que deshacer. Devolver una copia identica
        # duplicaria las corridas sin aportar ningun contraste.
        for factor in (None, 0.0, -1.0):
            with self.subTest(factor=factor):
                self.assertEqual(
                    m13.duplicar_sin_factor(self.HIETOGRAMA, factor), [])

    def test_pedir_dos_y_escribir_uno_se_reporta(self) -> None:
        """
        Es el fallo que hubo, y no dio ninguna senal.

        Los hietogramas del estudio entregado eran de una version anterior del
        M12b, sin la columna 'factor_cc'. La configuracion pedia los dos
        escenarios y el M13 escribio ocho corridas en lugar de dieciseis, en
        silencio: el informe habria comparado el escenario de diseno consigo
        mismo.
        """
        viejos = [{k: v for k, v in paso.items() if k != "factor_cc"}
                  for paso in self.HIETOGRAMA]
        copia, hallazgo = m13.escenario_de_referencia(viejos, True)
        self.assertEqual(copia, [])
        self.assertEqual(hallazgo.clave, "escenarios.sin_factor_en_hietogramas")
        self.assertEqual(hallazgo.severidad, BLOQUEANTE)

    def test_dos_factores_distintos_no_se_pueden_deshacer(self) -> None:
        # Dividir por uno de ellos daria un escenario de referencia falso en
        # los periodos del otro, sin que nada lo dijera.
        mezcla = [dict(self.HIETOGRAMA[0]),
                  dict(self.HIETOGRAMA[1], factor_cc="1.2000")]
        copia, hallazgo = m13.escenario_de_referencia(mezcla, True)
        self.assertEqual(copia, [])
        self.assertEqual(hallazgo.severidad, BLOQUEANTE)

    def test_un_factor_que_no_incrementa_no_da_dos_escenarios(self) -> None:
        # La regla condicional manda no aplicarlo si la proyeccion es a la
        # baja. Entonces los dos escenarios serian el mismo y correr el segundo
        # solo costaria tiempo de computo.
        iguales = [dict(paso, factor_cc="1.0") for paso in self.HIETOGRAMA]
        copia, hallazgo = m13.escenario_de_referencia(iguales, True)
        self.assertEqual(copia, [])
        self.assertEqual(hallazgo.clave, "escenarios.factor_no_aplicado")
        self.assertEqual(hallazgo.severidad, INFORMATIVO)

    def test_con_factor_de_incremento_salen_los_dos(self) -> None:
        copia, hallazgo = m13.escenario_de_referencia(self.HIETOGRAMA, True)
        self.assertEqual(len(copia), len(self.HIETOGRAMA))
        self.assertEqual(hallazgo.clave, "escenarios.dos_escenarios")

    def test_sin_pedirlo_no_se_escribe_ni_se_reporta(self) -> None:
        # Un estudio que no declara la comparacion no tiene por que recibir
        # avisos sobre ella.
        self.assertEqual(
            m13.escenario_de_referencia(self.HIETOGRAMA, False), ([], None))


if __name__ == "__main__":
    unittest.main(verbosity=2)

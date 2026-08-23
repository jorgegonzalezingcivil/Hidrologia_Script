# -*- coding: utf-8 -*-
"""
Pruebas del M05b y del adaptador del índice ONI.

Se verifican contra episodios reales de fecha conocida: el Niño de 1997-98 es la
prueba de que la clasificación es mensual y no anual, porque cruza el cambio de
año.

    python tests/test_m05b.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_DIRECTORIO_SRC = _RAIZ_REPO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import oni  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorFormato  # noqa: E402

try:
    import numpy as np
    import M05b_enso as m05b
    HAY_NUMPY = True
except ImportError:  # pragma: no cover
    HAY_NUMPY = False

_CFG = cargar(raiz=_RAIZ_REPO)
_CRUDO = _RAIZ_REPO / "data" / "01_crudos" / "enso" / "oni.ascii.txt"
HAY_ONI = _CRUDO.is_file()

_CABECERA = " SEAS  YR   TOTAL   ANOM"


def _texto(filas: list[tuple[str, int, float]]) -> str:
    lineas = [_CABECERA]
    for temporada, anio, anomalia in filas:
        lineas.append(f"  {temporada} {anio}  25.00  {anomalia:5.2f}")
    return "\n".join(lineas) + "\n"


class PruebaConvencionDeFase(unittest.TestCase):
    """
    El nombre y el color de cada fase se declaran UNA vez, en comun/oni.py.

    El informe pone juntas las figuras del M05b y las del M06, y basta con que
    una use rojo para el Niño y otra verde para que el lector deje de fiarse de
    las dos. Estas pruebas no comprueban gusto: comprueban que la convención
    existe, que cubre las cuatro fases y que ningún par comparte color, que es
    lo que la haría inservible.
    """

    def test_las_cuatro_fases_tienen_nombre_y_color(self) -> None:
        fases = (oni.FASE_NINO, oni.FASE_NINA, oni.FASE_NEUTRAL,
                 oni.FASE_COMPUESTA)
        for fase in fases:
            self.assertIn(fase, oni.NOMBRE_DE_FASE)
            self.assertIn(fase, oni.COLOR_DE_FASE)
            self.assertIn(fase, oni.RELLENO_DE_FASE)

    def test_ninguna_fase_comparte_color_con_otra(self) -> None:
        # Dos fases del mismo color hacen ilegible cualquier figura que las
        # ponga juntas, que son casi todas las del capitulo.
        self.assertEqual(len(set(oni.COLOR_DE_FASE.values())),
                         len(oni.COLOR_DE_FASE))
        self.assertEqual(len(set(oni.RELLENO_DE_FASE.values())),
                         len(oni.RELLENO_DE_FASE))

    def test_los_colores_son_hexadecimales(self) -> None:
        for tabla in (oni.COLOR_DE_FASE, oni.RELLENO_DE_FASE):
            for fase, color in tabla.items():
                self.assertRegex(color, r"^#[0-9a-fA-F]{6}$", fase)

    def test_el_calido_es_rojo_y_el_frio_azul(self) -> None:
        """
        La convención del NOAA y del IDEAM, que es contra quien se contrasta.

        Se comprueba por el canal dominante y no por el valor exacto, para que
        ajustar el tono no rompa la prueba pero invertir la convención sí.
        """
        def canales(color: str) -> tuple[int, int, int]:
            return (int(color[1:3], 16), int(color[3:5], 16),
                    int(color[5:7], 16))

        for tabla in (oni.COLOR_DE_FASE, oni.RELLENO_DE_FASE):
            rojo, _verde, azul = canales(tabla[oni.FASE_NINO])
            self.assertGreater(rojo, azul, "El Niño debe tirar a rojo")
            rojo, _verde, azul = canales(tabla[oni.FASE_NINA])
            self.assertGreater(azul, rojo, "La Niña debe tirar a azul")

    def test_ningun_modulo_declara_su_propia_tabla(self) -> None:
        # La regresión que se quiere evitar: una figura que vuelve a inventar
        # los colores en su propio archivo.
        raiz = Path(__file__).resolve().parents[1] / "src"
        culpables = []
        for archivo in raiz.glob("M*.py"):
            texto = archivo.read_text(encoding="utf-8", errors="replace")
            if "FASE_NINO:" in texto and "COLOR_DE_FASE" not in texto:
                culpables.append(archivo.name)
        self.assertEqual(culpables, [])


class PruebaMesCentral(unittest.TestCase):
    """
    Cada temporada de tres meses se sitúa en su mes central.

    Es lo que permite clasificar mes a mes. Sin esta correspondencia solo cabe
    una etiqueta por año, que es el defecto de la rutina heredada.
    """

    def test_las_doce_temporadas_cubren_los_doce_meses(self) -> None:
        self.assertEqual(sorted(oni.MES_CENTRAL.values()), list(range(1, 13)))

    def test_djf_es_enero_y_ndj_es_diciembre(self) -> None:
        self.assertEqual(oni.MES_CENTRAL["DJF"], 1)
        self.assertEqual(oni.MES_CENTRAL["NDJ"], 12)

    def test_el_registro_conoce_su_mes(self) -> None:
        registro = oni.RegistroONI("JJA", 2000, 27.0, 0.8)
        self.assertEqual(registro.mes, 7)
        self.assertEqual(registro.clave, (2000, 7))


class PruebaInterpretacion(unittest.TestCase):
    def test_lee_un_archivo_bien_formado(self) -> None:
        registros = oni.interpretar(_texto([("DJF", 1950, -1.32),
                                            ("JFM", 1950, -1.20)]))
        self.assertEqual(len(registros), 2)
        self.assertAlmostEqual(registros[0].anomalia, -1.32)

    def test_una_cabecera_distinta_detiene_el_modulo(self) -> None:
        # Si la NOAA reordena columnas, leer por posición daría una
        # clasificación errónea sin ninguna señal.
        with self.assertRaises(ErrorFormato) as contexto:
            oni.interpretar(" YR  SEAS  ANOM  TOTAL\n  DJF 1950 1.0 2.0\n")
        self.assertIn("cabecera", str(contexto.exception))

    def test_una_temporada_inventada_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            oni.interpretar(_texto([("XYZ", 1950, 0.1)]))

    def test_una_linea_incompleta_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            oni.interpretar(_CABECERA + "\n  DJF 1950  25.00\n")

    def test_archivo_vacio_es_error(self) -> None:
        with self.assertRaises(ErrorFormato):
            oni.interpretar("   \n")


class PruebaClasificacion(unittest.TestCase):
    """
    La definición oficial exige que el umbral se sostenga.

    Una temporada aislada por encima del umbral no constituye episodio: sin ese
    control, cualquier oscilación breve inflaría el conteo.
    """

    TEMPORADAS = list(oni.MES_CENTRAL)

    def _serie(self, anomalias: list[float]) -> list[oni.RegistroONI]:
        registros = []
        for indice, valor in enumerate(anomalias):
            registros.append(oni.RegistroONI(
                self.TEMPORADAS[indice % 12], 2000 + indice // 12, 27.0, valor))
        return registros

    def test_una_racha_larga_es_episodio(self) -> None:
        filas = oni.clasificar(self._serie([1.0] * 6 + [0.0] * 6),
                               umbral=0.5, consecutivas=5)
        self.assertEqual([f["fase"] for f in filas[:6]],
                         [oni.FASE_NINO] * 6)

    def test_una_racha_corta_no_es_episodio(self) -> None:
        filas = oni.clasificar(self._serie([1.0] * 4 + [0.0] * 8),
                               umbral=0.5, consecutivas=5)
        self.assertTrue(all(f["fase"] == oni.FASE_NEUTRAL for f in filas))

    def test_sin_exigir_racha_el_umbral_manda(self) -> None:
        filas = oni.clasificar(self._serie([1.0] * 4 + [0.0] * 8),
                               umbral=0.5, consecutivas=5,
                               exigir_consecutivas=False)
        self.assertEqual(sum(1 for f in filas if f["fase"] == oni.FASE_NINO), 4)

    def test_la_fase_por_umbral_se_conserva_siempre(self) -> None:
        # Permite reportar cuántas temporadas cambian por exigir la racha.
        filas = oni.clasificar(self._serie([1.0] * 4 + [0.0] * 8),
                               umbral=0.5, consecutivas=5)
        self.assertEqual(sum(1 for f in filas
                             if f["fase_por_umbral"] == oni.FASE_NINO), 4)

    def test_la_niña_se_reconoce_por_el_signo(self) -> None:
        filas = oni.clasificar(self._serie([-1.0] * 6 + [0.0] * 6),
                               umbral=0.5, consecutivas=5)
        self.assertEqual(filas[0]["fase"], oni.FASE_NINA)

    def test_episodios_distintos_reciben_numero_distinto(self) -> None:
        filas = oni.clasificar(
            self._serie([1.0] * 5 + [0.0] * 5 + [-1.0] * 5 + [0.0] * 9),
            umbral=0.5, consecutivas=5)
        episodios = {f["episodio"] for f in filas if f["episodio"] is not None}
        self.assertEqual(len(episodios), 2)

    def test_serie_vacia(self) -> None:
        self.assertEqual(oni.clasificar([]), [])


@unittest.skipUnless(HAY_ONI, "no hay copia local del índice ONI")
class PruebaIndiceReal(unittest.TestCase):
    """
    Contraste contra episodios de fecha conocida.

    El Niño de 1997-98 es la prueba de que la clasificación es mensual: va de
    mayo de 1997 a abril de 1998 y cruza el cambio de año, de modo que una
    etiqueta por año calendario no puede representarlo.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.filas = oni.clasificar(
            oni.interpretar(oni.leer(_CRUDO)), umbral=0.5, consecutivas=5)
        cls.por_clave = {(f["anio"], f["mes"]): f for f in cls.filas}

    def test_el_nino_de_1997_98_es_continuo_a_traves_del_anio(self) -> None:
        for clave in ((1997, 8), (1997, 12), (1998, 1), (1998, 3)):
            self.assertEqual(self.por_clave[clave]["fase"], oni.FASE_NINO, clave)

    def test_el_pico_de_1997_supera_dos_grados(self) -> None:
        self.assertGreater(self.por_clave[(1997, 12)]["anomalia"], 2.0)

    def test_la_niña_de_1999_2000_se_reconoce(self) -> None:
        self.assertEqual(self.por_clave[(1999, 12)]["fase"], oni.FASE_NINA)

    def test_las_tres_fases_estan_representadas(self) -> None:
        conteo = oni.resumen_por_fase(self.filas)
        for fase in (oni.FASE_NINO, oni.FASE_NINA, oni.FASE_NEUTRAL):
            self.assertGreater(conteo[fase], 0, fase)

    def test_lo_neutral_es_la_condicion_mas_frecuente(self) -> None:
        conteo = oni.resumen_por_fase(self.filas)
        self.assertGreater(conteo[oni.FASE_NEUTRAL],
                           conteo[oni.FASE_NINO] + conteo[oni.FASE_NINA] - 1)


@unittest.skipUnless(HAY_NUMPY, "numpy no está instalado")
class PruebaAgregacion(unittest.TestCase):
    def setUp(self) -> None:
        self.codigos = ["A"]
        self.claves = [(2000 + a, m) for a in range(4) for m in range(1, 13)]
        # Niño en el primer año, neutral el resto; el Niño llueve la mitad.
        self.fases = {}
        for anio, mes in self.claves:
            self.fases[(anio, mes)] = (oni.FASE_NINO if anio == 2000
                                       else oni.FASE_NEUTRAL)
        self.valores = np.array([[50.0 if a == 2000 else 100.0]
                                 for a, _ in self.claves])

    def test_el_ciclo_separa_las_fases(self) -> None:
        ciclo = m05b.ciclo_anual_por_fase(
            self.codigos, self.claves, self.valores, self.fases)
        nino = [f for f in ciclo if f["fase"] == oni.FASE_NINO]
        neutral = [f for f in ciclo if f["fase"] == oni.FASE_NEUTRAL]
        self.assertEqual(len(nino), 12)
        self.assertAlmostEqual(nino[0]["media_mm"], 50.0)
        self.assertAlmostEqual(neutral[0]["media_mm"], 100.0)

    def test_el_total_suma_las_doce_medias(self) -> None:
        ciclo = m05b.ciclo_anual_por_fase(
            self.codigos, self.claves, self.valores, self.fases)
        totales = {t["fase"]: t for t in m05b.totales_por_fase(ciclo)}
        self.assertAlmostEqual(totales[oni.FASE_NINO]["total_anual_mm"], 600.0)
        self.assertAlmostEqual(totales[oni.FASE_NEUTRAL]["total_anual_mm"], 1200.0)
        self.assertTrue(totales[oni.FASE_NINO]["completo"])

    def test_un_total_al_que_le_faltan_meses_se_marca(self) -> None:
        # Comparar un total incompleto con otro completo daría una diferencia
        # que es del muestreo y no del clima.
        fases = dict(self.fases)
        for mes in range(1, 13):
            if mes > 6:
                fases[(2000, mes)] = oni.FASE_NEUTRAL
        ciclo = m05b.ciclo_anual_por_fase(
            self.codigos, self.claves, self.valores, fases)
        totales = {t["fase"]: t for t in m05b.totales_por_fase(ciclo)}
        self.assertFalse(totales[oni.FASE_NINO]["completo"])
        self.assertEqual(totales[oni.FASE_NINO]["meses_del_anio"], 6)

    def test_el_contraste_solo_usa_totales_completos(self) -> None:
        ciclo = m05b.ciclo_anual_por_fase(
            self.codigos, self.claves, self.valores, self.fases)
        contraste = m05b.contraste_entre_fases(m05b.totales_por_fase(ciclo))
        self.assertEqual(len(contraste), 1)
        self.assertAlmostEqual(contraste[0]["nino_pct"], -50.0)

    def test_sin_fase_neutral_no_hay_contraste(self) -> None:
        fases = {c: oni.FASE_NINO for c in self.claves}
        ciclo = m05b.ciclo_anual_por_fase(
            self.codigos, self.claves, self.valores, fases)
        self.assertEqual(m05b.contraste_entre_fases(
            m05b.totales_por_fase(ciclo)), [])


class PruebaInvarianteEnso(unittest.TestCase):
    def test_el_enso_nunca_elimina_registros(self) -> None:
        # CLAUDE.md, sección 6: "No elimina estaciones ni registros. Solo
        # clasifica". El esquema lo eleva a BLOQUEANTE si se activa.
        self.assertFalse(_CFG.obtener("enso.elimina_registros"))

    def test_el_criterio_es_el_oficial(self) -> None:
        self.assertEqual(_CFG.obtener("enso.criterio"), "consecutivo")
        self.assertEqual(int(_CFG.obtener("enso.temporadas_consecutivas")), 5)
        self.assertAlmostEqual(
            float(_CFG.obtener("enso.umbral_anomalia_c")), 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

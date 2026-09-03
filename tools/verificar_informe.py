# -*- coding: utf-8 -*-
"""
Contrasta las cifras escritas en el informe contra los productos de la cadena.

POR QUE HACE FALTA. Los analisis del informe citan numeros concretos: 220,31
km2, 13 estaciones, un IRH de 0,70, un caudal de 73,67 m3/s. Si la cadena se
vuelve a correr y alguno cambia, el texto sigue diciendo el valor viejo y NADA
lo advierte: el informe se genera igual, con las tablas y las figuras nuevas y
la prosa vieja. Es el mismo modo de fallo de la prosa heredada, pero producido
por nosotros mismos.

COMO FUNCIONA. Cada fila declara la cifra tal como aparece en el texto y de
donde sale en los productos. Se compara el valor formateado, no el numero
crudo, porque lo que hay que verificar es lo que el lector ve.

    python verificar_informe.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ESTUDIO = Path(r"C:\Estudios\refugio_del_valle")
PROCESADO = ESTUDIO / "data" / "02_procesado"
ANALISIS = ESTUDIO / "config" / "analisis.yaml"


def leer(nombre: str) -> dict:
    """El JSON de un modulo, ya desenvuelto de su clave 'resultado'."""
    datos = json.loads((PROCESADO / f"{nombre}.json").read_text(
        encoding="utf-8"))
    return datos.get("resultado", datos)


def hondo(datos, ruta: str):
    """Valor de una ruta con puntos, o None si no esta."""
    actual = datos
    for parte in ruta.split("."):
        if isinstance(actual, dict) and parte in actual:
            actual = actual[parte]
        else:
            return None
    return actual


# (modulo, ruta, formato, texto tal como aparece en el informe)
CIFRAS = (
    ("M04b_sensibilidad", "estaciones", "{:.0f}", "74"),
    ("M04b_sensibilidad", "estaciones_del_m03", "{:.0f}", "55"),
    ("M04b_sensibilidad", "por_estado.vigente", "{:.0f}", "28"),
    ("M05_precipitacion", "estaciones_admitidas", "{:.0f}", "13"),
    ("M05_precipitacion", "meses_mensual", "{:.0f}", "6.973"),
    ("M05_precipitacion", "meses_completados", "{:.0f}", "1.134"),
    ("M05_precipitacion", "anomalos", "{:.0f}", "172"),
    ("M05_precipitacion", "registros_excluidos", "{:.0f}", "610"),
    ("M05_precipitacion", "correlaciones.parejas", "{:.0f}", "105"),
    ("M05_precipitacion", "correlaciones.percentiles.p50", "{:.3f}", "0,585"),
    ("M05b_enso", "temporadas", "{:.0f}", "919"),
    ("M05b_enso", "por_fase.nino", "{:.0f}", "215"),
    ("M05b_enso", "por_fase.nina", "{:.0f}", "221"),
    ("M05b_enso", "por_fase.neutral", "{:.0f}", "483"),
    ("M10_morfometria", "magnitudes.area_km2", "{:.2f}", "220,31"),
    ("M10_morfometria", "drenaje.long_cauce_principal_km", "{:.2f}", "48,95"),
    ("M10_morfometria", "drenaje.cota_nacimiento", "{:.2f}", "3.288,58"),
    ("M10_morfometria", "drenaje.cota_cierre", "{:.2f}", "2.592,09"),
    ("M10_morfometria", "drenaje.desnivel_cauce_m", "{:.2f}", "696,49"),
    ("M10_morfometria", "drenaje.pendiente_media_cauce_pct", "{:.3f}", "1,423"),
    ("M10_morfometria", "drenaje.indice_sinuosidad", "{:.3f}", "2,852"),
    ("M10_morfometria", "drenaje.densidad_drenaje_km_km2", "{:.4f}", "1,5719"),
    ("M10_morfometria", "drenaje.corrientes_totales", "{:.0f}", "156"),
    ("M10_morfometria", "suelos.cn_ponderado", "{:.1f}", "73,8"),
    ("M10_morfometria", "suelos.cobertura_pct", "{:.2f}", "98,75"),
    ("M10_morfometria", "suelos.pct_dual", "{:.2f}", "10,04"),
    ("M10_morfometria", "tiempo_concentracion.formulas_aplicables", "{:.0f}", "6"),
    ("M10_morfometria", "tiempo_concentracion.estadisticos.mediana", "{:.4f}", "10,6684"),
    ("M10_morfometria", "tiempo_concentracion.estadisticos.cv", "{:.4f}", "0,6659"),
    ("M11c_arf", "adoptado.arf_serie_24h", "{:.4f}", "0,9372"),
    ("M11c_arf", "adoptado.arf_diseno", "{:.4f}", "0,8558"),
    ("M12a_idf", "cambio_climatico_adoptado.cambio_pct", "{:.3f}", "10,583"),
    ("M12a_idf", "cambio_climatico_adoptado.factor_aplicado", "{:.4f}", "1,1058"),
    ("M12a_idf", "cambio_climatico_adoptado.minimo_pct", "{:.1f}", "3,9"),
    ("M12a_idf", "cambio_climatico_adoptado.maximo_pct", "{:.1f}", "17,7"),
    ("M12a_idf", "anclaje.diferencia_maxima_pct", "{:.1f}", "6,2"),
    ("M18_balance", "contraste.p_media_cuenca_mm", "{:.1f}", "991,1"),
    ("M18_balance", "contraste.caudal_budyko_m3s", "{:.4f}", "2,2818"),
    ("M18_balance", "contraste.caudal_dekop_m3s", "{:.4f}", "1,8168"),
    ("M18_balance", "contraste.caudal_turc_m3s", "{:.4f}", "3,0577"),
    ("M18_balance", "contraste.diferencia_pct", "{:.2f}", "1,56"),
    ("M18_balance", "contraste.cobertura.pct_extrapolado", "{:.2f}", "17,79"),
    ("M18_balance", "contraste.cobertura.area_sobre_estaciones_km2", "{:.1f}", "39,2"),
    ("M18a_temperatura", "cobertura.t_media_cuenca_c", "{:.2f}", "11,34"),
    ("M18a_temperatura", "cobertura.cota_media_cuenca_m", "{:.1f}", "2.983,7"),
    ("M18a_temperatura", "cobertura.etp.etp_multianual_mm", "{:.2f}", "936,77"),
    ("M18a_temperatura", "cobertura.etp.etp_sin_ajustar_mm", "{:.2f}", "609,66"),
    ("M18a_temperatura", "cobertura.etp.factor_aplicado", "{:.5f}", "1,53654"),
    ("M18a_temperatura", "cobertura.etp.discrepancia_pct", "{:.2f}", "-34,92"),
    ("M18b_infiltracion", "resumen.c_medio", "{:.4f}", "0,4934"),
    ("M18b_infiltracion", "resumen.infiltracion_anual_mm", "{:.1f}", "424,9"),
    ("M18b_infiltracion", "resumen.retencion_anual_mm", "{:.1f}", "117,4"),
    ("M18b_infiltracion", "resumen.escorrentia_del_balance_anual_mm", "{:.1f}", "332,2"),
    ("M19_duracion", "resumen.meses", "{:.0f}", "456"),
    ("M19_duracion", "resumen.indice_variabilidad_q10_q90", "{:.2f}", "12,36"),
    ("M19_duracion", "resumen.fraccion_bajo_la_media", "{:.4f}", "0,6118"),
    ("M19_duracion", "irh.irh", "{:.4f}", "0,6957"),
    ("M19_duracion", "caudal_ambiental.q95_m3s", "{:.5f}", "0,17463"),
    ("M19_duracion", "caudal_ambiental.qirh_m3s", "{:.5f}", "0,55116"),
    ("M19_duracion", "caudal_ambiental.caudal_disponible_m3s", "{:.5f}", "1,73064"),
    ("M19_duracion", "caudal_ambiental.reserva_pct", "{:.2f}", "24,15"),
)

CAUDALES = (("2.33", "13,98", "11,05"), ("25", "44,60", "36,07"),
            ("100", "73,67", "60,28"), ("500", "126,15", "104,52"))


def formatear(valor, formato: str) -> str:
    """
    El numero como se escribe en el informe: coma decimal y punto de miles.

    EL SEPARADOR DE MILES HAY QUE PONERLO. Sin el, 3288,58 no coincidia con el
    3.288,58 que el informe escribe y el contraste lo daba por discrepancia:
    tres falsos positivos que enmascaraban los dos cambios reales.
    """
    entero, _, decimal = formato.format(float(valor)).partition(".")
    signo, digitos = ("-", entero[1:]) if entero.startswith("-") else ("", entero)
    grupos = []
    while len(digitos) > 3:
        grupos.insert(0, digitos[-3:])
        digitos = digitos[:-3]
    grupos.insert(0, digitos)
    texto = signo + ".".join(grupos)
    return f"{texto},{decimal}" if decimal else texto


def main() -> int:
    if not ANALISIS.is_file():
        raise SystemExit(f"no esta {ANALISIS}")
    texto = ANALISIS.read_text(encoding="utf-8")

    cache: dict[str, dict] = {}
    discrepan, ausentes, sin_citar = [], [], []
    for modulo, ruta, formato, escrito in CIFRAS:
        if modulo not in cache:
            try:
                cache[modulo] = leer(modulo)
            except FileNotFoundError:
                ausentes.append(f"{modulo}: no hay producto")
                cache[modulo] = {}
        valor = hondo(cache[modulo], ruta)
        if valor is None:
            ausentes.append(f"{modulo}.{ruta}")
            continue
        actual = formatear(valor, formato)
        if actual != escrito:
            discrepan.append(f"{modulo}.{ruta}: el informe dice {escrito} y la "
                             f"cadena da {actual}")
        elif escrito not in texto:
            # La cifra coincide pero no aparece en el texto: o se escribio de
            # otra forma, o el analisis que la citaba se quito.
            sin_citar.append(f"{modulo}.{ruta} = {escrito}")

    # LOS CAUDALES DE DISENO son el resultado del estudio y salen de la tabla
    # de escenarios, no de un JSON: se contrastan aparte. Si alguno cambia,
    # cambia el informe entero.
    import csv

    ruta = PROCESADO / "hidrologia" / "escenarios_cc.csv"
    if ruta.is_file():
        with ruta.open(encoding="utf-8-sig", newline="") as manejador:
            filas = {f["periodo_retorno"]: f
                     for f in csv.DictReader(manejador, delimiter=";")}
        for periodo, diseno, referencia in CAUDALES:
            fila = filas.get(periodo)
            if fila is None:
                ausentes.append(f"escenarios_cc: falta el periodo {periodo}")
                continue
            for columna, escrito in (("q_diseno_m3s", diseno),
                                     ("q_referencia_m3s", referencia)):
                actual = formatear(fila[columna], "{:.2f}")
                if actual != escrito:
                    discrepan.append(
                        f"Tr {periodo} {columna}: el informe dice {escrito} y "
                        f"la cadena da {actual}")
                elif escrito not in texto:
                    sin_citar.append(f"Tr {periodo} {columna} = {escrito}")
    else:
        ausentes.append("escenarios_cc.csv: no esta")

    print(f"{len(CIFRAS) + 2 * len(CAUDALES)} cifra(s) contrastadas contra los "
          "productos.\n")
    for titulo, filas in (("DISCREPAN", discrepan),
                          ("SIN PRODUCTO", ausentes),
                          ("NO SE ENCUENTRAN EN EL TEXTO", sin_citar)):
        print(f"{titulo}: {len(filas)}")
        for fila in filas:
            print(f"   - {fila}")
        print()
    return 1 if discrepan or ausentes else 0


if __name__ == "__main__":
    sys.exit(main())

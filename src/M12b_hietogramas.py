#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M12b - Hietogramas de diseño por el método de Huff
==================================================
Entorno: venv del proyecto.

CIERRA LA CADENA DE LLUVIA. Toma la lámina puntual que el M11 promedió sobre
cada subcuenca, le aplica lo que falta y la reparte en el tiempo. Lo que entrega
es lo que HEC-HMS lee: una serie de precipitación por intervalo, por subcuenca y
por periodo de retorno.

AQUÍ SE APLICA EL FACTOR DE REDUCCIÓN POR ÁREA, UNA SOLA VEZ. El M11c lo evalúa
y no lo aplica, porque la lámina que él tiene es de 24 horas y la de diseño es de
otra duración: el factor que corresponde es el de la duración de diseño y solo
existe cuando la lámina ya está desagregada. Repartirlo entre dos módulos daba
el mismo número y confiaba en que nadie olvidase la segunda mitad.

EL ORDEN IMPORTA Y ES ESTE: lámina de diseño, factor de reducción por área,
factor de cambio climático, reparto temporal. Los dos factores son
multiplicativos y conmutan entre sí, pero el reparto va necesariamente al final:
distribuir primero y corregir después obligaría a reescalar cada intervalo, que
es la misma operación hecha en más pasos.

POR QUÉ HUFF Y NO BLOQUES ALTERNOS. El pico no queda en el centro del hietograma
sino desplazado, con una forma que se parece a la de un hidrograma real. Se
adopta el segundo cuartil con 50% de probabilidad de excedencia (CLAUDE.md,
sección 6): tormentas de severidad media, ni la más extrema ni la más suave.

Productos:
    data/02_procesado/tormenta/hietogramas.csv
    data/02_procesado/tormenta/hietograma_resumen.csv
    data/05_resultados/graficos/M12b_hietograma_*.png y .svg
    data/02_procesado/M12b_hietogramas.json

Uso:
    python src/M12b_hietogramas.py

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M12b"
DESCRIPCION = "Hietogramas de diseño por el método de Huff"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3


@dataclass
class ResultadoM12b:
    curva: list[dict[str, float]] = field(default_factory=list)
    factores: dict[str, Any] = field(default_factory=dict)
    hietogramas: list[dict[str, Any]] = field(default_factory=list)
    resumen: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def leer_curva_huff(
    ruta: Path, delimitador: str, cuartil: int, probabilidad: float,
) -> list[dict[str, float]]:
    """
    Curva acumulada de Huff para un cuartil y una probabilidad de excedencia.

    Es doctrina y vive en data/referencia. Se valida su forma al leerla, porque
    una curva mal transcrita no da error en ninguna parte: reparte la misma
    lámina de otra manera y produce un hidrograma verosímil y equivocado.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si no hay filas para ese cuartil y probabilidad, si la curva no empieza
        en cero, no termina en cien o no es creciente.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la tabla de Huff en {ruta}.")

    puntos: list[dict[str, float]] = []
    origen = ""
    validado = False
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            try:
                if int(fila["cuartil"]) != cuartil:
                    continue
                if abs(float(fila["probabilidad_pct"]) - probabilidad) > 1e-9:
                    continue
                puntos.append({"tiempo_pct": float(fila["tiempo_pct"]),
                               "precipitacion_pct": float(
                                   fila["precipitacion_pct"])})
            except (KeyError, TypeError, ValueError) as exc:
                raise ErrorFormato(
                    f"{ruta.name}: fila ilegible ({exc}).") from exc
            origen = origen or str(fila.get("origen", "")).strip()
            validado = str(fila.get("validado", "")).strip().lower() in (
                "si", "sí", "true")

    if not puntos:
        raise ErrorFormato(
            f"{ruta.name} no trae la curva del cuartil {cuartil} con "
            f"{probabilidad:g} % de probabilidad de excedencia.")

    puntos.sort(key=lambda p: p["tiempo_pct"])
    if puntos[0]["tiempo_pct"] != 0.0 or puntos[0]["precipitacion_pct"] != 0.0:
        raise ErrorFormato(
            f"{ruta.name}: la curva debe empezar en (0, 0) y empieza en "
            f"({puntos[0]['tiempo_pct']}, {puntos[0]['precipitacion_pct']}).")
    if puntos[-1]["tiempo_pct"] != 100.0 or puntos[-1]["precipitacion_pct"] != 100.0:
        raise ErrorFormato(
            f"{ruta.name}: la curva debe terminar en (100, 100) y termina en "
            f"({puntos[-1]['tiempo_pct']}, {puntos[-1]['precipitacion_pct']}).")
    for anterior, siguiente in zip(puntos, puntos[1:]):
        if siguiente["precipitacion_pct"] < anterior["precipitacion_pct"]:
            raise ErrorFormato(
                f"{ruta.name}: la curva acumulada decrece entre "
                f"{anterior['tiempo_pct']:.0f} % y "
                f"{siguiente['tiempo_pct']:.0f} % del tiempo. Una lluvia "
                "acumulada no puede disminuir.")

    for punto in puntos:
        punto["origen"] = origen
        punto["validado"] = validado
    return puntos


def acumulada_en(curva: Sequence[dict[str, float]], tiempo_pct: float) -> float:
    """
    Fracción acumulada de lluvia en un porcentaje del tiempo, interpolando.

    La curva se tabula en deciles y el intervalo de cálculo rara vez cae en
    uno: con tres horas y pasos de cinco minutos, cada intervalo es el 2,78 %
    de la duración. Se interpola linealmente entre los dos deciles que lo
    encierran, que es como se lee la curva original.
    """
    if tiempo_pct <= curva[0]["tiempo_pct"]:
        return curva[0]["precipitacion_pct"]
    if tiempo_pct >= curva[-1]["tiempo_pct"]:
        return curva[-1]["precipitacion_pct"]
    for anterior, siguiente in zip(curva, curva[1:]):
        if anterior["tiempo_pct"] <= tiempo_pct <= siguiente["tiempo_pct"]:
            tramo = siguiente["tiempo_pct"] - anterior["tiempo_pct"]
            if tramo <= 0:
                return anterior["precipitacion_pct"]
            peso = (tiempo_pct - anterior["tiempo_pct"]) / tramo
            return (anterior["precipitacion_pct"] * (1 - peso)
                    + siguiente["precipitacion_pct"] * peso)
    return curva[-1]["precipitacion_pct"]


def repartir(
    lamina_mm: float, duracion_min: float, intervalo_min: float,
    curva: Sequence[dict[str, float]],
) -> list[dict[str, float]]:
    """
    Reparte una lámina en intervalos según la curva acumulada de Huff.

    Cada intervalo recibe la DIFERENCIA de la acumulada entre su final y su
    principio. Restar acumuladas y no interpolar intensidades garantiza que la
    suma de los intervalos sea exactamente la lámina de partida: cualquier otra
    forma deja un residuo que se arrastra al volumen de escorrentía.

    Excepciones
    -----------
    ErrorHidrologia
        Si la duración no es múltiplo del intervalo. Un intervalo truncado al
        final repartiría menos lámina de la que corresponde sin decirlo.
    """
    if lamina_mm < 0 or duracion_min <= 0 or intervalo_min <= 0:
        raise ErrorHidrologia(
            f"lámina ({lamina_mm}), duración ({duracion_min} min) e intervalo "
            f"({intervalo_min} min) deben ser positivos.")
    cuantos = duracion_min / intervalo_min
    if abs(cuantos - round(cuantos)) > 1e-9:
        raise ErrorHidrologia(
            f"la duración de {duracion_min:.0f} min no es múltiplo del "
            f"intervalo de {intervalo_min:.0f} min: el último intervalo "
            "quedaría truncado y repartiría menos lámina de la que "
            "corresponde.")

    intervalos: list[dict[str, float]] = []
    acumulado_previo = 0.0
    for paso in range(int(round(cuantos))):
        fin_min = (paso + 1) * intervalo_min
        fraccion = acumulada_en(curva, 100.0 * fin_min / duracion_min) / 100.0
        acumulado = lamina_mm * fraccion
        incremento = acumulado - acumulado_previo
        intervalos.append({
            "intervalo": paso + 1,
            "minuto_inicio": paso * intervalo_min,
            "minuto_fin": fin_min,
            "lamina_mm": round(incremento, 4),
            "intensidad_mm_h": round(incremento * 60.0 / intervalo_min, 3),
            "acumulado_mm": round(acumulado, 4),
        })
        acumulado_previo = acumulado
    return intervalos


def resumir_hietograma(
    intervalos: Sequence[dict[str, float]], lamina_mm: float,
) -> dict[str, Any]:
    """
    Comprueba el reparto y describe su forma.

    LA SUMA DEBE SER LA LÁMINA. Es la verificación que atrapa una curva mal
    leída o un intervalo perdido, y por eso el residuo viaja al reporte en lugar
    de darse por bueno.

    El instante del pico dice en qué cuartil cae: es lo que distingue una curva
    de Huff del segundo cuartil de otra, y si no cae donde debe, la curva
    adoptada no es la que se cree.
    """
    if not intervalos:
        return {"error": "sin intervalos"}
    suma = sum(i["lamina_mm"] for i in intervalos)
    pico = max(intervalos, key=lambda i: i["lamina_mm"])
    duracion = intervalos[-1]["minuto_fin"]
    return {
        "lamina_mm": round(lamina_mm, 3),
        "suma_intervalos_mm": round(suma, 3),
        "residuo_mm": round(suma - lamina_mm, 6),
        "intervalos": len(intervalos),
        "pico_mm": pico["lamina_mm"],
        "pico_intensidad_mm_h": pico["intensidad_mm_h"],
        "pico_minuto": pico["minuto_fin"],
        "pico_fraccion_del_tiempo": round(pico["minuto_fin"] / duracion, 3),
        "cuartil_del_pico": min(4, int(pico["minuto_fin"] / duracion * 4) + 1)
        if duracion else None,
    }


def leer_tabla(ruta: Path, delimitador: str) -> list[dict[str, str]]:
    """Lee una tabla de la cadena, con error explícito si falta."""
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra {ruta}.")
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        raise ErrorFormato(f"{ruta.name} está vacío.")
    return filas


def factor_arf(filas: Sequence[dict[str, str]], duracion_h: float) -> float:
    """
    Factor de reducción por área para la duración de diseño, del M11c.

    Se toma el que ese módulo calculó para esta duración y NO se recalcula: si
    dos partes del estudio interpolasen la misma tabla por su cuenta, una
    discrepancia entre ellas no tendría dónde detectarse.

    Excepciones
    -----------
    ErrorHidrologia
        Si el M11c no dejó el factor de esta duración.
    """
    for fila in filas:
        try:
            if abs(float(fila["duracion_h"]) - duracion_h) < 1e-9:
                return float(fila["arf"])
        except (KeyError, TypeError, ValueError):
            continue
    raise ErrorHidrologia(
        f"el M11c no calculó el factor de reducción para {duracion_h:g} h. "
        "Volver a ejecutarlo con la duración de diseño declarada.")


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Construye los hietogramas de diseño de cada subcuenca y periodo."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM12b()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M12b_hietogramas.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "precipitacion por subcuenca":
                "data/02_procesado/precipitacion/precipitacion_por_subcuenca.csv",
            "factor de reduccion": "data/02_procesado/precipitacion/arf.csv",
            "curva de Huff": configuracion.obtener("tormenta.huff.tabla"),
        },
        parametros=configuracion.parametros("tormenta"))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    duracion_h = float(configuracion.obtener("tormenta.duracion_h"))
    duracion_min = duracion_h * 60.0
    intervalo_min = float(configuracion.obtener("tormenta.intervalo_calculo_min"))
    hipotesis = str(configuracion.obtener(
        "tormenta.hipotesis_adoptada", "") or "").strip().lower()

    if not hipotesis:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "tormenta.sin_hipotesis",
            "tormenta.hipotesis_adoptada esta en null: sin declarar como se "
            "pasa de la lamina de 24 h a la duracion de diseno no hay "
            "hietograma que construir. El M12a calcula las tres y el consultor "
            "decide.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    directorio_precipitacion = (rutas.directorio("procesado", base)
                                / "precipitacion")
    try:
        subcuencas = leer_tabla(
            directorio_precipitacion / "precipitacion_por_subcuenca.csv",
            delimitador)
        arf = factor_arf(leer_tabla(directorio_precipitacion / "arf.csv",
                                    delimitador), duracion_h)
        resultado.curva = leer_curva_huff(
            rutas.resolver(configuracion.obtener("tormenta.huff.tabla"), base),
            delimitador,
            int(configuracion.obtener("tormenta.huff.cuartil")),
            float(configuracion.obtener("tormenta.huff.probabilidad_excedencia")))
    except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "hietograma.insumos", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    factor_cc, detalle_cc = _factor_cambio_climatico(base, resultado)
    resultado.factores = {
        "hipotesis": hipotesis,
        "arf": arf,
        "duracion_h": duracion_h,
        "intervalo_min": intervalo_min,
        "cambio_climatico": factor_cc,
        "detalle_cambio_climatico": detalle_cc,
        "cuartil": int(configuracion.obtener("tormenta.huff.cuartil")),
        "probabilidad_excedencia": float(configuracion.obtener(
            "tormenta.huff.probabilidad_excedencia")),
    }
    logger.info("Hipotesis %s | ARF %.4f | cambio climatico %.4f | "
                "%.0f min en pasos de %.0f", hipotesis, arf, factor_cc,
                duracion_min, intervalo_min)

    with registro.bloque(logger, "Hietogramas"):
        _construir(configuracion, resultado, subcuencas, duracion_min,
                   intervalo_min, arf, factor_cc, hipotesis, logger)

    with registro.bloque(logger, "Figuras"):
        _escribir_figuras(configuracion, base, resultado, logger)

    _escribir_productos(base, resultado, delimitador, logger)
    return _cerrar(logger, resultado, base, ruta_json, inicio, SALIDA_CORRECTA)


def _factor_cambio_climatico(base, resultado) -> tuple[float, dict[str, Any]]:
    """Factor adoptado por el M12a, o 1,0 si no hay ninguno aplicable."""
    ruta = rutas.directorio("procesado", base) / "M12a_idf.json"
    if not ruta.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "cambio_climatico.sin_reporte",
            "no se encuentra M12a_idf.json: el hietograma se construye SIN "
            "factor de cambio climatico y el informe debe declararlo.",
        ))
        return 1.0, {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1.0, {}
    adoptado = datos.get("cambio_climatico_adoptado") or {}
    if not adoptado.get("aplicado"):
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "cambio_climatico.no_aplicado",
            "no hay proyeccion de incremento adoptada: el hietograma no se "
            "afecta por cambio climatico, por la regla condicional de la "
            "seccion 6.",
        ))
        return 1.0, adoptado
    return float(adoptado["factor_aplicado"]), adoptado


def _construir(configuracion, resultado, subcuencas, duracion_min,
               intervalo_min, arf, factor_cc, hipotesis, logger) -> None:
    """Reparte la lámina de cada subcuenca y periodo en el tiempo."""
    columnas = sorted(
        {c for fila in subcuencas for c in fila
         if c.startswith("p_T") and c.endswith("_mm")},
        key=lambda c: float(c[3:-3]))
    if not columnas:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "hietograma.sin_precipitacion",
            "la tabla del M11 no trae ninguna columna 'p_T*_mm'.",
        ))
        return

    if hipotesis != "h1_directa":
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "hietograma.hipotesis_no_soportada",
            f"la hipotesis adoptada es {hipotesis!r}, pero este modulo solo "
            "sabe partir de la lamina de 24 h que el M11 promedio por "
            "subcuenca ('h1_directa'). Para 'h2_idf' o 'h3_factor' hace falta "
            "que el M12a publique la lamina desagregada POR SUBCUENCA, y hoy "
            "solo la publica para el conjunto.",
        ))
        return

    residuos = []
    sin_lamina = 0
    for fila in subcuencas:
        nombre = str(fila.get("subcuenca", "")).strip()
        for columna in columnas:
            periodo = columna[3:-3]
            try:
                puntual = float(fila[columna])
            except (KeyError, TypeError, ValueError):
                sin_lamina += 1
                continue
            lamina = puntual * arf * factor_cc
            try:
                intervalos = repartir(lamina, duracion_min, intervalo_min,
                                      resultado.curva)
            except ErrorHidrologia as error:
                resultado.hallazgos.append(Hallazgo(
                    BLOQUEANTE, "hietograma.reparto", str(error)))
                return
            for paso in intervalos:
                resultado.hietogramas.append(
                    {"subcuenca": nombre, "periodo_retorno": periodo, **paso})
            resumen = resumir_hietograma(intervalos, lamina)
            resumen.update({"subcuenca": nombre, "periodo_retorno": periodo,
                            "pmax24_puntual_mm": round(puntual, 3),
                            "arf": arf, "factor_cc": factor_cc})
            resultado.resumen.append(resumen)
            residuos.append(abs(resumen["residuo_mm"]))

    if not resultado.resumen:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "hietograma.vacio",
            "no se construyo ningun hietograma."))
        return

    logger.info("%d hietograma(s) de %d intervalo(s) sobre %d subcuenca(s) y "
                "%d periodo(s)", len(resultado.resumen),
                resultado.resumen[0]["intervalos"], len(subcuencas),
                len(columnas))

    # EL MARGEN ES EL DEL REDONDEO, no cero. Cada intervalo se publica con
    # cuatro decimales, de modo que la suma de treinta y seis puede apartarse
    # hasta medio digito por intervalo. Exigir cero convertiria el redondeo en
    # una alarma permanente y taparia el fallo real que esta comprobacion
    # busca: una curva que no cierra en cien o un intervalo perdido.
    intervalos_por_hietograma = resultado.resumen[0]["intervalos"]
    margen = intervalos_por_hietograma * 0.5e-4
    peor = max(residuos) if residuos else 0.0
    if peor > margen:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "hietograma.residuo",
            f"la suma de los intervalos se aparta de la lamina hasta "
            f"{peor:.6f} mm, por encima del margen de redondeo de "
            f"{margen:.6f} mm. Cada intervalo es una diferencia de acumuladas, "
            "de modo que un residuo mayor indica que la curva o el reparto no "
            "cierran.",
        ))

    cuartiles = {r["cuartil_del_pico"] for r in resultado.resumen}
    esperado = int(configuracion.obtener("tormenta.huff.cuartil"))
    if cuartiles != {esperado}:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "hietograma.cuartil_del_pico",
            f"se adopto la curva del cuartil {esperado} pero el pico cae en el "
            f"cuartil {sorted(c for c in cuartiles if c)}. Es lo que distingue "
            "una curva de Huff de otra: si el pico no cae donde debe, la curva "
            "leida no es la que se cree.",
        ))

    if sin_lamina:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "hietograma.sin_lamina",
            f"{sin_lamina} combinacion(es) de subcuenca y periodo sin lamina "
            "legible: esas no tienen hietograma y no pueden entrar en el "
            "modelo.",
        ))

    if not resultado.curva[0].get("validado"):
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "hietograma.curva_sin_validar",
            "la curva de Huff esta declarada como NO validada contra la "
            f"fuente ({resultado.curva[0].get('origen')}). Gobierna el reparto "
            "temporal entero: una curva mal transcrita no da error en ninguna "
            "parte, reparte la misma lamina de otra manera y produce un "
            "hidrograma verosimil y equivocado. Contrastarla con la figura del "
            "informe de referencia, numeral 5.2.2.",
        ))

    muestra = resultado.resumen[-1]
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "hietograma.construidos",
        f"{len(resultado.resumen)} hietograma(s) de "
        f"{muestra['intervalos']} intervalos de {intervalo_min:.0f} min sobre "
        f"{duracion_min / 60:.0f} h, cuartil {esperado} con "
        f"{resultado.factores['probabilidad_excedencia']:.0f} % de "
        f"probabilidad de excedencia. Cadena aplicada: lamina de 24 h por "
        f"{hipotesis}, factor de reduccion por area {arf:.3f} y factor de "
        f"cambio climatico {factor_cc:.3f}. El pico cae al "
        f"{muestra['pico_fraccion_del_tiempo']:.0%} de la duracion.",
    ))


def _escribir_figuras(configuracion, base, resultado, logger) -> None:
    """Un hietograma por periodo de retorno, sobre la subcuenca de mayor área."""
    if not resultado.hietogramas:
        return
    try:
        import graficos
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron dibujar los hietogramas: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    individuales = graficos.directorio_tema(directorio / "individuales",
                                            "hietogramas")

    # Se dibuja la MEDIA de todas las subcuencas por periodo. Un hietograma por
    # subcuenca serian mil figuras, y todas tienen la misma forma: lo que
    # cambia entre ellas es la escala, no el reparto.
    periodos = sorted({h["periodo_retorno"] for h in resultado.hietogramas},
                      key=float)
    for periodo in periodos:
        de_ese = [h for h in resultado.hietogramas
                  if h["periodo_retorno"] == periodo]
        por_intervalo: dict[int, list[float]] = {}
        for paso in de_ese:
            por_intervalo.setdefault(paso["intervalo"], []).append(
                paso["lamina_mm"])
        intervalos = sorted(por_intervalo)
        medias = [sum(por_intervalo[i]) / len(por_intervalo[i])
                  for i in intervalos]
        minutos = [resultado.factores["intervalo_min"] * i for i in intervalos]
        ancho = resultado.factores["intervalo_min"] * 0.9

        with graficos.figura(
                estilo,
                titulo=f"Hietograma de diseno, T = {periodo} anos",
                etiqueta_x="Tiempo (min)",
                etiqueta_y="Precipitacion por intervalo (mm)") as (fig, ax):
            ax.bar(minutos, medias, width=ancho, color=estilo.color(0),
                   align="edge", linewidth=0)
            acumulado, suma = [], 0.0
            for valor in medias:
                suma += valor
                acumulado.append(suma)
            otro = ax.twinx()
            otro.plot([m + ancho for m in minutos], acumulado, color="#b03a2e",
                      linewidth=1.4, label="acumulado")
            otro.set_ylabel("Acumulado (mm)", fontsize=estilo.tamano_fuente)
            otro.set_ylim(bottom=0)
            otro.grid(False)
            ax.set_xlim(0, resultado.factores["duracion_h"] * 60.0)
            fig.text(0.01, -0.04,
                     f"Media de las subcuencas. Huff cuartil "
                     f"{resultado.factores['cuartil']}, "
                     f"{resultado.factores['probabilidad_excedencia']:.0f} % de "
                     f"excedencia. Incluye ARF {resultado.factores['arf']:.3f} "
                     f"y cambio climatico "
                     f"{resultado.factores['cambio_climatico']:.3f}.",
                     fontsize=estilo.tamano_fuente - 2, color="#555555")
            destino = (directorio / f"M12b_hietograma_T{periodo.replace('.', '_')}"
                       if periodo == periodos[-1]
                       else individuales / f"M12b_hietograma_T{periodo.replace('.', '_')}")
            for ruta in graficos.guardar(fig, destino, estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("%d hietograma(s) dibujado(s)", len(periodos))


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Escribe una tabla con las columnas de todas sus filas."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    filas = list(filas)
    if not filas:
        destino.write_text("", encoding="utf-8-sig")
        return
    columnas: list[str] = []
    for fila in filas:
        for clave in fila:
            if clave not in columnas:
                columnas.append(clave)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=columnas,
                                  delimiter=delimitador, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(filas)


def _escribir_productos(base, resultado, delimitador, logger) -> None:
    """Escribe las tablas del módulo."""
    directorio = rutas.directorio("procesado_tormenta", base, crear=True)
    for nombre, contenido in (("hietogramas.csv", resultado.hietogramas),
                              ("hietograma_resumen.csv", resultado.resumen)):
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))
    logger.info("Tablas escritas en %s", rutas.relativa(directorio, base))


def _cerrar(logger, resultado, base, ruta_json, inicio, codigo):
    """Emite el reporte, escribe el JSON y cierra el log."""
    orden = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}
    hallazgos = sorted(resultado.hallazgos,
                       key=lambda h: (orden.get(h.severidad, 9), h.clave))

    logger.info(registro.SEPARADOR)
    for severidad, emitir in ((BLOQUEANTE, logger.error),
                              (ADVERTENCIA, logger.warning),
                              (INFORMATIVO, logger.info)):
        grupo = [h for h in hallazgos if h.severidad == severidad]
        if not grupo:
            continue
        emitir("%s (%d)", severidad, len(grupo))
        for hallazgo in grupo:
            emitir("  %-44s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    reporte = {
        "modulo": MODULO,
        "factores": resultado.factores,
        "curva_huff": [{k: v for k, v in p.items() if k != "origen"}
                       for p in resultado.curva],
        "resumen": resultado.resumen[:200],
        "productos": resultado.productos,
        "conteo": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=1),
                         encoding="utf-8")

    productos = {f"producto {i}": p
                 for i, p in enumerate(resultado.productos, start=1)}
    productos["reporte JSON"] = rutas.relativa(ruta_json, base)
    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecucion"] = rutas.relativa(archivo_log, base)

    registro.registrar_cierre(
        logger, MODULO, "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO",
        segundos=time.perf_counter() - inicio, productos=productos)
    return codigo, hallazgos


def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None)
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json)
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato,
            ErrorHidrologia) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    sys.exit(main())

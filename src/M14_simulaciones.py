#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M14 - Ejecución de simulaciones y extracción de resultados
==========================================================
Entorno: venv del proyecto.

CIERRA EL MODELO LLUVIA-ESCORRENTÍA. El M13 dejó el proyecto actualizado y los
ocho escenarios escritos; aquí se computan sin abrir el programa y se extrae de
sus resultados lo que el informe necesita: el caudal pico por elemento y periodo
de retorno, y el hidrograma de creciente en el sitio de proyecto.

SE EJECUTA SIN INTERFAZ. HEC-HMS admite un guion en Jython y esa es la única
forma de que la cadena compute sin intervención manual (CLAUDE.md, sección 4).
Todo lo que toca el ejecutable está en el adaptador 'hms.py'; todo lo que toca el
formato DSS, en 'dss.py'.

EL CÓDIGO DE SALIDA DEL PROCESO NO ES PRUEBA DE NADA. Medido sobre la 4.13: una
invocación que no llegó a computar terminó en cero. Y el DSS de una corrida que
aborta CONSERVA LOS RESULTADOS DE LA ANTERIOR, de modo que leerlo sin comprobar
el log entrega caudales de un modelo que ya no existe, con formato correcto y sin
ninguna señal. Por eso cada corrida se valida contra su log antes de leer su DSS,
y una corrida abortada es bloqueante.

EL PICO EN EL BORDE DE LA VENTANA NO ES UN PICO. Si el máximo cae en la primera o
en la última ordenada, la ventana de control no contiene la creciente: el valor
que se leería es el mayor de los calculados, no el mayor de los que ocurren. Se
detecta y se advierte, porque un hidrograma truncado produce una tabla de
caudales verosímil y baja.

EL BALANCE DE CADA SUBCUENCA SE VERIFICA. La lámina de exceso que HEC-HMS reporta
y el volumen del hidrograma directo son la misma agua contada de dos maneras: si
no coinciden, algo entre el número de curva, el área y la transformación no es lo
que se cree. Es la comprobación que permite defender el CN ante interventoría.

Productos:
    data/02_procesado/hidrologia/resultados_por_elemento.csv
    data/02_procesado/hidrologia/qmax_por_periodo.csv
    data/02_procesado/hidrologia/qmax_por_periodo_referencia.csv
    data/02_procesado/hidrologia/balance_subcuencas.csv
    data/02_procesado/hidrologia/hidrogramas.csv
    data/02_procesado/hidrologia/escenarios_cc.csv
    data/05_resultados/graficos/M14_qmax_vs_periodo[_referencia].png y .svg
    data/05_resultados/graficos/M14_hidrograma_*[_referencia].png y .svg
    data/05_resultados/graficos/M14_escenarios_cc.png y .svg
    data/02_procesado/M14_simulaciones.json

Uso:
    python src/M14_simulaciones.py
    python src/M14_simulaciones.py --sin-computar   # lee los DSS que ya existen

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
import re
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

MODULO = "M14"
DESCRIPCION = "Ejecución de simulaciones y extracción de resultados"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Partes C del pathname que se extraen. El DSS de resultados trae dieciséis por
# elemento; estas son las que sostienen la tabla de caudales y el balance.
CAUDAL = "FLOW"
DIRECTO = "FLOW-DIRECT"
EXCESO = "PRECIP-EXCESS"
PERDIDA = "PRECIP-LOSS"
PARAMETROS = (CAUDAL, DIRECTO, EXCESO, PERDIDA)


@dataclass
class ResultadoM14:
    proyecto: str = ""
    corridas: list[dict[str, Any]] = field(default_factory=list)
    elementos: dict[str, str] = field(default_factory=dict)
    punto_de_proyecto: str = ""
    resultados: list[dict[str, Any]] = field(default_factory=list)
    balance: list[dict[str, Any]] = field(default_factory=list)
    hidrogramas: list[dict[str, Any]] = field(default_factory=list)
    escenarios: list[dict[str, Any]] = field(default_factory=list)
    # EL ESCENARIO DE REFERENCIA VA APARTE Y NO MEZCLADO. El informe presenta
    # las dos tablas y los dos hidrogramas por separado, y el de diseno es el
    # que alimenta la modelacion hidraulica. En una sola lista, los dieciseis
    # periodos de retorno de las corridas se leerian como dieciseis periodos.
    resultados_referencia: list[dict[str, Any]] = field(default_factory=list)
    hidrogramas_referencia: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def volumen_m3(instantes: Sequence[Any], valores: Sequence[float]) -> float:
    """
    Volumen bajo un hidrograma, por trapecios.

    SE INTEGRA POR TRAPECIOS Y NO POR RECTÁNGULOS porque el caudal es un valor
    INSTANTÁNEO (HEC-HMS lo marca 'INST-VAL' en el DSS), no una media del
    intervalo. Multiplicar cada ordenada por el paso sobrestima el volumen en la
    rama ascendente y lo subestima en la de recesión; sobre una creciente
    puntiaguda la diferencia no se compensa.
    """
    if len(valores) < 2 or len(instantes) != len(valores):
        return 0.0
    total = 0.0
    for (t0, t1), (v0, v1) in zip(zip(instantes, instantes[1:]),
                                  zip(valores, valores[1:])):
        segundos = (t1 - t0).total_seconds()
        total += 0.5 * (float(v0) + float(v1)) * segundos
    return total


def lamina_mm(volumen: float, area_km2: float) -> float:
    """Lámina equivalente en milímetros de un volumen sobre un área."""
    if area_km2 <= 0:
        return 0.0
    return volumen / (area_km2 * 1.0e6) * 1000.0


def resumir_hidrograma(instantes: Sequence[Any],
                       valores: Sequence[float]) -> dict[str, Any]:
    """
    Caudal pico, instante en que ocurre y volumen de un hidrograma.

    'pico_en_el_borde' avisa de que el máximo cae en la primera o en la última
    ordenada. Eso no es un pico: es el mayor valor DENTRO de una ventana que no
    contiene la creciente, y produce una tabla de caudales verosímil y baja.

    Excepciones
    -----------
    ErrorHidrologia
        Si la serie está vacía o las marcas de tiempo no acompañan a los
        valores. Devolver ceros sería un caudal de diseño inventado.
    """
    if not valores:
        raise ErrorHidrologia("el hidrograma no trae ninguna ordenada.")
    if len(instantes) != len(valores):
        raise ErrorHidrologia(
            f"el hidrograma trae {len(valores)} valor(es) y "
            f"{len(instantes)} marca(s) de tiempo.")

    indice = max(range(len(valores)), key=lambda i: valores[i])
    minutos = ((instantes[indice] - instantes[0]).total_seconds() / 60.0
               if len(instantes) > 1 else 0.0)
    return {
        "qmax_m3s": float(valores[indice]),
        "instante_pico": instantes[indice].isoformat(timespec="minutes"),
        "t_pico_min": round(minutos, 1),
        "t_pico_h": round(minutos / 60.0, 3),
        "volumen_Mm3": round(volumen_m3(instantes, valores) / 1.0e6, 6),
        "ordenadas": len(valores),
        "pico_en_el_borde": indice in (0, len(valores) - 1),
    }


def balance_de_subcuenca(
    exceso_mm: Sequence[float],
    perdida_mm: Sequence[float],
    volumen_directo_m3: float,
    area_km2: float,
) -> dict[str, Any]:
    """
    Contrasta la lámina de exceso con el volumen del hidrograma directo.

    SON LA MISMA AGUA CONTADA DE DOS MANERAS: lo que el método de pérdidas dejó
    escurrir y lo que la transformación convirtió en hidrograma. Que no
    coincidan señala una incoherencia entre el número de curva, el área que
    HEC-HMS tiene declarada y el método de transformación, y es el tipo de fallo
    que no da error en ninguna parte.

    La desviación se expresa en porcentaje de la lámina de exceso. Con exceso
    nulo no hay contra qué comparar y se devuelve None, que no es lo mismo que
    cero.
    """
    exceso = sum(float(v) for v in exceso_mm)
    perdida = sum(float(v) for v in perdida_mm)
    precipitacion = exceso + perdida
    verificada = lamina_mm(volumen_directo_m3, area_km2)
    desviacion = (100.0 * (verificada - exceso) / exceso) if exceso > 0 else None
    return {
        "precipitacion_mm": round(precipitacion, 3),
        "perdida_mm": round(perdida, 3),
        "exceso_mm": round(exceso, 3),
        "coef_escorrentia": (round(exceso / precipitacion, 4)
                             if precipitacion > 0 else None),
        "lamina_del_hidrograma_mm": round(verificada, 3),
        "desviacion_pct": (round(desviacion, 3)
                           if desviacion is not None else None),
    }


def periodos_no_monotonos(
    caudales: dict[str, float], orden: Sequence[str],
) -> list[tuple[str, str]]:
    """
    Pares de periodos consecutivos en que el caudal NO crece con el periodo.

    Un caudal de diseño que baja al subir el periodo de retorno es imposible con
    la misma cuenca y el mismo método: delata una lámina mal asignada, un
    pluviómetro cruzado o una corrida leída de un modelo anterior. No da error en
    ninguna parte y la tabla sale con aspecto normal.
    """
    fallos: list[tuple[str, str]] = []
    disponibles = [p for p in orden if caudales.get(p) is not None]
    for anterior, siguiente in zip(disponibles, disponibles[1:]):
        if caudales[siguiente] < caudales[anterior]:
            fallos.append((anterior, siguiente))
    return fallos


def elementos_del_modelo(texto_basin: str) -> dict[str, dict[str, Any]]:
    """
    Tipo y área de cada elemento, leídos del modelo de cuenca.

    EL ÁREA SE TOMA DEL .basin Y NO DEL M10. Es la que HEC-HMS usó para calcular
    el hidrograma; verificar el balance contra otra cifra, aunque fuera más
    exacta, compararía dos cosas distintas y culparía al modelo de una
    discrepancia que está en la comparación.
    """
    elementos: dict[str, dict[str, Any]] = {}
    tipo = nombre = ""
    for linea in texto_basin.splitlines():
        encabezado = re.match(
            r"^(Subbasin|Reach|Junction|Sink|Reservoir|Source|Diversion): (.+)$",
            linea)
        if encabezado:
            tipo, nombre = encabezado.group(1), encabezado.group(2).strip()
            elementos[nombre] = {"tipo": tipo, "area_km2": None}
            continue
        area = re.match(r"^\s+Area: ([0-9.eE+-]+)\s*$", linea)
        if area and nombre and tipo == "Subbasin":
            try:
                elementos[nombre]["area_km2"] = float(area.group(1))
            except ValueError:
                pass
    return elementos


def corridas_declaradas(texto_run: str) -> list[tuple[str, str]]:
    """
    Corridas del proyecto, como pares (nombre, modelo meteorológico).

    SE LEEN DEL .run Y NO SE DEDUCEN DE LA CONFIGURACIÓN. El archivo de
    simulaciones es lo que HEC-HMS va a ejecutar; una lista construida a partir
    de los periodos de retorno coincidiría casi siempre y fallaría justo cuando
    el consultor añadió o quitó un escenario a mano.
    """
    corridas: list[tuple[str, str]] = []
    nombre = ""
    for linea in texto_run.splitlines():
        encabezado = re.match(r"^Run: (.+)$", linea)
        if encabezado:
            nombre = encabezado.group(1).strip()
            continue
        precip = re.match(r"^\s+Precip: (.+)$", linea)
        if precip and nombre:
            corridas.append((nombre, precip.group(1).strip()))
            nombre = ""
    return corridas


def ordenar_periodos(periodos: Sequence[str]) -> list[str]:
    """
    Ordena etiquetas de periodo de retorno por su VALOR, no por su texto.

    EL ORDEN DEL ARCHIVO DE SIMULACIONES NO SIRVE. HEC-HMS reordena las corridas
    alfabéticamente al guardar, y en ese orden '100' va antes que '15' y '2.33'
    queda en medio. Medido sobre el proyecto del estudio: la comprobación de que
    el caudal crece con el periodo comparaba T100 contra T15 y denunciaba un
    descenso que no existía.
    """
    def clave(texto: str) -> float:
        try:
            return float(texto)
        except (TypeError, ValueError):
            return float("inf")
    return sorted(dict.fromkeys(periodos), key=clave)


SUFIJO_SIN_FACTOR = "_SF"


def periodo_de_meteorologia(nombre: str) -> str:
    """
    Periodo de retorno que representa un modelo meteorológico ('T2_33' -> '2.33').

    El nombre lo escribió el M13 sustituyendo el punto decimal, porque HEC-HMS no
    lo admite en un identificador. Aquí se deshace esa sustitución.

    EL SUFIJO DEL ESCENARIO SE QUITA ANTES. Con dos escenarios, 'T100' y
    'T100_SF' son el MISMO periodo de retorno calculado con dos lluvias; si el
    sufijo entrara en el periodo, el modulo veria dieciseis periodos en lugar de
    ocho y la tabla de caudales quedaria sin sentido.
    """
    limpio = nombre.strip()
    if limpio.upper().endswith(SUFIJO_SIN_FACTOR):
        limpio = limpio[: -len(SUFIJO_SIN_FACTOR)]
    if limpio.upper().startswith("T"):
        limpio = limpio[1:]
    return limpio.replace("_", ".")


def escenario_de_meteorologia(nombre: str) -> str:
    """
    A que escenario pertenece un modelo meteorologico.

    'diseno' lleva el factor de cambio climatico y 'referencia' es la lluvia
    registrada. El de diseno es el que alimenta la tabla de caudales del
    informe; el otro se presenta al lado para mostrar cuanto del caudal procede
    de la proyeccion y cuanto del dato.
    """
    return ("referencia" if nombre.strip().upper().endswith(SUFIJO_SIN_FACTOR)
            else "diseno")


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    computar: bool | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Computa los escenarios de HEC-HMS y extrae sus resultados."""
    inicio_reloj = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM14()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M14_simulaciones.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"proyecto": configuracion.obtener(
            "hec_hms.proyecto.directorio")},
        parametros=configuracion.parametros("hec_hms"))

    directorio = str(configuracion.obtener(
        "hec_hms.proyecto.directorio", "") or "").strip()
    if not directorio:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "proyecto.sin_ruta",
            "hec_hms.proyecto.directorio esta vacio: hay que declarar donde "
            "vive el modelo. La cadena no lo busca ni lo adivina.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    proyecto = Path(directorio)
    resultado.proyecto = str(proyecto)
    ruta_basin = proyecto / str(configuracion.obtener(
        "hec_hms.proyecto.modelo_cuenca"))
    archivo_hms = str(configuracion.obtener("hec_hms.proyecto.archivo", "")).strip()
    ruta_run = (proyecto / archivo_hms).with_suffix(".run") if archivo_hms else None

    for ruta, clave in ((ruta_basin, "modelo de cuenca"),
                        (ruta_run, "archivo de simulaciones")):
        if ruta is None or not ruta.is_file():
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "proyecto.incompleto",
                f"no se encuentra el {clave} en {ruta}. El M13 es quien lo "
                "escribe: ejecutarlo antes.",
            ))
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)

    resultado.elementos = elementos_del_modelo(
        ruta_basin.read_text(encoding="utf-8", errors="replace"))
    corridas = corridas_declaradas(
        ruta_run.read_text(encoding="utf-8", errors="replace"))
    if not corridas:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "proyecto.sin_corridas",
            f"{ruta_run.name} no declara ninguna simulacion.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    punto = _resolver_punto_de_proyecto(configuracion, resultado)
    if punto is None:
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)
    resultado.punto_de_proyecto = punto

    if computar is None:
        computar = bool(configuracion.obtener("hec_hms.simulacion.ejecutar", True))
    if computar:
        with registro.bloque(logger, "Computo de los escenarios"):
            if not _computar(configuracion, proyecto, archivo_hms,
                             [c for c, _ in corridas], base, resultado, logger):
                return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                               SALIDA_BLOQUEANTE)
    else:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "computo.omitido",
            "se leen los DSS que ya existian sin volver a computar. Nada "
            "garantiza que correspondan al modelo actual: si el M13 se ejecuto "
            "despues, los caudales son de un modelo anterior.",
        ))
        with registro.bloque(logger, "Estado de las corridas"):
            if not _revisar_logs(proyecto, [c for c, _ in corridas], resultado,
                                 logger):
                return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                               SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Extraccion de resultados"):
        try:
            _extraer(configuracion, proyecto, corridas, resultado, logger)
        except (ErrorFormato, ErrorHidrologia, ErrorRutas) as error:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "resultados.lectura", str(error)))
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Tablas"):
        _escribir_tablas(configuracion, base, resultado, logger)

    with registro.bloque(logger, "Figuras"):
        _escribir_figuras(configuracion, base, resultado, logger)

    resultado.productos = [str(p) for p in resultado.productos]
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _resolver_punto_de_proyecto(configuracion, resultado) -> str | None:
    """
    Elemento de cierre del modelo, declarado o deducido del sumidero.

    Un modelo con varios sumideros no tiene un cierre evidente y elegir el
    primero produciría la tabla de caudales de otro sitio. Se exige declararlo.
    """
    declarado = str(configuracion.obtener(
        "hec_hms.resultados.punto_de_proyecto", "") or "").strip()
    if declarado:
        if declarado not in resultado.elementos:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "resultados.punto_inexistente",
                f"hec_hms.resultados.punto_de_proyecto declara {declarado!r}, "
                f"que no es un elemento del modelo.",
            ))
            return None
        return declarado

    sumideros = [n for n, d in resultado.elementos.items() if d["tipo"] == "Sink"]
    if len(sumideros) == 1:
        return sumideros[0]
    resultado.hallazgos.append(Hallazgo(
        BLOQUEANTE, "resultados.punto_ambiguo",
        f"el modelo tiene {len(sumideros)} sumidero(s) y no se puede deducir el "
        "sitio de proyecto. Declararlo en hec_hms.resultados.punto_de_proyecto.",
    ))
    return None


def _computar(configuracion, proyecto, archivo_hms, corridas, base, resultado,
              logger) -> bool:
    """Lanza HEC-HMS sin interfaz y valida cada corrida contra su log."""
    import hms

    instalacion = Path(str(configuracion.obtener("software.hec_hms.ruta")))
    limite = float(configuracion.obtener(
        "hec_hms.simulacion.tiempo_limite_s", 3600))
    guion = rutas.directorio("modelos_hec_hms", base, crear=True) / "M14_computo.script"

    logger.info("Computando %d corrida(s) en una sola sesion de HEC-HMS",
                len(corridas))
    try:
        salida = hms.ejecutar_corridas(
            instalacion, proyecto, Path(archivo_hms).stem, corridas, guion,
            tiempo_limite_s=limite)
    except hms.ErrorHms as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "computo.fallido", str(error)))
        return False

    resultado.productos.append(str(guion))
    return _clasificar_corridas(salida["corridas"], resultado, logger,
                                codigo=salida["codigo"])


def _revisar_logs(proyecto, corridas, resultado, logger) -> bool:
    """Lee el estado de corridas ya computadas, sin volver a lanzarlas."""
    import hms

    estados = []
    for corrida in corridas:
        try:
            estados.append(hms.leer_log_de_corrida(
                proyecto / f"{corrida}.log", corrida))
        except hms.ErrorHms as error:
            estados.append(hms.ResultadoCorrida(corrida=corrida,
                                                errores=[str(error)]))
    return _clasificar_corridas(estados, resultado, logger)


def _clasificar_corridas(estados, resultado, logger, codigo: int = 0) -> bool:
    """
    Convierte el estado de cada corrida en hallazgos y decide si se sigue.

    UNA SOLA CORRIDA INUTILIZABLE DETIENE EL MODULO. La tabla de caudales por
    periodo de retorno no admite huecos: publicarla sin un periodo, o peor, con
    el resultado que quedo de una corrida anterior, es lo que no se puede
    defender.
    """
    resultado.corridas = [{
        "corrida": e.corrida, "terminada": e.terminada, "abortada": e.abortada,
        "duracion": e.duracion, "errores": e.errores[:10],
        "advertencias_n": len(e.advertencias),
    } for e in estados]

    utilizables = [e for e in estados if e.utilizable]
    fallidas = [e for e in estados if not e.utilizable]
    logger.info("%d corrida(s) utilizable(s) de %d", len(utilizables),
                len(estados))

    if fallidas:
        detalle = "; ".join(
            f"{e.corrida}: " + (", ".join(e.errores[:3]) if e.errores
                                else ("abortada" if e.abortada
                                      else "no llego a terminar"))
            for e in fallidas)
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "computo.corridas_fallidas",
            f"{len(fallidas)} de {len(estados)} corrida(s) no son utilizables: "
            f"{detalle}. El proceso de HEC-HMS termino con codigo {codigo}, que "
            "no es prueba de nada: el log de cada corrida es la autoridad. NO se "
            "leen sus resultados, porque el DSS de una corrida abortada conserva "
            "los de la anterior y los entregaria sin ninguna senal.",
        ))
        return False

    con_avisos = [e for e in estados if e.advertencias]
    if con_avisos:
        muestra = sorted({a for e in con_avisos for a in e.advertencias})[:4]
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "computo.advertencias",
            f"{len(con_avisos)} corrida(s) terminaron con advertencias de "
            f"HEC-HMS. Ejemplos: {muestra}. No invalidan el resultado, pero "
            "quedan registradas: las de inestabilidad numerica en tramos cortos "
            "y las de celeridad fuera del indice afectan al transito.",
        ))

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "computo.terminado",
        f"{len(utilizables)} escenario(s) computados y validados contra su "
        "propio log.",
    ))
    return True


def _extraer(configuracion, proyecto, corridas, resultado, logger) -> None:
    """Lee el DSS de cada corrida y arma las tablas de resultados."""
    import dss

    puntos = _puntos_con_hidrograma(configuracion, resultado)
    tolerancia = float(configuracion.obtener(
        "hec_hms.resultados.tolerancia_balance_pct", 1.0))

    truncados: list[str] = []
    fuera_de_balance: list[str] = []
    por_elemento: dict[str, dict[str, float]] = {}
    por_elemento_ref: dict[str, dict[str, float]] = {}

    for corrida, meteorologia in corridas:
        periodo = periodo_de_meteorologia(meteorologia)
        escenario = escenario_de_meteorologia(meteorologia)
        origen = proyecto / f"{corrida}.dss"
        series = dss.leer_series(origen, parametros=PARAMETROS)
        agrupadas: dict[str, dict[str, Any]] = {}
        for serie in series:
            agrupadas.setdefault(serie.elemento, {})[serie.parametro] = serie
        logger.info("%s: %d elemento(s) con resultados", corrida, len(agrupadas))

        for elemento, del_elemento in sorted(agrupadas.items()):
            caudal = del_elemento.get(CAUDAL)
            if caudal is None:
                continue
            resumen = resumir_hidrograma(caudal.instantes, caudal.valores)
            ficha = resultado.elementos.get(elemento, {})
            fila = {
                "elemento": elemento,
                "tipo": ficha.get("tipo", "desconocido"),
                "area_km2": ficha.get("area_km2"),
                "periodo_retorno": periodo,
                "corrida": corrida,
                "unidades": caudal.unidades,
            }
            fila.update({c: v for c, v in resumen.items()
                         if c != "pico_en_el_borde"})
            fila["pico_en_el_borde"] = resumen["pico_en_el_borde"]

            # EL ESCENARIO DE REFERENCIA NO ENTRA EN LAS TABLAS DE DISENO. Se
            # guarda con la misma forma, en listas propias, porque el informe
            # presenta las dos tablas y los dos hidrogramas por separado.
            # NO alimenta el balance de subcuencas, la comprobacion de
            # monotonia ni el aviso de hidrograma truncado: esos vigilan el
            # modelo que produce el caudal de diseno, y duplicarlos daria dos
            # avisos por el mismo elemento.
            if escenario == "referencia":
                por_elemento_ref.setdefault(
                    elemento, {})[periodo] = resumen["qmax_m3s"]
                resultado.resultados_referencia.append(fila)
                if elemento in puntos:
                    resultado.hidrogramas_referencia += _pasos_del_hidrograma(
                        elemento, periodo, caudal)
                continue

            if resumen["pico_en_el_borde"]:
                truncados.append(f"{elemento} (T{periodo})")
            resultado.resultados.append(fila)
            por_elemento.setdefault(elemento, {})[periodo] = resumen["qmax_m3s"]

            if elemento in puntos:
                resultado.hidrogramas += _pasos_del_hidrograma(
                    elemento, periodo, caudal)

            if ficha.get("tipo") != "Subbasin":
                continue
            directo = del_elemento.get(DIRECTO)
            exceso = del_elemento.get(EXCESO)
            perdida = del_elemento.get(PERDIDA)
            if directo is None or exceso is None or perdida is None:
                continue
            balance = balance_de_subcuenca(
                exceso.valores, perdida.valores,
                volumen_m3(directo.instantes, directo.valores),
                float(ficha.get("area_km2") or 0.0))
            balance.update({"subcuenca": elemento, "periodo_retorno": periodo,
                            "area_km2": ficha.get("area_km2")})
            resultado.balance.append(balance)
            desviacion = balance["desviacion_pct"]
            if desviacion is not None and abs(desviacion) > tolerancia:
                fuera_de_balance.append(
                    f"{elemento} (T{periodo}, {desviacion:+.1f} %)")

    if por_elemento_ref:
        resultado.escenarios = comparar_escenarios(
            por_elemento, por_elemento_ref, resultado.punto_de_proyecto)

    _hallazgos_de_extraccion(resultado, por_elemento, corridas, truncados,
                             fuera_de_balance, tolerancia, puntos)


def _pasos_del_hidrograma(elemento: str, periodo: str, caudal) -> list[dict]:
    """Los pasos de un hidrograma, en la forma de la tabla del informe."""
    paso = caudal.intervalo_min
    return [{"elemento": elemento, "periodo_retorno": periodo,
             "minuto": round(indice * paso, 1),
             "caudal_m3s": round(float(valor), 4)}
            for indice, valor in enumerate(caudal.valores)]


def tabla_ancha(filas, periodos) -> list[dict[str, Any]]:
    """
    La tabla del informe: una fila por elemento y una columna por periodo.

    LAS COLUMNAS VAN EN ORDEN DE PERIODO y no en el de las corridas: HEC-HMS
    las reordena alfabeticamente al guardar, y una tabla de caudales de diseno
    con T100 antes que T15 se lee mal.

    Se usa para los dos escenarios, con la MISMA forma: asi la declaracion de
    las dos tablas del informe es simetrica y quien lea cualquiera de los dos
    archivos encuentra las mismas columnas.
    """
    fichas: dict[str, dict[str, Any]] = {}
    valores: dict[str, dict[str, dict[str, Any]]] = {}
    for fila in filas:
        fichas.setdefault(fila["elemento"], {
            "elemento": fila["elemento"], "tipo": fila["tipo"],
            "area_km2": fila["area_km2"]})
        valores.setdefault(fila["elemento"], {})[fila["periodo_retorno"]] = fila

    anchas: list[dict[str, Any]] = []
    for elemento, ficha in fichas.items():
        registro_ = dict(ficha)
        for periodo in periodos:
            fila = valores[elemento].get(periodo)
            etiqueta = periodo.replace(".", "_")
            registro_[f"q_T{etiqueta}_m3s"] = (round(fila["qmax_m3s"], 3)
                                               if fila else None)
            registro_[f"tp_T{etiqueta}_h"] = fila["t_pico_h"] if fila else None
        anchas.append(registro_)
    return anchas


def comparar_escenarios(diseno: dict, referencia: dict,
                        punto: str) -> list[dict[str, Any]]:
    """
    Los dos escenarios de cambio climatico en el punto de proyecto.

    EL DE DISENO ES EL QUE LLEVA EL FACTOR. El de referencia representa la
    lluvia registrada y no es una alternativa entre la que elegir: la doctrina
    aplica el factor cuando es de incremento, y este contraste sirve para
    mostrar cuanto del caudal procede de la proyeccion y cuanto del dato.

    El aporte NO es igual al factor. Medido en este estudio, un 10,6 % mas de
    lluvia produce entre un 21 y un 27 % mas de caudal, mas en las crecientes
    frecuentes, porque el umbral de perdidas amplifica mas cuando la lluvia es
    pequena.
    """
    con = diseno.get(punto) or {}
    sin = referencia.get(punto) or {}
    filas: list[dict[str, Any]] = []
    for periodo in ordenar_periodos(set(con) & set(sin)):
        a, b = float(sin[periodo]), float(con[periodo])
        filas.append({
            "periodo_retorno": periodo,
            "q_diseno_m3s": round(b, 3),
            "q_referencia_m3s": round(a, 3),
            "aporte_factor_m3s": round(b - a, 3),
            "aporte_factor_pct": round(100.0 * (b - a) / a, 1) if a else None,
        })
    return filas


def _puntos_con_hidrograma(configuracion, resultado) -> list[str]:
    """
    Elementos que reciben figura de hidrograma: el cierre y los declarados.

    Los declarados se comprueban contra el modelo. Un nombre mal escrito en la
    configuracion produciria una figura menos y ninguna senal.
    """
    puntos = [resultado.punto_de_proyecto]
    declarados = configuracion.obtener("hec_hms.resultados.puntos_de_interes", [])
    ausentes = []
    for nombre in [str(p).strip() for p in (declarados or []) if str(p).strip()]:
        if nombre not in resultado.elementos:
            ausentes.append(nombre)
        elif nombre not in puntos:
            puntos.append(nombre)
    if ausentes:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "resultados.puntos_inexistentes",
            f"hec_hms.resultados.puntos_de_interes nombra {ausentes}, que no "
            "son elementos del modelo. Se ignoran: revisar la ortografia contra "
            "el .basin.",
        ))
    return puntos


def _hallazgos_de_extraccion(resultado, por_elemento, corridas, truncados,
                             fuera_de_balance, tolerancia, puntos) -> None:
    """Convierte lo medido durante la extraccion en hallazgos del reporte."""
    orden = ordenar_periodos([
        periodo_de_meteorologia(m) for _, m in corridas
        if escenario_de_meteorologia(m) == "diseno"])
    cierre = por_elemento.get(resultado.punto_de_proyecto, {})
    if cierre:
        caudales = ", ".join(f"T{p} = {cierre[p]:.1f}"
                             for p in orden if p in cierre)
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "resultados.sitio_de_proyecto",
            f"caudal pico en {resultado.punto_de_proyecto!r}, en m3/s: "
            f"{caudales}. Es la tabla Qmax contra periodo de retorno del "
            "informe, y el insumo de la modelacion hidraulica.",
        ))

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "resultados.extraidos",
        f"{len(por_elemento)} elemento(s) con caudal pico, tiempo al pico y "
        f"volumen en {len(corridas)} periodo(s) de retorno, y balance de "
        f"{len({b['subcuenca'] for b in resultado.balance})} subcuenca(s). "
        f"Hidrograma completo para {puntos}.",
    ))

    no_monotonos = []
    for elemento, caudales in por_elemento.items():
        fallos = periodos_no_monotonos(caudales, orden)
        if fallos:
            no_monotonos.append(f"{elemento} {fallos[:2]}")
    if no_monotonos:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "resultados.no_monotonos",
            f"{len(no_monotonos)} elemento(s) con caudal que NO crece al crecer "
            f"el periodo de retorno: {no_monotonos[:6]}. Con la misma cuenca y "
            "el mismo metodo eso es imposible: delata una lamina mal asignada, "
            "un pluviometro cruzado o un DSS leido de un modelo anterior. La "
            "tabla sale con aspecto normal, y por eso se detiene aqui.",
        ))

    if truncados:
        elementos = sorted({t.split(" (")[0] for t in truncados})
        severidad = (BLOQUEANTE if resultado.punto_de_proyecto in elementos
                     else ADVERTENCIA)
        resultado.hallazgos.append(Hallazgo(
            severidad, "resultados.hidrograma_truncado",
            f"{len(elementos)} elemento(s) con el maximo en el borde de la "
            f"ventana de calculo: {elementos[:8]}. Eso no es un pico, es el "
            "mayor valor de una ventana que no contiene la creciente, y el "
            "caudal que se leeria es menor que el real. Alargar el fin en las "
            "especificaciones de control (hec_hms.control) y volver a computar.",
        ))

    if fuera_de_balance:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "resultados.balance",
            f"{len(fuera_de_balance)} caso(s) en que el volumen del hidrograma "
            f"directo se aparta mas de {tolerancia:.1f} % de la lamina de "
            f"exceso: {fuera_de_balance[:6]}. Son la misma agua contada de dos "
            "maneras: la diferencia apunta al area declarada en HEC-HMS, al "
            "numero de curva o a la transformacion.",
        ))


def _escribir_tablas(configuracion, base, resultado, logger) -> None:
    """Escribe las cuatro tablas de resultados."""
    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))
    destino = rutas.resolver(
        configuracion.obtener("hec_hms.resultados.salida"), base)
    destino.mkdir(parents=True, exist_ok=True)

    for nombre, filas in (("resultados_por_elemento", resultado.resultados),
                          ("balance_subcuencas", resultado.balance),
                          ("hidrogramas", resultado.hidrogramas),
                          ("escenarios_cc", resultado.escenarios)):
        ruta = destino / f"{nombre}.csv"
        _escribir_csv(ruta, filas, delimitador)
        resultado.productos.append(rutas.relativa(ruta, base))

    # LA TABLA ANCHA ES LA DEL INFORME, y hay una por escenario: la plantilla
    # pide 'Qmax Vs. Periodo de Retorno (con cambio climatico)' y la misma
    # tabla sin el factor. La de diseno conserva su nombre de siempre, porque
    # es la que alimenta la modelacion hidraulica y la citan otros modulos.
    periodos = _periodos_en_orden(resultado)
    anchas = tabla_ancha(resultado.resultados, periodos)
    ruta = destino / "qmax_por_periodo.csv"
    _escribir_csv(ruta, anchas, delimitador)
    resultado.productos.append(rutas.relativa(ruta, base))
    logger.info("%d elemento(s) en la tabla de caudales, %d periodo(s)",
                len(anchas), len(periodos))

    if resultado.resultados_referencia:
        referencia = tabla_ancha(resultado.resultados_referencia, periodos)
        ruta = destino / "qmax_por_periodo_referencia.csv"
        _escribir_csv(ruta, referencia, delimitador)
        resultado.productos.append(rutas.relativa(ruta, base))
        logger.info("%d elemento(s) en la tabla de caudales de referencia",
                    len(referencia))

    _completar_transito(configuracion, base, resultado,
                        delimitador, logger)



# =============================================================================
# Parametros de Muskingum, que completan la tabla de transito del M13
# =============================================================================
def profundidad_normal(caudal_m3s: float, n_manning: float, ancho_fondo_m: float,
                       talud_h_por_v: float, pendiente: float,
                       tolerancia_m: float = 1e-4) -> float:
    """
    Calado normal de una seccion trapezoidal para un caudal, por Manning.

    Q = (1/n) * A * R^(2/3) * S^(1/2), con A = (b + z*y)*y y el perimetro
    mojado P = b + 2*y*sqrt(1 + z^2).

    NO TIENE SOLUCION CERRADA y se resuelve por biseccion, que converge siempre
    porque el caudal crece de forma monotona con el calado. Se acota primero
    duplicando el limite superior hasta pasarse: fijar un maximo a ojo dejaria
    sin solucion los tramos de cierre, que son los de mas caudal.

    Excepciones
    -----------
    ErrorHidrologia
        Si alguna magnitud impide el calculo.
    """
    if caudal_m3s <= 0 or n_manning <= 0 or pendiente <= 0:
        raise ErrorHidrologia(
            f"caudal ({caudal_m3s} m3/s), n de Manning ({n_manning}) y "
            f"pendiente ({pendiente}) deben ser positivos.")
    if ancho_fondo_m < 0 or talud_h_por_v < 0:
        raise ErrorHidrologia(
            f"ancho de fondo ({ancho_fondo_m} m) y talud ({talud_h_por_v}) no "
            "pueden ser negativos.")

    def caudal_de(y: float) -> float:
        area = (ancho_fondo_m + talud_h_por_v * y) * y
        perimetro = ancho_fondo_m + 2.0 * y * math.sqrt(
            1.0 + talud_h_por_v ** 2)
        if area <= 0 or perimetro <= 0:
            return 0.0
        return (area * (area / perimetro) ** (2.0 / 3.0)
                * math.sqrt(pendiente) / n_manning)

    alto = 1.0
    for _ in range(60):
        if caudal_de(alto) >= caudal_m3s:
            break
        alto *= 2.0
    else:
        raise ErrorHidrologia(
            f"no se alcanza {caudal_m3s} m3/s ni con {alto:.0f} m de calado: "
            "revisar la seccion y la pendiente del tramo.")

    bajo = 0.0
    while alto - bajo > tolerancia_m:
        medio = 0.5 * (bajo + alto)
        if caudal_de(medio) < caudal_m3s:
            bajo = medio
        else:
            alto = medio
    return 0.5 * (bajo + alto)


def parametros_muskingum(
    caudal_m3s: float, n_manning: float, ancho_fondo_m: float,
    talud_h_por_v: float, pendiente: float, longitud_m: float,
    celeridad_declarada: float | None = None,
) -> dict[str, Any]:
    """
    K y X de Muskingum de un tramo, linealizados por Cunge.

    MUSKINGUM PIDE K Y X, y la tabla del informe solo traia K. Sin X la
    parametrizacion no esta definida: con X = 0 el tramo se comporta como un
    embalse de nivel horizontal y con X = 0,5 traslada la onda sin atenuarla.

        K = L / c
        X = 0,5 * (1 - Q / (B * S0 * c * L))

    con B el ancho superficial y c la celeridad de la onda cinematica. Se
    adopta c = (5/3) * V, la aproximacion de canal ancho: para una seccion
    trapezoidal el valor exacto de dQ/dA difiere, y la diferencia es menor que
    la incertidumbre del n de Manning y del ancho de fondo, que aqui vienen de
    una regionalizacion y no de secciones levantadas.

    SE LINEALIZA EN UN CAUDAL DE REFERENCIA, no en el de cada evento. Muskingum
    es un metodo de parametros constantes: esa es justamente su limitacion
    frente a Muskingum-Cunge, que rehace K y X en cada paso.

    X SE RECORTA A [0, 0,5] Y SE DICE. Un X negativo no significa un cauce raro:
    significa que el tramo es demasiado largo para un solo elemento a ese
    caudal, y HEC-HMS lo rechazaria. Se recorta a cero, que es el limite fisico,
    y se marca para que el informe pueda explicarlo.

    Excepciones
    -----------
    ErrorHidrologia
        Si alguna magnitud impide el calculo.
    """
    if longitud_m <= 0:
        raise ErrorHidrologia(
            f"la longitud del tramo ({longitud_m} m) debe ser positiva.")

    calado = profundidad_normal(caudal_m3s, n_manning, ancho_fondo_m,
                                talud_h_por_v, pendiente)
    area = (ancho_fondo_m + talud_h_por_v * calado) * calado
    ancho_superior = ancho_fondo_m + 2.0 * talud_h_por_v * calado
    velocidad = caudal_m3s / area if area > 0 else 0.0
    celeridad = (float(celeridad_declarada) if celeridad_declarada
                 else 5.0 / 3.0 * velocidad)
    if celeridad <= 0:
        raise ErrorHidrologia(
            "la celeridad resulto nula: sin ella no hay K de Muskingum.")

    denominador = ancho_superior * pendiente * celeridad * longitud_m
    crudo = 0.5 * (1.0 - caudal_m3s / denominador) if denominador > 0 else 0.0
    x = min(0.5, max(0.0, crudo))
    return {
        "caudal_ref_m3s": round(caudal_m3s, 3),
        "calado_normal_m": round(calado, 3),
        "area_m2": round(area, 3),
        "ancho_superior_m": round(ancho_superior, 2),
        "velocidad_ms": round(velocidad, 3),
        "celeridad_ms": round(celeridad, 3),
        "celeridad_origen": ("declarada" if celeridad_declarada
                             else "5/3 de la velocidad media"),
        "k_s": round(longitud_m / celeridad, 1),
        "k_min": round(longitud_m / celeridad / 60.0, 2),
        "k_h": round(longitud_m / celeridad / 3600.0, 3),
        "x": round(x, 4),
        "x_crudo": round(crudo, 4),
        "x_recortado": abs(crudo - x) > 1e-9,
    }


def _completar_transito(configuracion, base, resultado, delimitador,
                        logger) -> None:
    """
    Anade a la tabla de transito del M13 los parametros de Muskingum.

    SE COMPLETA AQUI Y NO EN EL M13 porque hace falta el caudal de referencia y
    ese lo produce la simulacion, que corre despues. Cada modulo escribe lo que
    sabe: el M13 la geometria del tramo y este los parametros que la
    linealizacion necesita.

    EL MODELO CORRE MUSKINGUM-CUNGE Y LA TABLA ES DE MUSKINGUM. No es lo mismo:
    Muskingum-Cunge no toma K ni X como entrada, los rehace en cada subtramo y
    cada paso de tiempo a partir de la hidraulica. Los K y X que aqui se
    escriben son la PARAMETRIZACION EQUIVALENTE de parametros constantes, que
    CLAUDE.md pide calcular tambien, y el informe debe presentarlos como la
    alternativa y no como lo que produjo los caudales.
    """
    ruta = rutas.directorio("procesado", base) / "hidrologia" / "transito.csv"
    if not ruta.is_file():
        return

    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        return

    tr_referencia = str(configuracion.obtener(
        "hec_hms.transito.muskingum.caudal_referencia_tr", "2.33"))
    declarada = configuracion.obtener(
        "hec_hms.transito.muskingum.celeridad_ms", None)
    import hms

    clases = configuracion.obtener(
        "hec_hms.transito.muskingum.clases_pendiente", []) or []
    n_manning = float(configuracion.obtener(
        "hec_hms.transito.muskingum_cunge.n_manning"))
    talud = float(configuracion.obtener(
        "hec_hms.transito.muskingum_cunge.talud_h_por_v"))

    picos: dict[str, float] = {}
    for fila in resultado.resultados:
        if str(fila.get("periodo_retorno")) == tr_referencia:
            try:
                picos[str(fila["elemento"])] = float(fila["qmax_m3s"])
            except (KeyError, TypeError, ValueError):
                continue
    if not picos:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "transito.sin_caudal_referencia",
            f"no hay resultados para Tr = {tr_referencia} anios: los "
            "parametros de Muskingum de la tabla de transito quedan sin "
            "calcular. Se declara en "
            "hec_hms.transito.muskingum.caudal_referencia_tr.",
        ))
        return

    sin_pico, recortados, fallidos = [], [], []
    for fila in filas:
        tramo = str(fila.get("tramo", ""))
        caudal = picos.get(tramo)
        if caudal is None:
            sin_pico.append(tramo)
            continue
        clase = hms.clase_por_pendiente(
            float(fila["pendiente_pct"]), clases)
        celeridad = (float(clase["celeridad_ms"]) if clase
                     else (float(declarada) if declarada else None))
        try:
            parametros = parametros_muskingum(
                caudal, n_manning, float(fila["ancho_fondo_m"]), talud,
                float(fila["pendiente_pct"]) / 100.0,
                float(fila["longitud_m"]), celeridad)
        except (ErrorHidrologia, KeyError, TypeError, ValueError) as error:
            fallidos.append(f"{tramo} ({error})")
            continue
        if clase:
            # La X de la clase SUSTITUYE a la de Cunge, que sobre esta
            # geometria devuelve 0,497 de mediana, es decir traslacion pura.
            parametros["x_cunge"] = parametros["x"]
            parametros["x"] = float(clase["x"])
            parametros["x_recortado"] = False
            parametros["clase_pendiente"] = str(clase.get("nombre", ""))
        if parametros["x_recortado"]:
            recortados.append(tramo)
        fila["n_manning"] = n_manning
        fila["talud_h_por_v"] = talud
        fila.update(parametros)

    campos: list[str] = []
    for fila in filas:
        for clave in fila:
            if clave not in campos:
                campos.append(clave)
    with ruta.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=campos,
                                  delimiter=delimitador, restval="")
        escritor.writeheader()
        escritor.writerows(filas)
    logger.info("Parametros de Muskingum en %d de %d tramo(s), Tr = %s",
                len(filas) - len(sin_pico) - len(fallidos), len(filas),
                tr_referencia)

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "transito.muskingum",
        f"la tabla de transito lleva K y X de Muskingum linealizados en el "
        f"pico de Tr = {tr_referencia} anios. EL MODELO NO LOS USA: corre "
        f"Muskingum-Cunge, que rehace K y X en cada subtramo y cada paso de "
        f"tiempo a partir de la hidraulica. Son la parametrizacion "
        f"equivalente de parametros constantes, que CLAUDE.md pide calcular "
        f"tambien, y el informe debe presentarlos como la alternativa.",
    ))
    equis = sorted(f["x"] for f in filas if isinstance(f.get("x"), float))
    if equis:
        mediana = equis[len(equis) // 2]
        altos = sum(1 for v in equis if v >= 0.45)
        if altos > len(equis) // 2:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "transito.x_sin_atenuacion",
                f"X mediano {mediana:.3f}, con {altos} de {len(equis)} "
                f"tramo(s) por encima de 0,45: con estos parametros Muskingum "
                f"TRASLADARIA la onda casi sin atenuarla, porque X = 0,5 es el "
                f"limite de traslacion pura. No es un error de calculo sino la "
                f"consecuencia de la geometria disponible: el ancho de fondo "
                f"viene de una regionalizacion y da cauces anchos y someros, "
                f"que almacenan poco. Refuerza que el metodo adoptado sea "
                f"Muskingum-Cunge, que rehace la atenuacion en cada subtramo. "
                f"Con secciones levantadas del proyecto, esta tabla cambia.",
            ))

    if recortados:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "transito.x_recortado",
            f"{len(recortados)} tramo(s) dan X negativo por Cunge y se "
            f"recorto a cero: {sorted(recortados)[:8]}. NO significa un cauce "
            "raro sino que el tramo es demasiado largo para un solo elemento a "
            "ese caudal, y HEC-HMS rechazaria el valor crudo. Queda en "
            "'x_crudo' del CSV. Muskingum-Cunge no tiene este problema porque "
            "subdivide el tramo por su cuenta.",
        ))
    if fallidos:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "transito.sin_muskingum",
            f"{len(fallidos)} tramo(s) sin parametros de Muskingum: "
            f"{fallidos[:5]}.",
        ))


def _periodos_en_orden(resultado) -> list[str]:
    """Periodos de retorno presentes, ordenados por su valor numerico."""
    return ordenar_periodos([f["periodo_retorno"] for f in resultado.resultados])


def _escribir_figuras(configuracion, base, resultado, logger) -> None:
    """Curva Qmax contra periodo de retorno e hidrogramas de los puntos."""
    if not resultado.resultados:
        return
    try:
        import graficos
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron dibujar los resultados: {error}"))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(
        configuracion.obtener("graficos.directorio"), base)
    periodos = _periodos_en_orden(resultado)

    # LOS DOS ESCENARIOS LLEVAN FIGURA PROPIA, con el mismo dibujo. La
    # plantilla los presenta uno detras del otro, y una sola figura con las
    # dieciseis curvas superpuestas no se leeria: los hidrogramas de los dos
    # escenarios se cruzan y comparten la escala.
    escritas = _figura_qmax(graficos, estilo, directorio, base, resultado,
                            resultado.resultados, periodos, "")
    escritas += _figuras_hidrograma(graficos, estilo, directorio, base,
                                    resultado, resultado.hidrogramas,
                                    periodos, "")
    if resultado.resultados_referencia:
        escritas += _figura_qmax(
            graficos, estilo, directorio, base, resultado,
            resultado.resultados_referencia, periodos, SUFIJO_REFERENCIA)
        escritas += _figuras_hidrograma(
            graficos, estilo, directorio, base, resultado,
            resultado.hidrogramas_referencia, periodos, SUFIJO_REFERENCIA)
    if resultado.escenarios:
        escritas += _figura_escenarios(graficos, estilo, directorio, base,
                                       resultado)
    logger.info("%d figura(s) escritas", escritas)


# El sufijo del escenario sin factor en los productos del informe. NO es
# SUFIJO_SIN_FACTOR, que es el de los nombres dentro de HEC-HMS: alli tiene que
# ser corto porque el programa lo usa como identificador, y aqui tiene que
# decirle al consultor de que archivo se trata.
SUFIJO_REFERENCIA = "_referencia"

NOTA_ESCENARIO = {
    "": "Escenario de diseño, con el factor de cambio climático aplicado.",
    SUFIJO_REFERENCIA: ("Escenario de referencia, sin el factor de cambio "
                        "climático: representa la lluvia registrada."),
}


def _figura_qmax(graficos, estilo, directorio, base, resultado, filas,
                 periodos, sufijo: str) -> int:
    """Caudal maximo contra periodo de retorno, un trazo por punto."""
    puntos = sorted({f["elemento"] for f in filas})
    series: dict[str, tuple[list[float], list[float]]] = {}
    for punto in puntos:
        pares = [(float(p), f["qmax_m3s"]) for p in periodos
                 for f in filas
                 if f["elemento"] == punto and f["periodo_retorno"] == p]
        if pares:
            series[punto] = ([x for x, _ in pares], [y for _, y in pares])
    if not series:
        return 0
    with graficos.figura(
            estilo, titulo="Caudal máximo contra periodo de retorno",
            etiqueta_x="Periodo de retorno (años)",
            etiqueta_y="Caudal pico (m3/s)") as (fig, ax):
        graficos.lineas(ax, series, estilo)
        ax.set_xscale("log")
        ax.set_xticks([float(p) for p in periodos])
        ax.set_xticklabels(periodos)
        ax.set_ylim(bottom=0)
        fig.text(0.01, -0.04,
                 f"Sitio de proyecto: {resultado.punto_de_proyecto}. "
                 "Modelo HEC-HMS, SCS Curve Number y SCS Unit Hydrograph, "
                 f"tránsito Muskingum-Cunge. {NOTA_ESCENARIO[sufijo]}",
                 fontsize=estilo.tamano_fuente - 2, color="#555555")
        for ruta in graficos.guardar(
                fig, directorio / f"M14_qmax_vs_periodo{sufijo}", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
    return 1


def _figuras_hidrograma(graficos, estilo, directorio, base, resultado,
                        hidrogramas, periodos, sufijo: str) -> int:
    """Un hidrograma por punto de interes, con los periodos superpuestos."""
    escritas = 0
    for punto in sorted({h["elemento"] for h in hidrogramas}):
        curvas: dict[str, tuple[list[float], list[float]]] = {}
        for periodo in periodos:
            pasos = [h for h in hidrogramas
                     if h["elemento"] == punto
                     and h["periodo_retorno"] == periodo]
            if pasos:
                pasos.sort(key=lambda h: h["minuto"])
                curvas[f"T = {periodo} anos"] = (
                    [p["minuto"] / 60.0 for p in pasos],
                    [p["caudal_m3s"] for p in pasos])
        if not curvas:
            continue
        # RAMPA Y NO PALETA CATEGORICA. Los periodos de retorno son una
        # categoria ORDENADA y la paleta de identificacion se repite a partir
        # del sexto color: con ocho curvas, T = 2,33 salia del mismo azul que
        # T = 100 y T = 5 del mismo rojo que T = 500. Sobre una familia de
        # hidrogramas anidados eso invierte la lectura.
        colores = graficos.rampa(len(curvas), estilo)
        with graficos.figura(
                estilo, titulo=f"Hidrograma de creciente, {punto}",
                etiqueta_x="Tiempo desde el inicio de la tormenta (h)",
                etiqueta_y="Caudal (m3/s)") as (fig, ax):
            for color, (nombre, (x, y)) in zip(colores, curvas.items()):
                ax.plot(x, y, color=color, linewidth=1.5, label=nombre)
            graficos.leyenda(ax, estilo)
            ax.set_ylim(bottom=0)
            ax.set_xlim(left=0)
            fig.text(0.01, -0.04, NOTA_ESCENARIO[sufijo],
                     fontsize=estilo.tamano_fuente - 2, color="#555555")
            nombre_punto = punto.replace(" ", "_")
            for ruta in graficos.guardar(
                    fig,
                    directorio / f"M14_hidrograma_{nombre_punto}{sufijo}",
                    estilo):
                resultado.productos.append(rutas.relativa(ruta, base))
            escritas += 1
    return escritas


def _figura_escenarios(graficos, estilo, directorio, base, resultado) -> int:
    """
    Los dos escenarios de cambio climatico, en barras por periodo de retorno.

    BARRAS Y NO DOS LINEAS. Lo que se compara son valores de una categoria
    ordenada pero discreta, ocho periodos de retorno, y el interes esta en la
    DIFERENCIA entre las dos barras de cada par, no en la forma de la curva:
    esa ya la dan las dos figuras de Qmax contra periodo. Con dos lineas sobre
    escala logaritmica la separacion se lee como una franja constante y el
    aporte del factor, que baja del 27 al 21 % al crecer el periodo, se pierde.
    """
    filas = resultado.escenarios
    if not filas:
        return 0
    etiquetas = [str(f["periodo_retorno"]) for f in filas]
    referencia = [float(f["q_referencia_m3s"]) for f in filas]
    diseno = [float(f["q_diseno_m3s"]) for f in filas]
    posiciones = list(range(len(filas)))
    ancho = 0.38

    with graficos.figura(
            estilo,
            titulo="Caudal de diseño y de referencia por periodo de retorno",
            etiqueta_x="Periodo de retorno (años)",
            etiqueta_y="Caudal pico (m3/s)") as (fig, ax):
        # EL DE DISENO LLEVA EL COLOR DE LA PALETA y el de referencia un gris.
        # Con los dos en colores de identificacion, el segundo color de la
        # paleta es rojo y la figura ponia el enfasis en el escenario que NO
        # es de diseno: el rojo se lee como el valor critico.
        ax.bar([p - ancho / 2 for p in posiciones], referencia, ancho,
               color="#9e9e9e", label="Sin factor (referencia)")
        ax.bar([p + ancho / 2 for p in posiciones], diseno, ancho,
               color=estilo.color(0), label="Con factor (diseño)")
        # EL APORTE EN PORCENTAJE, SOBRE CADA PAR. Es el dato que la figura
        # tiene que dejar leer y no se deduce de la altura de las barras: a
        # T 500 la diferencia absoluta es la mayor de todas y la relativa la
        # menor.
        for posicion, fila, alto in zip(posiciones, filas, diseno):
            aporte = fila.get("aporte_factor_pct")
            if aporte is None:
                continue
            ax.annotate(f"+{float(aporte):.1f} %",
                        (posicion, alto), textcoords="offset points",
                        xytext=(0, 4), ha="center",
                        fontsize=estilo.tamano_fuente - 3, color="#555555")
        ax.set_xticks(posiciones)
        ax.set_xticklabels(etiquetas)
        ax.set_ylim(bottom=0)
        graficos.leyenda(ax, estilo)
        fig.text(0.01, -0.04,
                 f"Sitio de proyecto: {resultado.punto_de_proyecto}. El "
                 "escenario de diseño es el que lleva el factor; el de "
                 "referencia representa la lluvia registrada y no es una "
                 "alternativa de diseño.",
                 fontsize=estilo.tamano_fuente - 2, color="#555555")
        for ruta in graficos.guardar(
                fig, directorio / "M14_escenarios_cc", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))
    return 1


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Escribe una tabla con la union de las columnas de todas sus filas."""
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
    if conteo[BLOQUEANTE] and codigo == SALIDA_CORRECTA:
        codigo = SALIDA_BLOQUEANTE
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    reporte = {
        "modulo": MODULO,
        "proyecto": resultado.proyecto,
        "punto_de_proyecto": resultado.punto_de_proyecto,
        "corridas": resultado.corridas,
        "elementos": len(resultado.elementos),
        "resultados": len(resultado.resultados),
        "balance": len(resultado.balance),
        "productos": resultado.productos,
        "resumen": conteo,
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
    analizador.add_argument(
        "--sin-computar", dest="computar", action="store_false", default=None,
        help="lee los DSS que ya existen en lugar de volver a computar")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json, computar=argumentos.computar)
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato,
            ErrorHidrologia) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())

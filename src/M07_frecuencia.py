#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M07 - Series de Pmáx 24 h y análisis de frecuencia
==================================================
Entorno: venv del proyecto.

CLAUDE.md, sección 6, cierra la matriz de este módulo: diez distribuciones, tres
métodos de ajuste, pruebas KS, Anderson-Darling y chi-cuadrado, criterios AIC y
BIC, y periodos de retorno de 2.33 a 500 años. El motor vive en src/frecuencia.py
y aquí se orquesta sobre la serie diaria.

La serie diaria se conserva íntegra. CLAUDE.md, sección 6: "Precipitación diaria
| Se conserva íntegra para Pmáx24h. No se construye serie sintética diaria
interpolada". Esto importa al leer el resultado: a diferencia de la mensual del
M05, aquí NINGÚN valor es sintético, y el análisis de frecuencia se apoya solo
en observación.

Dos exclusiones, ambas por Calificador y no por estadística:

    DATO RECHAZADO  el IDEAM lo declara excluible del análisis
    ACUMULADO       agrupa varios días en un registro, de modo que se leería
                    como un máximo de 24 horas que nunca ocurrió

La segunda es la que CLAUDE.md, sección 7, señala como crítica, y la que el
formato reducido de descarga perdió. Sin ella, un acumulado de cinco días entra
como Pmáx24h y contamina el dato de diseño.

NO se aplica IQR. La sección 7 lo prohíbe de forma expresa: truncaría el dato de
diseño. Lo que sí se corre es Grubbs-Beck del Bulletin 17C, que busca atípicos
BAJOS, y solo se reporta.

Completitud estacional. Un año aporta su máximo solo si ningún mes baja del
mínimo de días declarado. Un total anual no basta: un año con 340 días puede
tener abril entero vacío, y abril es temporada húmeda en la sabana; su máximo
sería el de un año seco y entraría indistinguible de los demás.

Productos:
    data/02_procesado/frecuencia/pmax24h_anual.csv
    data/02_procesado/frecuencia/ajustes.csv
    data/02_procesado/frecuencia/cuantiles.csv
    data/02_procesado/frecuencia/M07_frecuencia.md
    data/02_procesado/M07_frecuencia.json
    data/05_resultados/graficos/M07_*.png y .svg

Uso:
    python src/M07_frecuencia.py
    python src/M07_frecuencia.py --sin-graficas

Códigos de salida:
    0  análisis producido
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import frecuencia as fr  # noqa: E402
from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorClaveInexistente,
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M07"
DESCRIPCION = "Series de Pmáx 24 h y análisis de frecuencia"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

ETIQUETA_DIARIA = "PTPM_CON"

# Calificadores que excluyen el registro de la serie de máximos. La lista REAL
# vive en config/perfiles_ideam.yaml, que es doctrina; esta es el respaldo
# mínimo para cuando el perfil no se puede leer, y no debería usarse nunca.
#
# ACUMULADO es el crítico y el que CLAUDE.md, sección 7, señala: agrupa varios
# días y se leería como un máximo de 24 horas que nunca ocurrió.
CALIFICADORES_EXCLUIDOS = ("DATO RECHAZADO", "ACUMULADO")


def calificadores_excluidos(configuracion, base) -> tuple[tuple[str, ...], str]:
    """
    Lee del perfil del IDEAM qué calificadores impiden sustentar un máximo.

    Devuelve (calificadores, procedencia). Que la lista sea doctrina y no una
    constante del código importa: añadir un calificador nuevo, como los tres de
    pluviógrafo que aparecieron en 2021, no debería exigir tocar el programa.
    """
    try:
        ruta = rutas.resolver(
            configuracion.obtener("ideam.dhime_zip.perfiles"), base)
        datos = leer_yaml(ruta)
    except (ErrorConfiguracion, ErrorRutas, ErrorClaveInexistente):
        return CALIFICADORES_EXCLUIDOS, "respaldo del código"

    bloque = datos.get("calificadores") or {}
    declarados = [str(m).strip() for m in (bloque.get("excluidos_de_maximos") or ())]
    # El rechazado no es cuestión de máximos: no es un dato utilizable en nada.
    rechazados = [m for m, d in (bloque.get("observados") or {}).items()
                  if "excluir del análisis" in str((d or {}).get("efecto", ""))]
    juntos = tuple(dict.fromkeys([*declarados, *rechazados]))
    if not juntos:
        return CALIFICADORES_EXCLUIDOS, "respaldo del código"
    return juntos, str(rutas.relativa(ruta, base))


@dataclass
class ResultadoM07:
    estaciones: int = 0
    registros_leidos: int = 0
    excluidos_calificador: dict[str, int] = field(default_factory=dict)
    anios_rechazados: int = 0
    series: list[dict[str, Any]] = field(default_factory=list)
    serie_anual: list[dict[str, Any]] = field(default_factory=list)
    ajustes: list[dict[str, Any]] = field(default_factory=list)
    cuantiles: list[dict[str, Any]] = field(default_factory=list)
    adoptadas: dict[str, dict[str, Any]] = field(default_factory=dict)
    atipicos: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Etapa 1: serie de máximos anuales
# =============================================================================
def maximos_anuales(
    dias: dict[int, dict[int, list[float]]], min_dias_mes: int,
) -> tuple[dict[int, float], int]:
    """
    Máximo diario de cada año que cumple la completitud estacional.

    Recibe los valores agrupados por año y mes. Un año aporta su máximo solo si
    los DOCE meses alcanzan el mínimo de días: si falta un mes entero, el máximo
    resultante corresponde a un año recortado y no es comparable con los demás.

    Devuelve los máximos y cuántos años se rechazaron.
    """
    salida: dict[int, float] = {}
    rechazados = 0
    for anio, meses in dias.items():
        if len(meses) < 12 or any(len(v) < min_dias_mes for v in meses.values()):
            rechazados += 1
            continue
        maximo = max(max(v) for v in meses.values() if v)
        salida[anio] = float(maximo)
    return salida, rechazados


def leer_diaria(
    ruta: Path, delimitador: str, ventana: tuple[int, int],
    admitidas: set[str] | None = None,
    excluidos_declarados: tuple[str, ...] = (),
) -> tuple[dict[str, dict[int, dict[int, list[float]]]], int, dict[str, int]]:
    """
    Recorre la serie consolidada del M04 y agrupa la precipitación diaria.

    Un solo recorrido en flujo. Los registros con Calificador excluible se
    descartan aquí y se cuentan por marca, de modo que el reporte pueda decir
    cuántos y de qué tipo.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra {ruta}. Ejecutar el M04 antes que este módulo."
        )
    por_estacion: dict[str, dict[int, dict[int, list[float]]]] = {}
    leidos = 0
    excluidos: dict[str, int] = {}
    marcas_excluidas = {m.upper() for m in (excluidos_declarados
                                            or CALIFICADORES_EXCLUIDOS)}

    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            if fila.get("etiqueta") != ETIQUETA_DIARIA:
                continue
            fecha = fila.get("fecha", "")
            if len(fecha) < 10:
                continue
            try:
                anio, mes = int(fecha[:4]), int(fecha[5:7])
                valor = float(fila.get("valor", ""))
            except (TypeError, ValueError):
                continue
            if not (ventana[0] <= anio <= ventana[1]):
                continue
            codigo = fila.get("codigo", "").strip()
            if admitidas is not None and codigo not in admitidas:
                continue

            marcas = {m.strip().upper()
                      for m in (fila.get("calificador") or "").split("|")}
            coincidentes = marcas & marcas_excluidas
            if coincidentes:
                for marca in coincidentes:
                    excluidos[marca] = excluidos.get(marca, 0) + 1
                continue

            leidos += 1
            por_estacion.setdefault(codigo, {}).setdefault(
                anio, {}).setdefault(mes, []).append(valor)
    return por_estacion, leidos, excluidos


def estaciones_del_m05(base: Path, delimitador: str) -> set[str]:
    """
    Códigos que el M05 conservó tras su análisis de consistencia.

    La comunicación es por archivo, como exige la doctrina. El alcance es una
    decisión declarada: usar el mismo conjunto en todo el estudio evita tener
    que justificar por qué una estación descartada reaparece más adelante.
    """
    ruta = rutas.directorio("procesado_estaciones", base) / "M05_consistencia.csv"
    if not ruta.is_file():
        return set()
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        return {fila["codigo"].strip()
                for fila in csv.DictReader(manejador, delimiter=delimitador)
                if fila.get("codigo")}


# =============================================================================
# Etapa 2: análisis de frecuencia
# =============================================================================
def analizar_estacion(
    codigo: str,
    maximos: dict[int, float],
    distribuciones: Sequence[str],
    metodos: Sequence[str],
    periodos: Sequence[float],
) -> tuple[list[fr.Ajuste], dict[str, Any]]:
    """
    Ajusta toda la matriz de distribuciones y métodos sobre una serie.

    Se conservan también los ajustes que fallan, con su motivo: una combinación
    ausente del informe se leería como no intentada, y la diferencia importa
    cuando alguien pregunta por qué no se adoptó tal distribución.
    """
    valores = [maximos[a] for a in sorted(maximos)]
    ajustes: list[fr.Ajuste] = []
    for distribucion in distribuciones:
        for metodo in metodos:
            ajuste = fr.ajustar(valores, distribucion, metodo)
            if ajuste.valido:
                ajuste.bondad = fr.bondad_de_ajuste(ajuste, valores)
                ajuste.cuantiles = fr.cuantiles(ajuste, periodos)
            ajustes.append(ajuste)
    return ajustes, fr.grubbs_beck(valores)


def es_plausible(ajuste: fr.Ajuste, observados: Sequence[float]) -> bool:
    """
    Descarta ajustes que el criterio de información premiaría por error.

    Un criterio de información compara verosimilitudes y no comprueba que el
    resultado tenga sentido físico. Medido en este estudio: la Pareto
    generalizada, ajustada con el umbral en el mínimo de la muestra, alcanza una
    verosimilitud enorme con una densidad degenerada (un pico sobre el borde) y
    gana por AIC en dos estaciones. Ese ajuste habría entrado al diseño como
    válido.

    Dos comprobaciones, ambas necesarias:

      - los cuantiles deben crecer con el periodo de retorno
      - el cuantil de periodo alto debe superar el máximo observado, porque una
        crecida centenaria no puede ser menor que la mayor de treinta años
    """
    if not ajuste.valido or not ajuste.cuantiles:
        return False
    periodos = sorted(ajuste.cuantiles)
    valores = [ajuste.cuantiles[p] for p in periodos]
    if valores != sorted(valores):
        return False
    maximo_observado = max(observados)
    if valores[-1] < maximo_observado:
        return False
    return True


def seleccionar(
    ajustes: Sequence[fr.Ajuste], criterio: str = "aic",
    observados: Sequence[float] | None = None,
    excluidas: Sequence[str] = (),
) -> fr.Ajuste | None:
    """
    Mejor ajuste por el criterio de información pedido.

    AIC y BIC penalizan el número de parámetros, de modo que una distribución
    de tres no gana solo por tener más grados de libertad. El módulo propone;
    la adopción es del consultor y se declara en config.

    Se excluyen las distribuciones declaradas como no adoptables y las que no
    superan la comprobación de plausibilidad: un criterio de información compara
    verosimilitudes, no comprueba que el resultado tenga sentido.
    """
    clave = criterio.strip().lower()
    fuera = {d.strip().lower() for d in excluidas}
    candidatos = [a for a in ajustes
                  if a.valido and a.distribucion.lower() not in fuera
                  and isinstance(a.bondad.get(clave), (int, float))]
    if observados is not None:
        candidatos = [a for a in candidatos if es_plausible(a, observados)]
    if not candidatos:
        return None
    return min(candidatos, key=lambda a: a.bondad[clave])


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    con_graficas: bool = True,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Construye la serie de máximos y ajusta la matriz de distribuciones."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")
    anio_estudio = int(configuracion.obtener("proyecto.anio_estudio"))
    min_dias_mes = int(configuracion.obtener("frecuencia.min_dias_mes"))
    periodos = [float(p) for p in
                configuracion.obtener("frecuencia.periodos_retorno")]
    distribuciones = list(configuracion.obtener("frecuencia.distribuciones"))
    metodos = list(configuracion.obtener("frecuencia.ajuste"))
    criterios = list(configuracion.obtener("frecuencia.criterios_seleccion"))
    adoptada = configuracion.obtener("frecuencia.distribucion_adoptada")
    excluidas = list(
        configuracion.obtener("frecuencia.excluidas_de_seleccion") or ())
    # La forma fija de la GEV es una eleccion regional, no una constante: se
    # inyecta desde config en lugar de quedar escrita en el motor.
    fr.FORMA_GEV_FIJA = float(configuracion.obtener("frecuencia.forma_gev_fija"))

    ventana_adoptada = configuracion.obtener("sensibilidad_series.ventana_adoptada")
    if ventana_adoptada is None:
        limites = (1900, anio_estudio)
    else:
        limites = (int(ventana_adoptada[0]) if ventana_adoptada[0] is not None
                   else 1900,
                   int(ventana_adoptada[1]) if ventana_adoptada[1] is not None
                   else anio_estudio)

    admitidas = None
    if bool(configuracion.obtener("frecuencia.usar_estaciones_del_m05")):
        admitidas = estaciones_del_m05(base, delimitador)

    ruta_serie = rutas.resolver(
        configuracion.obtener("series.consolidada"), base)

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"serie consolidada": rutas.relativa(ruta_serie, base),
                 "estaciones del M05": len(admitidas) if admitidas else "todas",
                 "ventana": f"{limites[0]}-{limites[1]}"},
        parametros={
            "frecuencia.min_dias_mes": min_dias_mes,
            "frecuencia.periodos_retorno": periodos,
            "frecuencia.distribuciones": distribuciones,
            "frecuencia.ajuste": metodos,
            "frecuencia.criterios_seleccion": criterios,
            "frecuencia.distribucion_adoptada": adoptada,
        },
    )

    resultado = ResultadoM07()

    # --- Etapa 1 -------------------------------------------------------------
    with registro.bloque(logger, "Etapa 1: serie de maximos anuales"):
        marcas, procedencia = calificadores_excluidos(configuracion, base)
        logger.info("Calificadores excluidos de los maximos (%s): %s",
                    procedencia, ", ".join(marcas))
        por_estacion, leidos, excluidos = leer_diaria(
            ruta_serie, delimitador, limites, admitidas, marcas)
        resultado.registros_leidos = leidos
        resultado.excluidos_calificador = excluidos

        maximos_por_estacion: dict[str, dict[int, float]] = {}
        for codigo, anios in sorted(por_estacion.items()):
            maximos, rechazados = maximos_anuales(anios, min_dias_mes)
            resultado.anios_rechazados += rechazados
            if maximos:
                maximos_por_estacion[codigo] = maximos
        resultado.estaciones = len(maximos_por_estacion)
        # LA SERIE ANO POR ANO SE GUARDA, no solo su resumen. Es la que el
        # informe tabula en la hoja de calculo de la IDF por Silva, y es el
        # dato de partida de todo el analisis de frecuencia: un anexo que
        # solo trae la media, el minimo y el maximo no permite rehacerlo.
        resultado.serie_anual = [
            {"codigo": codigo, "anio": anio, "pmax24_mm": round(valor, 1)}
            for codigo, anios in sorted(maximos_por_estacion.items())
            for anio, valor in sorted(anios.items())]

        logger.info(
            "%s registro(s) diarios | %d estacion(es) con maximos | "
            "%d anio-estacion rechazados por completitud",
            f"{leidos:,}", resultado.estaciones, resultado.anios_rechazados)

        if excluidos:
            detalle = ", ".join(f"{k}: {v:,}" for k, v in sorted(excluidos.items()))
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "maximos.calificador",
                f"registros excluidos por Calificador ({detalle}). ACUMULADO es "
                "el critico: agrupa varios dias y se leeria como un maximo de 24 "
                "horas que nunca ocurrio. NO se aplica IQR a esta serie, porque "
                "truncaria el dato de diseno (CLAUDE.md, seccion 7).",
            ))

    if not maximos_por_estacion:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "maximos.vacio",
            "ninguna estacion produjo serie de maximos anuales. Revisar el "
            "criterio de completitud y la ingesta del M04.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    # --- Etapa 2 -------------------------------------------------------------
    criterio = (criterios[0] if criterios else "aic")
    with registro.bloque(logger, "Etapa 2: analisis de frecuencia"):
        cortas = []
        for codigo, maximos in sorted(maximos_por_estacion.items()):
            anios = sorted(maximos)
            resultado.series.append({
                "codigo": codigo, "n_anios": len(anios),
                "anio_min": anios[0], "anio_max": anios[-1],
                "pmax_min_mm": round(min(maximos.values()), 1),
                "pmax_media_mm": round(
                    float(np.mean(list(maximos.values()))), 1),
                "pmax_max_mm": round(max(maximos.values()), 1),
            })
            if len(anios) < 20:
                cortas.append((codigo, len(anios)))

            ajustes, atipicos = analizar_estacion(
                codigo, maximos, distribuciones, metodos, periodos)
            atipicos["codigo"] = codigo
            resultado.atipicos.append(atipicos)

            for ajuste in ajustes:
                fila = {"codigo": codigo, **ajuste.como_dict()}
                resultado.ajustes.append(fila)

            observados = [maximos[a] for a in anios]
            mejor = seleccionar(ajustes, criterio, observados, excluidas)
            if adoptada:
                forzados = [a for a in ajustes
                            if a.valido and a.distribucion == adoptada]
                if forzados:
                    mejor = seleccionar(forzados, criterio, observados)
            if mejor is None:
                continue
            resultado.adoptadas[codigo] = {
                "distribucion": mejor.distribucion,
                "metodo": mejor.metodo,
                "criterio": criterio,
                "valor_criterio": mejor.bondad.get(criterio),
                "ks_p": mejor.bondad.get("ks_p"),
                "anderson_darling": mejor.bondad.get("anderson_darling"),
            }
            fila_cuantiles = {"codigo": codigo,
                              "distribucion": mejor.distribucion,
                              "metodo": mejor.metodo,
                              "n_anios": len(anios)}
            for periodo in periodos:
                valor = mejor.cuantiles.get(float(periodo))
                fila_cuantiles[f"T{periodo:g}"] = (round(valor, 1)
                                                   if valor is not None else None)
            resultado.cuantiles.append(fila_cuantiles)

        validos = sum(1 for a in resultado.ajustes if not a.get("error"))
        logger.info("%d ajuste(s) validos de %d intentados; criterio %s",
                    validos, len(resultado.ajustes), criterio)

        if cortas:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "frecuencia.series_cortas",
                f"{len(cortas)} estacion(es) con menos de 20 anios de maximos: "
                f"{cortas[:6]}. Extrapolar a 100 o 500 anios desde una muestra "
                "asi da un cuantil con incertidumbre muy superior a la que "
                "sugiere su cifra.",
            ))

        con_bajos = [a for a in resultado.atipicos if a.get("cuantos")]
        if con_bajos:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "frecuencia.atipicos_bajos",
                f"{len(con_bajos)} estacion(es) con atipicos bajos segun "
                "Grubbs-Beck (Bulletin 17C). Solo se REPORTAN: un anio seco real "
                "y un error de registro se ven igual desde la estadistica. Los "
                "atipicos ALTOS no se tocan: son el dato de diseno.",
            ))

    # --- Productos -----------------------------------------------------------
    with registro.bloque(logger, "Escritura de productos"):
        _escribir_productos(configuracion, base, resultado, periodos,
                            delimitador, logger)

    if con_graficas:
        with registro.bloque(logger, "Graficas"):
            _figuras(configuracion, base, resultado, maximos_por_estacion,
                     periodos, logger)

    resultado.hallazgos.extend(_resumir(resultado, configuracion, periodos))
    codigo_salida = (SALIDA_BLOQUEANTE
                     if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
                     else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo_salida)


def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Vuelca una tabla de diccionarios."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    filas = list(filas)
    if not filas:
        destino.write_text("", encoding="utf-8-sig")
        return
    campos: list[str] = []
    for fila in filas:
        for clave in fila:
            if clave not in campos:
                campos.append(clave)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(manejador, fieldnames=campos,
                                  delimiter=delimitador, restval="")
        escritor.writeheader()
        escritor.writerows(filas)


def _escribir_productos(configuracion, base, resultado, periodos, delimitador,
                        logger) -> None:
    """Escribe series, ajustes, cuantiles e informe."""
    directorio = rutas.directorio("procesado_frecuencia", base, crear=True)

    for nombre, contenido in (
        ("pmax24h_anual.csv", resultado.series),
        ("pmax24h_serie.csv", resultado.serie_anual),
        ("ajustes.csv", resultado.ajustes),
        ("cuantiles.csv", resultado.cuantiles),
        ("atipicos_bajos.csv", [
            {k: (";".join(f"{v:.1f}" for v in a[k]) if k == "atipicos_bajos"
                 else a[k]) for k in a} for a in resultado.atipicos]),
    ):
        destino = directorio / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))

    informe = directorio / "M07_frecuencia.md"
    _escribir_informe(informe, resultado, configuracion, periodos)
    resultado.productos.append(rutas.relativa(informe, base))
    logger.info("%d serie(s), %d ajuste(s), %d juego(s) de cuantiles",
                len(resultado.series), len(resultado.ajustes),
                len(resultado.cuantiles))


def _tabla_markdown(filas, columnas) -> list[str]:
    lineas = ["| " + " | ".join(str(c) for c in columnas) + " |",
              "|" + "|".join("---" for _ in columnas) + "|"]
    for fila in filas:
        lineas.append("| " + " | ".join(str(fila.get(c, "")) for c in columnas) + " |")
    return lineas


def _escribir_informe(destino, resultado, configuracion, periodos) -> None:
    """Informe en Markdown, en la linea de las rutinas heredadas."""
    import collections
    conteo = collections.Counter(
        d["distribucion"] for d in resultado.adoptadas.values())
    metodos = collections.Counter(
        d["metodo"] for d in resultado.adoptadas.values())
    lineas = [
        "# M07 - Pmax 24 h y analisis de frecuencia",
        "",
        f"* Estaciones: {resultado.estaciones}",
        f"* Registros diarios leidos: {resultado.registros_leidos:,}",
        f"* Anio-estacion rechazados por completitud: "
        f"{resultado.anios_rechazados}",
        f"* Ajustes intentados: {len(resultado.ajustes)}",
        f"* Periodos de retorno: {', '.join(f'{p:g}' for p in periodos)} anios",
        "",
        "## La serie es integra",
        "",
        "CLAUDE.md, seccion 6, conserva la serie diaria sin complemento: "
        "NINGUN",
        "valor de este analisis es sintetico, a diferencia de la serie mensual "
        "del",
        "M05. El analisis de frecuencia se apoya solo en observacion.",
        "",
        "Se excluyen dos Calificadores del IDEAM, no por estadistica:",
        "",
    ]
    for marca, cuantos in sorted(resultado.excluidos_calificador.items()):
        lineas.append(f"* `{marca}`: {cuantos:,} registro(s)")
    lineas += [
        "",
        "`ACUMULADO` es el critico y el que el formato reducido de descarga",
        "perdio: agrupa varios dias en un registro, de modo que se leeria como",
        "un maximo de 24 horas que nunca ocurrio.",
        "",
        "NO se aplica IQR a esta serie. La seccion 7 lo prohibe de forma",
        "expresa: truncaria el dato de diseno. Lo que si se corre es",
        "Grubbs-Beck del Bulletin 17C, que busca atipicos BAJOS, y solo se",
        "reporta.",
        "",
        "## Completitud estacional",
        "",
        f"Un anio aporta su maximo solo si NINGUN mes baja de "
        f"{configuracion.obtener('frecuencia.min_dias_mes')} dias. Un total",
        "anual no basta: un anio con 340 dias puede tener abril entero vacio, y",
        "abril es temporada humeda en la sabana. Su maximo seria el de un anio",
        "seco y entraria indistinguible de los demas.",
        "",
        "## Distribuciones adoptadas",
        "",
        f"Seleccion automatica por {configuracion.obtener('frecuencia.criterios_seleccion')[0]}"
        if configuracion.obtener("frecuencia.criterios_seleccion") else "",
        "",
    ]
    if conteo:
        lineas += _tabla_markdown(
            [{"distribucion": d, "estaciones": c} for d, c in conteo.most_common()],
            ["distribucion", "estaciones"])
        lineas += ["", "Metodo de ajuste seleccionado:", ""]
        lineas += _tabla_markdown(
            [{"metodo": m, "estaciones": c} for m, c in metodos.most_common()],
            ["metodo", "estaciones"])
    lineas += [
        "",
        "El detalle por estacion esta en `cuantiles.csv`, y la matriz completa",
        "de ajustes, con las combinaciones que fallaron y su motivo, en",
        "`ajustes.csv`.",
        "",
        "## Figuras",
        "",
        "* `M07_series_pmax`: maximos anuales de cada estacion.",
        "* `M07_papel_probabilidad`: dato empirico y distribucion adoptada.",
        "* `M07_cuantiles`: cuantil por periodo de retorno en cada estacion.",
        "",
    ]
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(l for l in lineas) + "\n", encoding="utf-8")


# =============================================================================
# Graficas
# =============================================================================
def _figuras(configuracion, base, resultado, maximos_por_estacion, periodos,
             logger) -> None:
    """Emite las figuras del modulo."""
    try:
        import graficos
    except ImportError as exc:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "graficos.ausente",
            f"no se pudieron generar las figuras: {exc}.",
        ))
        return

    estilo = graficos.Estilo.desde_config(configuracion)
    directorio = rutas.resolver(configuracion.obtener("graficos.directorio"), base)
    formula = str(configuracion.obtener("frecuencia.posicion_grafica"))

    # --- Series de maximos ---------------------------------------------------
    with graficos.figura(
        estilo, titulo="Precipitación máxima en 24 horas, máximos anuales",
        etiqueta_x="año", etiqueta_y="Pmáx 24 h (mm)",
    ) as (fig, ax):
        for codigo, maximos in sorted(maximos_por_estacion.items()):
            anios = sorted(maximos)
            ax.plot(anios, [maximos[a] for a in anios], color=estilo.color(0),
                    alpha=0.30, linewidth=0.8)
        todos: dict[int, list[float]] = {}
        for maximos in maximos_por_estacion.values():
            for anio, valor in maximos.items():
                todos.setdefault(anio, []).append(valor)
        anios = sorted(todos)
        ax.plot(anios, [float(np.mean(todos[a])) for a in anios],
                color="#c00000", linewidth=2.0,
                label="promedio de las estaciones")
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M07_series_pmax",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Papel de probabilidad, rejilla por estacion -------------------------
    codigos = sorted(maximos_por_estacion)
    columnas = 4
    filas = (len(codigos) + columnas - 1) // columnas
    with graficos.figura(
        estilo,
        titulo=f"Papel de probabilidad ({formula}) y distribución adoptada",
        filas=filas, columnas=columnas,
        alto_cm=max(estilo.alto_cm, 4.2 * filas),
    ) as (fig, ejes):
        for indice, codigo in enumerate(codigos):
            ax = ejes[indice // columnas][indice % columnas]
            valores = np.sort([maximos_por_estacion[codigo][a]
                               for a in maximos_por_estacion[codigo]])
            n = valores.size
            probabilidad = fr.posicion_grafica(n, formula)
            retorno = 1.0 / (1.0 - probabilidad)
            ax.semilogx(retorno, valores, linestyle="none", marker="o",
                        markersize=2.5, color=estilo.color(0), zorder=3)
            adoptada = resultado.adoptadas.get(codigo)
            if adoptada:
                fila = next((c for c in resultado.cuantiles
                             if c["codigo"] == codigo), None)
                if fila:
                    equis = [p for p in periodos
                             if fila.get(f"T{p:g}") is not None]
                    yes = [fila[f"T{p:g}"] for p in equis]
                    ax.semilogx(equis, yes, color="#c00000", linewidth=1.3,
                                zorder=4)
                ax.set_title(f"{codigo}  {adoptada['distribucion']}",
                             fontsize=estilo.tamano_fuente - 2, loc="left",
                             color="#333333")
            else:
                ax.set_title(codigo, fontsize=estilo.tamano_fuente - 2,
                             loc="left", color="#333333")
            ax.tick_params(labelsize=estilo.tamano_fuente - 3)
            ax.grid(True, which="both", color=graficos.GRIS_CONTEXTO,
                    linewidth=0.3, alpha=0.5)
            for lado in ("top", "right"):
                ax.spines[lado].set_visible(False)
        for sobrante in range(len(codigos), filas * columnas):
            ejes[sobrante // columnas][sobrante % columnas].axis("off")
        fig.supxlabel("periodo de retorno (años)",
                      fontsize=estilo.tamano_fuente)
        fig.supylabel("Pmáx 24 h (mm)", fontsize=estilo.tamano_fuente)
        fig.tight_layout()
        for ruta in graficos.guardar(
                fig, directorio / "M07_papel_probabilidad", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Cuantiles por periodo de retorno ------------------------------------
    if resultado.cuantiles:
        with graficos.figura(
            estilo,
            titulo="Pmáx 24 h por periodo de retorno, todas las estaciones",
            etiqueta_x="periodo de retorno (años)",
            etiqueta_y="Pmáx 24 h (mm)",
        ) as (fig, ax):
            for fila in resultado.cuantiles:
                equis = [p for p in periodos if fila.get(f"T{p:g}") is not None]
                yes = [fila[f"T{p:g}"] for p in equis]
                if equis:
                    ax.semilogx(equis, yes, color=estilo.color(0), alpha=0.35,
                                linewidth=0.9)
            medianas = []
            for periodo in periodos:
                valores = [f[f"T{periodo:g}"] for f in resultado.cuantiles
                           if f.get(f"T{periodo:g}") is not None]
                medianas.append(float(np.median(valores)) if valores else np.nan)
            ax.semilogx(periodos, medianas, color="#c00000", linewidth=2.0,
                        marker="o", markersize=4, label="mediana")
            ax.set_xticks(periodos)
            ax.set_xticklabels([f"{p:g}" for p in periodos])
            ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
            fig.tight_layout()
            for ruta in graficos.guardar(fig, directorio / "M07_cuantiles",
                                         estilo):
                resultado.productos.append(rutas.relativa(ruta, base))

    _figura_histograma(graficos, estilo, directorio, resultado,
                       maximos_por_estacion, configuracion, base)
    _figuras_por_estacion(graficos, estilo, configuracion, base, resultado,
                          maximos_por_estacion, periodos, logger)
    logger.info("Figuras escritas en %s", rutas.relativa(directorio, base))


def _figura_histograma(graficos, estilo, directorio, resultado,
                       maximos_por_estacion, configuracion, base) -> None:
    """
    Histograma de la muestra con TODAS las densidades superpuestas.

    Reproduce el Grafico 5-43 del informe de referencia, que es una captura de
    Hydrognomon. Su utilidad es distinta de la del papel de probabilidad: aquel
    juzga la cola, que es donde vive el periodo de retorno, y este juzga la
    FORMA de la distribucion frente a la del dato. Una distribucion puede seguir
    bien la cola y describir mal el cuerpo, y solo esta figura lo delata.

    La adoptada se resalta, de modo que coincida con la que el papel de
    probabilidad dibuja en rojo.
    """
    distribuciones = list(configuracion.obtener("frecuencia.distribuciones"))
    metodos = list(configuracion.obtener("frecuencia.ajuste"))
    codigos = sorted(maximos_por_estacion)
    if not codigos:
        return
    columnas = 3
    filas = (len(codigos) + columnas - 1) // columnas
    with graficos.figura(
        estilo,
        filas=filas, columnas=columnas,
        alto_cm=max(estilo.alto_cm, 5.0 * filas),
    ) as (fig, ejes):
        etiquetas: dict[str, str] = {}
        for indice, codigo in enumerate(codigos):
            ax = ejes[indice // columnas][indice % columnas]
            valores = np.array(sorted(maximos_por_estacion[codigo].values()))
            ax.hist(valores, bins=max(6, int(round(np.sqrt(valores.size)))),
                    density=True, color=graficos.GRIS_CONTEXTO, alpha=0.35,
                    edgecolor="white", linewidth=0.4, zorder=1)
            malla = np.linspace(valores.min() * 0.6, valores.max() * 1.5, 300)

            adoptada = resultado.adoptadas.get(codigo)
            for orden, distribucion in enumerate(distribuciones):
                metodo = (adoptada["metodo"] if adoptada else metodos[0])
                ajuste = fr.ajustar(list(valores), distribucion, metodo)
                if not ajuste.valido:
                    ajuste = fr.ajustar(list(valores), distribucion,
                                        "maxima_verosimilitud")
                curva = fr.densidad(ajuste, malla)
                if curva is None or not np.any(np.isfinite(curva)):
                    continue
                es_adoptada = bool(adoptada
                                   and distribucion == adoptada["distribucion"])
                color = "#c00000" if es_adoptada else estilo.color(orden)
                ax.plot(malla, curva, color=color,
                        linewidth=2.0 if es_adoptada else 0.7,
                        alpha=1.0 if es_adoptada else 0.55,
                        zorder=5 if es_adoptada else 2)
                etiquetas.setdefault(distribucion, color)
            titulo = codigo + (f"  {adoptada['distribucion']}"
                               if adoptada else "")
            ax.set_title(titulo, fontsize=estilo.tamano_fuente - 2,
                         loc="left", color="#333333")
            ax.tick_params(labelsize=estilo.tamano_fuente - 3)
            ax.set_yticks([])
            for lado in ("top", "right", "left"):
                ax.spines[lado].set_visible(False)
        for sobrante in range(len(codigos), filas * columnas):
            ejes[sobrante // columnas][sobrante % columnas].axis("off")

        from matplotlib.lines import Line2D
        manijas = [Line2D([0], [0], color=c, linewidth=1.4, label=d)
                   for d, c in etiquetas.items()]
        manijas.append(Line2D([0], [0], color="#c00000", linewidth=2.4,
                              label="adoptada por el criterio"))
        fig.legend(handles=manijas, loc="lower center", ncol=5,
                   fontsize=estilo.tamano_fuente - 2, frameon=False,
                   bbox_to_anchor=(0.5, -0.015))
        fig.supxlabel("Pmáx 24 h (mm)", fontsize=estilo.tamano_fuente)
        fig.tight_layout()
        # El titulo se pone DESPUES de ajustar: puesto antes, tight_layout no
        # reserva su espacio y se solapa con la primera fila de paneles.
        fig.suptitle("Histograma de Pmáx 24 h y funciones de densidad ajustadas",
                     fontsize=estilo.tamano_fuente + 2, y=1.005)
        for ruta in graficos.guardar(fig, directorio / "M07_histograma_pdf",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))


def _figuras_por_estacion(graficos, estilo, configuracion, base, resultado,
                          maximos_por_estacion, periodos, logger) -> None:
    """
    Una figura por estacion para el informe, agrupada por tema.

    Las rejillas se conservan como resumen de revision, pero el informe necesita
    una figura por estacion: en una rejilla de treinta y dos paneles no se lee
    ni el eje.
    """
    if not bool(configuracion.obtener("graficos.figuras_individuales")):
        return
    raiz = rutas.resolver(
        configuracion.obtener("graficos.directorio_individuales"), base)
    individual = graficos.estilo_individual(
        estilo,
        float(configuracion.obtener("graficos.ancho_individual_cm")),
        float(configuracion.obtener("graficos.alto_individual_cm")))
    formula = str(configuracion.obtener("frecuencia.posicion_grafica"))
    distribuciones = list(configuracion.obtener("frecuencia.distribuciones"))
    metodos = list(configuracion.obtener("frecuencia.ajuste"))
    escritas = 0

    # --- Serie de maximos ----------------------------------------------------
    carpeta = graficos.directorio_tema(raiz, "pmax_serie")
    for codigo, maximos in sorted(maximos_por_estacion.items()):
        anios = sorted(maximos)
        with graficos.figura(
            individual, titulo=f"Pmáx 24 h  {codigo}",
            etiqueta_x="año", etiqueta_y="Pmáx 24 h (mm)",
        ) as (fig, ax):
            ax.bar(anios, [maximos[a] for a in anios],
                   color=individual.color(0), alpha=0.8)
            media = float(np.mean(list(maximos.values())))
            ax.axhline(media, color="#c00000", linestyle="--", linewidth=1.0)
            ax.annotate(f"media {media:.1f} mm", xy=(0.02, 0.94),
                        xycoords="axes fraction",
                        fontsize=individual.tamano_fuente - 2, color="#c00000")
            fig.tight_layout()
            graficos.guardar(fig, carpeta / str(codigo), individual)
            escritas += 1

    # --- Papel de probabilidad ----------------------------------------------
    carpeta = graficos.directorio_tema(raiz, "papel_probabilidad")
    for codigo, maximos in sorted(maximos_por_estacion.items()):
        valores = np.sort(np.array(list(maximos.values()), dtype=float))
        probabilidad = fr.posicion_grafica(valores.size, formula)
        retorno = 1.0 / (1.0 - probabilidad)
        adoptada = resultado.adoptadas.get(codigo)
        with graficos.figura(
            individual,
            titulo=f"Papel de probabilidad  {codigo}",
            etiqueta_x="periodo de retorno (años)",
            etiqueta_y="Pmáx 24 h (mm)",
        ) as (fig, ax):
            ax.semilogx(retorno, valores, linestyle="none", marker="o",
                        markersize=3.5, color=individual.color(0),
                        label=f"dato ({formula})", zorder=3)
            fila = next((c for c in resultado.cuantiles
                         if c["codigo"] == codigo), None)
            if fila and adoptada:
                equis = [p for p in periodos if fila.get(f"T{p:g}") is not None]
                ax.semilogx(equis, [fila[f"T{p:g}"] for p in equis],
                            color="#c00000", linewidth=1.6, marker="s",
                            markersize=3.0, zorder=4,
                            label=f"{adoptada['distribucion']} "
                                  f"({adoptada['metodo']})")
            ax.legend(fontsize=individual.tamano_fuente - 2, frameon=False)
            ax.grid(True, which="both", color=graficos.GRIS_CONTEXTO,
                    linewidth=0.3, alpha=0.5)
            fig.tight_layout()
            graficos.guardar(fig, carpeta / str(codigo), individual)
            escritas += 1

    # --- Histograma con todas las densidades ---------------------------------
    carpeta = graficos.directorio_tema(raiz, "histograma_pdf")
    for codigo, maximos in sorted(maximos_por_estacion.items()):
        valores = np.sort(np.array(list(maximos.values()), dtype=float))
        adoptada = resultado.adoptadas.get(codigo)
        malla = np.linspace(valores.min() * 0.6, valores.max() * 1.5, 300)
        with graficos.figura(
            individual,
            titulo=f"Histograma y densidades  {codigo}",
            etiqueta_x="Pmáx 24 h (mm)", etiqueta_y="densidad",
        ) as (fig, ax):
            ax.hist(valores, bins=max(6, int(round(np.sqrt(valores.size)))),
                    density=True, color=graficos.GRIS_CONTEXTO, alpha=0.35,
                    edgecolor="white", linewidth=0.4, zorder=1)
            for orden_d, distribucion in enumerate(distribuciones):
                metodo = adoptada["metodo"] if adoptada else metodos[0]
                ajuste = fr.ajustar(list(valores), distribucion, metodo)
                if not ajuste.valido:
                    ajuste = fr.ajustar(list(valores), distribucion,
                                        "maxima_verosimilitud")
                curva = fr.densidad(ajuste, malla)
                if curva is None or not np.any(np.isfinite(curva)):
                    continue
                es_adoptada = bool(adoptada
                                   and distribucion == adoptada["distribucion"])
                ax.plot(malla, curva,
                        color="#c00000" if es_adoptada
                        else individual.color(orden_d),
                        linewidth=2.2 if es_adoptada else 0.7,
                        alpha=1.0 if es_adoptada else 0.5,
                        label=distribucion if es_adoptada else None,
                        zorder=5 if es_adoptada else 2)
            if adoptada:
                ax.legend(fontsize=individual.tamano_fuente - 2, frameon=False)
            fig.tight_layout()
            graficos.guardar(fig, carpeta / str(codigo), individual)
            escritas += 1

    resultado.productos.append(
        f"{rutas.relativa(raiz, base)} ({escritas} figura(s) por estacion)")
    logger.info("%d figura(s) por estacion en %s", escritas,
                rutas.relativa(raiz, base))

# =============================================================================
# Cierre
# =============================================================================
def _resumir(resultado, configuracion, periodos) -> list[Hallazgo]:
    """Informativos de sintesis y advertencias sobre la extrapolacion."""
    import collections
    hallazgos = [Hallazgo(
        INFORMATIVO, "frecuencia.series",
        f"{resultado.estaciones} estacion(es) con serie de maximos anuales, "
        f"{sum(s['n_anios'] for s in resultado.series)} anio-estacion en total. "
        "Ningun valor es sintetico: la serie diaria se conserva integra.",
    )]

    if resultado.adoptadas:
        conteo = collections.Counter(
            d["distribucion"] for d in resultado.adoptadas.values())
        hallazgos.append(Hallazgo(
            INFORMATIVO, "frecuencia.adoptadas",
            "distribucion seleccionada por estacion: "
            + ", ".join(f"{d} ({c})" for d, c in conteo.most_common()) + ".",
        ))

    mayor = max(periodos) if periodos else 0
    longitudes = [s["n_anios"] for s in resultado.series]
    if longitudes and mayor:
        mediana = float(np.median(longitudes))
        if mayor > 2 * mediana:
            hallazgos.append(Hallazgo(
                ADVERTENCIA, "frecuencia.extrapolacion",
                f"se piden cuantiles hasta {mayor:g} anios sobre series cuya "
                f"longitud mediana es de {mediana:.0f} anios. Extrapolar mas "
                "alla del doble de la muestra produce un cuantil cuya "
                "incertidumbre supera con creces lo que sugiere su cifra, y "
                "debe declararse asi en el informe.",
            ))
    return hallazgos


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

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M07_frecuencia.json"
    reporte = {
        "modulo": MODULO,
        "estaciones": resultado.estaciones,
        "registros_leidos": resultado.registros_leidos,
        "excluidos_calificador": resultado.excluidos_calificador,
        "anios_rechazados": resultado.anios_rechazados,
        "series": resultado.series,
        "adoptadas": resultado.adoptadas,
        "cuantiles": resultado.cuantiles,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=2),
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


# =============================================================================
# Interfaz de linea de comandos
# =============================================================================
def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(
        prog="M07_frecuencia.py",
        description="Series de Pmax 24 h y analisis de frecuencia.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--sin-graficas", action="store_true",
                            dest="sin_graficas")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida,
            con_graficas=not argumentos.sin_graficas,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorRutas, ErrorConfiguracion, ErrorFormato) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR


if __name__ == "__main__":
    sys.exit(main())

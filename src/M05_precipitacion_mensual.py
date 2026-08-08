#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M05 - Precipitación mensual: anómalos, consistencia y complemento
=================================================================
Entorno: venv del proyecto.

CLAUDE.md, sección 6, fija el orden del análisis: anómalos, consistencia,
complemento y después ENSO (que es el M05b). Este módulo cubre los tres
primeros, más una etapa previa que la doctrina exige y que suele darse por
supuesta: CONSTRUIR la serie mensual.

Etapa 0. La serie mensual del IDEAM (PTPM_TT_M) es la fuente primaria, y la
agregación de la diaria (PTPM_CON) es la secundaria y el control cruzado. Ambas
existen y no siempre coinciden; donde discrepan, el módulo lo reporta en lugar
de elegir en silencio.

Totalizar la diaria exige un umbral de completitud. Sumar los días presentes sin
ese control subestima los meses incompletos, y un mes al que le falten diez días
de temporada de lluvias entraría al análisis como un mes seco que nunca existió.
El umbral vive en ideam.agregacion_diaria_a_mensual.max_dias_faltantes.

Etapa 1. Anómalos, POR MES CALENDARIO y no sobre la serie entera. En un régimen
bimodal como el de la sabana, abril y julio tienen distribuciones distintas:
aplicar un solo rango intercuartílico a los doce meses marcaría como anómalo
cualquier mes de temporada húmeda. La rutina heredada lo hacía por estación,
sobre toda la serie, y ese es el defecto de fondo que se corrige aquí.

Los anómalos se MARCAN, no se eliminan (config: anomalos.tratamiento). Un valor
extremo puede ser un error de transcripción o una tormenta real, y la estadística
no distingue: lo hace el consultor mirando el registro.

Etapa 2. Consistencia sobre la serie ANUAL, no la mensual. Las pruebas de
homogeneidad suponen una muestra sin estacionalidad, y aplicarlas a datos
mensuales detectaría el ciclo anual como si fuera un quiebre. Se corren Pettitt,
SNHT, Mann-Kendall y rachas sobre el total anual, más doble masa y correlación
contra las vecinas.

Etapa 3. Complemento. Se evalúan todos los métodos declarados con validación
cruzada (enmascarar dato conocido y medir el error de reconstrucción), se
publica la comparación y el consultor adopta uno. La rutina heredada rellenaba
sin validar, de modo que ningún método podía compararse con otro y la elección
quedaba sin sustento.

Nada se descarta en silencio. Toda estación que sale del análisis lo hace con su
motivo escrito, porque un estudio que no puede explicar sus descartes no es
defendible ante interventoría (CLAUDE.md, sección 7).

Productos:
    data/02_procesado/series/precipitacion_mensual.csv
    data/02_procesado/series/precipitacion_mensual_complementada.csv
    data/02_procesado/estaciones/M05_consistencia.csv
    data/02_procesado/estaciones/M05_complemento.csv
    data/02_procesado/estaciones/M05_precipitacion.md
    data/02_procesado/M05_precipitacion.json
    data/05_resultados/graficos/M05_*.png y .svg

Uso:
    python src/M05_precipitacion_mensual.py
    python src/M05_precipitacion_mensual.py --sin-graficas

Códigos de salida:
    0  serie mensual producida
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
from typing import Any, Iterable, Sequence

import numpy as np

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import estadistica as est  # noqa: E402
from comun import esquema, registro, rutas, shapefile  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M05"
DESCRIPCION = "Precipitación mensual: anómalos, consistencia y complemento"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

ETIQUETA_MENSUAL = "PTPM_TT_M"
ETIQUETA_DIARIA = "PTPM_CON"

# Origen de cada valor de la serie final. Viaja con el dato hasta el informe:
# el M15 debe poder decir qué proporción de la serie es observada.
ORIGEN_MENSUAL = "mensual_ideam"
ORIGEN_AGREGADO = "agregado_diaria"
ORIGEN_SINTETICO = "complementado"
ORIGEN_CLIMATOLOGIA = "climatologia_propia"

# El Catalogo Nacional de Estaciones publica la ubicacion en MAGNA-SIRGAS
# geografico, no en WGS84. Se declara en lugar de asumirlo, igual que en el M04b.
CRS_CATALOGO = "EPSG:4686"


# =============================================================================
# Estructuras
# =============================================================================
@dataclass
class SerieMensual:
    """Serie mensual de una estación, con la procedencia de cada valor."""

    codigo: str
    valores: dict[tuple[int, int], float] = field(default_factory=dict)
    origen: dict[tuple[int, int], str] = field(default_factory=dict)

    def fijar(self, anio: int, mes: int, valor: float, procedencia: str) -> None:
        self.valores[(anio, mes)] = valor
        self.origen[(anio, mes)] = procedencia


@dataclass
class ResultadoM05:
    estaciones_evaluadas: int = 0
    estaciones_admitidas: int = 0
    meses_mensual: int = 0
    meses_agregados: int = 0
    meses_completados: int = 0
    registros_excluidos: int = 0
    correcciones: list[dict[str, Any]] = field(default_factory=list)
    meses_climatologia: int = 0
    discrepancias: list[dict[str, Any]] = field(default_factory=list)
    anomalos: list[dict[str, Any]] = field(default_factory=list)
    consistencia: list[dict[str, Any]] = field(default_factory=list)
    correlaciones: dict[str, Any] = field(default_factory=dict)
    estado: list[dict[str, Any]] = field(default_factory=list)
    complemento: list[dict[str, Any]] = field(default_factory=list)
    metodo_recomendado: str = ""
    descartes: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Etapa 0: construcción de la serie mensual
# =============================================================================
def dias_del_mes(anio: int, mes: int) -> int:
    """Días que tiene el mes, atendiendo al bisiesto."""
    import calendar
    return calendar.monthrange(int(anio), int(mes))[1]


def agregar_diaria_a_mensual(
    dias: dict[tuple[int, int], list[float]], max_faltantes: int,
) -> tuple[dict[tuple[int, int], float], int]:
    """
    Totaliza la serie diaria a mensual con control de completitud.

    Devuelve los meses aceptados y cuántos se rechazaron por incompletos. Sumar
    los días presentes sin este control subestima el mes: CLAUDE.md, sección 7,
    lo señala de forma expresa.
    """
    mensual: dict[tuple[int, int], float] = {}
    rechazados = 0
    for (anio, mes), valores in dias.items():
        faltantes = dias_del_mes(anio, mes) - len(valores)
        if faltantes > max_faltantes:
            rechazados += 1
            continue
        mensual[(anio, mes)] = float(sum(valores))
    return mensual, rechazados


def comparar_fuentes(
    mensual: dict[tuple[int, int], float],
    agregado: dict[tuple[int, int], float],
    tolerancia_relativa: float = 0.05,
) -> list[dict[str, Any]]:
    """
    Compara la serie mensual publicada con la agregación de la diaria.

    Solo sobre los meses en que ambas existen. Una discrepancia no dice cuál de
    las dos está mal, y por eso el módulo la reporta en lugar de corregirla: el
    consultor debe mirar el registro.
    """
    discrepancias: list[dict[str, Any]] = []
    for clave in sorted(set(mensual) & set(agregado)):
        a, b = mensual[clave], agregado[clave]
        referencia = max(abs(a), abs(b))
        if referencia == 0:
            continue
        relativa = abs(a - b) / referencia
        if relativa > tolerancia_relativa:
            discrepancias.append({
                "anio": clave[0], "mes": clave[1],
                "mensual_ideam": round(a, 2), "agregado_diaria": round(b, 2),
                "diferencia_rel": round(relativa, 4),
            })
    return discrepancias


def construir_serie(
    codigo: str,
    mensual: dict[tuple[int, int], float],
    agregado: dict[tuple[int, int], float],
    completar: bool,
) -> SerieMensual:
    """
    Fusiona ambas fuentes respetando la precedencia declarada.

    La mensual del IDEAM manda; la agregación de la diaria solo rellena donde
    aquella no tiene dato, y el origen queda anotado por valor.
    """
    serie = SerieMensual(codigo)
    for clave, valor in mensual.items():
        serie.fijar(clave[0], clave[1], valor, ORIGEN_MENSUAL)
    if completar:
        for clave, valor in agregado.items():
            if clave not in serie.valores:
                serie.fijar(clave[0], clave[1], valor, ORIGEN_AGREGADO)
    return serie


# =============================================================================
# Etapa 1: anómalos
# =============================================================================
def limites_de_metodo(
    valores: Sequence[float], metodo: str, configuracion: Config,
) -> est.LimitesAnomalos:
    """Aplica el método declarado, con el mínimo físico de la variable."""
    minimo = configuracion.obtener("complemento.valor_minimo")
    nombre = str(metodo).strip().upper()
    if nombre == "IQR":
        return est.limites_iqr(
            valores,
            q1=float(configuracion.obtener("anomalos.q1")),
            q3=float(configuracion.obtener("anomalos.q3")),
            valor_minimo=minimo,
        )
    if nombre == "ER":
        return est.limites_er(
            valores, k=float(configuracion.obtener("anomalos.k_sigma")),
            valor_minimo=minimo)
    if nombre == "ZSCORE":
        return est.limites_zscore(
            valores, umbral=float(configuracion.obtener("anomalos.zscore_umbral")),
            valor_minimo=minimo)
    raise ErrorConfiguracion(f"método de anómalos no reconocido: {metodo!r}.")


def detectar_anomalos_por_mes(
    serie: SerieMensual, metodo: str, configuracion: Config,
) -> list[dict[str, Any]]:
    """
    Busca anómalos dentro de cada mes calendario por separado.

    Es la corrección de fondo sobre la rutina heredada. En un régimen bimodal,
    un solo rango para los doce meses marcaría toda la temporada húmeda: la
    comparación tiene que ser contra los abriles anteriores, no contra los
    eneros.
    """
    marcados: list[dict[str, Any]] = []
    for mes in range(1, 13):
        claves = sorted(c for c in serie.valores if c[1] == mes)
        valores = [serie.valores[c] for c in claves]
        if len(valores) < 4:
            continue
        try:
            limites = limites_de_metodo(valores, metodo, configuracion)
        except est.ErrorEstadistica:
            continue
        mascara = est.marcar_anomalos(valores, limites)
        for indice, es_anomalo in enumerate(mascara):
            if not es_anomalo:
                continue
            marcados.append({
                "codigo": serie.codigo,
                "anio": claves[indice][0], "mes": mes,
                "valor": round(valores[indice], 2),
                "limite_inf": round(limites.inferior, 2),
                "limite_sup": round(limites.superior, 2),
                "metodo": limites.metodo,
            })
    return marcados


# =============================================================================
# Etapa 2: consistencia
# =============================================================================
def totales_anuales(
    serie: SerieMensual, min_meses: int = 12,
) -> dict[int, float]:
    """
    Total anual, solo de los años completos.

    Un año al que le falten meses da un total menor que no es una señal
    climática sino un hueco, y las pruebas de homogeneidad lo leerían como un
    quiebre.
    """
    por_anio: dict[int, list[float]] = {}
    for (anio, _), valor in serie.valores.items():
        por_anio.setdefault(anio, []).append(valor)
    return {anio: float(sum(v)) for anio, v in por_anio.items()
            if len(v) >= min_meses}


def pruebas_de_homogeneidad(
    anuales: dict[int, float], alfa: float = 0.05,
) -> dict[str, Any]:
    """
    Corre las cuatro pruebas sobre la serie anual y reúne su lectura.

    Cada prueba mira algo distinto: Pettitt y SNHT buscan un quiebre, la primera
    con más sensibilidad en el centro de la serie y la segunda en los extremos;
    Mann-Kendall busca tendencia; rachas busca aleatoriedad. Que dos coincidan
    refuerza el indicio, y que solo una lo señale invita a mirar el registro
    antes de concluir.
    """
    anios = sorted(anuales)
    valores = [anuales[a] for a in anios]
    salida: dict[str, Any] = {"anios": len(anios)}
    for nombre, funcion in (("pettitt", est.pettitt), ("snht", est.snht),
                            ("mann_kendall", est.mann_kendall),
                            ("rachas", est.rachas)):
        try:
            resultado = funcion(valores, alfa)
        except est.ErrorEstadistica as exc:
            salida[nombre] = {"error": str(exc)}
            continue
        datos = resultado.como_dict()
        if "indice_quiebre" in datos and datos["indice_quiebre"] is not None:
            indice = int(datos["indice_quiebre"])
            if 0 <= indice < len(anios):
                datos["anio_quiebre"] = anios[indice]
        salida[nombre] = datos
    return salida


def vecinas_por_correlacion(
    codigo: str,
    matriz: dict[str, dict[tuple[int, int], float]],
    claves: Sequence[tuple[int, int]],
    cuantas: int,
    minima: float,
    ubicaciones: dict[str, dict] | None = None,
    distancia_max_km: float = float("inf"),
    desnivel_max_m: float = float("inf"),
) -> list[dict[str, Any]]:
    """
    Vecinas que lo son por correlacion Y por proximidad en el terreno.

    Los dos criterios hacen falta. La correlacion sola no basta: medido en este
    estudio, entre las parejas que superaban 0,70 el percentil 90 de la distancia
    llegaba a 58 km y el maximo a 105 km, con desniveles de hasta 3157 m. A esa
    separacion la correlacion no significa regimen compartido, sino que a ambas
    las gobierna el mismo ciclo estacional regional, y eso no sustenta una curva
    de doble masa.

    La proximidad sola tampoco basta: en terreno montanoso dos estaciones a diez
    kilometros pueden estar en vertientes opuestas, y por eso se exige ademas la
    correlacion y se limita el desnivel.
    """
    propia = [matriz[codigo].get(c, np.nan) for c in claves]
    candidatas: list[dict[str, Any]] = []
    for otro in matriz:
        if otro == codigo:
            continue
        ajena = [matriz[otro].get(c, np.nan) for c in claves]
        correlacion, comunes = est.correlacion_pareada(propia, ajena)
        if not np.isfinite(correlacion) or correlacion < minima:
            continue
        admisible, distancia, desnivel = es_vecina_admisible(
            codigo, otro, ubicaciones or {}, distancia_max_km, desnivel_max_m)
        if not admisible:
            continue
        candidatas.append({
            "codigo": otro, "correlacion": float(correlacion),
            "meses_comunes": comunes,
            "distancia_km": round(distancia, 2) if distancia is not None else None,
            "desnivel_m": round(desnivel, 1) if desnivel is not None else None,
        })
    candidatas.sort(key=lambda d: -d["correlacion"])
    return candidatas[:cuantas]


def leer_ubicaciones(base: Path, configuracion: Config) -> dict[str, dict]:
    """
    Coordenadas planas y altitud de cada estación, desde la capa del M03.

    Se reproyecta al CRS de cálculo para medir distancias en metros. Hacerlo en
    grados daría distancias que dependen de la latitud, y el área del estudio
    abarca un grado completo.
    """
    ruta = rutas.resolver(
        configuracion.obtener("estaciones.salida_seleccionadas"), base)
    if not ruta.is_file():
        return {}
    try:
        from pyproj import Transformer
    except ImportError:
        return {}
    conversor = Transformer.from_crs(
        CRS_CATALOGO, configuracion.obtener("crs.calculo"), always_xy=True)
    ubicaciones: dict[str, dict] = {}
    for fila in shapefile.leer_registros(
        ruta, ["codigo", "latitud", "longitud", "altitud"],
    ):
        codigo = str(fila.get("codigo", "")).strip()
        try:
            este, norte = conversor.transform(
                float(fila["longitud"]), float(fila["latitud"]))
            altitud = float(fila["altitud"])
        except (TypeError, ValueError, KeyError):
            continue
        ubicaciones[codigo] = {"x": este, "y": norte, "z": altitud}
    return ubicaciones


def es_vecina_admisible(
    uno: str, otro: str, ubicaciones: dict[str, dict],
    distancia_max_km: float, desnivel_max_m: float,
) -> tuple[bool, float | None, float | None]:
    """
    Comprueba que dos estaciones sean vecinas también en el terreno.

    Devuelve la decisión con la distancia y el desnivel, para que el motivo
    quede escrito y no haya que recalcularlo al explicar un descarte.

    Sin ubicación de alguna de las dos se admite el par y quien llama lo
    reporta: negar la vecindad por falta de metadato descartaría dato bueno.
    """
    a, b = ubicaciones.get(uno), ubicaciones.get(otro)
    if a is None or b is None:
        return True, None, None
    distancia = ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5 / 1000.0
    desnivel = abs(a["z"] - b["z"])
    admisible = (distancia <= distancia_max_km) and (desnivel <= desnivel_max_m)
    return admisible, distancia, desnivel


def distribucion_correlaciones(
    matriz: dict[str, dict[tuple[int, int], float]],
    claves: Sequence[tuple[int, int]],
    candidatos: Sequence[float] = (0.60, 0.70, 0.80, 0.90),
) -> dict[str, Any]:
    """
    Mide cómo se distribuye la correlación entre todas las parejas.

    Existe porque el umbral de config no puede elegirse a ciegas. Medido en este
    estudio, la mediana entre parejas es 0,555 y solo el 1,6% alcanza 0,80: con
    ese umbral, más de la mitad de las estaciones se queda sin ninguna vecina.
    No es falta de muestra (la mediana de meses comunes supera los 400), sino que
    el área abarca la subzona entera, de 250 a más de 3000 m y cruzando la
    cordillera, con regímenes que no tienen por qué correlacionarse.

    Devuelve los percentiles y, para cada umbral candidato, cuántas estaciones
    quedarían aisladas. Es la misma lógica de la matriz del M04b: se mide el
    costo de cada elección y el consultor decide.
    """
    codigos = sorted(matriz)
    columnas = {c: [matriz[c].get(k, np.nan) for k in claves] for c in codigos}
    pares: list[float] = []
    comunes: list[int] = []
    mejor_de: dict[str, float] = {c: 0.0 for c in codigos}
    for i, uno in enumerate(codigos):
        for otro in codigos[i + 1:]:
            valor, n = est.correlacion_pareada(columnas[uno], columnas[otro])
            if not np.isfinite(valor):
                continue
            pares.append(valor)
            comunes.append(n)
            mejor_de[uno] = max(mejor_de[uno], valor)
            mejor_de[otro] = max(mejor_de[otro], valor)
    if not pares:
        return {"parejas": 0}
    arreglo = np.asarray(pares)
    return {
        "parejas": len(pares),
        "percentiles": {f"p{q}": round(float(np.percentile(arreglo, q)), 3)
                        for q in (10, 25, 50, 75, 90)},
        "meses_comunes_mediana": int(np.median(comunes)),
        "aisladas_por_umbral": {
            f"{u:.2f}": sum(1 for c in codigos if mejor_de[c] < u)
            for u in candidatos
        },
        "estaciones": len(codigos),
    }


def patron_de_vecinas(
    vecinas: Sequence[str],
    matriz: dict[str, dict[tuple[int, int], float]],
    claves: Sequence[tuple[int, int]],
) -> list[float]:
    """Promedio de las vecinas periodo a periodo, ignorando sus huecos."""
    patron: list[float] = []
    for clave in claves:
        disponibles = [matriz[v][clave] for v in vecinas if clave in matriz[v]]
        patron.append(float(np.mean(disponibles)) if disponibles else np.nan)
    return patron


# =============================================================================
# Etapa 3: complemento
# =============================================================================
def estado_por_estacion(
    orden: Sequence[str],
    claves: Sequence[tuple[int, int]],
    datos: np.ndarray,
    completada: np.ndarray | None,
    aisladas: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """
    Estado de cada estación al salir del módulo.

    Es lo que los módulos siguientes necesitan para decidir cómo usarla, y sin
    esta tabla no lo saben. Una estación sin vecina admisible no se rellena y
    llega al M05b y al M06 con sus huecos abiertos: medido en este estudio, las
    que tienen vecina quedan con 1,5% de huecos y las diez que no la tienen con
    11,8%, una de ellas con el 31%.

    Una media mensual multianual calculada sobre el 69% de los meses no es
    comparable con otra calculada sobre el 99%, y la interpolación las trata
    igual salvo que alguien lo advierta.
    """
    total = len(claves)
    filas: list[dict[str, Any]] = []
    for columna, codigo in enumerate(orden):
        observados = int(np.sum(np.isfinite(datos[:, columna])))
        finales = (int(np.sum(np.isfinite(completada[:, columna])))
                   if completada is not None else observados)
        filas.append({
            "codigo": codigo,
            "periodos": total,
            "observados": observados,
            "completados": finales - observados,
            "huecos": total - finales,
            "pct_observado": round(100.0 * observados / max(1, total), 1),
            "pct_huecos": round(100.0 * (total - finales) / max(1, total), 1),
            "sin_vecina": codigo in set(aisladas),
        })
    return filas


def corregir_por_doble_masa(
    serie: SerieMensual,
    claves_comunes: Sequence[tuple[int, int]],
    indice_quiebre: int | None,
    razon: float,
) -> int:
    """
    Homogeneiza el tramo anterior al quiebre multiplicandolo por la razon.

    Es el metodo clasico: si despues del quiebre la estacion registra en otra
    escala, se lleva el tramo antiguo a esa misma escala para que toda la serie
    sea comparable. Se corrige el tramo ANTIGUO y no el reciente porque el
    reciente refleja las condiciones actuales de la estacion.

    La correccion se aplica sobre los periodos anteriores al quiebre EN FECHA,
    no sobre el indice de la curva: la curva solo acumula periodos comunes con
    el patron, y usar su indice desplazaria el corte.
    """
    if not claves_comunes or indice_quiebre is None:
        return 0
    if indice_quiebre >= len(claves_comunes):
        return 0
    corte = claves_comunes[indice_quiebre]
    modificados = 0
    for clave in list(serie.valores):
        if clave < corte:
            serie.valores[clave] = serie.valores[clave] * razon
            modificados += 1
    return modificados


def climatologia_mensual(
    columna: np.ndarray, claves: Sequence[tuple[int, int]],
) -> dict[int, float]:
    """Media de cada mes calendario con el dato disponible de la propia serie."""
    acumulado: dict[int, list[float]] = {}
    for indice, (_, mes) in enumerate(claves):
        valor = columna[indice]
        if np.isfinite(valor):
            acumulado.setdefault(mes, []).append(float(valor))
    return {mes: float(np.mean(v)) for mes, v in acumulado.items() if v}


def rellenar_con_climatologia(
    datos: np.ndarray, claves: Sequence[tuple[int, int]],
) -> tuple[np.ndarray, int]:
    """
    Completa lo que las vecinas no pudieron, con la media mensual propia.

    No introduce informacion de otra estacion ni supone un regimen que no se ha
    verificado, y deja la serie continua. Aplana la variabilidad mas que
    cualquier metodo por vecinas, porque sustituye el hueco por el valor central
    de su mes: por eso el origen se marca aparte y el M07 debe poder medirlo.
    """
    salida = np.array(datos, dtype=float, copy=True)
    rellenados = 0
    for columna in range(datos.shape[1]):
        medias = climatologia_mensual(datos[:, columna], claves)
        if not medias:
            continue
        for fila, (_, mes) in enumerate(claves):
            if not np.isfinite(salida[fila, columna]) and mes in medias:
                salida[fila, columna] = medias[mes]
                rellenados += 1
    return salida, rellenados


def _matriz_numpy(
    codigos: Sequence[str],
    matriz: dict[str, dict[tuple[int, int], float]],
    claves: Sequence[tuple[int, int]],
) -> np.ndarray:
    """Matriz periodos por estaciones, con nan en los huecos."""
    salida = np.full((len(claves), len(codigos)), np.nan)
    for columna, codigo in enumerate(codigos):
        serie = matriz[codigo]
        for fila, clave in enumerate(claves):
            if clave in serie:
                salida[fila, columna] = serie[clave]
    return salida


def rellenar(
    datos: np.ndarray, metodo: str, configuracion: Config,
    admisibles: np.ndarray | None = None,
) -> np.ndarray:
    """
    Aplica un método de relleno a la matriz completa.

    Todos respetan el mínimo físico declarado: rellenar precipitación con un
    valor negativo es un resultado incorrecto, no una aproximación.
    """
    minimo = configuracion.obtener("complemento.valor_minimo")
    vecinos = int(configuracion.obtener("complemento.k_vecinos"))
    # El mismo umbral que gobierna la seleccion de vecinas en la etapa de
    # consistencia. Rellenar desde una vecina con correlacion 0,3 produce dato
    # sintetico sin sustento, y el valor resultante entraria al analisis con la
    # misma apariencia que una observacion.
    correlacion_minima = float(
        configuracion.obtener("consistencia.correlacion_minima"))
    nombre = str(metodo).strip().lower()
    relleno = np.array(datos, dtype=float, copy=True)

    if nombre == "razon_normal":
        salida = _razon_normal(relleno)
    elif nombre == "regresion_vecinas":
        salida = _regresion_vecinas(relleno, vecinos, correlacion_minima,
                                    admisibles)
    elif nombre == "idw":
        salida = _promedio_ponderado_correlacion(
            relleno, vecinos, correlacion_minima, admisibles)
    elif nombre == "knn":
        from sklearn.impute import KNNImputer
        salida = KNNImputer(n_neighbors=vecinos, weights="distance",
                            metric="nan_euclidean").fit_transform(relleno)
    elif nombre == "mice":
        from sklearn.experimental import enable_iterative_imputer  # noqa: F401
        from sklearn.impute import IterativeImputer
        from sklearn.linear_model import BayesianRidge
        salida = IterativeImputer(
            estimator=BayesianRidge(), n_nearest_features=vecinos,
            initial_strategy="mean", imputation_order="ascending",
            min_value=(minimo if minimo is not None else -np.inf),
            max_iter=10, random_state=int(
                configuracion.obtener("ejecucion.semilla_aleatoria")),
        ).fit_transform(relleno)
    else:
        raise ErrorConfiguracion(f"método de complemento no reconocido: {metodo!r}.")

    if minimo is not None:
        salida = np.where(np.isfinite(salida), np.maximum(salida, minimo), salida)
    return salida


def _media_por_columna(datos: np.ndarray) -> np.ndarray:
    """Media de cada estación ignorando huecos, con cero si no tiene dato."""
    with np.errstate(invalid="ignore"):
        medias = np.nanmean(datos, axis=0)
    return np.where(np.isfinite(medias), medias, 0.0)


def _razon_normal(datos: np.ndarray) -> np.ndarray:
    """
    Método de la razón normal: escala el promedio de las vecinas por la razón
    entre las medias.

    Es el método clásico de la hidrología y el más fácil de sustentar ante
    interventoría, porque no tiene parámetros que ajustar.
    """
    salida = np.array(datos, copy=True)
    medias = _media_por_columna(datos)
    for fila in range(datos.shape[0]):
        presentes = np.isfinite(datos[fila])
        if not presentes.any():
            continue
        for columna in np.where(~presentes)[0]:
            if medias[columna] == 0:
                continue
            razones = [datos[fila, otra] * medias[columna] / medias[otra]
                       for otra in np.where(presentes)[0] if medias[otra] > 0]
            if razones:
                salida[fila, columna] = float(np.mean(razones))
    return salida


def _correlaciones(
    datos: np.ndarray, admisibles: np.ndarray | None = None,
) -> np.ndarray:
    """
    Matriz de correlación entre estaciones, con cero donde no se puede.

    La máscara de admisibilidad anula las parejas que no son vecinas en el
    terreno. Rellenar desde una estación a cien kilómetros y tres mil metros de
    desnivel no está mejor sustentado que hacerlo desde una mal correlacionada:
    el mismo criterio que gobierna la doble masa debe gobernar el relleno.
    """
    columnas = datos.shape[1]
    matriz = np.zeros((columnas, columnas))
    for i in range(columnas):
        for j in range(i + 1, columnas):
            if admisibles is not None and not admisibles[i, j]:
                continue
            valor, _ = est.correlacion_pareada(datos[:, i], datos[:, j])
            if np.isfinite(valor):
                matriz[i, j] = matriz[j, i] = valor
    return matriz


def _promedio_ponderado_correlacion(
    datos: np.ndarray, vecinos: int, minima: float = 0.0,
    admisibles: np.ndarray | None = None,
) -> np.ndarray:
    """
    Promedio de las vecinas ponderado por el cuadrado de la correlación.

    Ocupa el lugar del IDW clásico, que pondera por distancia. Se pondera por
    correlación por la misma razón por la que las vecinas se eligen así: en
    terreno montañoso la cercanía no garantiza el mismo régimen.
    """
    salida = np.array(datos, copy=True)
    correlaciones = _correlaciones(datos, admisibles)
    for columna in range(datos.shape[1]):
        orden = np.argsort(-correlaciones[columna])
        mejores = [j for j in orden if j != columna
                   and correlaciones[columna, j] >= minima][:vecinos]
        if not mejores:
            continue
        pesos = np.array([correlaciones[columna, j] ** 2 for j in mejores])
        for fila in np.where(~np.isfinite(datos[:, columna]))[0]:
            valores = datos[fila, mejores]
            validos = np.isfinite(valores)
            if validos.any():
                salida[fila, columna] = float(
                    np.sum(valores[validos] * pesos[validos])
                    / np.sum(pesos[validos]))
    return salida


def _regresion_vecinas(
    datos: np.ndarray, vecinos: int, minima: float = 0.0,
    admisibles: np.ndarray | None = None,
) -> np.ndarray:
    """
    Regresión lineal simple contra la vecina mejor correlacionada disponible.

    Se recorre en orden de correlación y se usa la primera que tenga dato en el
    periodo, de modo que el relleno se apoye siempre en la mejor información
    disponible y no en un promedio que diluya.
    """
    salida = np.array(datos, copy=True)
    correlaciones = _correlaciones(datos, admisibles)
    for columna in range(datos.shape[1]):
        orden = [j for j in np.argsort(-correlaciones[columna])
                 if j != columna and correlaciones[columna, j] >= minima][:vecinos]
        ajustes: dict[int, tuple[float, float]] = {}
        for j in orden:
            comunes = np.isfinite(datos[:, columna]) & np.isfinite(datos[:, j])
            if int(np.sum(comunes)) < 12:
                continue
            pendiente, intercepto = np.polyfit(
                datos[comunes, j], datos[comunes, columna], 1)
            ajustes[j] = (float(pendiente), float(intercepto))
        for fila in np.where(~np.isfinite(datos[:, columna]))[0]:
            for j in orden:
                if j in ajustes and np.isfinite(datos[fila, j]):
                    pendiente, intercepto = ajustes[j]
                    salida[fila, columna] = pendiente * datos[fila, j] + intercepto
                    break
    return salida


def validacion_cruzada(
    datos: np.ndarray, metodo: str, configuracion: Config,
    proporcion: float = 0.10, semilla: int = 42,
    admisibles: np.ndarray | None = None,
) -> dict[str, Any]:
    """
    Enmascara datos conocidos, los reconstruye y mide el error.

    Es lo que la rutina heredada no hacía, y sin ello ningún método puede
    compararse con otro: rellenar siempre 'funciona', porque siempre produce un
    número. La pregunta es cuánto se parece ese número al dato real.

    Se enmascara solo donde hay dato observado, y se exige que la fila conserve
    al menos otra estación con dato: una fila enteramente vacía no se puede
    reconstruir con ningún método y su error no informa sobre ninguno.
    """
    generador = np.random.default_rng(semilla)
    observados = np.argwhere(np.isfinite(datos))
    if observados.size == 0:
        return {"metodo": metodo, "error": "no hay datos observados"}

    utilizables = [(f, c) for f, c in observados
                   if int(np.sum(np.isfinite(datos[f]))) >= 2]
    if not utilizables:
        return {"metodo": metodo, "error": "ninguna fila tiene dos estaciones"}

    cuantos = max(1, int(len(utilizables) * proporcion))
    elegidos = generador.choice(len(utilizables), size=cuantos, replace=False)
    mascara = [utilizables[i] for i in elegidos]

    perforada = np.array(datos, copy=True)
    reales = []
    for fila, columna in mascara:
        reales.append(datos[fila, columna])
        perforada[fila, columna] = np.nan

    try:
        reconstruida = rellenar(perforada, metodo, configuracion,
                                admisibles)
    except Exception as exc:  # noqa: BLE001
        return {"metodo": metodo, "error": f"{type(exc).__name__}: {exc}"}

    estimados = [reconstruida[f, c] for f, c in mascara]
    reales_ar = np.asarray(reales, dtype=float)
    estimados_ar = np.asarray(estimados, dtype=float)
    validos = np.isfinite(estimados_ar)
    if not validos.any():
        return {"metodo": metodo, "error": "el método no rellenó ningún hueco"}

    residuo = estimados_ar[validos] - reales_ar[validos]
    observado = reales_ar[validos]
    estimado = estimados_ar[validos]
    denominador = float(np.sum((observado - np.mean(observado)) ** 2))

    # Razon de desviaciones: el criterio que el error cuadratico NO recoge.
    # Los metodos de regresion tiran hacia la media, de modo que aciertan el
    # promedio y aplanan los extremos. Eso mejora el RMSE y empeora la serie
    # para lo que viene despues: el M07 ajusta distribuciones de maximos y el
    # M05b clasifica por fase ENSO, y ambos dependen de la variabilidad. Una
    # razon por debajo de 1 significa que la serie complementada es mas plana
    # que la observada.
    desviacion_real = float(np.std(observado, ddof=1)) if observado.size > 1 else 0.0
    desviacion_est = float(np.std(estimado, ddof=1)) if estimado.size > 1 else 0.0
    razon = (desviacion_est / desviacion_real) if desviacion_real > 0 else None

    correlacion = None
    if observado.size > 2 and desviacion_real > 0 and desviacion_est > 0:
        correlacion = float(np.corrcoef(observado, estimado)[0, 1])

    return {
        "metodo": metodo,
        "n_validacion": int(validos.sum()),
        "sin_rellenar": int((~validos).sum()),
        "rmse": round(float(np.sqrt(np.mean(residuo ** 2))), 3),
        "mae": round(float(np.mean(np.abs(residuo))), 3),
        "sesgo": round(float(np.mean(residuo)), 3),
        "nash_sutcliffe": round(
            float(1.0 - np.sum(residuo ** 2) / denominador), 4)
        if denominador > 0 else None,
        "razon_desviacion": round(razon, 4) if razon is not None else None,
        "r_validacion": round(correlacion, 4) if correlacion is not None else None,
    }


# =============================================================================
# Lectura de insumos
# =============================================================================
def estaciones_admitidas(
    base: Path, configuracion: Config,
) -> tuple[set[str], str, int]:
    """
    Estaciones que superan el umbral de longitud adoptado en el M04b.

    La comunicación es por archivo, como exige CLAUDE.md, sección 2: se lee el
    detalle que el M04b dejó escrito y no se recalcula nada. Si el consultor
    todavía no adoptó umbral, entran todas y el módulo lo advierte.
    """
    umbral = configuracion.obtener("sensibilidad_series.umbral_adoptado_anios")
    ventana = configuracion.obtener("sensibilidad_series.ventana_adoptada")
    excepciones = dict(
        configuracion.obtener("sensibilidad_series.umbrales_por_variable") or {})
    if excepciones.get("precipitacion") is not None:
        umbral = excepciones["precipitacion"]

    anio = int(configuracion.obtener("proyecto.anio_estudio"))
    if ventana is None:
        etiqueta = ""
    else:
        inicio = int(ventana[0]) if ventana[0] is not None else 1900
        fin = int(ventana[1]) if ventana[1] is not None else anio
        etiqueta = f"{inicio}-{fin}"

    detalle = rutas.directorio("procesado_estaciones", base) / \
        "sensibilidad_series.csv"
    if umbral is None or not etiqueta or not detalle.is_file():
        return set(), etiqueta, 0

    criterio = configuracion.obtener("sensibilidad_series.criterio_umbral")
    columna = ("racha_" if str(criterio).lower() == "racha" else "utiles_") + etiqueta
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")

    admitidas: set[str] = set()
    with detalle.open(encoding="utf-8-sig", newline="") as manejador:
        lector = csv.DictReader(manejador, delimiter=delimitador)
        if columna not in (lector.fieldnames or ()):
            raise ErrorFormato(
                f"el detalle del M04b no trae la columna {columna!r}. "
                f"Disponibles: {lector.fieldnames}. Volver a ejecutar el M04b "
                "con la ventana adoptada entre las evaluadas."
            )
        for fila in lector:
            if not fila.get("etiqueta", "").startswith("PTPM"):
                continue
            if fila.get("en_m03") != "True":
                continue
            try:
                if int(fila[columna]) >= int(umbral):
                    admitidas.add(fila["codigo"].strip())
            except (TypeError, ValueError):
                continue
    return admitidas, etiqueta, int(umbral)


def leer_precipitacion(
    ruta: Path, delimitador: str, ventana: tuple[int, int],
    excluidos: Sequence[str] = (),
) -> tuple[dict[str, dict], dict[str, dict], int, int]:
    """
    Recorre la serie consolidada y separa mensual publicada de diaria.

    Un solo recorrido en flujo sobre el archivo del M04, que pesa cientos de MB.
    """
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra la serie consolidada en {ruta}. Ejecutar el M04."
        )
    # El Calificador es el juicio del IDEAM sobre su propio dato, y
    # perfiles_ideam.yaml declara que 'DATO RECHAZADO' se excluye del analisis.
    # Un registro puede traer varias marcas separadas por barra.
    marcas_excluidas = {m.strip().upper() for m in excluidos if m}
    mensual: dict[str, dict[tuple[int, int], float]] = {}
    diaria: dict[str, dict[tuple[int, int], list[float]]] = {}
    leidos = excluidos_total = 0
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=delimitador):
            etiqueta = fila.get("etiqueta", "")
            if etiqueta not in (ETIQUETA_MENSUAL, ETIQUETA_DIARIA):
                continue
            fecha = fila.get("fecha", "")
            if len(fecha) < 7:
                continue
            try:
                anio, mes = int(fecha[:4]), int(fecha[5:7])
                valor = float(fila.get("valor", ""))
            except (TypeError, ValueError):
                continue
            if not (ventana[0] <= anio <= ventana[1]):
                continue
            if marcas_excluidas:
                marcas = {m.strip().upper()
                          for m in (fila.get("calificador") or "").split("|")}
                if marcas & marcas_excluidas:
                    excluidos_total += 1
                    continue
            leidos += 1
            codigo = fila.get("codigo", "").strip()
            if etiqueta == ETIQUETA_MENSUAL:
                mensual.setdefault(codigo, {})[(anio, mes)] = valor
            else:
                diaria.setdefault(codigo, {}).setdefault(
                    (anio, mes), []).append(valor)
    return mensual, diaria, leidos, excluidos_total


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
    """Encadena las cuatro etapas y escribe los productos."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")
    anio_estudio = int(configuracion.obtener("proyecto.anio_estudio"))
    max_faltantes = int(configuracion.obtener(
        "ideam.agregacion_diaria_a_mensual.max_dias_faltantes"))
    completar = bool(configuracion.obtener(
        "ideam.precipitacion_mensual.completar_con_agregacion_diaria"))
    ruta_serie = rutas.directorio("procesado_series", base) / "series_ideam.csv"

    admitidas, etiqueta_ventana, umbral = estaciones_admitidas(base, configuracion)
    ventana_adoptada = configuracion.obtener("sensibilidad_series.ventana_adoptada")
    if ventana_adoptada is None:
        limites = (1900, anio_estudio)
    else:
        limites = (int(ventana_adoptada[0]) if ventana_adoptada[0] is not None
                   else 1900,
                   int(ventana_adoptada[1]) if ventana_adoptada[1] is not None
                   else anio_estudio)

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"serie consolidada": rutas.relativa(ruta_serie, base),
                 "ventana adoptada": etiqueta_ventana or "sin adoptar",
                 "umbral adoptado": f"{umbral} anios" if umbral else "sin adoptar"},
        parametros={
            "ideam.agregacion_diaria_a_mensual.max_dias_faltantes": max_faltantes,
            "anomalos.metodo": configuracion.obtener("anomalos.metodo"),
            "anomalos.tratamiento": configuracion.obtener("anomalos.tratamiento"),
            "consistencia.correlacion_minima":
                configuracion.obtener("consistencia.correlacion_minima"),
            "complemento.metodos_evaluados":
                list(configuracion.obtener("complemento.metodos_evaluados")),
        },
    )

    resultado = ResultadoM05()
    if not admitidas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "m04b.umbral",
            "no se pudo aplicar el umbral de longitud del M04b: entran todas "
            "las estaciones con serie de precipitacion. Ejecutar el M04b y "
            "declarar umbral_adoptado_anios y ventana_adoptada.",
        ))

    # --- Etapa 0 -------------------------------------------------------------
    with registro.bloque(logger, "Etapa 0: construccion de la serie mensual"):
        excluidos_cal = list(
            configuracion.obtener("anomalos.calificadores_excluidos") or ())
        mensual, diaria, leidos, descartados_cal = leer_precipitacion(
            ruta_serie, delimitador, limites, excluidos_cal)
        if descartados_cal:
            resultado.registros_excluidos = descartados_cal
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "calificador.excluidos",
                f"{descartados_cal:,} registro(s) excluidos por Calificador "
                f"{excluidos_cal}. Es el juicio del IDEAM sobre su propio dato, "
                "y perfiles_ideam.yaml declara ese efecto. Los valores que el "
                "IQR senala NO se eliminan: son la cola natural de una "
                "distribucion asimetrica, no errores.",
            ))
        codigos = sorted(set(mensual) | set(diaria))
        if admitidas:
            codigos = [c for c in codigos if c in admitidas]
        resultado.estaciones_evaluadas = len(codigos)

        series: dict[str, SerieMensual] = {}
        rechazados_total = 0
        for codigo in codigos:
            agregado, rechazados = agregar_diaria_a_mensual(
                diaria.get(codigo, {}), max_faltantes)
            rechazados_total += rechazados
            propia = mensual.get(codigo, {})
            resultado.discrepancias.extend(
                {"codigo": codigo, **d}
                for d in comparar_fuentes(propia, agregado))
            serie = construir_serie(codigo, propia, agregado, completar)
            if serie.valores:
                series[codigo] = serie

        resultado.estaciones_admitidas = len(series)
        resultado.meses_mensual = sum(
            1 for s in series.values() for o in s.origen.values()
            if o == ORIGEN_MENSUAL)
        resultado.meses_agregados = sum(
            1 for s in series.values() for o in s.origen.values()
            if o == ORIGEN_AGREGADO)
        logger.info(
            "%s registro(s) leidos | %d estacion(es) | %s mes(es) de la serie "
            "mensual y %s completados con la diaria",
            f"{leidos:,}", len(series), f"{resultado.meses_mensual:,}",
            f"{resultado.meses_agregados:,}")
        if rechazados_total:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "agregacion.incompletos",
                f"{rechazados_total:,} mes(es) de la serie diaria no se "
                f"totalizaron por superar {max_faltantes} dia(s) faltante(s). "
                "Sumarlos habria subestimado el acumulado mensual.",
            ))

    if not series:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "serie.vacia",
            "ninguna estacion tiene serie mensual de precipitacion en la "
            "ventana adoptada. Revisar el umbral del M04b y la ingesta del M04.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    claves = sorted({c for s in series.values() for c in s.valores})
    matriz = {c: dict(s.valores) for c, s in series.items()}
    orden = sorted(series)

    # --- Etapa 1 -------------------------------------------------------------
    with registro.bloque(logger, "Etapa 1: datos anomalos"):
        metodo = configuracion.obtener("anomalos.metodo")
        for serie in series.values():
            resultado.anomalos.extend(
                detectar_anomalos_por_mes(serie, metodo, configuracion))
        tratamiento = configuracion.obtener("anomalos.tratamiento")
        logger.info("%d valor(es) anomalo(s) con metodo %s; tratamiento: %s",
                    len(resultado.anomalos), metodo, tratamiento)
        if tratamiento != "marcar":
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "anomalos.tratamiento",
                f"el tratamiento declarado es {tratamiento!r} y no 'marcar'. "
                "Un valor extremo puede ser un error de transcripcion o una "
                "tormenta real, y la estadistica no distingue: alterarlo sin "
                "revisar el registro modifica el dato de partida.",
            ))

    # --- Etapa 2 -------------------------------------------------------------
    with registro.bloque(logger, "Etapa 2: consistencia"):
        minima = float(configuracion.obtener("consistencia.correlacion_minima"))
        cuantas = int(configuracion.obtener("consistencia.n_estaciones_vecinas"))
        distancia_max = float(
            configuracion.obtener("consistencia.distancia_maxima_km"))
        desnivel_max = float(
            configuracion.obtener("consistencia.desnivel_maximo_m"))
        ubicaciones = leer_ubicaciones(base, configuracion)
        if not ubicaciones:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "consistencia.sin_ubicaciones",
                "no se pudieron leer las coordenadas de las estaciones: la "
                "vecindad se juzga solo por correlacion, sin limite de distancia "
                "ni de desnivel. Una vecina puede quedar a mas de 100 km.",
            ))
        for codigo, serie in sorted(series.items()):
            anuales = totales_anuales(serie)
            fila: dict[str, Any] = {"codigo": codigo,
                                    "anios_completos": len(anuales)}
            if len(anuales) >= 10:
                fila["pruebas"] = pruebas_de_homogeneidad(anuales)
            else:
                fila["pruebas"] = {"error": f"solo {len(anuales)} anio(s) completos"}

            vecinas = vecinas_por_correlacion(
                codigo, matriz, claves, cuantas, minima, ubicaciones,
                distancia_max, desnivel_max)
            fila["n_vecinas"] = len(vecinas)
            fila["mejor_correlacion"] = (
                round(vecinas[0]["correlacion"], 4) if vecinas else None)
            fila["dist_media_km"] = (
                round(float(np.mean([v["distancia_km"] for v in vecinas
                                     if v["distancia_km"] is not None])), 1)
                if any(v["distancia_km"] is not None for v in vecinas) else None)
            fila["vecinas"] = [v["codigo"] for v in vecinas]
            if vecinas:
                patron = patron_de_vecinas(
                    [v["codigo"] for v in vecinas], matriz, claves)
                propia = [matriz[codigo].get(c, np.nan) for c in claves]
                comunes = [k for k, a, b in zip(claves, propia, patron)
                           if np.isfinite(a) and np.isfinite(b)]
                # Correlacion sobre la serie SIN acumular. Es el discriminante,
                # y no el R2 de la doble masa: aquel sale casi 1 siempre porque
                # ambos ejes son acumulados.
                r_patron, _ = est.correlacion_pareada(propia, patron)
                fila["r_patron"] = (round(float(r_patron), 4)
                                    if np.isfinite(r_patron) else None)
                acum_e, acum_p = est.curva_doble_masa(propia, patron)
                fila["doble_masa"] = est.quiebre_doble_masa(acum_e, acum_p)
                fila["claves_comunes"] = comunes
                fila["acumulados"] = (acum_p.tolist(), acum_e.tolist())
            else:
                fila["r_patron"] = None
                fila["doble_masa"] = {"hay_quiebre": False,
                                      "motivo": "sin vecinas correlacionadas"}
            resultado.consistencia.append(fila)

        resultado.correlaciones = distribucion_correlaciones(matriz, claves)
        percentiles = resultado.correlaciones.get("percentiles", {})
        aisladas = resultado.correlaciones.get("aisladas_por_umbral", {})
        if percentiles:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "consistencia.correlaciones",
                "distribucion medida de la correlacion entre parejas: "
                + ", ".join(f"{k}={v}" for k, v in percentiles.items())
                + f" sobre {resultado.correlaciones['parejas']} pareja(s), con "
                f"mediana de {resultado.correlaciones['meses_comunes_mediana']} "
                "meses comunes. Estaciones que quedarian sin ninguna vecina "
                "segun el umbral: "
                + ", ".join(f"{k} -> {v}" for k, v in aisladas.items()) + ".",
            ))

        sin_vecinas = [f["codigo"] for f in resultado.consistencia
                       if not f["n_vecinas"]]
        descartar = bool(configuracion.obtener("consistencia.descartar_bajo_umbral"))
        if sin_vecinas:
            proporcion = 100.0 * len(sin_vecinas) / max(1, len(resultado.consistencia))
            severidad = BLOQUEANTE if (descartar and proporcion > 50.0) else ADVERTENCIA
            resultado.hallazgos.append(Hallazgo(
                severidad, "consistencia.sin_vecinas",
                f"{len(sin_vecinas)} de {len(resultado.consistencia)} estacion(es) "
                f"({proporcion:.0f}%) no tienen ninguna vecina que alcance "
                f"correlacion {minima}. No se les puede aplicar doble masa ni "
                "complementar por regresion o razon normal."
                + (" Con descartar_bajo_umbral activo se perderia mas de la mitad "
                   "de la red: el umbral es demasiado exigente para esta cuenca y "
                   "debe revisarse antes de continuar."
                   if severidad == BLOQUEANTE else
                   " Se conservan: la falta de una vecina correlacionada no "
                   "invalida la observacion, solo impide complementarla por "
                   "vecinas."),
            ))
            for codigo in sin_vecinas:
                resultado.descartes.append({
                    "codigo": codigo, "etapa": "consistencia",
                    "motivo": f"sin vecinas con correlacion >= {minima}",
                    # No se elimina de la serie. El umbral gobierna QUE VECINAS
                    # sirven para complementar, no si la estacion es valida:
                    # confundir ambas cosas descartaria observaciones buenas.
                    "descartada": False,
                })
        # Descarte por consistencia. El criterio es la correlacion contra el
        # patron de vecinas sobre la serie SIN acumular: una estacion que no
        # alcanza el umbral no comparte regimen con su entorno y su serie no
        # puede verificarse ni complementarse.
        if descartar:
            fuera = [f["codigo"] for f in resultado.consistencia
                     if f["r_patron"] is None or f["r_patron"] < minima]
            if fuera:
                for codigo in fuera:
                    series.pop(codigo, None)
                    matriz.pop(codigo, None)
                orden = sorted(series)
                resultado.consistencia = [
                    f for f in resultado.consistencia if f["codigo"] not in fuera]
                for registro_descarte in resultado.descartes:
                    if registro_descarte["codigo"] in fuera:
                        registro_descarte["descartada"] = True
                resultado.estaciones_admitidas = len(series)
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, "consistencia.descartadas",
                    f"{len(fuera)} estacion(es) ELIMINADAS del analisis por no "
                    f"alcanzar correlacion {minima} contra el patron de sus "
                    f"vecinas: {fuera}. No comparten regimen con su entorno, de "
                    "modo que su serie no puede verificarse por doble masa ni "
                    "complementarse por vecinas. Quedan "
                    f"{len(series)} estacion(es).",
                ))

        con_quiebre = [f["codigo"] for f in resultado.consistencia
                       if f["doble_masa"].get("hay_quiebre")]
        if con_quiebre:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "consistencia.doble_masa",
                f"{len(con_quiebre)} estacion(es) con quiebre de pendiente en la "
                f"curva de doble masa: {con_quiebre[:8]}"
                + ("..." if len(con_quiebre) > 8 else "")
                + ". Indica posible traslado, cambio de instrumento o de "
                "observador. La correccion exige criterio del consultor y queda "
                "sin aplicar.",
            ))
        if bool(configuracion.obtener("consistencia.corregir_doble_masa")):
            razon_maxima = float(
                configuracion.obtener("consistencia.razon_maxima_correccion"))
            sospechosas = []
            for fila in resultado.consistencia:
                doble = fila.get("doble_masa") or {}
                if not doble.get("hay_quiebre"):
                    continue
                razon = doble.get("razon_pendientes")
                if not razon or not np.isfinite(razon) or razon <= 0:
                    continue
                if max(razon, 1.0 / razon) > razon_maxima:
                    sospechosas.append((fila["codigo"], round(razon, 3)))
                    continue
                modificados = corregir_por_doble_masa(
                    series[fila["codigo"]], fila.get("claves_comunes") or (),
                    doble.get("indice"), razon)
                if modificados:
                    comunes = fila.get("claves_comunes") or ()
                    indice = doble.get("indice")
                    resultado.correcciones.append({
                        "codigo": fila["codigo"],
                        "razon_pendientes": round(razon, 4),
                        "valores_corregidos": modificados,
                        "anio_quiebre": (comunes[indice][0]
                                         if comunes and indice is not None
                                         and indice < len(comunes) else None),
                    })
            if resultado.correcciones:
                matriz = {c: dict(s.valores) for c, s in series.items()}
                resultado.hallazgos.append(Hallazgo(
                    INFORMATIVO, "consistencia.corregidas",
                    f"{len(resultado.correcciones)} estacion(es) homogeneizadas "
                    "multiplicando su tramo anterior al quiebre por la razon de "
                    "pendientes. Se corrige el tramo ANTIGUO porque el reciente "
                    "refleja las condiciones actuales. El factor aplicado se "
                    "publica en M05_correcciones.csv.",
                ))
            if sospechosas:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, "consistencia.correccion_sospechosa",
                    f"{len(sospechosas)} estacion(es) con razon de pendientes "
                    f"fuera de {razon_maxima}: {sospechosas}. NO se corrigieron. "
                    "Un factor asi no es deriva de instrumento: revisar si el "
                    "codigo agrupa dos estaciones distintas.",
                ))

        logger.info("%d estacion(es) evaluadas; %d con quiebre de doble masa",
                    len(resultado.consistencia), len(con_quiebre))

    # --- Etapa 3 -------------------------------------------------------------
    completada = None
    with registro.bloque(logger, "Etapa 3: complemento"):
        datos = _matriz_numpy(orden, matriz, claves)
        # El mismo criterio de vecindad que gobierna la doble masa gobierna el
        # relleno: si una estacion no sirve para verificar, tampoco para rellenar.
        admisibles = np.ones((len(orden), len(orden)), dtype=bool)
        for i, uno in enumerate(orden):
            for j, otro in enumerate(orden):
                if i >= j:
                    continue
                ok, _, _ = es_vecina_admisible(
                    uno, otro, ubicaciones, distancia_max, desnivel_max)
                admisibles[i, j] = admisibles[j, i] = ok
        huecos = int(np.sum(~np.isfinite(datos)))
        logger.info(
            "Matriz de %d periodo(s) x %d estacion(es); %s hueco(s) (%.1f%%)",
            len(claves), len(orden), f"{huecos:,}", 100.0 * huecos / datos.size)

        if bool(configuracion.obtener("complemento.validacion_cruzada")):
            semilla = int(configuracion.obtener("ejecucion.semilla_aleatoria"))
            for metodo in configuracion.obtener("complemento.metodos_evaluados"):
                resultado.complemento.append(
                    validacion_cruzada(datos, metodo, configuracion,
                                       semilla=semilla,
                                       admisibles=admisibles))
                ultimo = resultado.complemento[-1]
                logger.info("  %-20s %s", metodo,
                            ultimo.get("error")
                            or f"RMSE {ultimo['rmse']} | MAE {ultimo['mae']} | "
                               f"NSE {ultimo['nash_sutcliffe']} | "
                               f"sd_est/sd_obs {ultimo['razon_desviacion']}")
            validos = [c for c in resultado.complemento if "rmse" in c]
            if validos:
                resultado.metodo_recomendado = min(
                    validos, key=lambda c: c["rmse"])["metodo"]
        else:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "complemento.validacion_cruzada",
                "la validacion cruzada esta desactivada: los metodos no se "
                "pueden comparar entre si y la eleccion quedaria sin sustento.",
            ))

        adoptado = configuracion.obtener("complemento.metodo_adoptado")
        if adoptado:
            completada = rellenar(datos, adoptado, configuracion,
                                  admisibles)
            nuevos = int(np.sum(np.isfinite(completada) & ~np.isfinite(datos)))
            resultado.meses_completados = nuevos
            proporcion = 100.0 * nuevos / max(
                1, int(np.sum(np.isfinite(completada))))
            maximo = float(configuracion.obtener(
                "complemento.max_porcentaje_sintetico"))
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA if proporcion > maximo else INFORMATIVO,
                "complemento.sintetico",
                f"el metodo {adoptado!r} completo {nuevos:,} mes(es), el "
                f"{proporcion:.1f}% de la serie resultante"
                + (f", por encima del {maximo:.0f}% admitido."
                   if proporcion > maximo else "."),
            ))
        else:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, "complemento.metodo_adoptado",
                "sin metodo adoptado: la serie se entrega con sus huecos. El "
                "consultor debe declarar complemento.metodo_adoptado en "
                "config.yaml tras revisar la validacion cruzada"
                + (f" (menor error: {resultado.metodo_recomendado})."
                   if resultado.metodo_recomendado else "."),
            ))

        if (completada is not None and bool(configuracion.obtener(
                "complemento.rellenar_residual_con_climatologia"))):
            completada, por_clima = rellenar_con_climatologia(completada, claves)
            resultado.meses_climatologia = por_clima
            if por_clima:
                resultado.hallazgos.append(Hallazgo(
                    INFORMATIVO, "complemento.climatologia",
                    f"{por_clima:,} mes(es) completados con la media del mismo "
                    "mes calendario de la PROPIA estacion, tras agotar los "
                    "metodos por vecinas. No introduce informacion ajena, pero "
                    "aplana la variabilidad mas que cualquier metodo por "
                    "vecinas: el M07 y el M05b deben medir cuanto de la serie "
                    "llega por esta via.",
                ))

        aisladas = [d["codigo"] for d in resultado.descartes]
        resultado.estado = estado_por_estacion(
            orden, claves, datos, completada, aisladas)
        maximo_huecos = float(
            configuracion.obtener("complemento.max_huecos_residual_pct"))
        incompletas = [f for f in resultado.estado
                       if f["pct_huecos"] > maximo_huecos]
        if incompletas:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "complemento.huecos_residuales",
                f"{len(incompletas)} estacion(es) conservan mas del "
                f"{maximo_huecos:.0f}% de huecos tras el complemento: "
                + ", ".join(f"{f['codigo']} ({f['pct_huecos']:.0f}%)"
                            for f in sorted(incompletas,
                                            key=lambda x: -x["pct_huecos"])[:8])
                + (", ..." if len(incompletas) > 8 else "")
                + ". SIGUEN en la serie que consumen el M05b y el M06. Una media "
                "mensual multianual calculada sobre menos meses no es comparable "
                "con las demas, y la interpolacion las trata igual salvo que se "
                "declare lo contrario: el modulo siguiente debe decidir si las "
                "usa, con que peso, o si las excluye.",
            ))

    # --- Productos -----------------------------------------------------------
    with registro.bloque(logger, "Escritura de productos"):
        _escribir_productos(configuracion, base, resultado, series, orden,
                            claves, datos, completada, delimitador, logger)

    if con_graficas:
        with registro.bloque(logger, "Graficas"):
            _figuras(configuracion, base, resultado, series, orden, claves,
                     datos, logger, completada, ubicaciones)

    resultado.hallazgos.extend(_resumir(resultado, configuracion))
    codigo_salida = (SALIDA_BLOQUEANTE
                     if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
                     else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo_salida)


# =============================================================================
# Productos
# =============================================================================
def _escribir_csv(destino: Path, filas, delimitador: str) -> None:
    """Vuelca una tabla de diccionarios homogeneos."""
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


def _matriz_a_filas(orden, claves, datos, origenes=None):
    """Convierte la matriz en filas anio, mes, estacion por estacion."""
    for indice, (anio, mes) in enumerate(claves):
        fila = {"anio": anio, "mes": mes}
        for columna, codigo in enumerate(orden):
            valor = datos[indice, columna]
            fila[codigo] = "" if not np.isfinite(valor) else round(float(valor), 2)
        yield fila


def _aplanar_consistencia(filas):
    """Aplana el anidamiento de pruebas para que quepa en una tabla."""
    for fila in filas:
        plana = {
            "codigo": fila["codigo"],
            "anios_completos": fila["anios_completos"],
            "n_vecinas": fila["n_vecinas"],
            "mejor_correlacion": fila["mejor_correlacion"],
            "dist_media_km": fila.get("dist_media_km"),
            "vecinas": ";".join(fila.get("vecinas") or ()),
        }
        doble = fila.get("doble_masa") or {}
        plana["r_patron"] = fila.get("r_patron")
        plana["dm_r2_recta"] = doble.get("r2_recta")
        plana["dm_quiebre"] = doble.get("hay_quiebre")
        plana["dm_razon_pend"] = doble.get("razon_pendientes")
        plana["dm_indice"] = doble.get("indice")
        pruebas = fila.get("pruebas") or {}
        for nombre in ("pettitt", "snht", "mann_kendall", "rachas"):
            datos = pruebas.get(nombre) or {}
            plana[f"{nombre}_p"] = datos.get("valor_p")
            plana[f"{nombre}_indicio"] = datos.get("hay_indicio")
            if "anio_quiebre" in datos:
                plana[f"{nombre}_anio"] = datos["anio_quiebre"]
            if nombre == "mann_kendall" and "pendiente_sen" in datos:
                plana["sen_mm_anio"] = round(float(datos["pendiente_sen"]), 3)
        yield plana


def _escribir_productos(configuracion, base, resultado, series, orden, claves,
                        datos, completada, delimitador, logger) -> None:
    """Escribe series, tablas de diagnostico e informe."""
    directorio_series = rutas.directorio("procesado_series", base, crear=True)
    directorio_est = rutas.directorio("procesado_estaciones", base, crear=True)

    destino = directorio_series / "precipitacion_mensual.csv"
    _escribir_csv(destino, _matriz_a_filas(orden, claves, datos), delimitador)
    resultado.productos.append(rutas.relativa(destino, base))

    if completada is not None:
        destino = directorio_series / "precipitacion_mensual_complementada.csv"
        _escribir_csv(destino, _matriz_a_filas(orden, claves, completada),
                      delimitador)
        resultado.productos.append(rutas.relativa(destino, base))

    # Procedencia de cada valor: el M15 debe poder declarar que proporcion de la
    # serie es observada y cual es sintetica.
    destino = directorio_series / "precipitacion_mensual_origen.csv"
    filas = []
    for indice, (anio, mes) in enumerate(claves):
        fila = {"anio": anio, "mes": mes}
        for codigo in orden:
            fila[codigo] = series[codigo].origen.get((anio, mes), "")
        filas.append(fila)
    _escribir_csv(destino, filas, delimitador)
    resultado.productos.append(rutas.relativa(destino, base))

    # Matriz de marcas, en paralelo a la serie. Sin ella la marca vive solo en
    # M05_anomalos.csv y cualquier modulo que lea la serie no sabe que valores
    # estan senalados: el dato anomalo entraria al analisis indistinguible del
    # resto, que es justo lo que marcar pretende evitar.
    marcados = {(int(a["anio"]), int(a["mes"]), a["codigo"]) for a in resultado.anomalos}
    destino = directorio_series / "precipitacion_mensual_anomalos.csv"
    filas = []
    for anio, mes in claves:
        fila = {"anio": anio, "mes": mes}
        for codigo in orden:
            fila[codigo] = 1 if (anio, mes, codigo) in marcados else 0
        filas.append(fila)
    _escribir_csv(destino, filas, delimitador)
    resultado.productos.append(rutas.relativa(destino, base))

    for nombre, contenido in (
        ("M05_estado_estaciones.csv", resultado.estado),
        ("M05_correcciones.csv", resultado.correcciones),
        ("M05_consistencia.csv", list(_aplanar_consistencia(resultado.consistencia))),
        ("M05_complemento.csv", resultado.complemento),
        ("M05_anomalos.csv", resultado.anomalos),
        ("M05_discrepancias.csv", resultado.discrepancias),
    ):
        destino = directorio_est / nombre
        _escribir_csv(destino, contenido, delimitador)
        resultado.productos.append(rutas.relativa(destino, base))

    informe = directorio_est / "M05_precipitacion.md"
    _escribir_informe(informe, resultado, configuracion, orden, claves, datos)
    resultado.productos.append(rutas.relativa(informe, base))
    logger.info("Serie de %d periodo(s) x %d estacion(es)", len(claves), len(orden))


def _tabla_markdown(filas, columnas) -> list[str]:
    lineas = ["| " + " | ".join(columnas) + " |",
              "|" + "|".join("---" for _ in columnas) + "|"]
    for fila in filas:
        lineas.append("| " + " | ".join(str(fila.get(c, "")) for c in columnas) + " |")
    return lineas


def _escribir_informe(destino, resultado, configuracion, orden, claves,
                      datos) -> None:
    """Informe en Markdown, en la linea de las rutinas heredadas."""
    huecos = int(np.sum(~np.isfinite(datos)))
    lineas = [
        "# M05 - Precipitacion mensual",
        "",
        f"* Estaciones evaluadas: {resultado.estaciones_evaluadas}",
        f"* Estaciones con serie: {resultado.estaciones_admitidas}",
        f"* Periodos: {len(claves)} ({claves[0][0]}-{claves[0][1]} a "
        f"{claves[-1][0]}-{claves[-1][1]})" if claves else "* Periodos: 0",
        f"* Meses de la serie mensual del IDEAM: {resultado.meses_mensual:,}",
        f"* Meses completados con la agregacion diaria: {resultado.meses_agregados:,}",
        f"* Huecos en la matriz: {huecos:,} de {datos.size:,} "
        f"({100.0 * huecos / max(1, datos.size):.1f}%)",
        "",
        "## Etapa 0. Construccion de la serie",
        "",
        "La serie mensual del IDEAM es la fuente primaria y la agregacion de la",
        "diaria la secundaria. Totalizar la diaria exige un umbral de",
        "completitud: sumar los dias presentes sin ese control subestima los",
        "meses incompletos.",
        "",
        f"Discrepancias entre ambas fuentes por encima del 5%: "
        f"**{len(resultado.discrepancias)}**. Se reportan en",
        "`M05_discrepancias.csv` y no se corrigen: una diferencia no dice cual",
        "de las dos fuentes esta mal.",
        "",
        "## Etapa 1. Datos anomalos",
        "",
        f"Metodo: **{configuracion.obtener('anomalos.metodo')}**. "
        f"Tratamiento: **{configuracion.obtener('anomalos.tratamiento')}**.",
        "",
        f"Valores marcados: **{len(resultado.anomalos)}**, en",
        "`M05_anomalos.csv`.",
        "",
        "La deteccion se hace POR MES CALENDARIO y no sobre la serie entera. En",
        "un regimen bimodal, abril y julio tienen distribuciones distintas, y un",
        "solo rango para los doce meses marcaria toda la temporada humeda.",
        "",
        "## Etapa 2. Consistencia",
        "",
        "Pruebas sobre la serie ANUAL, no la mensual: las pruebas de",
        "homogeneidad suponen una muestra sin estacionalidad, y sobre datos",
        "mensuales detectarian el ciclo anual como si fuera un quiebre.",
        "",
    ]

    con_quiebre = [f for f in resultado.consistencia
                   if (f.get("doble_masa") or {}).get("hay_quiebre")]
    correlaciones = resultado.correlaciones or {}
    if correlaciones.get("percentiles"):
        lineas += [
            "Correlacion medida entre parejas de estaciones:",
            "",
            "| percentil | " + " | ".join(correlaciones["percentiles"]) + " |",
            "|---|" + "|".join("---" for _ in correlaciones["percentiles"]) + "|",
            "| r | " + " | ".join(
                str(v) for v in correlaciones["percentiles"].values()) + " |",
            "",
            "Estaciones que quedarian sin ninguna vecina segun el umbral:",
            "",
        ]
        lineas += _tabla_markdown(
            [{"umbral": k, "aisladas": v}
             for k, v in correlaciones.get("aislada_por_umbral",
                                           correlaciones.get(
                                               "aisladas_por_umbral", {})).items()],
            ["umbral", "aisladas"])
        lineas += [
            "",
            "El umbral de `consistencia.correlacion_minima` decide QUE VECINAS",
            "sirven para complementar, no si una estacion es valida. Una",
            "estacion sin vecina correlacionada se conserva.",
            "",
        ]
    lineas += [
        f"* Estaciones con quiebre de doble masa: **{len(con_quiebre)}**",
    ]
    for nombre in ("pettitt", "snht", "mann_kendall", "rachas"):
        cuantas = sum(1 for f in resultado.consistencia
                      if ((f.get("pruebas") or {}).get(nombre) or {}).get("hay_indicio"))
        lineas.append(f"* Estaciones con indicio en {nombre}: **{cuantas}**")
    lineas += [
        "",
        "El detalle esta en `M05_consistencia.csv`. Ninguna correccion se aplica",
        "de forma automatica: un quiebre indica posible traslado o cambio de",
        "instrumento, y la decision de homogeneizar es del consultor.",
        "",
        "## Etapa 3. Complemento",
        "",
        "Validacion cruzada: se enmascara dato observado, se reconstruye y se",
        "mide el error. Sin ella ningun metodo puede compararse con otro, porque",
        "rellenar siempre produce un numero.",
        "",
    ]
    validos = [c for c in resultado.complemento if "rmse" in c]
    if validos:
        lineas += _tabla_markdown(
            sorted(validos, key=lambda c: c["rmse"]),
            ["metodo", "n_validacion", "rmse", "mae", "sesgo",
             "nash_sutcliffe", "razon_desviacion", "r_validacion"])
        lineas += [
            "",
            f"Menor error: **{resultado.metodo_recomendado}**. El modulo no lo",
            "adopta: el consultor declara `complemento.metodo_adoptado` en",
            "config.yaml y la decision se registra en `MANIFIESTO.yaml`.",
            "",
        ]
    fallidos = [c for c in resultado.complemento if "error" in c]
    if fallidos:
        lineas += ["Metodos que no pudieron evaluarse:", ""]
        lineas += [f"* `{c['metodo']}`: {c['error']}" for c in fallidos]
        lineas.append("")

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")


# =============================================================================
# Graficas
# =============================================================================
def _figuras(configuracion, base, resultado, series, orden, claves, datos,
             logger, completada=None, ubicaciones=None) -> None:
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
    nombres = dict(configuracion.obtener("graficos.nombres_variable") or {})

    # --- Ciclo anual medio ---------------------------------------------------
    with graficos.figura(
        estilo, titulo="Ciclo anual medio de precipitación por estación",
        etiqueta_x="mes", etiqueta_y="precipitación media (mm)",
    ) as (fig, ax):
        for columna, codigo in enumerate(orden):
            medias = []
            for mes in range(1, 13):
                valores = [datos[i, columna] for i, c in enumerate(claves)
                           if c[1] == mes and np.isfinite(datos[i, columna])]
                medias.append(float(np.mean(valores)) if valores else np.nan)
            ax.plot(range(1, 13), medias, color=estilo.color(0), alpha=0.30,
                    linewidth=0.9)
        promedio = [float(np.nanmean([
            np.nanmean([datos[i, c] for i, k in enumerate(claves) if k[1] == mes])
            for c in range(len(orden))])) for mes in range(1, 13)]
        ax.plot(range(1, 13), promedio, color="#c00000", linewidth=2.2,
                label="promedio de las estaciones")
        ax.set_xticks(range(1, 13))
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05_ciclo_anual", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

    # --- Comparacion de metodos de complemento -------------------------------
    validos = [c for c in resultado.complemento if "rmse" in c]
    if validos:
        validos = sorted(validos, key=lambda c: c["rmse"])
        with graficos.figura(
            estilo, titulo="Validación cruzada de los métodos de complemento",
            etiqueta_y="error (mm)",
        ) as (fig, ax):
            posiciones = np.arange(len(validos))
            ax.bar(posiciones - 0.2, [c["rmse"] for c in validos], width=0.4,
                   color=estilo.color(0), label="RMSE")
            ax.bar(posiciones + 0.2, [c["mae"] for c in validos], width=0.4,
                   color=estilo.color(1), label="MAE")
            ax.set_xticks(posiciones)
            ax.set_xticklabels([c["metodo"] for c in validos],
                               fontsize=estilo.tamano_fuente - 1)
            ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)
            fig.tight_layout()
            for ruta in graficos.guardar(
                    fig, directorio / "M05_complemento", estilo):
                resultado.productos.append(rutas.relativa(ruta, base))

    minima = float(configuracion.obtener("consistencia.correlacion_minima"))
    _figura_doble_masa(graficos, estilo, directorio, resultado,
                       {c: dict(s.valores) for c, s in series.items()},
                       claves, base)
    _figura_correlaciones(graficos, estilo, directorio, orden, datos,
                          resultado, base, minima)
    _figura_faltantes(graficos, estilo, directorio, orden, claves, datos,
                      completada, resultado, base)
    _figura_anomalos(graficos, estilo, directorio, orden, claves, datos,
                     resultado, base)
    _figura_estaciones(graficos, estilo, directorio, resultado, base,
                       configuracion, ubicaciones or {})
    _figuras_por_estacion(graficos, estilo, configuracion, base, resultado,
                          orden, claves, datos, completada, logger)
    logger.info("Figuras escritas en %s", rutas.relativa(directorio, base))


def _figura_doble_masa(graficos, estilo, directorio, resultado, matriz, claves,
                       base) -> None:
    """
    Una curva de doble masa por estación, en rejilla.

    Es la figura que sustenta el descarte y la corrección, y hasta ahora la
    decisión viajaba solo como número. Un quiebre se discute mirando la curva:
    la tabla dice que existe, la figura dice si es un codo o una inflexión
    gradual, y esa diferencia cambia si conviene corregir o descartar.
    """
    con_curva = [f for f in resultado.consistencia
                 if f.get("acumulados") and len(f["acumulados"][0]) > 3]
    if not con_curva:
        return
    columnas = 4
    filas = (len(con_curva) + columnas - 1) // columnas
    with graficos.figura(
        estilo, titulo="Curvas de doble masa contra el patrón de vecinas",
        filas=filas, columnas=columnas,
        alto_cm=max(estilo.alto_cm, 4.2 * filas),
    ) as (fig, ejes):
        for indice, fila in enumerate(con_curva):
            ax = ejes[indice // columnas][indice % columnas]
            patron, estacion = fila["acumulados"]
            doble = fila.get("doble_masa") or {}
            graficos.curva_doble_masa(
                ax, patron, estacion, estilo,
                indice_quiebre=doble.get("indice") if doble.get("hay_quiebre") else None,
                razon=doble.get("razon_pendientes") if doble.get("hay_quiebre") else None,
            )
            ax.set_title(f"{fila['codigo']}  r={fila.get('r_patron')}",
                         fontsize=estilo.tamano_fuente - 1, loc="left",
                         color="#333333")
            ax.tick_params(labelsize=estilo.tamano_fuente - 3)
            for lado in ("top", "right"):
                ax.spines[lado].set_visible(False)
        for sobrante in range(len(con_curva), filas * columnas):
            ejes[sobrante // columnas][sobrante % columnas].axis("off")
        fig.supxlabel("acumulado del patrón (mm)",
                      fontsize=estilo.tamano_fuente)
        fig.supylabel("acumulado de la estación (mm)",
                      fontsize=estilo.tamano_fuente)
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05_doble_masa", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))


def _figura_correlaciones(graficos, estilo, directorio, orden, datos,
                          resultado, base, minima) -> None:
    """
    Mapa de calor de las correlaciones entre estaciones.

    Conserva la figura de la rutina heredada EDA.py. Su utilidad no es leer un
    valor concreto sino ver la estructura: bloques de estaciones que se parecen
    y filas oscuras que delatan a la que no se parece a nadie.
    """
    n = len(orden)
    if n < 2:
        return
    matriz = np.ones((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            valor, _ = est.correlacion_pareada(datos[:, i], datos[:, j])
            matriz[i, j] = matriz[j, i] = valor if np.isfinite(valor) else np.nan
    with graficos.figura(
        estilo, titulo=f"Correlacion entre estaciones (umbral adoptado {minima})",
        alto_cm=max(estilo.alto_cm, 0.35 * n + 5.0),
    ) as (fig, ax):
        graficos.mapa_calor(ax, matriz, orden, estilo, minimo=0.0, maximo=1.0)
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05_correlaciones",
                                     estilo):
            resultado.productos.append(rutas.relativa(ruta, base))


def _figura_faltantes(graficos, estilo, directorio, orden, claves, datos,
                      completada, resultado, base) -> None:
    """
    Diagrama de datos faltantes antes y despues del complemento.

    Es el 'missing values diagram' que producia Impute.py. Distingue de un
    vistazo la estacion con huecos dispersos de la que tiene un tramo entero sin
    registro: son problemas distintos y admiten soluciones distintas.
    """
    etiquetas_fila = [f"{a}" for a, _ in claves]
    paneles = [("observado", np.isfinite(datos))]
    if completada is not None:
        paneles.append(("tras el complemento", np.isfinite(completada)))
    with graficos.figura(
        estilo, titulo="Disponibilidad de dato por estación y periodo",
        filas=1, columnas=len(paneles),
        alto_cm=max(estilo.alto_cm, 12.0),
    ) as (fig, ejes):
        for indice, (nombre, presente) in enumerate(paneles):
            ax = ejes[0][indice]
            graficos.matriz_faltantes(
                ax, presente, orden, estilo,
                etiquetas_fila=etiquetas_fila if indice == 0 else None)
            faltan = int(np.sum(~presente))
            ax.set_title(f"{nombre}: {faltan:,} hueco(s)",
                         fontsize=estilo.tamano_fuente, loc="left",
                         color="#333333")
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05_faltantes", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))


def _figura_anomalos(graficos, estilo, directorio, orden, claves, datos,
                     resultado, base) -> None:
    """
    Cajas por mes calendario con los valores marcados encima.

    Conserva el boxplot de EDA.py y le anade lo que aquel no mostraba: que
    puntos quedaron senalados. Ver la caja sin ellos obliga a creer el conteo;
    verlos encima permite juzgar si son error o cola natural de la distribucion,
    que es la razon por la que en este estudio NO se eliminan.
    """
    marcados = {(int(a["anio"]), int(a["mes"]), a["codigo"])
                for a in resultado.anomalos}
    grupos: dict[str, list[float]] = {}
    senalados: dict[str, list[float]] = {}
    for mes in range(1, 13):
        nombre = f"{mes:02d}"
        grupos[nombre] = []
        senalados[nombre] = []
        for indice, (anio, mes_clave) in enumerate(claves):
            if mes_clave != mes:
                continue
            for columna, codigo in enumerate(orden):
                valor = datos[indice, columna]
                if not np.isfinite(valor):
                    continue
                grupos[nombre].append(float(valor))
                if (anio, mes, codigo) in marcados:
                    senalados[nombre].append(float(valor))
    if not any(grupos.values()):
        return
    with graficos.figura(
        estilo,
        titulo="Precipitación mensual por mes calendario, con los valores marcados",
        etiqueta_x="mes", etiqueta_y="precipitación (mm)",
    ) as (fig, ax):
        graficos.cajas_por_grupo(ax, grupos, estilo, marcados=senalados)
        graficos.leyenda_manual(ax, [
            ("señalado por el método de anómalos", "#c00000"),
        ], estilo)
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05_anomalos", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))


def _figura_estaciones(graficos, estilo, directorio, resultado, base,
                       configuracion, ubicaciones) -> None:
    """
    Ubicacion de las estaciones conservadas, corregidas y eliminadas.

    Cierra el ciclo del descarte: la tabla dice cuantas se fueron y esta figura
    dice DONDE. En este estudio muestra que las eliminadas son las de cota baja,
    de modo que la red conservada no cubre la franja inferior del area.
    """
    if not ubicaciones:
        return
    crs = (configuracion.obtener("graficos.crs_figuras")
           or configuracion.obtener("punto_descarga.crs"))
    a_plano_area = graficos.transformador("EPSG:4326", crs)
    conversor = graficos.transformador(
        configuracion.obtener("crs.calculo"), crs)

    poligonos: list = []
    reporte = rutas.directorio("procesado", base) / "M02_delimitacion.json"
    if reporte.is_file():
        try:
            datos_m02 = json.loads(reporte.read_text(encoding="utf-8"))
            wkt = (datos_m02.get("geometrias_epsg4326") or {}).get("area_estaciones")
            if wkt:
                from comun import geometria
                poligonos = [[[a_plano_area(float(x), float(y)) for x, y in anillo]
                              for anillo in poli]
                             for poli in geometria.poligonos_de_wkt(wkt)]
        except (OSError, json.JSONDecodeError, ErrorFormato):
            poligonos = []

    eliminadas = {d["codigo"] for d in resultado.descartes if d["descartada"]}
    corregidas = {c["codigo"] for c in resultado.correcciones}
    conservadas = {f["codigo"] for f in resultado.estado}

    grupos: dict[str, tuple[list[float], list[float]]] = {}
    for nombre, codigos in (
        ("conservadas", conservadas - corregidas),
        ("corregidas por doble masa", corregidas),
        ("eliminadas por consistencia", eliminadas),
    ):
        equis, yes = [], []
        for codigo in sorted(codigos):
            sitio = ubicaciones.get(codigo)
            if sitio is None:
                continue
            este, norte = conversor(sitio["x"], sitio["y"])
            equis.append(este)
            yes.append(norte)
        if equis:
            grupos[f"{nombre} ({len(equis)})"] = (equis, yes)
    if not grupos:
        return

    with graficos.figura(
        estilo, titulo="Estaciones de precipitación tras el análisis del M05",
        etiqueta_x="Este (m)", etiqueta_y="Norte (m)",
        alto_cm=max(estilo.alto_cm, 12.0),
    ) as (fig, ax):
        graficos.dispersion_sobre_area(ax, poligonos, grupos, estilo,
                                       tamanos={k: 26.0 for k in grupos})
        graficos.rotular_en_miles(ax)
        ax.annotate(f"Coordenadas {crs}", xy=(1, -0.09),
                    xycoords="axes fraction", ha="right",
                    fontsize=estilo.tamano_fuente - 2, color="#555555")
        fig.tight_layout()
        for ruta in graficos.guardar(fig, directorio / "M05_estaciones", estilo):
            resultado.productos.append(rutas.relativa(ruta, base))

def _figuras_por_estacion(graficos, estilo, configuracion, base, resultado,
                          orden, claves, datos, completada, logger) -> None:
    """
    Una figura por estacion, agrupada por tema.

    Una rejilla de treinta y dos paneles sirve para revisar de un vistazo, pero
    no se puede insertar en el informe: cada panel queda del tamano de una
    estampilla. Las rutinas heredadas escribian una figura por estacion, y se
    recupera ese criterio.
    """
    if not bool(configuracion.obtener("graficos.figuras_individuales")):
        return
    raiz = rutas.resolver(
        configuracion.obtener("graficos.directorio_individuales"), base)
    individual = graficos.estilo_individual(
        estilo,
        float(configuracion.obtener("graficos.ancho_individual_cm")),
        float(configuracion.obtener("graficos.alto_individual_cm")))

    escritas = 0

    # --- Curva de doble masa -------------------------------------------------
    carpeta = graficos.directorio_tema(raiz, "doble_masa")
    for fila in resultado.consistencia:
        acumulados = fila.get("acumulados")
        if not acumulados or len(acumulados[0]) < 4:
            continue
        patron, estacion = acumulados
        doble = fila.get("doble_masa") or {}
        with graficos.figura(
            individual,
            titulo=f"Doble masa  {fila['codigo']}",
            etiqueta_x="acumulado del patrón de vecinas (mm)",
            etiqueta_y="acumulado de la estación (mm)",
        ) as (fig, ax):
            graficos.curva_doble_masa(
                ax, patron, estacion, individual,
                indice_quiebre=(doble.get("indice")
                                if doble.get("hay_quiebre") else None),
                razon=(doble.get("razon_pendientes")
                       if doble.get("hay_quiebre") else None))
            pie = (f"r contra el patrón = {fila.get('r_patron')}"
                   f"   |   {fila.get('n_vecinas', 0)} vecina(s)")
            if doble.get("hay_quiebre"):
                pie += "   |   quiebre, factor " + str(doble.get("razon_pendientes"))
            ax.annotate(pie, xy=(0, -0.16), xycoords="axes fraction",
                        fontsize=individual.tamano_fuente - 2, color="#555555")
            fig.tight_layout()
            graficos.guardar(fig, carpeta / str(fila["codigo"]), individual)
            escritas += 1

    # --- Serie mensual, observada y complementada ---------------------------
    carpeta = graficos.directorio_tema(raiz, "serie_mensual")
    tiempo = [a + (m - 0.5) / 12.0 for a, m in claves]
    for columna, codigo in enumerate(orden):
        observado = datos[:, columna]
        with graficos.figura(
            individual,
            titulo=f"Precipitación mensual  {codigo}",
            etiqueta_x="año", etiqueta_y="precipitación (mm)",
        ) as (fig, ax):
            if completada is not None:
                relleno = np.where(np.isfinite(observado), np.nan,
                                   completada[:, columna])
                ax.plot(tiempo, relleno, linestyle="none", marker="o",
                        markersize=2.2, color="#c00000", zorder=3,
                        label="complementado")
            ax.plot(tiempo, observado, color=individual.color(0),
                    linewidth=0.8, zorder=2, label="observado")
            ax.legend(fontsize=individual.tamano_fuente - 2, frameon=False)
            fig.tight_layout()
            graficos.guardar(fig, carpeta / str(codigo), individual)
            escritas += 1

    # --- Ciclo anual medio ---------------------------------------------------
    carpeta = graficos.directorio_tema(raiz, "ciclo_anual")
    for columna, codigo in enumerate(orden):
        medias, desviaciones = [], []
        for mes in range(1, 13):
            muestras = [datos[i, columna] for i, c in enumerate(claves)
                        if c[1] == mes and np.isfinite(datos[i, columna])]
            medias.append(float(np.mean(muestras)) if muestras else np.nan)
            desviaciones.append(float(np.std(muestras, ddof=1))
                                if len(muestras) > 1 else 0.0)
        with graficos.figura(
            individual,
            titulo=f"Ciclo anual medio  {codigo}",
            etiqueta_x="mes", etiqueta_y="precipitación media (mm)",
        ) as (fig, ax):
            meses = list(range(1, 13))
            ax.bar(meses, medias, color=individual.color(0), alpha=0.75,
                   yerr=desviaciones, capsize=2.5,
                   error_kw={"linewidth": 0.7, "ecolor": "#555555"})
            ax.set_xticks(meses)
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
def _resumir(resultado, configuracion) -> list[Hallazgo]:
    """Informativos de sintesis."""
    hallazgos = [Hallazgo(
        INFORMATIVO, "m05.serie",
        f"{resultado.estaciones_admitidas} estacion(es) con serie mensual; "
        f"{resultado.meses_mensual:,} mes(es) de la fuente primaria y "
        f"{resultado.meses_agregados:,} de la agregacion diaria.",
    )]
    if resultado.discrepancias:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "fuentes.discrepancia",
            f"{len(resultado.discrepancias):,} mes(es) en que la serie mensual "
            "del IDEAM y la agregacion de la diaria difieren mas del 5%. No se "
            "corrigen: la diferencia no dice cual de las dos esta mal.",
        ))
    if resultado.anomalos:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "anomalos.marcados",
            f"{len(resultado.anomalos):,} valor(es) marcados como anomalos, "
            "comparando cada mes con los mismos meses de otros anios. Se marcan "
            "y se conservan.",
        ))
    return hallazgos


def _cerrar(logger, resultado, base, ruta_json, inicio, codigo):
    """Emite el reporte, escribe el JSON y cierra el log."""
    orden_sev = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}
    hallazgos = sorted(resultado.hallazgos,
                       key=lambda h: (orden_sev.get(h.severidad, 9), h.clave))

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
            "M05_precipitacion.json"
    reporte = {
        "modulo": MODULO,
        "estaciones_evaluadas": resultado.estaciones_evaluadas,
        "estaciones_admitidas": resultado.estaciones_admitidas,
        "meses_mensual": resultado.meses_mensual,
        "meses_agregados": resultado.meses_agregados,
        "meses_completados": resultado.meses_completados,
        "discrepancias": len(resultado.discrepancias),
        "anomalos": len(resultado.anomalos),
        "complemento": resultado.complemento,
        "metodo_recomendado": resultado.metodo_recomendado,
        "correlaciones": resultado.correlaciones,
        "estado_estaciones": resultado.estado,
        "registros_excluidos": resultado.registros_excluidos,
        "correcciones": resultado.correcciones,
        "meses_climatologia": resultado.meses_climatologia,
        "descartes": resultado.descartes,
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
        prog="M05_precipitacion_mensual.py",
        description="Precipitacion mensual: anomalos, consistencia y complemento.",
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

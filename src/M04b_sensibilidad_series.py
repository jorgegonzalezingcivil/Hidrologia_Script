#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M04b - Análisis de sensibilidad de longitud de series
=====================================================
Entorno: venv del proyecto.

CLAUDE.md, sección 6, cierra la decisión así: "Longitud de serie | Análisis de
sensibilidad por umbral y ventana; el consultor decide". Este módulo produce esa
evidencia y NO decide nada. Emite la matriz de cuántas estaciones sobreviven a
cada pareja (umbral de años, ventana temporal) y deja constancia de lo que cada
elección cuesta en cobertura.

Por qué la longitud no es la resta de fechas extremas. Una estación con registro
en 1970 y en 2020 y nada en medio tiene cincuenta años de amplitud y dos años de
dato. El módulo distingue tres medidas y las reporta por separado:

    amplitud        último año menos primero, más uno
    años con dato   años que tienen al menos un registro
    años útiles     años que alcanzan el umbral de completitud declarado

La tercera es la que alimenta la matriz. Las dos primeras se conservan porque su
diferencia con la tercera es justamente el diagnóstico: una amplitud grande con
pocos años útiles señala una serie con huecos, que el M05 tendrá que complementar
o descartar.

Se reporta además la racha máxima de años útiles consecutivos. Treinta años
útiles repartidos en dos bloques separados por veinte de silencio no equivalen a
treinta continuos, y el análisis de frecuencia del M07 no es indiferente a esa
diferencia.

Nada se elimina aquí. El módulo marca, cuenta y reporta; el descarte efectivo lo
aplica el M05 una vez el consultor fija 'sensibilidad_series.umbral_adoptado_anios'
y 'sensibilidad_series.ventana_adoptada' en config.yaml.

Sin librerías de terceros. La serie consolidada del M04 pesa cientos de MB y se
recorre en flujo, acumulando solo el conteo de registros por (estación, etiqueta,
año). Cargarla entera en memoria no aportaría nada y multiplicaría el consumo.

Productos:
    data/02_procesado/estaciones/sensibilidad_series.csv
    data/02_procesado/estaciones/matriz_sensibilidad.csv
    data/02_procesado/estaciones/M04b_sensibilidad.md
    data/02_procesado/M04b_sensibilidad.json

Uso:
    python src/M04b_sensibilidad_series.py

Códigos de salida:
    0  matriz producida
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas, shapefile  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M04b"
DESCRIPCION = "Análisis de sensibilidad de longitud de series"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Registros que cabe esperar en un año completo, por frecuencia declarada por el
# IDEAM. El valor diario depende del año, de modo que se resuelve por función.
REGISTROS_MENSUALES = 12

# Estados de la estación frente a la antigüedad de la suspensión.
VIGENTE = "vigente"
SUSPENDIDA_RECIENTE = "suspendida reciente"
SUSPENDIDA_ANTIGUA = "suspendida antigua"


# =============================================================================
# Estructuras
# =============================================================================
@dataclass
class SerieEstacion:
    """Conteo de registros por año de una serie de una estación."""

    codigo: str
    etiqueta: str
    frecuencia: str
    por_anio: dict[int, int] = field(default_factory=dict)

    def sumar(self, anio: int) -> None:
        self.por_anio[anio] = self.por_anio.get(anio, 0) + 1

    @property
    def registros(self) -> int:
        return sum(self.por_anio.values())


@dataclass
class ResultadoM04b:
    registros_leidos: int = 0
    series_analizadas: int = 0
    estaciones: int = 0
    estaciones_del_m03: int = 0
    filas: list[dict[str, Any]] = field(default_factory=list)
    matriz: list[dict[str, Any]] = field(default_factory=list)
    por_estado: dict[str, int] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def registros_esperados(frecuencia: str, anio: int) -> int | None:
    """
    Registros de un año completo según la frecuencia declarada.

    Devuelve None cuando la frecuencia no se reconoce, y en ese caso la
    completitud no se puede juzgar: el módulo lo reporta en lugar de inventar
    un denominador.
    """
    normalizada = (frecuencia or "").strip().lower()
    if normalizada.startswith("diaria"):
        return 366 if calendar.isleap(anio) else 365
    if normalizada.startswith("mensual"):
        return REGISTROS_MENSUALES
    if normalizada.startswith("anual"):
        return 1
    return None


def completitud_anual(registros: int, esperados: int | None) -> float | None:
    """Proporción del año cubierta. None si la frecuencia no se reconoce."""
    if not esperados:
        return None
    return min(1.0, registros / esperados)


def anios_utiles(
    por_anio: dict[int, int],
    frecuencia: str,
    minimo: float,
    ventana: tuple[int, int] | None = None,
) -> set[int]:
    """
    Años que alcanzan el umbral de completitud, dentro de la ventana pedida.

    Si la frecuencia no se reconoce se cuenta la presencia del año, y el aviso
    correspondiente lo emite quien llama: es preferible un conteo optimista
    señalado a la omisión silenciosa de la serie.
    """
    utiles: set[int] = set()
    for anio, registros in por_anio.items():
        if ventana is not None and not (ventana[0] <= anio <= ventana[1]):
            continue
        esperados = registros_esperados(frecuencia, anio)
        proporcion = completitud_anual(registros, esperados)
        if proporcion is None or proporcion >= minimo:
            utiles.add(anio)
    return utiles


def racha_maxima(anios: Iterable[int]) -> int:
    """Mayor cantidad de años consecutivos dentro del conjunto."""
    ordenados = sorted(set(anios))
    if not ordenados:
        return 0
    mejor = actual = 1
    for previo, siguiente in zip(ordenados, ordenados[1:]):
        actual = actual + 1 if siguiente == previo + 1 else 1
        mejor = max(mejor, actual)
    return mejor


def resolver_ventana(ventana: Sequence[Any], anio_estudio: int) -> tuple[int, int]:
    """
    Convierte una ventana declarada en una pareja de años concreta.

    El nulo en cualquiera de los extremos significa 'sin límite por ese lado',
    y el extremo final se resuelve al año del estudio.
    """
    inicio, fin = ventana[0], ventana[1]
    return (
        int(inicio) if inicio is not None else 1900,
        int(fin) if fin is not None else int(anio_estudio),
    )


def etiqueta_de_ventana(ventana: Sequence[Any], anio_estudio: int) -> str:
    """Nombre legible de la ventana, para encabezar la matriz."""
    inicio, fin = resolver_ventana(ventana, anio_estudio)
    return f"{inicio}-{fin}"


def mapa_etiqueta_variable(series_por_variable: dict) -> dict[str, str]:
    """Invierte la declaración de config para saber a qué variable sirve cada
    etiqueta."""
    mapa: dict[str, str] = {}
    for variable, definiciones in (series_por_variable or {}).items():
        for definicion in definiciones or ():
            etiqueta = (definicion or {}).get("etiqueta")
            if etiqueta:
                mapa[str(etiqueta)] = str(variable)
    return mapa


def anio_de_fecha(fecha: str) -> int | None:
    """Año de una fecha ISO ya normalizada por el M04."""
    texto = (fecha or "").strip()
    if len(texto) < 4 or not texto[:4].isdigit():
        return None
    return int(texto[:4])


def estado_por_suspension(
    fecha_suspension: str, anio_estudio: int, maximo: int,
) -> str:
    """
    Clasifica la estación por la antigüedad de su suspensión.

    El M03 conserva las suspendidas de forma deliberada. Aquí se separan las que
    llevan demasiado tiempo fuera de servicio, que es donde CLAUDE.md sitúa el
    criterio, pero tampoco se eliminan: la matriz las cuenta aparte.
    """
    texto = (fecha_suspension or "").strip()
    if not texto:
        return VIGENTE
    anio = anio_de_fecha(texto)
    if anio is None:
        return VIGENTE
    return (SUSPENDIDA_ANTIGUA if (int(anio_estudio) - anio) > maximo
            else SUSPENDIDA_RECIENTE)


def acumular_series(
    filas: Iterator[dict[str, str]],
) -> tuple[dict[tuple[str, str], SerieEstacion], int, int]:
    """
    Recorre la serie consolidada y acumula registros por estación, etiqueta y año.

    Devuelve el acumulado, los registros leídos y los de fecha ilegible. No
    conserva ningún valor: la sensibilidad de longitud es un problema de
    presencia, no de magnitud.
    """
    acumulado: dict[tuple[str, str], SerieEstacion] = {}
    leidos = ilegibles = 0
    for fila in filas:
        leidos += 1
        anio = anio_de_fecha(fila.get("fecha", ""))
        if anio is None:
            ilegibles += 1
            continue
        clave = (fila.get("codigo", ""), fila.get("etiqueta", ""))
        serie = acumulado.get(clave)
        if serie is None:
            serie = SerieEstacion(clave[0], clave[1], fila.get("frecuencia", ""))
            acumulado[clave] = serie
        serie.sumar(anio)
    return acumulado, leidos, ilegibles


def resumir_serie(
    serie: SerieEstacion, minimo: float, ventanas: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Medidas de longitud de una serie, globales y por ventana."""
    anios = sorted(serie.por_anio)
    utiles = anios_utiles(serie.por_anio, serie.frecuencia, minimo)
    resumen: dict[str, Any] = {
        "codigo": serie.codigo,
        "etiqueta": serie.etiqueta,
        "frecuencia": serie.frecuencia,
        "registros": serie.registros,
        "anio_min": anios[0] if anios else None,
        "anio_max": anios[-1] if anios else None,
        "amplitud": (anios[-1] - anios[0] + 1) if anios else 0,
        "anios_con_dato": len(anios),
        "anios_utiles": len(utiles),
        "racha_max": racha_maxima(utiles),
    }
    for nombre, limites in ventanas.items():
        en_ventana = anios_utiles(
            serie.por_anio, serie.frecuencia, minimo, ventana=limites,
        )
        resumen[f"utiles_{nombre}"] = len(en_ventana)
        resumen[f"racha_{nombre}"] = racha_maxima(en_ventana)
    return resumen


def construir_matriz(
    resumenes: Sequence[dict[str, Any]],
    umbrales: Sequence[int],
    ventanas: dict[str, tuple[int, int]],
    variable_de: dict[str, str],
    admitidos: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Cuenta, por variable y ventana, cuántas estaciones alcanzan cada umbral.

    Se cuentan ESTACIONES, no series: una estación con dos etiquetas de la misma
    variable que alcanzan el umbral cuenta una vez. Es la unidad que interesa,
    porque lo que se interpola en el M06 y el M11 son estaciones.
    """
    matriz: list[dict[str, Any]] = []
    variables = sorted({variable_de.get(r["etiqueta"], "sin declarar")
                        for r in resumenes})
    amplitud = {n: (f - i + 1) for n, (i, f) in ventanas.items()}
    for variable in variables:
        pertinentes = [r for r in resumenes
                       if variable_de.get(r["etiqueta"], "sin declarar") == variable
                       and (admitidos is None or r["codigo"] in admitidos)]
        for nombre in ventanas:
            for umbral in umbrales:
                estaciones = {r["codigo"] for r in pertinentes
                              if r[f"utiles_{nombre}"] >= umbral}
                continuas = {r["codigo"] for r in pertinentes
                             if r[f"racha_{nombre}"] >= umbral}
                matriz.append({
                    "variable": variable,
                    "ventana": nombre,
                    "umbral_anios": umbral,
                    "estaciones": len(estaciones),
                    "estaciones_continuas": len(continuas),
                    # Pedir mas años útiles de los que la ventana contiene es
                    # imposible por aritmética, no por escasez de dato. La
                    # celda se marca para no confundir una cosa con la otra.
                    "evaluable": umbral <= amplitud[nombre],
                })
    return matriz


# =============================================================================
# Lectura de insumos
# =============================================================================
def leer_serie_consolidada(
    ruta: Path, delimitador: str,
) -> tuple[dict[tuple[str, str], SerieEstacion], int, int]:
    """Abre la serie del M04 y delega el acumulado."""
    if not ruta.is_file():
        raise ErrorRutas(
            f"no se encuentra la serie consolidada en {ruta}. "
            "Ejecutar antes el M04 sin --solo-inventario."
        )
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        lector = csv.DictReader(manejador, delimiter=delimitador)
        faltantes = {"codigo", "etiqueta", "fecha", "frecuencia"} - set(
            lector.fieldnames or ())
        if faltantes:
            raise ErrorFormato(
                f"la serie consolidada no trae la(s) columna(s) "
                f"{sorted(faltantes)}. Cabecera leída: {lector.fieldnames}."
            )
        return acumular_series(lector)


def verificar_contra_m04(
    acumulado: dict[tuple[str, str], SerieEstacion], reporte_m04: Path,
) -> list[Hallazgo]:
    """
    Compara lo que hay en el CSV con lo que el M04 declaró haber ingerido.

    Existe por un fallo real: una corrida del M04 con --solo-inventario reporta
    la estadística completa y termina 'CORRECTO' sin reescribir el CSV, de modo
    que el archivo en disco puede quedar atrasado respecto de su propio reporte.
    Consumirlo sin comprobar es construir sobre datos que faltan, en silencio.
    """
    if not reporte_m04.is_file():
        return [Hallazgo(
            ADVERTENCIA, "m04.reporte",
            f"no se encuentra {reporte_m04.name}: no se puede comprobar que la "
            "serie consolidada corresponda a la última ingesta.",
        )]
    try:
        declarado = json.loads(reporte_m04.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [Hallazgo(ADVERTENCIA, "m04.reporte",
                         f"no se pudo leer {reporte_m04.name}: {exc}.")]

    series_declaradas = dict(declarado.get("series") or {})
    presentes: dict[str, int] = {}
    for serie in acumulado.values():
        presentes[serie.etiqueta] = presentes.get(serie.etiqueta, 0) + serie.registros

    ausentes = sorted(set(series_declaradas) - set(presentes))
    if ausentes:
        detalle = ", ".join(f"{e} ({series_declaradas[e]:,} reg.)" for e in ausentes)
        return [Hallazgo(
            BLOQUEANTE, "m04.serie_desactualizada",
            f"el M04 declara {len(series_declaradas)} serie(s) pero la serie "
            f"consolidada solo contiene {len(presentes)}. Falta(n): {detalle}. "
            "El CSV es anterior a la última ingesta. Volver a ejecutar el M04 "
            "sin --solo-inventario antes de continuar.",
        )]

    hallazgos: list[Hallazgo] = []
    total_declarado = int(declarado.get("registros_unicos") or 0)
    total_presente = sum(presentes.values())
    if total_declarado and total_declarado != total_presente:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "m04.serie_desactualizada",
            f"el M04 declara {total_declarado:,} registro(s) único(s) y la "
            f"serie consolidada trae {total_presente:,}. El CSV no corresponde "
            "a la última ingesta: volver a ejecutar el M04 sin "
            "--solo-inventario antes de continuar.",
        ))
        return hallazgos

    sobrantes = sorted(set(presentes) - set(series_declaradas))
    if sobrantes:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "m04.serie_sobrante",
            f"la serie consolidada trae etiqueta(s) que el M04 no declara: "
            f"{sobrantes}.",
        ))

    hallazgos.append(Hallazgo(
        INFORMATIVO, "m04.serie",
        f"la serie consolidada coincide con el reporte del M04: "
        f"{total_presente:,} registro(s) en {len(presentes)} serie(s).",
    ))
    return hallazgos


def leer_metadatos_estaciones(base: Path, configuracion: Config) -> dict[str, dict]:
    """
    Estado y fecha de suspensión de cada estación, desde la capa del M03.

    Se lee el shapefile y no el inventario CSV porque aquel conserva los nombres
    de campo cortos, que son estables; las cabeceras del CSV son descriptivas y
    están pensadas para el informe.
    """
    ruta = rutas.resolver(
        configuracion.obtener("estaciones.salida_seleccionadas"), base)
    if not ruta.is_file():
        return {}
    metadatos: dict[str, dict] = {}
    for fila in shapefile.leer_registros(
        ruta, ["codigo", "nombre", "categoria", "estado", "f_suspen",
               "altitud", "latitud", "longitud"],
    ):
        metadatos[str(fila.get("codigo", "")).strip()] = fila
    return metadatos


def codigos_del_m03(base: Path) -> tuple[set[str], list[str]]:
    """
    Códigos que el M03 seleccionó, y los que aparecen repetidos.

    El catálogo del IDEAM contiene estaciones duplicadas, y el M03 cuenta filas.
    Aquí interesa la estación como unidad: la matriz cuenta cada código una vez.
    """
    reporte = rutas.directorio("procesado", base) / "M03_estaciones.json"
    if not reporte.is_file():
        return set(), []
    try:
        datos = json.loads(reporte.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), []
    listados = [str(c).strip() for c in (datos.get("codigos") or ())]
    vistos: set[str] = set()
    repetidos: list[str] = []
    for codigo in listados:
        if codigo in vistos and codigo not in repetidos:
            repetidos.append(codigo)
        vistos.add(codigo)
    return vistos, sorted(repetidos)


# =============================================================================
# Escritura de productos
# =============================================================================
def escribir_csv(
    destino: Path, filas: Sequence[dict[str, Any]], delimitador: str,
) -> None:
    """Vuelca una tabla de diccionarios homogéneos."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    if not filas:
        destino.write_text("", encoding="utf-8-sig")
        return
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.DictWriter(
            manejador, fieldnames=list(filas[0]), delimiter=delimitador)
        escritor.writeheader()
        escritor.writerows(filas)


def _tabla_markdown(filas: Sequence[dict[str, Any]],
                    columnas: Sequence[str]) -> list[str]:
    """Tabla en el formato Markdown que usaban las rutinas heredadas."""
    lineas = ["| " + " | ".join(columnas) + " |",
              "|" + "|".join("---" for _ in columnas) + "|"]
    for fila in filas:
        lineas.append(
            "| " + " | ".join(str(fila.get(c, "")) for c in columnas) + " |")
    return lineas


def escribir_reporte_markdown(
    destino: Path,
    resultado: ResultadoM04b,
    ventanas: dict[str, tuple[int, int]],
    umbrales: Sequence[int],
    minimo: float,
    anio_estudio: int,
) -> None:
    """
    Informe en Markdown, en la línea de las rutinas heredadas.

    CLAUDE.md, sección 9, conserva de ellas la lógica de negocio y el formato de
    reporte. Este archivo es el que el consultor lee para fijar el umbral.
    """
    lineas = [
        "# M04b - Sensibilidad de longitud de series",
        "",
        f"* Año del estudio: {anio_estudio}",
        f"* Umbral de completitud anual: {minimo:.0%}",
        f"* Umbrales evaluados: {', '.join(str(u) for u in umbrales)} años",
        f"* Ventanas evaluadas: {', '.join(ventanas)}",
        f"* Series analizadas: {resultado.series_analizadas:,}",
        f"* Estaciones: {resultado.estaciones:,} "
        f"({resultado.estaciones_del_m03:,} seleccionadas por el M03)",
        "",
        "Este informe NO adopta ningún umbral. Presenta el costo en cobertura de",
        "cada elección para que el consultor la fije en config.yaml, en",
        "`sensibilidad_series.umbral_adoptado_anios` y `ventana_adoptada`.",
        "",
        "## Matriz de sensibilidad",
        "",
        "Estaciones que alcanzan cada umbral, contadas una sola vez aunque",
        "sirvan a la variable con varias etiquetas. La columna de continuidad",
        "exige que los años útiles sean consecutivos.",
        "",
    ]

    for variable in sorted({f["variable"] for f in resultado.matriz}):
        lineas += [f"### {variable}", ""]
        for nombre in ventanas:
            celdas = [f for f in resultado.matriz
                      if f["variable"] == variable and f["ventana"] == nombre]
            if not celdas:
                continue
            lineas += [f"Ventana {nombre}", ""]
            lineas += _tabla_markdown(
                [{"Umbral (años)": c["umbral_anios"],
                  "Estaciones": c["estaciones"],
                  "Con años consecutivos": c["estaciones_continuas"]}
                 for c in celdas],
                ["Umbral (años)", "Estaciones", "Con años consecutivos"],
            )
            lineas.append("")

    lineas += [
        "## Estado de operación",
        "",
        "La suspensión no elimina la estación: se cuenta aparte.",
        "",
    ]
    lineas += _tabla_markdown(
        [{"Estado": k, "Estaciones": v}
         for k, v in sorted(resultado.por_estado.items())],
        ["Estado", "Estaciones"],
    )
    lineas += [
        "",
        "## Detalle por estación y serie",
        "",
        "En `sensibilidad_series.csv`. La diferencia entre amplitud y años",
        "útiles mide el hueco que el M05 tendrá que resolver.",
        "",
    ]

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(lineas) + "\n", encoding="utf-8")


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Construye la matriz de sensibilidad y escribe los productos."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)

    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    umbrales = [int(u) for u in
                configuracion.obtener("sensibilidad_series.umbrales_anios")]
    declaradas = configuracion.obtener("sensibilidad_series.ventanas")
    anio_estudio = int(configuracion.obtener("proyecto.anio_estudio"))
    minimo = float(
        configuracion.obtener("sensibilidad_series.completitud_anual_minima"))
    max_suspension = int(
        configuracion.obtener("sensibilidad_series.anios_max_suspension"))
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")

    ventanas = {etiqueta_de_ventana(v, anio_estudio):
                resolver_ventana(v, anio_estudio) for v in declaradas}

    ruta_serie = rutas.directorio("procesado_series", base) / "series_ideam.csv"
    reporte_m04 = rutas.directorio("procesado", base) / "M04_ingesta.json"

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "serie consolidada": rutas.relativa(ruta_serie, base),
            "reporte del M04": rutas.relativa(reporte_m04, base),
        },
        parametros={
            "sensibilidad_series.umbrales_anios": umbrales,
            "sensibilidad_series.ventanas": list(ventanas),
            "sensibilidad_series.completitud_anual_minima": minimo,
            "sensibilidad_series.anios_max_suspension": max_suspension,
        },
    )

    resultado = ResultadoM04b()

    with registro.bloque(logger, "Lectura de la serie consolidada"):
        acumulado, leidos, ilegibles = leer_serie_consolidada(
            ruta_serie, delimitador)
        resultado.registros_leidos = leidos
        resultado.series_analizadas = len(acumulado)
        resultado.estaciones = len({c for c, _ in acumulado})
        logger.info("Leídos %s registro(s) en %s serie(s) de %s estación(es)",
                    f"{leidos:,}", f"{len(acumulado):,}",
                    f"{resultado.estaciones:,}")
        if ilegibles:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "serie.fecha",
                f"{ilegibles:,} registro(s) con fecha ilegible, excluidos del "
                "conteo. El M04 debería haberlos normalizado.",
            ))

    resultado.hallazgos.extend(verificar_contra_m04(acumulado, reporte_m04))
    if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos):
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Medición de longitud por serie"):
        metadatos = leer_metadatos_estaciones(base, configuracion)
        seleccionadas, repetidos = codigos_del_m03(base)
        resultado.estaciones_del_m03 = len(seleccionadas)
        if repetidos:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "m03.codigos_repetidos",
                f"el M03 lista {len(repetidos)} código(s) más de una vez: "
                f"{repetidos}. Su inventario cuenta filas del catálogo, no "
                "estaciones, y su capa de puntos las escribe repetidas: una "
                "interpolación posterior las pesaría varias veces. La matriz "
                "de este módulo cuenta cada código una sola vez.",
            ))

        sin_frecuencia: set[str] = set()
        for clave in sorted(acumulado):
            serie = acumulado[clave]
            if registros_esperados(serie.frecuencia, 2000) is None:
                sin_frecuencia.add(serie.frecuencia or "(vacía)")
            fila = resumir_serie(serie, minimo, ventanas)
            meta = metadatos.get(serie.codigo, {})
            fila["nombre"] = meta.get("nombre", "")
            fila["categoria"] = meta.get("categoria", "")
            fila["altitud"] = meta.get("altitud", "")
            fila["estado_susp"] = estado_por_suspension(
                str(meta.get("f_suspen", "")), anio_estudio, max_suspension)
            fila["en_m03"] = serie.codigo in seleccionadas
            resultado.filas.append(fila)

        estado_de: dict[str, str] = {}
        for fila in resultado.filas:
            if fila["en_m03"]:
                estado_de[fila["codigo"]] = fila["estado_susp"]
        for estado in estado_de.values():
            resultado.por_estado[estado] = resultado.por_estado.get(estado, 0) + 1

        if sin_frecuencia:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "serie.frecuencia",
                f"frecuencia(s) no reconocida(s): {sorted(sin_frecuencia)}. "
                "En esas series la completitud no se puede juzgar y se contó la "
                "presencia del año, lo que sobrestima la longitud útil.",
            ))

    with registro.bloque(logger, "Matriz de sensibilidad"):
        variable_de = mapa_etiqueta_variable(
            configuracion.obtener("ideam.descarga.series_por_variable"))
        resultado.matriz = construir_matriz(
            resultado.filas, umbrales, ventanas, variable_de,
            admitidos=seleccionadas or None,
        )
        logger.info("Matriz de %d celda(s)", len(resultado.matriz))

    with registro.bloque(logger, "Escritura de productos"):
        _escribir_productos(configuracion, base, resultado, ventanas, umbrales,
                            minimo, anio_estudio, delimitador, logger)

    resultado.hallazgos.extend(
        _resumir(resultado, configuracion, ventanas, umbrales))

    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _escribir_productos(configuracion, base, resultado, ventanas, umbrales,
                        minimo, anio_estudio, delimitador, logger) -> None:
    """Escribe el detalle, la matriz y el informe en Markdown."""
    directorio = rutas.directorio("procesado_estaciones", base, crear=True)

    detalle = directorio / "sensibilidad_series.csv"
    escribir_csv(detalle, resultado.filas, delimitador)
    resultado.productos.append(rutas.relativa(detalle, base))

    matriz = directorio / "matriz_sensibilidad.csv"
    escribir_csv(matriz, resultado.matriz, delimitador)
    resultado.productos.append(rutas.relativa(matriz, base))

    informe = directorio / "M04b_sensibilidad.md"
    escribir_reporte_markdown(
        informe, resultado, ventanas, umbrales, minimo, anio_estudio)
    resultado.productos.append(rutas.relativa(informe, base))

    logger.info("Detalle de %d fila(s) y matriz de %d celda(s)",
                len(resultado.filas), len(resultado.matriz))


def _resumir(resultado, configuracion, ventanas, umbrales) -> list[Hallazgo]:
    """Informativos de lectura y advertencias sobre el umbral adoptado."""
    con_dato = {f["codigo"] for f in resultado.filas if f["en_m03"]}
    sin_dato = resultado.estaciones_del_m03 - len(con_dato)
    hallazgos: list[Hallazgo] = [Hallazgo(
        INFORMATIVO, "sensibilidad.series",
        f"{resultado.series_analizadas:,} serie(s) de "
        f"{resultado.estaciones:,} estación(es); "
        f"{resultado.estaciones_del_m03:,} seleccionada(s) por el M03 entran en "
        "la matriz.",
    )]

    if sin_dato > 0:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "sensibilidad.sin_serie",
            f"{sin_dato} estación(es) seleccionada(s) por el M03 no tienen "
            "ninguna serie en la ingesta del M04. Existen en el catálogo y "
            "caen en el área, pero no aportan dato: o no se descargaron, o el "
            "servicio no publica series para ellas.",
        ))

    for celda in resultado.matriz:
        if not celda["evaluable"] or celda["estaciones"]:
            continue
        if celda["umbral_anios"] != max(
            u for u in umbrales
            if any(c["evaluable"] for c in resultado.matriz
                   if c["ventana"] == celda["ventana"] and c["umbral_anios"] == u)
        ):
            continue
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"sensibilidad.{celda['variable']}.{celda['ventana']}",
            f"ninguna estación alcanza {celda['umbral_anios']} años útiles "
            f"en la ventana {celda['ventana']}. Ese umbral dejaría la "
            "variable sin dato en esa ventana.",
        ))

    imposibles = sorted({(c["ventana"], c["umbral_anios"])
                         for c in resultado.matriz if not c["evaluable"]})
    if imposibles:
        detalle = ", ".join(f"{u} años en {v}" for v, u in imposibles)
        hallazgos.append(Hallazgo(
            INFORMATIVO, "sensibilidad.combinacion_imposible",
            f"combinacion(es) sin sentido aritmético, excluidas de la lectura "
            f"de la matriz: {detalle}. La ventana es más corta que el umbral.",
        ))

    adoptado = configuracion.obtener("sensibilidad_series.umbral_adoptado_anios")
    ventana_adoptada = configuracion.obtener("sensibilidad_series.ventana_adoptada")
    if adoptado is None or ventana_adoptada is None:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "sensibilidad.decision",
            "el umbral y la ventana siguen sin adoptar. El M04b no los fija: "
            "el consultor debe declararlos en config.yaml antes del M05, y la "
            "decisión debe quedar registrada en MANIFIESTO.yaml.",
        ))
        return hallazgos

    nombre = etiqueta_de_ventana(
        ventana_adoptada, int(configuracion.obtener("proyecto.anio_estudio")))
    for celda in resultado.matriz:
        if celda["ventana"] == nombre and celda["umbral_anios"] == int(adoptado):
            severidad = ADVERTENCIA if celda["estaciones"] < 3 else INFORMATIVO
            hallazgos.append(Hallazgo(
                severidad, f"adoptado.{celda['variable']}",
                f"con {adoptado} años en la ventana {nombre} quedan "
                f"{celda['estaciones']} estación(es) de {celda['variable']}, "
                f"de las cuales {celda['estaciones_continuas']} con años "
                "consecutivos.",
            ))
    return hallazgos


def _cerrar(logger, resultado: ResultadoM04b, base: Path, ruta_json: Path | None,
            inicio: float, codigo: int) -> tuple[int, list[Hallazgo]]:
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
    logger.info(
        "RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
        conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO],
    )

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M04b_sensibilidad.json"

    reporte = {
        "modulo": MODULO,
        "registros_leidos": resultado.registros_leidos,
        "series_analizadas": resultado.series_analizadas,
        "estaciones": resultado.estaciones,
        "estaciones_del_m03": resultado.estaciones_del_m03,
        "por_estado": resultado.por_estado,
        "matriz": resultado.matriz,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(
        json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8")

    productos = {f"producto {i}": p
                 for i, p in enumerate(resultado.productos, start=1)}
    productos["reporte JSON"] = rutas.relativa(ruta_json, base)
    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecución"] = rutas.relativa(archivo_log, base)

    estado = "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO"
    registro.registrar_cierre(
        logger, MODULO, estado,
        segundos=time.perf_counter() - inicio, productos=productos,
    )
    return codigo, hallazgos


# =============================================================================
# Interfaz de línea de comandos
# =============================================================================
def _analizar_argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        prog="M04b_sensibilidad_series.py",
        description="Análisis de sensibilidad de longitud de series del IDEAM.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida, consola=not argumentos.silencioso,
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

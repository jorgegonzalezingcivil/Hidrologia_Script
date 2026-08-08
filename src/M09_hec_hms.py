#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M09 - Insumos para HEC-HMS y exportación de subcuencas y corrientes
===================================================================
Entorno: venv del proyecto.

Este módulo está a caballo del ÚNICO paso manual del estudio. CLAUDE.md, sección
4: "Único paso con intervención manual obligatoria: el módulo de
geoprocesamiento de HEC-HMS (delimitación asistida). Todo lo demás se ejecuta sin
abrir software".

Por eso tiene dos modos, y no uno:

    --preparar   deja los insumos donde HEC-HMS los espera y escribe el
                 instructivo de los pasos manuales
    --importar   lee lo que el consultor exportó, lo verifica y lo normaliza
                 para el M10, el M11 y el M13

Se prefieren dos modos explícitos a una detección automática porque la misma
orden produciría resultados distintos según el estado del disco, y eso no se
puede auditar. Un log debe poder decir qué se hizo, no solo que algo se hizo.

Por qué corre en el venv y no en QGIS. No necesita geoprocesar: copia el DEM que
el M02 ya recortó, copia capas vectoriales con sus archivos acompañantes y lee
atributos y áreas con el adaptador de shapefile de librería estándar. La
verificación que importa (que el área delimitada se parezca a la cartográfica)
se resuelve con el área de los polígonos, que ese adaptador calcula.

LO QUE ESTE MÓDULO NO PUEDE VERIFICAR. La topología (que cada tramo conecte con
su subcuenca y la red drene al punto) exige leer geometría vértice a vértice, y
eso pertenece al entorno SIG. Se declara como no verificado en lugar de darlo
por bueno.

ADVERTENCIA sobre el DEM. Se entrega SIN reacondicionar, por decisión declarada.
El riesgo es conocido y está medido: con el DEM de radar crudo, el M02 llegó a
delimitar una cuenca de 6,59 km² en terreno plano, porque el ruido vertical
gobierna las direcciones de flujo donde el relieve es de centímetros por
kilómetro. Por eso el drenaje cartográfico viaja entre los insumos como capa de
verificación, y la comprobación de área al importar es el control que atrapa esa
falla si se repite.

Productos (--preparar):
    data/04_modelos/hec_hms/insumos/    DEM, punto, cuenca, drenaje
    data/04_modelos/hec_hms/insumos/INSTRUCTIVO.md

Productos (--importar):
    data/03_SIG/vector/subcuencas.shp
    data/03_SIG/vector/corrientes.shp
    data/02_procesado/M09_hec_hms.json

Uso:
    python src/M09_hec_hms.py --preparar
    python src/M09_hec_hms.py --importar

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

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

MODULO = "M09"
DESCRIPCION = "Insumos para HEC-HMS y exportación de subcuencas y corrientes"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Extensiones que acompañan a un shapefile. Copiar solo el .shp produce una capa
# ilegible: el .dbf lleva los atributos, el .shx el índice y el .prj el sistema
# de referencia, sin el cual QGIS pregunta al abrir.
ACOMPANANTES = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx")


@dataclass
class ResultadoM09:
    modo: str = ""
    insumos: list[str] = field(default_factory=list)
    subcuencas: dict[str, Any] = field(default_factory=dict)
    corrientes: dict[str, Any] = field(default_factory=dict)
    verificaciones: list[dict[str, Any]] = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def copiar_capa(origen: Path, destino_dir: Path) -> list[Path]:
    """
    Copia un shapefile con todos sus archivos acompañantes.

    Copiar solo el .shp entrega una capa que ningún programa puede abrir: los
    atributos viven en el .dbf y el sistema de referencia en el .prj.
    """
    destino_dir.mkdir(parents=True, exist_ok=True)
    copiados: list[Path] = []
    for extension in ACOMPANANTES:
        candidato = origen.with_suffix(extension)
        if candidato.is_file():
            destino = destino_dir / candidato.name
            shutil.copy2(candidato, destino)
            copiados.append(destino)
    if not copiados:
        raise ErrorRutas(f"no se encontró ningún archivo de {origen}.")
    return copiados


def diferencia_relativa(uno: float, otro: float) -> float | None:
    """Diferencia porcentual respecto de la referencia."""
    if not otro:
        return None
    return 100.0 * (uno - otro) / otro


def verificar_area(
    area_delimitada_km2: float,
    area_preliminar_km2: float,
    fraccion_minima_pct: float,
) -> dict[str, Any]:
    """
    Comprueba que el área delimitada quepa dentro de la preliminar.

    La preliminar es COTA SUPERIOR, no objetivo de igualdad. Para el escenario 1
    el M02 adopta la subzona hidrográfica entera, que es una unidad cerrada y
    por definición CONTIENE la cuenca aportante al punto: si HEC-HMS delimitara
    más, estaría tomando agua de otra vertiente.

    Que sea cota y no igualdad importa: la subzona sobredimensiona el área
    varias veces, y exigir que coincidan rechazaría cualquier delimitación
    correcta.

    El control por abajo es el que atrapa el fallo conocido. Con el DEM de radar
    sin reacondicionar, el análisis de terreno del M02 llegó a producir 6,59 km²
    sobre una subzona de 5.926: una fracción del 0,1%. En terreno plano el ruido
    vertical gobierna las direcciones de flujo, y el resultado es una cuenca
    verosímil y equivocada.
    """
    if area_preliminar_km2 <= 0:
        return {"error": "área preliminar no utilizable"}
    fraccion = 100.0 * area_delimitada_km2 / area_preliminar_km2
    return {
        "area_delimitada_km2": round(area_delimitada_km2, 3),
        "area_preliminar_km2": round(area_preliminar_km2, 3),
        "fraccion_pct": round(fraccion, 2),
        "fraccion_minima_pct": fraccion_minima_pct,
        "excede_la_preliminar": bool(area_delimitada_km2 > area_preliminar_km2),
        "demasiado_pequena": bool(fraccion < fraccion_minima_pct),
    }


def resumir_capa(ruta: Path) -> dict[str, Any]:
    """Metadatos de una capa: entidades, sistema de referencia y campos."""
    info = shapefile.leer_shapefile(ruta)
    return {
        "archivo": ruta.name,
        "entidades": info.n_registros,
        "geometria": info.codigo_geometria,
        "crs_epsg": info.crs_epsg,
        "campos": list(info.nombres_campos),
        "extension": list(info.extension) if info.extension else None,
    }


def subcuencas_pequenas(
    ruta: Path, minimo_km2: float,
) -> list[dict[str, Any]]:
    """
    Subcuencas por debajo del área mínima, que suelen ser artefactos.

    Una subcuenca de unas hectáreas junto a otras de decenas de kilómetros
    cuadrados no es una unidad hidrológica: es un residuo del trazado, y
    arrastrarla al modelo produce un hidrograma sin sentido físico y un tiempo
    de concentración absurdo.

    Se reporta sin eliminar: fusionarla con su vecina es decisión del consultor
    y se hace en HEC-HMS, no aquí.
    """
    pequenas: list[dict[str, Any]] = []
    try:
        registros = list(shapefile.leer_registros(ruta))
    except (ErrorFormato, ErrorRutas):
        return pequenas
    for indice, fila in enumerate(registros):
        area = None
        for clave in ("area_km2", "AREA_KM2", "Area_km2", "area", "AREA"):
            if clave in fila:
                try:
                    area = float(fila[clave])
                except (TypeError, ValueError):
                    area = None
                break
        if area is not None and area < minimo_km2:
            pequenas.append({"indice": indice, "area_km2": round(area, 4),
                             **{k: v for k, v in list(fila.items())[:3]}})
    return pequenas


# =============================================================================
# Modo preparar
# =============================================================================
INSTRUCTIVO = """# M09 - Instructivo del paso manual de HEC-HMS

Este es el **único paso con intervención manual obligatoria** del estudio
(CLAUDE.md, sección 4). Todo lo demás se ejecuta sin abrir software.

Sin este documento el paso no sería reproducible por otra persona, y un estudio
cuyo eslabón manual no está escrito no se puede repetir ni auditar.

## Qué hay en esta carpeta

| Archivo | Para qué sirve |
|---|---|
| `dem_recortado.tif` | Terreno del que HEC-HMS deriva direcciones de flujo |
| `punto_descarga.shp` | Punto de cierre de la cuenca |
| `area_influencia.shp` | Cuenca preliminar del M02, sirve de cota superior |
| `drenaje_sencillo_area.shp` | Red del IGAC, para verificar y reacondicionar |
| `drenaje_doble_area.shp` | Cauces anchos del IGAC |

Todo está en {crs}.

## Pasos

1. Abrir HEC-HMS {version} y crear (o abrir) el proyecto del estudio.
2. Definir el terreno: `Components > Terrain Data Manager`, y cargar
   `dem_recortado.tif`.
3. Crear el modelo de cuenca: `Components > Basin Model Manager`.
4. En el modelo, `GIS > Preprocess Sinks` y luego `GIS > Preprocess Drainage`.
5. `GIS > Identify Streams` con el umbral de área que corresponda al detalle
   buscado. Comparar el resultado con `drenaje_sencillo_area.shp`: **si las
   corrientes trazadas no siguen la red cartográfica, detenerse aquí** y ver la
   advertencia de más abajo.
6. `GIS > Break Points` en el punto de `punto_descarga.shp`, y en los sitios
   donde se quiera separar subcuencas.
7. `GIS > Delineate Elements` para generar subcuencas y tramos.
8. Comparar el área total obtenida con la de `area_influencia.shp`
   ({area_preliminar:.2f} km²), que es la **cuenca preliminar**.

   Esa cifra es una **cota superior**, no un objetivo. Para este escenario el
   M02 adopta la subzona hidrográfica entera, que es una unidad cerrada y
   contiene la cuenca aportante por definición. La delimitación debe quedar por
   debajo de ella y por encima del {tolerancia:.0f}% de ella.
9. Exportar la delimitación a shapefile y depositarla en:

       {salida}

   con los nombres `{subcuencas}` y `{corrientes}`.

10. Volver a la línea de órdenes y ejecutar:

        python src/M09_hec_hms.py --importar

## ADVERTENCIA sobre el terreno

El DEM se entrega **sin reacondicionar**, por decisión declarada en
`MANIFIESTO.yaml`.

El riesgo está medido en este mismo estudio: con el DEM de radar crudo, el
análisis de terreno del M02 llegó a delimitar una cuenca de **6,59 km²** donde
la cartografía daba órdenes de magnitud más. La causa es que en la sabana el
relieve cae centímetros por kilómetro mientras el DEM tiene incertidumbre
vertical de varios metros: el ruido gobierna las direcciones de flujo.

Si en el paso 5 las corrientes trazadas no siguen la red del IGAC, o si en el
paso 8 el área se aparta de la cartográfica, el remedio es **reacondicionar el
DEM** rebajándolo a lo largo de los cauces (AGREE o burn-in) antes de volver a
delimitar. Los shapefiles de drenaje están en esta carpeta para eso.

## Qué queda sin verificar

El M09 corre en el venv y comprueba número de entidades, sistema de referencia,
campos y **área total**. NO comprueba la topología (que cada tramo conecte con
su subcuenca y que la red drene al punto de cierre), porque eso exige leer
geometría vértice a vértice y pertenece al entorno SIG. Revisar la conectividad
en HEC-HMS o en QGIS antes de continuar.
"""


def _preparar(configuracion, base, resultado, logger) -> None:
    """Deja los insumos donde HEC-HMS los espera y escribe el instructivo."""
    destino = rutas.resolver(
        configuracion.obtener("hec_hms.intercambio.insumos"), base)
    destino.mkdir(parents=True, exist_ok=True)
    salida = rutas.resolver(
        configuracion.obtener("hec_hms.intercambio.salida"), base)
    salida.mkdir(parents=True, exist_ok=True)

    dem = rutas.directorio("sig_raster", base) / "dem_recortado.tif"
    if not dem.is_file():
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "insumos.dem",
            f"no se encuentra {dem.name}. El M02 debe haberlo producido: sin "
            "terreno, HEC-HMS no puede derivar direcciones de flujo.",
        ))
        return
    shutil.copy2(dem, destino / dem.name)
    resultado.insumos.append(rutas.relativa(destino / dem.name, base))

    vectoriales = [
        rutas.directorio("sig_vector", base) / "punto_descarga.shp",
        rutas.directorio("sig_vector", base) / "area_influencia.shp",
        rutas.resolver(
            configuracion.obtener("referencia_nacional.salida_recorte_sencillo"),
            base),
        rutas.resolver(
            configuracion.obtener("referencia_nacional.salida_recorte_doble"),
            base),
    ]
    faltan: list[str] = []
    for ruta in vectoriales:
        if not ruta.is_file():
            faltan.append(ruta.name)
            continue
        for copiado in copiar_capa(ruta, destino):
            if copiado.suffix == ".shp":
                resultado.insumos.append(rutas.relativa(copiado, base))
    if faltan:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "insumos.faltantes",
            f"no se copiaron {faltan}: no existen todavia. El drenaje procede "
            "del M02 y la cuenca preliminar tambien; sin ellos, la verificacion "
            "del paso manual queda sin referencia.",
        ))

    area_preliminar = 0.0
    cuenca = rutas.directorio("sig_vector", base) / "area_influencia.shp"
    if cuenca.is_file():
        try:
            area_preliminar = float(shapefile.area_poligonos(cuenca)) / 1e6
        except (ErrorFormato, ErrorRutas, TypeError, ValueError):
            area_preliminar = 0.0

    instructivo = destino / "INSTRUCTIVO.md"
    instructivo.write_text(
        INSTRUCTIVO.format(
            crs=configuracion.obtener("crs.calculo"),
            version=configuracion.obtener("software.hec_hms.version"),
            area_preliminar=area_preliminar,
            tolerancia=float(configuracion.obtener(
                "hec_hms.intercambio.fraccion_minima_pct")),
            salida=rutas.relativa(salida, base),
            subcuencas=configuracion.obtener("hec_hms.intercambio.subcuencas"),
            corrientes=configuracion.obtener("hec_hms.intercambio.corrientes"),
        ),
        encoding="utf-8")
    resultado.productos.append(rutas.relativa(instructivo, base))
    resultado.productos.extend(resultado.insumos)

    logger.info("%d insumo(s) en %s", len(resultado.insumos),
                rutas.relativa(destino, base))
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "preparar.listo",
        f"insumos listos en {rutas.relativa(destino, base)} y area preliminar "
        f"de referencia {area_preliminar:.2f} km2. El siguiente paso es MANUAL: "
        "seguir INSTRUCTIVO.md y despues ejecutar este modulo con --importar.",
    ))


# =============================================================================
# Modo importar
# =============================================================================
def _importar(configuracion, base, resultado, logger) -> None:
    """Lee lo que el consultor exporto desde HEC-HMS, lo verifica y lo publica."""
    salida = rutas.resolver(
        configuracion.obtener("hec_hms.intercambio.salida"), base)
    nombre_sub = configuracion.obtener("hec_hms.intercambio.subcuencas")
    nombre_cor = configuracion.obtener("hec_hms.intercambio.corrientes")
    ruta_sub = salida / nombre_sub
    ruta_cor = salida / nombre_cor

    ausentes = [n for n, r in ((nombre_sub, ruta_sub), (nombre_cor, ruta_cor))
                if not r.is_file()]
    if ausentes:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "importar.ausente",
            f"no se encuentra(n) {ausentes} en {rutas.relativa(salida, base)}. "
            "Ejecutar antes el modo --preparar, seguir INSTRUCTIVO.md y "
            "depositar alli la exportacion de HEC-HMS con esos nombres exactos.",
        ))
        return

    resultado.subcuencas = resumir_capa(ruta_sub)
    resultado.corrientes = resumir_capa(ruta_cor)
    logger.info("subcuencas: %d entidad(es) | corrientes: %d entidad(es)",
                resultado.subcuencas["entidades"],
                resultado.corrientes["entidades"])

    # --- Sistema de referencia ----------------------------------------------
    esperado = str(configuracion.obtener("crs.calculo")).upper()
    for nombre, resumen in (("subcuencas", resultado.subcuencas),
                            ("corrientes", resultado.corrientes)):
        declarado = (resumen.get("crs_epsg") or "").upper()
        if not declarado:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, f"importar.{nombre}.crs",
                f"la capa de {nombre} no trae .prj legible. Se asume el CRS de "
                f"calculo ({esperado}), y si no lo fuera todo lo que sigue "
                "quedaria desplazado sin ninguna senal.",
            ))
        elif declarado != esperado:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, f"importar.{nombre}.crs",
                f"la capa de {nombre} declara {declarado} y el calculo ocurre en "
                f"{esperado}. Reproyectar antes de continuar: mezclarlos "
                "produce areas y longitudes equivocadas.",
            ))

    # --- Area frente a la delimitacion cartografica --------------------------
    # La cuenca preliminar del escenario 1 es el area de influencia: la subzona
    # entera, que CONTIENE la cuenca aportante por definicion.
    cuenca = rutas.directorio("sig_vector", base) / "area_influencia.shp"
    fraccion_minima = float(configuracion.obtener(
        "hec_hms.intercambio.fraccion_minima_pct"))
    if cuenca.is_file():
        try:
            preliminar = float(shapefile.area_poligonos(cuenca)) / 1e6
            delimitada = float(shapefile.area_poligonos(ruta_sub)) / 1e6
        except (ErrorFormato, ErrorRutas, TypeError, ValueError) as exc:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "importar.area",
                f"no se pudo comparar el area: {exc}.",
            ))
        else:
            verificacion = verificar_area(delimitada, preliminar,
                                          fraccion_minima)
            verificacion["prueba"] = "area_total"
            resultado.verificaciones.append(verificacion)
            problemas = []
            if verificacion.get("excede_la_preliminar"):
                problemas.append(
                    "EXCEDE la cuenca preliminar, que es una subzona cerrada y "
                    "la contiene por definicion: la delimitacion esta tomando "
                    "agua de otra vertiente")
            if verificacion.get("demasiado_pequena"):
                problemas.append(
                    f"queda por debajo del {fraccion_minima:.0f}% de la "
                    "preliminar. Es el fallo conocido: con el DEM de radar sin "
                    "reacondicionar, el M02 llego a delimitar 6,59 km2 sobre "
                    "una subzona de 5.926, porque en terreno plano el ruido "
                    "vertical gobierna las direcciones de flujo. Reacondicionar "
                    "el DEM con el drenaje del IGAC y repetir la delimitacion")
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE if problemas else INFORMATIVO, "importar.area",
                f"area delimitada {delimitada:.2f} km2, el "
                f"{verificacion['fraccion_pct']:.1f}% de la cuenca preliminar "
                f"({preliminar:.2f} km2)."
                + ("" if not problemas else " " + ". ".join(problemas) + "."),
            ))

    # --- Subcuencas diminutas ------------------------------------------------
    minimo = float(configuracion.obtener(
        "hec_hms.intercambio.area_minima_subcuenca_km2"))
    pequenas = subcuencas_pequenas(ruta_sub, minimo)
    if pequenas:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "importar.subcuencas_pequenas",
            f"{len(pequenas)} subcuenca(s) por debajo de {minimo} km2. Suelen ser "
            "artefactos del trazado y no unidades hidrologicas: arrastrarlas "
            "produce un hidrograma sin sentido fisico y un tiempo de "
            "concentracion absurdo. Fusionarlas es decision del consultor y se "
            "hace en HEC-HMS.",
        ))

    # --- Publicacion ---------------------------------------------------------
    destino = rutas.directorio("sig_vector", base, crear=True)
    for ruta, nombre in ((ruta_sub, "subcuencas"), (ruta_cor, "corrientes")):
        for copiado in copiar_capa(ruta, destino):
            if copiado.suffix == ".shp":
                final = destino / f"{nombre}.shp"
                if copiado != final:
                    for extension in ACOMPANANTES:
                        origen_ext = copiado.with_suffix(extension)
                        if origen_ext.is_file():
                            origen_ext.replace(final.with_suffix(extension))
                resultado.productos.append(rutas.relativa(final, base))

    resultado.hallazgos.append(Hallazgo(
        ADVERTENCIA, "importar.topologia",
        "la topologia NO se verifico. Comprobar que cada tramo conecte con su "
        "subcuenca y que la red drene al punto de cierre exige leer geometria "
        "vertice a vertice, y eso pertenece al entorno SIG. Revisarla en QGIS o "
        "en HEC-HMS antes del M13.",
    ))
    logger.info("Capas publicadas en %s", rutas.relativa(destino, base))


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    modo: str = "preparar",
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Prepara los insumos o importa la salida, segun el modo pedido."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    resultado = ResultadoM09(modo=modo)
    registro.registrar_cabecera(
        logger, MODULO, f"{DESCRIPCION} (modo {modo})", config=configuracion,
        insumos={
            "intercambio": configuracion.obtener("hec_hms.intercambio.insumos"),
            "salida esperada": configuracion.obtener("hec_hms.intercambio.salida"),
        },
        parametros={
            "hec_hms.intercambio.fraccion_minima_pct": configuracion.obtener(
                "hec_hms.intercambio.fraccion_minima_pct"),
            "hec_hms.intercambio.area_minima_subcuenca_km2":
                configuracion.obtener(
                    "hec_hms.intercambio.area_minima_subcuenca_km2"),
            "software.hec_hms.version":
                configuracion.obtener("software.hec_hms.version"),
        },
    )

    with registro.bloque(logger, f"Modo {modo}"):
        if modo == "preparar":
            _preparar(configuracion, base, resultado, logger)
        elif modo == "importar":
            _importar(configuracion, base, resultado, logger)
        else:
            raise ErrorConfiguracion(f"modo no reconocido: {modo!r}")

    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


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
        ruta_json = (rutas.directorio("procesado", base, crear=True)
                     / "M09_hec_hms.json")
    reporte = {
        "modulo": MODULO,
        "modo": resultado.modo,
        "insumos": resultado.insumos,
        "subcuencas": resultado.subcuencas,
        "corrientes": resultado.corrientes,
        "verificaciones": resultado.verificaciones,
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
        prog="M09_hec_hms.py",
        description="Insumos para HEC-HMS y exportacion de subcuencas.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    grupo = analizador.add_mutually_exclusive_group()
    grupo.add_argument("--preparar", action="store_const", const="preparar",
                       dest="modo",
                       help="Deja los insumos y escribe el instructivo.")
    grupo.add_argument("--importar", action="store_const", const="importar",
                       dest="modo",
                       help="Lee y verifica la salida de HEC-HMS.")
    analizador.set_defaults(modo="preparar")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            modo=argumentos.modo, ruta_json=argumentos.json_salida,
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

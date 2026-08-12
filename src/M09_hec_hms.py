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
    data/03_SIG/vector/corrientes.shp        solo si origen_corrientes = hec_hms
    data/02_procesado/M09_subcuencas_pequenas.csv    si las hay
    data/02_procesado/M09_hec_hms.json

LAS CORRIENTES NO SIEMPRE VIENEN DE HEC-HMS. La clave
hec_hms.intercambio.origen_corrientes declara de dónde salen los tramos del
modelo. Con 'red_topologica' se usa la red que el M02b ya construyó, que trae
orden de Strahler, adyacencia y el puente sobre los embalses, y el consultor
solo exporta las subcuencas. Se declara y no se deduce del disco, porque la
misma orden daría resultados distintos según lo que hubiera allí.

LAS DOS REFERENCIAS DE ÁREA NO VALEN LO MISMO. El área de influencia es una
COTA SUPERIOR: contiene la cuenca por definición, pero admite una delimitación
tres veces mayor de lo debido sin emitir señal. La superficie drenada por la red
que llega al punto, con el radio calibrado contra la densidad de drenaje de la
subzona, es la referencia con significado hidrológico, y contra ella se contrasta
con una banda ancha porque no es una divisoria.

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
    Comprueba que el área delimitada quepa dentro del área de influencia.

    El área de influencia es COTA SUPERIOR, no objetivo de igualdad: es la
    envolvente del trazado aguas arriba más el buffer, o la subzona entera si no
    se acotó. En ambos casos CONTIENE la cuenca aportante al punto, de modo que
    si HEC-HMS delimitara más estaría tomando agua de otra vertiente.

    Que sea cota y no igualdad importa: sobredimensiona el área varias veces, y
    exigir que coincidan rechazaría cualquier delimitación correcta. Por eso NO
    basta como control: una delimitación tres veces mayor de lo debido pasa esta
    prueba sin una sola señal. El contraste con significado hidrológico es el de
    'contrastar_con_la_drenada'.

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


def contrastar_con_la_drenada(
    area_delimitada_km2: float,
    area_drenada_km2: float,
    banda_pct: float,
) -> dict[str, Any]:
    """
    Contrasta la delimitación con la superficie drenada por la red.

    La superficie drenada NO es una divisoria: es el conjunto de puntos a menos
    de un radio de algún cauce que llega al punto de cierre, con el radio
    calibrado para que la densidad de drenaje resultante reproduzca la medida en
    la subzona. La divisoria está en las cumbres y solo sale del terreno o de la
    delimitación asistida.

    Por eso la banda es ancha y el resultado no bloquea. Sirve para detectar el
    orden de magnitud equivocado, que es el fallo que importa, y no para
    arbitrar una diferencia del veinte por ciento entre dos cosas que no son la
    misma. Medido en este estudio: 220,60 km² delimitados frente a 305,45 km²
    drenados, un 27,8% por debajo, que está dentro de lo esperable entre una
    divisoria real y una mancha alrededor de los cauces.
    """
    if area_drenada_km2 <= 0:
        return {"error": "superficie drenada no utilizable"}
    desviacion = diferencia_relativa(area_delimitada_km2, area_drenada_km2)
    return {
        "area_delimitada_km2": round(area_delimitada_km2, 3),
        "area_drenada_km2": round(area_drenada_km2, 3),
        "desviacion_pct": round(desviacion, 2) if desviacion is not None else None,
        "banda_pct": banda_pct,
        "fuera_de_banda": bool(desviacion is not None
                               and abs(desviacion) > banda_pct),
    }


def superficie_drenada_de_referencia(
    ruta_red: Path,
    ruta_reporte_m02: Path,
    radio_m: float,
) -> dict[str, Any] | None:
    """
    Área de la superficie que drena al punto, para contrastar la delimitación.

    Se reconstruye a partir de dos productos ya escritos, no de estado en
    memoria: la red topológica del M02b y la lista de tramos aguas arriba que el
    M02 dejó en su reporte. Si el estudio no acotó por la red (escenario sin
    trazado aguas arriba), no hay lista y esta referencia no existe: se devuelve
    None y el módulo se queda con la cota superior, declarándolo.

    Devuelve el área en km², la longitud de red y la densidad de drenaje, que
    es lo que permite juzgar si el radio sigue siendo el calibrado.
    """
    if not ruta_red.is_file() or not ruta_reporte_m02.is_file() or radio_m <= 0:
        return None

    try:
        reporte = json.loads(ruta_reporte_m02.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    aguas_arriba = set(reporte.get("acotado", {}).get("aguas_arriba") or [])
    if not aguas_arriba:
        return None

    try:
        registros = list(shapefile.leer_registros(ruta_red, ["id_tramo"]))
        geometrias = shapefile.leer_geometrias(ruta_red)
    except (ErrorFormato, ErrorRutas):
        return None

    lineas: list[list[tuple[float, float]]] = []
    for registro, entidad in zip(registros, geometrias):
        try:
            identificador = int(registro["id_tramo"])
        except (KeyError, TypeError, ValueError):
            continue
        if identificador not in aguas_arriba:
            continue
        for parte in entidad:
            if len(parte) >= 2:
                lineas.append([(x, y) for x, y in parte])
    if not lineas:
        return None

    try:
        import red_drenaje

        superficie = red_drenaje.superficie_drenada(lineas, radio_m)
    except (ImportError, ErrorFormato, ValueError):
        return None

    return {
        "area_km2": float(superficie["area_km2"]),
        "longitud_red_km": float(superficie["longitud_red_km"]),
        "densidad_km_km2": float(superficie["densidad_km_km2"]),
        "radio_m": radio_m,
        "tramos": len(lineas),
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


CAMPOS_NOMBRE = ("name", "Name", "NAME", "nombre", "Nombre", "NOMBRE",
                 "subcuenca", "SUBCUENCA", "id", "ID")


def subcuencas_pequenas(
    ruta: Path, minimo_km2: float,
) -> list[dict[str, Any]]:
    """
    Subcuencas por debajo del área mínima, que suelen ser artefactos.

    Una subcuenca de unas hectáreas junto a otras de decenas de kilómetros
    cuadrados no es una unidad hidrológica: es un residuo del trazado, y
    arrastrarla al modelo produce un hidrograma sin sentido físico y un tiempo
    de concentración absurdo.

    EL ÁREA SE MIDE SOBRE LA GEOMETRÍA, no se lee de un atributo. La exportación
    de HEC-HMS trae los parámetros que el programa calculó (`long_len`,
    `long_slo`, `basin_slo`, `drain_den`) y ninguno es el área. Buscarla entre
    los campos devolvía una lista vacía, y el módulo concluía que no había
    ninguna subcuenca diminuta. Medido sobre la exportación de este estudio:
    por atributo ninguna, por geometría 24 por debajo de 0,5 km², la menor de
    0,006 km², es decir seis mil metros cuadrados.

    Se reporta sin eliminar: fusionarla con su vecina es decisión del consultor
    y se hace en HEC-HMS, no aquí.
    """
    try:
        areas = shapefile.areas_poligonos(ruta)
    except (ErrorFormato, ErrorRutas, OSError):
        return []

    try:
        registros = list(shapefile.leer_registros(ruta))
    except (ErrorFormato, ErrorRutas, OSError):
        registros = []

    pequenas: list[dict[str, Any]] = []
    for indice, area_m2 in enumerate(areas):
        area = area_m2 / 1e6
        if area >= minimo_km2:
            continue
        fila = registros[indice] if indice < len(registros) else {}
        nombre = ""
        for clave in CAMPOS_NOMBRE:
            if clave in fila and str(fila[clave]).strip():
                nombre = str(fila[clave]).strip()
                break
        pequenas.append({"indice": indice, "nombre": nombre,
                         "area_km2": round(area, 4)})
    return pequenas


def escribir_pequenas(destino: Path, pequenas: Sequence[dict[str, Any]]) -> Path:
    """
    Deja la lista de subcuencas diminutas en un CSV.

    El hallazgo dice cuántas son; el consultor necesita saber CUÁLES para
    fusionarlas en HEC-HMS. Un aviso que obliga a buscarlas a mano en una capa
    de ciento veinticinco entidades no es accionable.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", encoding="utf-8", newline="") as manejador:
        escritor = csv.writer(manejador, delimiter=";")
        escritor.writerow(["indice", "nombre", "area_km2"])
        for fila in pequenas:
            escritor.writerow([fila["indice"], fila["nombre"], fila["area_km2"]])
    return destino


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
| `area_influencia.shp` | Cota superior del M02. **No es una cuenca**: es una envolvente con holgura |
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
8. Comparar el área total obtenida con las dos referencias:

{referencia}
9. Exportar la delimitación a shapefile y depositarla en:

       {salida}

{exportar}
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
campos, **área total** y **área de cada subcuenca**. NO comprueba la topología
(que cada tramo conecte con su subcuenca y que la red drene al punto de cierre),
porque eso exige leer geometría vértice a vértice y pertenece al entorno SIG.
Revisar la conectividad en HEC-HMS o en QGIS antes de continuar.
"""

REFERENCIA_DRENADA = """   **Superficie drenada: {area_drenada:.2f} km²**, que es la referencia con
   significado hidrológico. La envuelve la red que drena al punto ({red:.1f} km
   de cauces), con un radio de {radio:.0f} m calibrado para que la densidad de
   drenaje resultante ({densidad:.2f} km/km²) reproduzca la medida en la
   subzona.

   No es una divisoria, y por eso la comparación es de orden de magnitud: se
   admite una desviación de hasta el {banda:.0f}%. La divisoria está en las
   cumbres, esto es una mancha alrededor de los cauces.

   **Cota superior: {area_preliminar:.2f} km²** (`area_influencia.shp`). La
   delimitación no puede excederla, porque esa envolvente contiene la cuenca
   aportante por definición: si la supera, está tomando agua de otra vertiente.
   Tampoco puede quedar por debajo del {tolerancia:.0f}% de ella, que es el
   control que atrapa el fallo del DEM descrito más abajo.
"""

REFERENCIA_SIN_DRENADA = """   **Cota superior: {area_preliminar:.2f} km²** (`area_influencia.shp`).

   Es la ÚNICA referencia disponible en este estudio, porque no se acotó el área
   trazando aguas arriba y no hay superficie drenada con la que contrastar. Y es
   una cota débil: una envolvente con holgura admite delimitaciones varias veces
   mayores de lo debido sin emitir señal. La delimitación debe quedar por debajo
   de ella y por encima del {tolerancia:.0f}% de ella, pero cumplir eso no
   confirma que sea correcta. Verificarla contra la cartografía.
"""

EXPORTAR_AMBAS = """   con los nombres `{subcuencas}` y `{corrientes}`.
"""

EXPORTAR_SOLO_SUBCUENCAS = """   con el nombre `{subcuencas}`.

   **Las corrientes no se exportan.** Por configuración
   (`hec_hms.intercambio.origen_corrientes`), los tramos del modelo salen de
   `red_topologica.shp`, que el M02b ya construyó y que trae orden de Strahler,
   adyacencia y el puente sobre los embalses. La exportación de HEC-HMS no tiene
   nada de eso.
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

    tolerancia = float(configuracion.obtener(
        "hec_hms.intercambio.fraccion_minima_pct"))
    drenada = _referencia_drenada(configuracion, base)
    if drenada:
        referencia = REFERENCIA_DRENADA.format(
            area_drenada=drenada["area_km2"],
            red=drenada["longitud_red_km"],
            radio=drenada["radio_m"],
            densidad=drenada["densidad_km_km2"],
            banda=float(configuracion.obtener(
                "hec_hms.intercambio.banda_area_pct")),
            area_preliminar=area_preliminar,
            tolerancia=tolerancia,
        )
    else:
        referencia = REFERENCIA_SIN_DRENADA.format(
            area_preliminar=area_preliminar, tolerancia=tolerancia)
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "preparar.sin_referencia",
            "no hay superficie drenada con la que contrastar la delimitacion: "
            "el estudio no acoto el area trazando aguas arriba, o falta "
            "red_topologica.shp. Queda solo el area de influencia como cota "
            "superior, y una envolvente con holgura admite delimitaciones "
            "varias veces mayores de lo debido sin emitir ninguna senal.",
        ))

    origen = str(configuracion.obtener(
        "hec_hms.intercambio.origen_corrientes")).strip().lower()
    plantilla_exportar = (EXPORTAR_AMBAS if origen == "hec_hms"
                          else EXPORTAR_SOLO_SUBCUENCAS)

    instructivo = destino / "INSTRUCTIVO.md"
    instructivo.write_text(
        INSTRUCTIVO.format(
            crs=configuracion.obtener("crs.calculo"),
            version=configuracion.obtener("software.hec_hms.version"),
            referencia=referencia,
            exportar=plantilla_exportar.format(
                subcuencas=configuracion.obtener(
                    "hec_hms.intercambio.subcuencas"),
                corrientes=configuracion.obtener(
                    "hec_hms.intercambio.corrientes"),
            ),
            salida=rutas.relativa(salida, base),
        ),
        encoding="utf-8")
    resultado.productos.append(rutas.relativa(instructivo, base))
    resultado.productos.extend(resultado.insumos)

    logger.info("%d insumo(s) en %s", len(resultado.insumos),
                rutas.relativa(destino, base))
    if drenada:
        resultado.verificaciones.append({"prueba": "superficie_drenada",
                                         **drenada})
    referencia_texto = (
        f"superficie drenada de referencia {drenada['area_km2']:.2f} km2 "
        f"(densidad {drenada['densidad_km_km2']:.2f} km/km2) y cota superior "
        f"{area_preliminar:.2f} km2" if drenada else
        f"cota superior {area_preliminar:.2f} km2, sin superficie drenada")
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "preparar.listo",
        f"insumos listos en {rutas.relativa(destino, base)}, {referencia_texto}. "
        f"Corrientes del modelo: {origen}. El siguiente paso es MANUAL: seguir "
        "INSTRUCTIVO.md y despues ejecutar este modulo con --importar.",
    ))


def _referencia_drenada(configuracion, base) -> dict[str, Any] | None:
    """Superficie drenada de referencia, o None si el estudio no la sostiene."""
    try:
        radio = float(configuracion.obtener(
            "red_topologica.radio_cuenca_preliminar_m", 0.0))
    except (ErrorConfiguracion, TypeError, ValueError):
        radio = 0.0
    return superficie_drenada_de_referencia(
        rutas.directorio("sig_vector", base) / "red_topologica.shp",
        rutas.directorio("procesado", base) / "M02_delimitacion.json",
        radio,
    )


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

    # --- De donde salen las corrientes ---------------------------------------
    # Se declara en la configuracion, no se deduce del disco: la misma orden
    # daria resultados distintos segun lo que hubiera alli, y el log no podria
    # explicar cual se uso.
    origen = str(configuracion.obtener(
        "hec_hms.intercambio.origen_corrientes")).strip().lower()
    ruta_red = rutas.directorio("sig_vector", base) / "red_topologica.shp"

    exigidas = [(nombre_sub, ruta_sub)]
    if origen == "hec_hms":
        exigidas.append((nombre_cor, ruta_cor))
    ausentes = [n for n, r in exigidas if not r.is_file()]
    if ausentes:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "importar.ausente",
            f"no se encuentra(n) {ausentes} en {rutas.relativa(salida, base)}. "
            "Ejecutar antes el modo --preparar, seguir INSTRUCTIVO.md y "
            "depositar alli la exportacion de HEC-HMS con esos nombres exactos.",
        ))
        return

    resultado.subcuencas = resumir_capa(ruta_sub)
    capas_a_verificar = [("subcuencas", resultado.subcuencas)]

    if origen == "hec_hms":
        resultado.corrientes = resumir_capa(ruta_cor)
        resultado.corrientes["origen"] = "hec_hms"
        capas_a_verificar.append(("corrientes", resultado.corrientes))
        logger.info("subcuencas: %d entidad(es) | corrientes: %d entidad(es)",
                    resultado.subcuencas["entidades"],
                    resultado.corrientes["entidades"])
    elif ruta_red.is_file():
        resultado.corrientes = resumir_capa(ruta_red)
        resultado.corrientes["origen"] = "red_topologica"
        # La ruta viaja en el reporte para que el M10 y el M13 no tengan que
        # adivinar de que capa salen los tramos: la comunicacion entre modulos
        # es por archivos, no por convencion de nombres.
        resultado.corrientes["ruta"] = rutas.relativa(ruta_red, base)
        capas_a_verificar.append(("corrientes", resultado.corrientes))
        heredada = rutas.directorio("sig_vector", base) / "corrientes.shp"
        if heredada.is_file():
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "importar.corrientes_heredadas",
                f"existe {rutas.relativa(heredada, base)} de una ejecucion "
                "anterior con origen_corrientes 'hec_hms', y ya no se "
                "actualiza. Un modulo que la busque por su nombre usaria una "
                "capa vieja sin ninguna senal. Borrarla o volver a 'hec_hms'.",
            ))
        logger.info("subcuencas: %d entidad(es) | corrientes: %d tramo(s) "
                    "de red_topologica.shp",
                    resultado.subcuencas["entidades"],
                    resultado.corrientes["entidades"])
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "importar.corrientes",
            f"las corrientes del modelo salen de {ruta_red.name} "
            f"({resultado.corrientes['entidades']} tramos), no de la "
            "exportacion de HEC-HMS. Esa red trae orden de Strahler, adyacencia "
            "y el puente sobre los embalses, que la exportacion no tiene. "
            "Declarado en hec_hms.intercambio.origen_corrientes.",
        ))
    else:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "importar.corrientes",
            f"origen_corrientes es 'red_topologica' pero no existe "
            f"{rutas.relativa(ruta_red, base)}. Ejecutar antes el M02b, o "
            "cambiar la clave a 'hec_hms' y exportar las corrientes del paso "
            "manual.",
        ))
        return

    # --- Sistema de referencia ----------------------------------------------
    esperado = str(configuracion.obtener("crs.calculo")).upper()
    for nombre, resumen in capas_a_verificar:
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

    try:
        delimitada = float(shapefile.area_poligonos(ruta_sub)) / 1e6
    except (ErrorFormato, ErrorRutas, TypeError, ValueError) as exc:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "importar.area",
            f"no se pudo medir el area de la delimitacion: {exc}.",
        ))
        return

    # --- Cota superior: el area de influencia --------------------------------
    # NO es una cuenca. Es la envolvente del trazado aguas arriba mas el buffer,
    # o la subzona entera si no se acoto. Contiene la cuenca aportante por
    # definicion, y por eso sirve de tope; pero admite delimitaciones varias
    # veces mayores de lo debido sin emitir senal. El contraste con significado
    # hidrologico es el siguiente.
    cuenca = rutas.directorio("sig_vector", base) / "area_influencia.shp"
    fraccion_minima = float(configuracion.obtener(
        "hec_hms.intercambio.fraccion_minima_pct"))
    if cuenca.is_file():
        try:
            preliminar = float(shapefile.area_poligonos(cuenca)) / 1e6
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
                    "EXCEDE el area de influencia, que la contiene por "
                    "definicion: la delimitacion esta tomando agua de otra "
                    "vertiente")
            if verificacion.get("demasiado_pequena"):
                problemas.append(
                    f"queda por debajo del {fraccion_minima:.0f}% de ella. Es "
                    "el fallo conocido: con el DEM de radar sin reacondicionar, "
                    "el M02 llego a delimitar 6,59 km2 sobre una subzona de "
                    "5.926, porque en terreno plano el ruido vertical gobierna "
                    "las direcciones de flujo. Reacondicionar el DEM con el "
                    "drenaje del IGAC y repetir la delimitacion")
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE if problemas else INFORMATIVO, "importar.area",
                f"area delimitada {delimitada:.2f} km2, el "
                f"{verificacion['fraccion_pct']:.1f}% del area de influencia "
                f"({preliminar:.2f} km2), que es COTA SUPERIOR y no objetivo."
                + ("" if not problemas else " " + ". ".join(problemas) + "."),
            ))

    # --- Contraste con la superficie drenada ---------------------------------
    drenada = _referencia_drenada(configuracion, base)
    if drenada is None:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "importar.sin_referencia",
            "no hay superficie drenada con la que contrastar: el estudio no "
            "acoto el area trazando aguas arriba, o falta red_topologica.shp. "
            "El area delimitada queda verificada solo contra una envolvente con "
            "holgura, que no distingue una cuenca correcta de una tres veces "
            "mayor. Contrastarla contra la cartografia antes del M10.",
        ))
    else:
        banda = float(configuracion.obtener(
            "hec_hms.intercambio.banda_area_pct"))
        contraste = contrastar_con_la_drenada(
            delimitada, drenada["area_km2"], banda)
        contraste["prueba"] = "superficie_drenada"
        contraste["densidad_km_km2"] = drenada["densidad_km_km2"]
        contraste["radio_m"] = drenada["radio_m"]
        resultado.verificaciones.append(contraste)
        fuera = contraste.get("fuera_de_banda")
        desviacion = contraste.get("desviacion_pct")
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA if fuera else INFORMATIVO, "importar.area_drenada",
            f"area delimitada {delimitada:.2f} km2 frente a "
            f"{drenada['area_km2']:.2f} km2 de superficie drenada por la red "
            f"({drenada['longitud_red_km']:.1f} km, densidad "
            f"{drenada['densidad_km_km2']:.2f} km/km2): "
            f"{desviacion:+.1f}% de desviacion."
            + (f" Fuera de la banda del {banda:.0f}%. La superficie drenada no "
               "es una divisoria, de modo que una diferencia moderada es "
               "esperable, pero esta no lo es: revisar si la delimitacion dejo "
               "fuera afluentes que si drenan al punto, o si tomo area de otra "
               "vertiente." if fuera else
               f" Dentro de la banda del {banda:.0f}%. La superficie drenada no "
               "es una divisoria sino una mancha alrededor de los cauces: la "
               "coincidencia confirma el orden de magnitud, no la traza."),
        ))

    # --- Subcuencas diminutas ------------------------------------------------
    minimo = float(configuracion.obtener(
        "hec_hms.intercambio.area_minima_subcuenca_km2"))
    pequenas = subcuencas_pequenas(ruta_sub, minimo)
    if pequenas:
        listado = escribir_pequenas(
            rutas.directorio("procesado", base, crear=True)
            / "M09_subcuencas_pequenas.csv", pequenas)
        resultado.productos.append(rutas.relativa(listado, base))
        menor = min(fila["area_km2"] for fila in pequenas)
        suma = sum(fila["area_km2"] for fila in pequenas)
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "importar.subcuencas_pequenas",
            f"{len(pequenas)} de {resultado.subcuencas['entidades']} subcuenca(s) "
            f"por debajo de {minimo} km2, la menor de {menor:.4f} km2, "
            f"{suma:.2f} km2 en total. Suelen ser artefactos del trazado y no "
            "unidades hidrologicas: arrastrarlas produce un hidrograma sin "
            "sentido fisico y un tiempo de concentracion absurdo. La lista esta "
            f"en {rutas.relativa(listado, base)}. Fusionarlas es decision del "
            "consultor y se hace en HEC-HMS.",
        ))

    # --- Publicacion ---------------------------------------------------------
    destino = rutas.directorio("sig_vector", base, crear=True)
    publicar = [(ruta_sub, "subcuencas")]
    if origen == "hec_hms":
        publicar.append((ruta_cor, "corrientes"))
    for ruta, nombre in publicar:
        for copiado in copiar_capa(ruta, destino):
            if copiado.suffix == ".shp":
                final = destino / f"{nombre}.shp"
                if copiado != final:
                    for extension in ACOMPANANTES:
                        acompanante = copiado.with_suffix(extension)
                        if acompanante.is_file():
                            acompanante.replace(final.with_suffix(extension))
                resultado.productos.append(rutas.relativa(final, base))
                if nombre == "subcuencas":
                    resultado.subcuencas["ruta"] = rutas.relativa(final, base)

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

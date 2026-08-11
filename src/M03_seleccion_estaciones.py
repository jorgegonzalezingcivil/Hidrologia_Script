#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M03 - Selección de estaciones por área de influencia y categoría
================================================================
Entorno: venv del proyecto.

Cruza el Catálogo Nacional de Estaciones del IDEAM con el área de influencia que
entrega el M02 y con las categorías declaradas por variable, y produce el
inventario de estaciones candidatas del estudio.

Cómo se resuelve el cruce sin librerías geoespaciales. El módulo corre en el
venv, que no puede reproyectar. El M02, que sí puede, exporta en su reporte el
área de influencia y el área de selección en EPSG:4326, y el catálogo publica la
ubicación de cada estación en latitud y longitud. Con ambos en el mismo sistema,
la prueba de punto en polígono se resuelve con librería estándar
(comun.geometria). Ninguna reproyección ocurre en este entorno.

Las suspendidas se marcan, no se descartan. El inventario incluye toda estación
que caiga en el área y cuya categoría sirva a alguna variable, con su estado y
sus fechas. El descarte por antigüedad de la suspensión corresponde al M04b, que
es donde CLAUDE.md sitúa el análisis de sensibilidad de longitud de serie: quitar
estaciones antes de ver los datos es decidir sin evidencia.

Productos:
    data/03_SIG/vector/estaciones_seleccionadas.shp
    data/03_SIG/vector/estaciones_descartadas.shp
    data/02_procesado/estaciones/inventario_estaciones.csv
    data/02_procesado/M03_estaciones.json

Uso:
    python src/M03_seleccion_estaciones.py
    python src/M03_seleccion_estaciones.py --area area_influencia

Códigos de salida:
    0  inventario producido
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

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import campos as mod_campos  # noqa: E402
from comun import esquema, geometria, registro, rutas, shapefile  # noqa: E402
from comun.campos import CampoSalida  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M03"
DESCRIPCION = "Selección de estaciones por área de influencia y categoría"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Geometría del reporte del M02 que se usa por defecto para seleccionar. Incluye
# el buffer adicional de estaciones, aplicado allí en metros.
AREA_POR_DEFECTO = "area_estaciones"

MOTIVO_FUERA = "fuera del área"
MOTIVO_CATEGORIA = "categoría sin variable asociada"
MOTIVO_SIN_COORDENADAS = "sin coordenadas utilizables"

CAMPOS_ESTACION: tuple[CampoSalida, ...] = (
    CampoSalida("codigo", "Código de la estación", "texto", 15),
    CampoSalida("nombre", "Nombre de la estación", "texto", 120),
    CampoSalida("categoria", "Categoría", "texto", 6),
    CampoSalida("cat_desc", "Descripción de la categoría", "texto", 60),
    CampoSalida("estado", "Estado de operación", "texto", 24),
    CampoSalida("entidad", "Entidad operadora", "texto", 80),
    CampoSalida("altitud", "Altitud", "decimal", 12, 2, "m"),
    CampoSalida("latitud", "Latitud", "decimal", 14, 8, "grados"),
    CampoSalida("longitud", "Longitud", "decimal", 14, 8, "grados"),
    CampoSalida("corriente", "Corriente asociada", "texto", 80),
    CampoSalida("depto", "Departamento", "texto", 60),
    CampoSalida("municipio", "Municipio", "texto", 60),
    CampoSalida("subzona", "Subzona hidrográfica", "texto", 80),
    CampoSalida("f_instal", "Fecha de instalación", "texto", 12),
    CampoSalida("f_suspen", "Fecha de suspensión", "texto", 12),
    CampoSalida("variables", "Variables a las que sirve", "texto", 80),
    CampoSalida("motivo", "Motivo del descarte", "texto", 40),
    CampoSalida("dif_cota", "Diferencia de cota respecto al punto",
                "decimal", 12, 1, "m"),
    CampoSalida("dist_km", "Distancia al punto de descarga", "decimal",
                12, 3, "km"),
    CampoSalida("apta_cal", "Aptitud para calibrar", "texto", 24),
)


@dataclass
class Estacion:
    """Una estación del catálogo, ya normalizada."""

    valores: dict[str, Any]
    latitud: float | None
    longitud: float | None
    variables: tuple[str, ...] = ()
    en_area: bool = False
    motivo: str = ""

    @property
    def seleccionada(self) -> bool:
        return self.en_area and bool(self.variables)


@dataclass
class ResultadoM03:
    total_catalogo: int = 0
    con_coordenadas: int = 0
    en_area: int = 0
    seleccionadas: list[Estacion] = field(default_factory=list)
    descartadas: list[Estacion] = field(default_factory=list)
    por_variable: dict[str, int] = field(default_factory=dict)
    por_categoria: dict[str, int] = field(default_factory=dict)
    por_estado: dict[str, int] = field(default_factory=dict)
    duplicados: list = field(default_factory=list)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def a_decimal(valor: Any) -> float | None:
    """
    Convierte a número un valor del catálogo, admitiendo coma decimal.

    Devuelve None cuando el valor está vacío o no es interpretable, que es lo
    que debe distinguirse de un cero legítimo: una estación sin coordenada no
    es una estación en el origen.
    """
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        return float(texto.replace(",", "."))
    except ValueError:
        return None


def variables_de_categoria(
    categoria: str, categorias_por_variable: dict[str, Sequence[str]]
) -> tuple[str, ...]:
    """
    Devuelve las variables a las que sirve una categoría de estación.

    Una misma categoría sirve a varias variables: una climática principal aporta
    precipitación, temperatura y evaporación a la vez.
    """
    objetivo = (categoria or "").strip().upper()
    if not objetivo:
        return ()
    return tuple(
        variable for variable, categorias in sorted(categorias_por_variable.items())
        if objetivo in {str(c).strip().upper() for c in categorias}
    )


def leer_area(
    reporte_m02: Path, nombre_geometria: str = AREA_POR_DEFECTO
) -> list[geometria.Poligono]:
    """
    Lee del reporte del M02 la geometría de selección en EPSG:4326.

    Excepciones
    -----------
    ErrorFormato
        Si el reporte no existe, no trae geometrías geográficas o no contiene la
        pedida. Todas son señal de que el M02 no se ejecutó o quedó incompleto.
    """
    if not reporte_m02.is_file():
        raise ErrorFormato(
            f"No existe {reporte_m02.name}. El M03 parte del área de influencia "
            "que entrega el M02: ejecutarlo primero."
        )

    try:
        datos = json.loads(reporte_m02.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ErrorFormato(f"{reporte_m02.name} no es JSON válido: {exc}") from exc

    geometrias = datos.get("geometrias_epsg4326") or {}
    if not geometrias:
        raise ErrorFormato(
            f"{reporte_m02.name} no contiene geometrías en EPSG:4326. Volver a "
            "ejecutar el M02 para que las exporte."
        )

    wkt = geometrias.get(nombre_geometria)
    if not wkt:
        raise ErrorFormato(
            f"{reporte_m02.name} no contiene la geometría {nombre_geometria!r}. "
            f"Disponibles: {', '.join(sorted(geometrias))}."
        )

    return geometria.poligonos_de_wkt(wkt)


def deduplicar_catalogo(
    registros: Iterable[dict[str, str]],
    mapa_campos: dict[str, str],
    precedencia_estado: Sequence[str] = (),
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """
    Deja una fila por estación y devuelve (filas, conflictos).

    El Catálogo Nacional de Estaciones trae la misma estación varias veces.
    Medido sobre el catálogo de este estudio: 4.526 filas para 4.521 códigos,
    con cuatro repetidos. Las repeticiones no son copias iguales, y por eso no
    basta con quedarse con la primera:

        21201180  tres filas, una Activa y dos Suspendidas
        21205880  dos filas con fecha de suspensión distinta
        21201610  Activa con corriente declarada, y En Mantenimiento sin ella
        13085030  PM (pluviométrica) en una fila y CO (climática) en la otra

    El último es el que obliga a combinar en lugar de elegir: la CATEGORÍA
    decide a qué variables sirve la estación, de modo que quedarse con una de
    las dos filas le quitaría variables que sí tiene. Se toma la UNIÓN.

    Para el estado se aplica la precedencia declarada, y el criterio por
    defecto conserva el que mantiene la estación en juego. El argumento es que
    la METADATA se contradice y el DATO no: si la estación no tiene registro
    reciente, el M04b lo verá en la matriz de sensibilidad. Descartarla por una
    ficha inconsistente sería perder una serie por un error de la ficha.

    Sin esta deduplicación, el inventario cuenta filas del catálogo y no
    estaciones, y la capa de puntos escribe la misma estación varias veces:
    una interpolación posterior la pesaría dos o tres veces.
    """
    campo_codigo = mapa_campos.get("codigo", "CODIGO")
    campo_categoria = mapa_campos.get("categoria", "CATEGORIA")
    campo_estado = mapa_campos.get("estado", "")
    orden = [e.strip().upper() for e in precedencia_estado]

    por_codigo: dict[str, list[dict[str, str]]] = {}
    for fila in registros:
        codigo = str(fila.get(campo_codigo, "")).strip()
        por_codigo.setdefault(codigo, []).append(dict(fila))

    salida: list[dict[str, str]] = []
    conflictos: list[dict[str, Any]] = []

    for codigo, filas in por_codigo.items():
        if len(filas) == 1:
            salida.append(filas[0])
            continue

        def rango(fila: dict[str, str]) -> int:
            valor = str(fila.get(campo_estado, "")).strip().upper()
            return orden.index(valor) if valor in orden else len(orden)

        elegida = dict(min(filas, key=rango) if orden and campo_estado
                       else filas[0])

        categorias = [c for c in dict.fromkeys(
            str(f.get(campo_categoria, "")).strip().upper() for f in filas) if c]
        if len(categorias) > 1:
            elegida[campo_categoria] = ",".join(categorias)

        difieren = sorted({
            clave for clave in filas[0]
            if len({str(f.get(clave, "")) for f in filas}) > 1})
        conflictos.append({
            "codigo": codigo,
            "filas": len(filas),
            "campos_en_conflicto": difieren,
            "categorias": categorias,
            "estado_adoptado": str(elegida.get(campo_estado, "")).strip(),
        })
        salida.append(elegida)

    return salida, conflictos


def clasificar(
    registros: Iterable[dict[str, str]],
    mapa_campos: dict[str, str],
    categorias_por_variable: dict[str, Sequence[str]],
    poligonos: Sequence[geometria.Poligono],
    resultado: ResultadoM03,
) -> None:
    """
    Recorre el catálogo y separa las estaciones seleccionadas de las descartadas.

    Se aplica primero la envolvente y solo después el punto en polígono: sobre un
    catálogo de miles de estaciones y un área pequeña, esa comprobación barata
    descarta la inmensa mayoría sin evaluar aristas.
    """
    x_min, y_min, x_max, y_max = geometria.envolvente(poligonos)

    for fila in registros:
        resultado.total_catalogo += 1

        latitud = a_decimal(fila.get(mapa_campos["latitud"]))
        longitud = a_decimal(fila.get(mapa_campos["longitud"]))
        categoria = (fila.get(mapa_campos["categoria"]) or "").strip().upper()

        estacion = Estacion(
            valores=_normalizar(fila, mapa_campos),
            latitud=latitud, longitud=longitud,
            variables=variables_de_categoria(categoria, categorias_por_variable),
        )

        if latitud is None or longitud is None:
            estacion.motivo = MOTIVO_SIN_COORDENADAS
            resultado.descartadas.append(estacion)
            continue

        resultado.con_coordenadas += 1

        dentro_envolvente = (x_min <= longitud <= x_max and y_min <= latitud <= y_max)
        estacion.en_area = dentro_envolvente and geometria.punto_en_alguno(
            longitud, latitud, poligonos
        )

        if not estacion.en_area:
            # Solo se conservan como descartadas las que caen en la envolvente:
            # listar las 4500 estaciones del país no aportaría nada al informe.
            if dentro_envolvente:
                estacion.motivo = MOTIVO_FUERA
                resultado.descartadas.append(estacion)
            continue

        resultado.en_area += 1

        if not estacion.variables:
            estacion.motivo = MOTIVO_CATEGORIA
            resultado.descartadas.append(estacion)
            continue

        resultado.seleccionadas.append(estacion)
        for variable in estacion.variables:
            resultado.por_variable[variable] = \
                resultado.por_variable.get(variable, 0) + 1
        resultado.por_categoria[categoria] = \
            resultado.por_categoria.get(categoria, 0) + 1
        estado = str(estacion.valores.get("estado") or "sin declarar")
        resultado.por_estado[estado] = resultado.por_estado.get(estado, 0) + 1


def _normalizar(fila: dict[str, str], mapa_campos: dict[str, str]) -> dict[str, Any]:
    """Traduce los nombres del catálogo a los campos cortos de salida."""
    def leer(clave: str) -> str:
        return (fila.get(mapa_campos[clave]) or "").strip()

    return {
        "codigo": leer("codigo"),
        "nombre": leer("nombre"),
        "categoria": leer("categoria").upper(),
        "cat_desc": leer("categoria_desc"),
        "estado": leer("estado"),
        "entidad": leer("entidad"),
        "altitud": a_decimal(leer("altitud")),
        "latitud": a_decimal(leer("latitud")),
        "longitud": a_decimal(leer("longitud")),
        "corriente": leer("corriente"),
        "depto": leer("departamento"),
        "municipio": leer("municipio"),
        "subzona": leer("subzona"),
        "f_instal": leer("fecha_instalacion"),
        "f_suspen": leer("fecha_suspension"),
    }


def verificar_categorias(
    categorias_por_variable: dict[str, Sequence[str]],
    presentes: set[str],
) -> list[Hallazgo]:
    """Advierte sobre categorías declaradas que el catálogo no contiene."""
    hallazgos: list[Hallazgo] = []
    for variable, categorias in sorted(categorias_por_variable.items()):
        ausentes = [
            c for c in categorias
            if str(c).strip().upper() not in presentes
        ]
        if ausentes:
            hallazgos.append(Hallazgo(
                ADVERTENCIA, f"estaciones.categorias_por_variable.{variable}",
                f"la(s) categoría(s) {', '.join(ausentes)} no existen en el "
                "catálogo, de modo que no seleccionarán ninguna estación.",
            ))
    return hallazgos



# =============================================================================
# Aptitud para calibrar
# =============================================================================
# Categorias que miden caudal o nivel. Solo ellas pueden servir a la
# calibracion del M14b.
CATEGORIAS_CAUDAL = ("LG", "LM", "RM")

NO_APTA_COTA = "no: aguas abajo"
NO_APTA_TIPO = "no mide caudal"
DUDOSA = "verificar con la red"
DUDOSA_BANDA = "en la banda: verificar"


def evaluar_aptitud_calibracion(
    estacion: "Estacion",
    longitud_punto: float,
    latitud_punto: float,
    cota_punto: float | None,
    banda_cota_m: float = 30.0,
) -> tuple[float | None, float | None, str]:
    """
    Indica si una estacion puede servir para calibrar el modelo del punto.

    Devuelve (diferencia de cota, distancia en km, aptitud).

    La condicion necesaria es estar aguas arriba del punto de descarga: una
    estacion aguas abajo mide una cuenca que contiene a la del estudio y la
    supera, de modo que sus caudales son de otro orden. Verificado en este
    proyecto: de cuatro estaciones de caudal dentro del area de influencia,
    tres estaban 2.100 metros por debajo del punto, al otro lado de un salto,
    y ninguna servia.

    El criterio es la cota, que NO decide por si sola en ninguno de los dos
    sentidos:

    - Una estacion mas alta puede pertenecer a otra vertiente, de modo que no se
      declara apta sino 'verificar con la red'.
    - Una estacion que aparece mas baja puede estar aguas arriba, si la
      diferencia cae dentro del ruido del modelo de elevacion. Por eso existe la
      banda: dentro de ella tampoco se descarta.

    La banda no es una cautela teorica. La estacion 2120700146 aparecia 15 m por
    debajo del punto y estaba 6,26 km AGUAS ARRIBA sobre el mismo cauce: en
    terreno plano el desnivel real del rio es menor que la incertidumbre
    vertical del DEM de radar. Sin banda, ese caso se descartaba en silencio.

    La confirmacion definitiva exige el trazado de la red de drenaje, que vive
    en red_drenaje y corre en el entorno de QGIS.
    """
    if (estacion.valores.get("categoria") or "").strip().upper() \
            not in CATEGORIAS_CAUDAL:
        return None, None, NO_APTA_TIPO

    distancia = None
    if estacion.longitud is not None and estacion.latitud is not None:
        # Aproximacion suficiente para ordenar por cercania: un grado de
        # latitud son unos 111 km, y la longitud se corrige por el coseno.
        import math

        dy = (estacion.latitud - latitud_punto) * 111.0
        dx = ((estacion.longitud - longitud_punto) * 111.0
              * math.cos(math.radians(latitud_punto)))
        distancia = math.hypot(dx, dy)

    altitud = estacion.valores.get("altitud")
    if altitud is None or cota_punto is None:
        return None, distancia, DUDOSA

    diferencia = float(altitud) - float(cota_punto)
    if diferencia < -abs(banda_cota_m):
        return diferencia, distancia, NO_APTA_COTA
    if diferencia < 0:
        # Por debajo, pero dentro del ruido del modelo: no se descarta.
        return diferencia, distancia, DUDOSA_BANDA
    return diferencia, distancia, DUDOSA


def revisar_calibracion(
    seleccionadas, longitud_punto, latitud_punto, cota_punto,
) -> list[Hallazgo]:
    """
    Evalua todas las estaciones y reporta si la calibracion es viable.

    Responde de oficio a una pregunta que todo estudio debe hacerse y que hoy
    solo se descubria al llegar al M14b: hay estaciones de caudal utilizables?
    CLAUDE.md, seccion 6, declara la calibracion condicional a que existan
    series utilizables, y esta comprobacion es la que lo determina.
    """
    de_caudal = [e for e in seleccionadas
                 if (e.valores.get("categoria") or "").strip().upper()
                 in CATEGORIAS_CAUDAL]

    if not de_caudal:
        return [Hallazgo(
            INFORMATIVO, "calibracion",
            "no hay estaciones de caudal o nivel en el area. La calibracion "
            "del M14b no aplica (CLAUDE.md, seccion 6).",
        )]

    posibles = [e for e in de_caudal
                if e.valores.get("apta_cal") in (DUDOSA, DUDOSA_BANDA)]
    en_banda = [e for e in de_caudal
                if e.valores.get("apta_cal") == DUDOSA_BANDA]
    abajo = len(de_caudal) - len(posibles)

    hallazgos = [Hallazgo(
        INFORMATIVO, "calibracion",
        f"{len(de_caudal)} estacion(es) de caudal o nivel en el area: "
        f"{abajo} descartada(s) por estar aguas abajo del punto y "
        f"{len(posibles)} por verificar con la red de drenaje, "
        f"de las cuales {len(en_banda)} caen dentro de la banda de "
        "incertidumbre del modelo de elevación.",
    )]

    if not posibles:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "calibracion",
            "todas las estaciones de caudal del area estan aguas abajo del "
            "punto de descarga: miden cuencas que contienen a la del estudio. "
            "La calibracion del M14b no tiene base y debe documentarse.",
        ))
    return hallazgos


# =============================================================================
# Escritura de productos
# =============================================================================
def _fila_salida(estacion: Estacion) -> dict[str, Any]:
    fila = dict(estacion.valores)
    fila["variables"] = ";".join(estacion.variables)
    fila["motivo"] = estacion.motivo
    return fila


def escribir_capa(
    destino: Path, estaciones: Sequence[Estacion], wkt_crs: str
) -> Path:
    """Escribe una capa de puntos con el inventario indicado."""
    puntos = [(e.longitud or 0.0, e.latitud or 0.0) for e in estaciones]
    valores = [_fila_salida(e) for e in estaciones]
    return shapefile.escribir_puntos(
        destino=destino, puntos=puntos, campos=CAMPOS_ESTACION,
        valores=valores, wkt_crs=wkt_crs,
    )


def escribir_inventario(
    destino: Path, estaciones: Sequence[Estacion], delimitador: str
) -> Path:
    """
    Escribe el inventario en CSV con los nombres descriptivos.

    El shapefile conserva los nombres cortos de diez caracteres; el CSV es el
    que va al informe y al Excel, y por eso usa la columna descriptiva.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.writer(manejador, delimiter=delimitador)
        escritor.writerow([campo.descriptivo for campo in CAMPOS_ESTACION])
        for estacion in estaciones:
            fila = _fila_salida(estacion)
            escritor.writerow([
                fila.get(campo.corto, "") for campo in CAMPOS_ESTACION
            ])
    return destino


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    nombre_area: str = AREA_POR_DEFECTO,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Cruza el catálogo con el área y escribe el inventario."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)

    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    ruta_catalogo = configuracion.ruta_de("estaciones.catalogo", debe_existir=True)
    reporte_m02 = rutas.directorio("procesado", base) / "M02_delimitacion.json"
    mapa_campos = dict(configuracion.obtener("estaciones.campos"))
    categorias = {
        variable: list(valores) for variable, valores
        in configuracion.obtener("estaciones.categorias_por_variable").items()
    }

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "catálogo": rutas.relativa(ruta_catalogo, base),
            "fecha del catálogo": configuracion.obtener("estaciones.fecha_catalogo"),
            "área de selección": f"{reporte_m02.name} :: {nombre_area}",
        },
        parametros=configuracion.parametros((
            "estaciones.buffer_adicional_km",
            "estaciones.categorias_por_variable",
            "estaciones.fecha_catalogo",
        )),
    )

    resultado = ResultadoM03()

    with registro.bloque(logger, "Lectura del área de selección"):
        poligonos = leer_area(reporte_m02, nombre_area)
        x_min, y_min, x_max, y_max = geometria.envolvente(poligonos)
        logger.info(
            "Área en EPSG:4326: lon %.6f a %.6f, lat %.6f a %.6f (%d polígono(s))",
            x_min, x_max, y_min, y_max, len(poligonos),
        )

    with registro.bloque(logger, "Verificación del catálogo"):
        info = shapefile.leer_shapefile(ruta_catalogo)
        faltantes = [
            nombre for nombre in mapa_campos.values()
            if not info.tiene_campo(nombre)
        ]
        if faltantes:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "estaciones.campos",
                f"el catálogo no tiene el/los campo(s) {', '.join(faltantes)}. "
                f"Disponibles: {', '.join(info.nombres_campos)}.",
            ))
            return _cerrar(logger, resultado, base, ruta_json, inicio,
                           SALIDA_BLOQUEANTE)

        presentes = {
            v.strip().upper()
            for v in shapefile.valores_unicos(ruta_catalogo, mapa_campos["categoria"])
        }
        resultado.hallazgos.extend(verificar_categorias(categorias, presentes))
        logger.info(
            "Catálogo con %d estación(es) y %d categoría(s) distintas.",
            info.n_registros, len(presentes),
        )

    with registro.bloque(logger, "Cruce por área y categoría"):
        filas, conflictos = deduplicar_catalogo(
            shapefile.leer_registros(ruta_catalogo), mapa_campos,
            configuracion.obtener("estaciones.precedencia_estado", ()))
        if conflictos:
            detalle = "; ".join(
                f"{c['codigo']} ({c['filas']} filas, difieren "
                f"{', '.join(c['campos_en_conflicto'][:3])})"
                for c in conflictos[:6])
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "catalogo.duplicados",
                f"{len(conflictos)} estacion(es) aparecen mas de una vez en el "
                f"Catalogo Nacional y se consolidaron en una sola fila: "
                f"{detalle}. Sin consolidarlas, el inventario contaria filas y "
                "no estaciones, y la capa de puntos las escribiria repetidas: "
                "una interpolacion posterior las pesaria varias veces. La "
                "categoria se toma como UNION, porque decide a que variables "
                "sirve la estacion; el estado, por la precedencia declarada.",
            ))
            resultado.duplicados = conflictos
        clasificar(
            registros=filas,
            mapa_campos=mapa_campos,
            categorias_por_variable=categorias,
            poligonos=poligonos,
            resultado=resultado,
        )
        logger.info(
            "Catálogo: %d | con coordenadas: %d | dentro del área: %d | "
            "seleccionadas: %d",
            resultado.total_catalogo, resultado.con_coordenadas,
            resultado.en_area, len(resultado.seleccionadas),
        )

    if not resultado.seleccionadas:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "estaciones",
            "ninguna estación del catálogo cae en el área con una categoría "
            "aplicable. Revisar el área del M02 y las categorías declaradas.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Aptitud para calibrar"):
        punto = _punto_del_m01(base)
        for estacion in resultado.seleccionadas:
            dif, dist, apta = evaluar_aptitud_calibracion(
                estacion, punto["longitud"], punto["latitud"], punto["cota"],
                float(configuracion.obtener("estaciones.banda_cota_m")))
            estacion.valores["dif_cota"] = dif
            estacion.valores["dist_km"] = dist
            estacion.valores["apta_cal"] = apta
        for estacion in resultado.descartadas:
            estacion.valores.setdefault("dif_cota", None)
            estacion.valores.setdefault("dist_km", None)
            estacion.valores.setdefault("apta_cal", "")
        if punto["cota"] is None:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, "calibracion.cota",
                f"sin cota del punto de descarga ({punto['motivo_sin_cota']}): "
                "no se puede descartar por posición y todas las estaciones "
                "quedan como 'verificar con la red'. El resultado NO significa "
                "que sean aptas.",
            ))
        resultado.hallazgos.extend(revisar_calibracion(
            resultado.seleccionadas, punto["longitud"], punto["latitud"],
            punto["cota"]))

    with registro.bloque(logger, "Escritura del inventario"):
        _escribir_productos(configuracion, base, info, resultado, logger)

    resultado.hallazgos.extend(_resumir(resultado, configuracion))

    codigo = (SALIDA_BLOQUEANTE if esquema.hay_bloqueantes(resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)



def _punto_del_m01(base: Path) -> dict:
    """
    Lee del reporte del M01 la posicion del punto de descarga.

    La cota no la produce el M01, de modo que se toma del DEM del M02 si esta
    disponible. Sin ella la comparacion de alturas no se puede hacer y las
    estaciones quedan como 'verificar con la red' en lugar de descartarse.
    """
    ruta = rutas.directorio("procesado", base) / "M01_punto_descarga.json"
    if not ruta.is_file():
        raise ErrorFormato(
            f"No existe {ruta.name}. La aptitud para calibrar se evalua "
            "respecto al punto de descarga: ejecutar el M01 primero."
        )
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    geo = (datos.get("resultado") or {}).get("geografico_4326") or {}
    calc = (datos.get("resultado") or {}).get("calculo") or {}

    # La cota del punto sale del DEM del M02. El venv no tiene GDAL, de modo que
    # se toma del propio reporte del M02 si la publicó, y si no del atributo de
    # la capa de cuenca. Nunca se silencia el motivo del fallo: sin cota, la
    # comparación de alturas no se hace y TODAS las estaciones quedan como
    # 'verificar con la red', lo que parece un resultado pero no lo es.
    cota, motivo = None, ""
    ruta_m02 = rutas.directorio("procesado", base) / "M02_delimitacion.json"
    if ruta_m02.is_file():
        m02 = json.loads(ruta_m02.read_text(encoding="utf-8"))
        cota = (m02.get("punto") or {}).get("cota_m")
        if cota is None:
            motivo = ("el reporte del M02 no publica la cota del punto de "
                      "descarga")
    else:
        motivo = f"no existe {ruta_m02.name}"

    return {"longitud": geo.get("longitud"), "latitud": geo.get("latitud"),
            "cota": cota, "motivo_sin_cota": motivo}

def _escribir_productos(configuracion, base, info, resultado, logger) -> None:
    """Escribe las dos capas, el inventario y los diccionarios de campos."""
    wkt_crs = info.crs_wkt or ""
    if not wkt_crs:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "estaciones.catalogo",
            "el catálogo no trae .prj legible; las capas de salida se escriben "
            "sin sistema de referencia declarado.",
        ))

    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")

    for clave, estaciones in (
        ("estaciones.salida_seleccionadas", resultado.seleccionadas),
        ("estaciones.salida_descartadas", resultado.descartadas),
    ):
        destino = rutas.resolver(configuracion.obtener(clave), base)
        escribir_capa(destino, estaciones, wkt_crs)
        resultado.productos.append(rutas.relativa(destino, base))
        mod_campos.escribir_diccionario(
            CAMPOS_ESTACION,
            destino.with_name(f"{destino.stem}_campos.csv"),
            destino.stem, delimitador,
        )
        logger.info("%s: %d entidad(es)", destino.name, len(estaciones))

    inventario = rutas.resolver(
        configuracion.obtener("estaciones.inventario_csv"), base
    )
    escribir_inventario(inventario, resultado.seleccionadas, delimitador)
    resultado.productos.append(rutas.relativa(inventario, base))


def _resumir(resultado: ResultadoM03, configuracion: Config) -> list[Hallazgo]:
    """Emite los informativos y advertencias del inventario obtenido."""
    hallazgos: list[Hallazgo] = []

    for variable in sorted(configuracion.obtener("estaciones.categorias_por_variable")):
        cuantas = resultado.por_variable.get(variable, 0)
        severidad = INFORMATIVO if cuantas else ADVERTENCIA
        hallazgos.append(Hallazgo(
            severidad, f"estaciones.{variable}",
            f"{cuantas} estación(es) disponibles para {variable}."
            + ("" if cuantas else " Esa variable no podrá analizarse."),
        ))

    suspendidas = resultado.por_estado.get("Suspendida", 0)
    if suspendidas:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "estaciones.estado",
            f"{suspendidas} de {len(resultado.seleccionadas)} estación(es) "
            "seleccionadas están suspendidas. Se conservan en el inventario: el "
            "descarte por antigüedad lo decide el M04b con la matriz de "
            "sensibilidad.",
        ))

    if resultado.total_catalogo and not resultado.con_coordenadas:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "estaciones.campos",
            "ninguna estación del catálogo tiene coordenadas legibles. Revisar "
            "los campos de latitud y longitud declarados.",
        ))

    return hallazgos


def _cerrar(logger, resultado: ResultadoM03, base: Path, ruta_json: Path | None,
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

    productos: dict[str, Any] = {
        f"producto {i}": p for i, p in enumerate(resultado.productos, start=1)
    }
    if resultado.seleccionadas:
        productos["estaciones seleccionadas"] = len(resultado.seleccionadas)

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M03_estaciones.json"

    reporte = {
        "modulo": MODULO,
        "catalogo": {
            "total": resultado.total_catalogo,
            "con_coordenadas": resultado.con_coordenadas,
            "dentro_del_area": resultado.en_area,
        },
        "seleccionadas": len(resultado.seleccionadas),
        "descartadas": len(resultado.descartadas),
        "por_variable": resultado.por_variable,
        "por_categoria": resultado.por_categoria,
        "por_estado": resultado.por_estado,
        "codigos": sorted(
            str(e.valores.get("codigo") or "") for e in resultado.seleccionadas
        ),
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(
        json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8"
    )
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
        prog="M03_seleccion_estaciones.py",
        description="Selecciona estaciones por área de influencia y categoría.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument(
        "--area", default=AREA_POR_DEFECTO,
        help="Geometría del reporte del M02 a usar: area_estaciones, "
             "area_influencia, envolvente o cuenca_preliminar.",
    )
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            nombre_area=argumentos.area, ruta_json=argumentos.json_salida,
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

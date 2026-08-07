#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M01 - Punto de descarga e intersección con subzonas hidrográficas
=================================================================
Entorno: Python de QGIS.

Toma el punto de descarga declarado en config.yaml, lo reproyecta de forma
explícita y determina la subzona hidrográfica que lo contiene, con toda su
jerarquía (área, zona, subzona).

Entrada de coordenadas. El punto se declara en el CRS que el consultor tenga a
mano, no en uno fijo:

    punto_descarga:
      crs: "EPSG:3116"
      x: 1003512.4      # Este en proyectado, longitud en geográfico
      y: 1025917.54     # Norte en proyectado, latitud en geográfico

El módulo reproyecta a EPSG:9377 para el cálculo y al CRS de la capa de
subzonas para la intersección. Ninguna reproyección es implícita (CLAUDE.md,
sección 5).

Punto fuera de toda subzona. Un punto puede caer milimétricamente fuera del
polígono si la descarga está sobre el borde, que es el caso normal de una
desembocadura. Se admite hasta subzonas_hidrograficas.tolerancia_m de distancia,
asignando la subzona más cercana con advertencia explícita y registro de la
distancia. Más allá de esa tolerancia el módulo se detiene: la causa casi
siempre es un CRS o un orden de coordenadas equivocado.

Productos:
    data/03_SIG/vector/punto_descarga.shp
    data/03_SIG/vector/subzona_intersectada.shp
    el diccionario de campos de cada capa
    data/02_procesado/M01_punto_descarga.json

Uso:
    "C:/Program Files/QGIS 4.2.0/bin/python-qgis.bat" src/M01_punto_descarga.py

Códigos de salida:
    0  el punto se ubicó y las capas se escribieron
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o inicializar QGIS
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import sig  # noqa: E402
from comun import campos as mod_campos  # noqa: E402
from comun import entorno, esquema, registro, rutas  # noqa: E402
from comun.campos import CampoSalida  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorEntorno,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M01"
DESCRIPCION = "Punto de descarga e intersección con subzonas hidrográficas"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Envolvente aproximada del territorio continental e insular colombiano, en
# grados decimales. Se usa tras reproyectar a EPSG:4326.
ENVOLVENTE_COLOMBIA = (-82.0, -4.5, -66.5, 13.5)

METODO_CONTIENE = "contiene"
METODO_BORDE = "borde"
METODO_CERCANA = "mas_cercana"

# --- Campos de las capas de salida -------------------------------------------
# Nombres cortos de máximo 10 caracteres, con su equivalente descriptivo para el
# informe y el Excel (CLAUDE.md, sección 5).
CAMPOS_PUNTO: tuple[CampoSalida, ...] = (
    CampoSalida("nombre", "Nombre del punto de descarga", "texto", 60),
    CampoSalida("x_origen", "Coordenada X declarada por el consultor", "decimal",
                20, 4),
    CampoSalida("y_origen", "Coordenada Y declarada por el consultor", "decimal",
                20, 4),
    CampoSalida("crs_origen", "CRS en que se declararon las coordenadas", "texto",
                20),
    CampoSalida("longitud", "Longitud geográfica", "decimal", 20, 8, "grados"),
    CampoSalida("latitud", "Latitud geográfica", "decimal", 20, 8, "grados"),
    CampoSalida("x_calculo", "Este en el CRS de cálculo", "decimal", 20, 3, "m"),
    CampoSalida("y_calculo", "Norte en el CRS de cálculo", "decimal", 20, 3, "m"),
    CampoSalida("cod_ah", "Código del área hidrográfica", "texto", 10),
    CampoSalida("nom_ah", "Nombre del área hidrográfica", "texto", 60),
    CampoSalida("cod_zh", "Código de la zona hidrográfica", "texto", 10),
    CampoSalida("nom_zh", "Nombre de la zona hidrográfica", "texto", 60),
    CampoSalida("cod_szh", "Código de la subzona hidrográfica", "texto", 10),
    CampoSalida("nom_szh", "Nombre de la subzona hidrográfica", "texto", 80),
    CampoSalida("metodo", "Criterio de asignación de la subzona", "texto", 20),
    CampoSalida("dist_m", "Distancia del punto al borde de la subzona",
                "decimal", 20, 3, "m"),
)

CAMPOS_SUBZONA: tuple[CampoSalida, ...] = (
    CampoSalida("cod_ah", "Código del área hidrográfica", "texto", 10),
    CampoSalida("nom_ah", "Nombre del área hidrográfica", "texto", 60),
    CampoSalida("cod_zh", "Código de la zona hidrográfica", "texto", 10),
    CampoSalida("nom_zh", "Nombre de la zona hidrográfica", "texto", 60),
    CampoSalida("cod_szh", "Código de la subzona hidrográfica", "texto", 10),
    CampoSalida("nom_szh", "Nombre de la subzona hidrográfica", "texto", 80),
    CampoSalida("area_km2", "Área de la subzona", "decimal", 20, 4, "km2"),
    CampoSalida("perim_km", "Perímetro de la subzona", "decimal", 20, 4, "km"),
)


@dataclass
class Ubicacion:
    """Resultado de ubicar el punto dentro de la zonificación hidrográfica."""

    x_origen: float
    y_origen: float
    crs_origen: str
    longitud: float
    latitud: float
    x_calculo: float
    y_calculo: float
    metodo: str = ""
    distancia_m: float = 0.0
    atributos: dict[str, str] = field(default_factory=dict)
    area_km2: float = 0.0
    perimetro_km: float = 0.0
    # Geometría de la subzona, ya reproyectada al CRS de cálculo. Se adjunta en
    # una segunda pasada y no participa de la serialización a JSON.
    geometria_subzona: Any = None

    def como_dict(self) -> dict[str, Any]:
        return {
            "coordenadas_declaradas": {
                "x": self.x_origen, "y": self.y_origen, "crs": self.crs_origen,
            },
            "geografico_4326": {"longitud": self.longitud, "latitud": self.latitud},
            "calculo": {"x": self.x_calculo, "y": self.y_calculo},
            "asignacion": {"metodo": self.metodo, "distancia_m": self.distancia_m},
            "jerarquia": dict(self.atributos),
            "subzona": {"area_km2": self.area_km2, "perimetro_km": self.perimetro_km},
        }


@dataclass
class ResultadoM01:
    ubicacion: Ubicacion | None = None
    capas_escritas: list[str] = field(default_factory=list)
    diccionarios: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# El ciclo de vida de QGIS y la escritura de capas viven en src/sig.py, que los
# comparte con los demás módulos del entorno SIG.
iniciar_qgis = sig.iniciar_qgis
finalizar_qgis = sig.finalizar_qgis
reescribir_prj_con_autoridad = sig.reescribir_prj_con_autoridad


# =============================================================================
# Funciones puras
# =============================================================================
def dentro_de_colombia(longitud: float, latitud: float) -> bool:
    """Indica si unas coordenadas geográficas caen en la envolvente del país."""
    lon_min, lat_min, lon_max, lat_max = ENVOLVENTE_COLOMBIA
    return lon_min <= longitud <= lon_max and lat_min <= latitud <= lat_max


def normalizar_codigo(valor: Any) -> str:
    """
    Convierte a texto el código de una subzona conservando su forma.

    Los códigos del IDEAM se publican como enteros, de modo que 2120 llega como
    número. Se guardan como texto porque son identificadores, no cantidades: un
    código con cero a la izquierda no debe perderlo, y ninguna operación
    aritmética sobre ellos tiene sentido.
    """
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def verificar_mapeo_campos(
    disponibles: Sequence[str], declarados: dict[str, str]
) -> list[Hallazgo]:
    """Comprueba que los campos declarados en config existan en la capa."""
    presentes = {nombre.upper() for nombre in disponibles}
    hallazgos: list[Hallazgo] = []
    for clave, nombre in sorted(declarados.items()):
        if nombre.upper() not in presentes:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"subzonas_hidrograficas.campos.{clave}",
                f"el campo {nombre!r} no existe en la capa. Campos disponibles: "
                f"{', '.join(sorted(disponibles))}.",
            ))
    return hallazgos


# =============================================================================
# Ubicación del punto (requiere QGIS)
# =============================================================================
def ubicar_punto(
    configuracion: Config,
    ruta_subzonas: Path,
    logger: Any = None,
) -> tuple[Ubicacion | None, list[Hallazgo]]:
    """
    Reproyecta el punto y determina la subzona hidrográfica que lo contiene.

    Devuelve (ubicacion, hallazgos). La ubicación es None si el punto no se pudo
    asignar a ninguna subzona dentro de la tolerancia.
    """
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsFeatureRequest,
        QgsGeometry,
        QgsPointXY,
        QgsProject,
        QgsRectangle,
        QgsVectorLayer,
    )

    hallazgos: list[Hallazgo] = []

    x = configuracion.requerir("punto_descarga.x")
    y = configuracion.requerir("punto_descarga.y")
    crs_origen_id = configuracion.requerir("punto_descarga.crs")
    crs_calculo_id = configuracion.obtener("crs.calculo")
    tolerancia = float(configuracion.obtener("subzonas_hidrograficas.tolerancia_m"))
    declarados = dict(configuracion.obtener("subzonas_hidrograficas.campos"))

    crs_origen = QgsCoordinateReferenceSystem(crs_origen_id)
    if not crs_origen.isValid():
        return None, [Hallazgo(
            BLOQUEANTE, "punto_descarga.crs",
            f"QGIS no reconoce el CRS {crs_origen_id!r}.",
        )]

    crs_calculo = QgsCoordinateReferenceSystem(crs_calculo_id)
    crs_geografico = QgsCoordinateReferenceSystem(
        configuracion.obtener("crs.geografico")
    )

    capa = QgsVectorLayer(str(ruta_subzonas), "subzonas", "ogr")
    if not capa.isValid():
        return None, [Hallazgo(
            BLOQUEANTE, "subzonas_hidrograficas.archivo",
            f"QGIS no pudo abrir la capa: {ruta_subzonas}",
        )]
    if not capa.crs().isValid():
        return None, [Hallazgo(
            BLOQUEANTE, "subzonas_hidrograficas.archivo",
            f"{ruta_subzonas.name} no tiene un CRS utilizable. Sin .prj válido "
            "la intersección no es defendible.",
        )]

    hallazgos.extend(verificar_mapeo_campos(
        [c.name() for c in capa.fields()], declarados
    ))
    if esquema.hay_bloqueantes(hallazgos):
        return None, hallazgos

    contexto = QgsProject.instance().transformContext()
    punto_origen = QgsPointXY(float(x), float(y))
    a_geografico = QgsCoordinateTransform(crs_origen, crs_geografico, contexto)
    a_calculo = QgsCoordinateTransform(crs_origen, crs_calculo, contexto)
    a_capa = QgsCoordinateTransform(crs_origen, capa.crs(), contexto)

    try:
        geografico = a_geografico.transform(punto_origen)
        calculo = a_calculo.transform(punto_origen)
        en_capa = a_capa.transform(punto_origen)
    except Exception as exc:
        return None, hallazgos + [Hallazgo(
            BLOQUEANTE, "punto_descarga",
            f"no se pudo reproyectar el punto desde {crs_origen_id}: {exc}",
        )]

    if logger is not None:
        logger.info(
            "Punto %s -> EPSG:4326 lon=%.6f lat=%.6f -> %s E=%.2f N=%.2f",
            crs_origen_id, geografico.x(), geografico.y(), crs_calculo_id,
            calculo.x(), calculo.y(),
        )

    if not dentro_de_colombia(geografico.x(), geografico.y()):
        return None, hallazgos + [Hallazgo(
            BLOQUEANTE, "punto_descarga",
            f"el punto declarado ({x}, {y}) en {crs_origen_id} corresponde a "
            f"lon={geografico.x():.6f} lat={geografico.y():.6f}, fuera de "
            "Colombia. Revisar el CRS declarado y el orden de x e y.",
        )]

    ubicacion = Ubicacion(
        x_origen=float(x), y_origen=float(y), crs_origen=crs_origen_id,
        longitud=geografico.x(), latitud=geografico.y(),
        x_calculo=calculo.x(), y_calculo=calculo.y(),
    )

    geometria_punto = QgsGeometry.fromPointXY(en_capa)
    entidad_elegida, metodo, distancia = _buscar_subzona(
        capa=capa, geometria_punto=geometria_punto, punto_calculo=calculo,
        crs_calculo=crs_calculo, contexto=contexto, tolerancia_m=tolerancia,
    )

    if entidad_elegida is None:
        return None, hallazgos + [Hallazgo(
            BLOQUEANTE, "punto_descarga",
            f"el punto no cae en ninguna subzona ni hay ninguna a menos de "
            f"{tolerancia:g} m. Revisar el CRS declarado ({crs_origen_id}) y el "
            "orden de las coordenadas.",
        )]

    ubicacion.metodo = metodo
    ubicacion.distancia_m = distancia
    ubicacion.atributos = {
        "cod_ah": normalizar_codigo(entidad_elegida[declarados["codigo_ah"]]),
        "nom_ah": str(entidad_elegida[declarados["nombre_ah"]] or "").strip(),
        "cod_zh": normalizar_codigo(entidad_elegida[declarados["codigo_zh"]]),
        "nom_zh": str(entidad_elegida[declarados["nombre_zh"]] or "").strip(),
        "cod_szh": normalizar_codigo(entidad_elegida[declarados["codigo_szh"]]),
        "nom_szh": str(entidad_elegida[declarados["nombre_szh"]] or "").strip(),
    }

    geometria_subzona = entidad_elegida.geometry()
    geometria_metrica = QgsGeometry(geometria_subzona)
    geometria_metrica.transform(
        QgsCoordinateTransform(capa.crs(), crs_calculo, contexto)
    )
    ubicacion.area_km2 = geometria_metrica.area() / 1_000_000.0
    ubicacion.perimetro_km = geometria_metrica.length() / 1000.0

    if metodo == METODO_BORDE:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "punto_descarga",
            "el punto cae exactamente sobre el borde de la subzona "
            f"{ubicacion.atributos['cod_szh']}. Verificar que sea la subzona "
            "correcta y no la vecina.",
        ))
    elif metodo == METODO_CERCANA:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "punto_descarga",
            f"el punto cae fuera de toda subzona, a {distancia:.2f} m de la "
            f"{ubicacion.atributos['cod_szh']} ({ubicacion.atributos['nom_szh']}), "
            f"dentro de la tolerancia de {tolerancia:g} m. Se asigna esa subzona "
            "y la decisión queda registrada.",
        ))

    hallazgos.append(Hallazgo(
        INFORMATIVO, "punto_descarga",
        f"subzona {ubicacion.atributos['cod_szh']} "
        f"({ubicacion.atributos['nom_szh']}), zona "
        f"{ubicacion.atributos['cod_zh']} ({ubicacion.atributos['nom_zh']}), "
        f"área {ubicacion.atributos['cod_ah']} "
        f"({ubicacion.atributos['nom_ah']}).",
    ))

    return ubicacion, hallazgos


def _buscar_subzona(capa, geometria_punto, punto_calculo, crs_calculo,
                    contexto, tolerancia_m):
    """
    Localiza la subzona del punto: contención, borde o la más cercana.

    La distancia se mide siempre en el CRS de cálculo. Medirla sobre la capa,
    que está en coordenadas geográficas, daría grados y no metros.
    """
    from qgis.core import (
        QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsPointXY,
        QgsRectangle,
    )

    rectangulo = geometria_punto.boundingBox()
    candidatas = list(capa.getFeatures(
        QgsFeatureRequest().setFilterRect(rectangulo)
    ))

    for entidad in candidatas:
        if entidad.geometry().contains(geometria_punto):
            return entidad, METODO_CONTIENE, 0.0
    for entidad in candidatas:
        if entidad.geometry().intersects(geometria_punto):
            return entidad, METODO_BORDE, 0.0

    if tolerancia_m <= 0:
        return None, "", 0.0

    # Sin contención se amplía la búsqueda. Si la capa está en grados, la
    # tolerancia métrica se convierte de forma aproximada y holgada: un grado de
    # latitud son unos 111 km. El filtro solo preselecciona candidatas; la
    # distancia real se mide después en metros.
    if capa.crs().isGeographic():
        margen = (tolerancia_m / 111_000.0) * 1.5
    else:
        margen = tolerancia_m * 1.5

    ampliado = QgsRectangle(
        rectangulo.xMinimum() - margen, rectangulo.yMinimum() - margen,
        rectangulo.xMaximum() + margen, rectangulo.yMaximum() + margen,
    )

    transformacion = QgsCoordinateTransform(capa.crs(), crs_calculo, contexto)
    punto_metrico = QgsGeometry.fromPointXY(
        QgsPointXY(punto_calculo.x(), punto_calculo.y())
    )

    mejor, mejor_distancia = None, float("inf")
    for entidad in capa.getFeatures(QgsFeatureRequest().setFilterRect(ampliado)):
        geometria = QgsGeometry(entidad.geometry())
        if geometria.transform(transformacion) != 0:
            continue
        distancia = geometria.distance(punto_metrico)
        if distancia < mejor_distancia:
            mejor, mejor_distancia = entidad, distancia

    if mejor is None or mejor_distancia > tolerancia_m:
        return None, "", 0.0
    return mejor, METODO_CERCANA, mejor_distancia


# =============================================================================
# Escritura de las capas
# =============================================================================
def escribir_productos(
    ubicacion: Ubicacion,
    configuracion: Config,
    base: Path,
    resultado: ResultadoM01,
) -> None:
    """Escribe las dos capas, sus diccionarios de campos y el JSON del módulo."""
    from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsPointXY

    crs_calculo_id = configuracion.obtener("crs.calculo")
    crs_calculo = QgsCoordinateReferenceSystem(crs_calculo_id)

    # --- punto ---------------------------------------------------------------
    destino_punto = rutas.resolver(
        configuracion.obtener("subzonas_hidrograficas.salida_punto"), base
    )
    valores_punto: dict[str, Any] = {
        "nombre": configuracion.obtener("punto_descarga.nombre"),
        "x_origen": ubicacion.x_origen,
        "y_origen": ubicacion.y_origen,
        "crs_origen": ubicacion.crs_origen,
        "longitud": ubicacion.longitud,
        "latitud": ubicacion.latitud,
        "x_calculo": ubicacion.x_calculo,
        "y_calculo": ubicacion.y_calculo,
        "metodo": ubicacion.metodo,
        "dist_m": ubicacion.distancia_m,
    }
    valores_punto.update(ubicacion.atributos)

    sig.escribir_capa(
        destino=destino_punto,
        campos_salida=CAMPOS_PUNTO,
        geometrias=[QgsGeometry.fromPointXY(
            QgsPointXY(ubicacion.x_calculo, ubicacion.y_calculo)
        )],
        valores=[valores_punto],
        crs_id=crs_calculo.authid(),
        tipo_geometria="Point",
    )
    resultado.capas_escritas.append(rutas.relativa(destino_punto, base))
    resultado.diccionarios.append(rutas.relativa(mod_campos.escribir_diccionario(
        CAMPOS_PUNTO,
        destino_punto.with_name(f"{destino_punto.stem}_campos.csv"),
        destino_punto.stem,
        configuracion.obtener("insumos_usuario.delimitador_csv"),
    ), base))

    # --- subzona -------------------------------------------------------------
    destino_subzona = rutas.resolver(
        configuracion.obtener("subzonas_hidrograficas.salida_subzona"), base
    )
    valores_subzona: dict[str, Any] = dict(ubicacion.atributos)
    valores_subzona["area_km2"] = ubicacion.area_km2
    valores_subzona["perim_km"] = ubicacion.perimetro_km

    sig.escribir_capa(
        destino=destino_subzona,
        campos_salida=CAMPOS_SUBZONA,
        geometrias=[ubicacion.geometria_subzona],
        valores=[valores_subzona],
        crs_id=crs_calculo.authid(),
        tipo_geometria="MultiPolygon",
    )
    resultado.capas_escritas.append(rutas.relativa(destino_subzona, base))
    resultado.diccionarios.append(rutas.relativa(mod_campos.escribir_diccionario(
        CAMPOS_SUBZONA,
        destino_subzona.with_name(f"{destino_subzona.stem}_campos.csv"),
        destino_subzona.stem,
        configuracion.obtener("insumos_usuario.delimitador_csv"),
    ), base))


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Ubica el punto, escribe los productos y emite el reporte."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)

    logger = registro.configurar(
        MODULO,
        nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    ruta_subzonas = configuracion.ruta_de("subzonas_hidrograficas.archivo",
                                          debe_existir=True)

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION,
        config=configuracion,
        insumos={
            "capa de subzonas": rutas.relativa(ruta_subzonas, base),
            "punto declarado": (
                f"x={configuracion.obtener('punto_descarga.x')} "
                f"y={configuracion.obtener('punto_descarga.y')} "
                f"en {configuracion.obtener('punto_descarga.crs')}"
            ),
        },
        parametros=configuracion.parametros((
            "crs.calculo", "crs.geografico",
            "punto_descarga.crs", "punto_descarga.x", "punto_descarga.y",
            "subzonas_hidrograficas.tolerancia_m",
            "subzonas_hidrograficas.campos",
        )),
    )

    resultado = ResultadoM01()
    iniciar_qgis(configuracion.obtener("entornos.qgis.prefix_path"))

    with registro.bloque(logger, "Ubicación del punto en la zonificación"):
        ubicacion, hallazgos = ubicar_punto(configuracion, ruta_subzonas, logger)
        resultado.hallazgos.extend(hallazgos)
        resultado.ubicacion = ubicacion

    if ubicacion is None or esquema.hay_bloqueantes(resultado.hallazgos):
        logger.error("El punto no se pudo ubicar. No se escriben capas.")
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Escritura de capas y diccionarios"):
        _adjuntar_geometria(ubicacion, configuracion, ruta_subzonas)
        escribir_productos(ubicacion, configuracion, base, resultado)

    codigo = (SALIDA_BLOQUEANTE if esquema.hay_bloqueantes(resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _adjuntar_geometria(ubicacion: Ubicacion, configuracion: Config,
                        ruta_subzonas: Path) -> None:
    """
    Recupera la geometría de la subzona ya reproyectada al CRS de cálculo.

    Se hace en una segunda pasada, y no durante la búsqueda, para no arrastrar
    objetos de QGIS dentro de una estructura que también se serializa a JSON.
    """
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
        QgsGeometry, QgsProject, QgsVectorLayer,
    )

    capa = QgsVectorLayer(str(ruta_subzonas), "subzonas", "ogr")
    campo_codigo = configuracion.obtener("subzonas_hidrograficas.campos.codigo_szh")
    objetivo = ubicacion.atributos["cod_szh"]

    crs_calculo = QgsCoordinateReferenceSystem(configuracion.obtener("crs.calculo"))
    transformacion = QgsCoordinateTransform(
        capa.crs(), crs_calculo, QgsProject.instance().transformContext()
    )

    for entidad in capa.getFeatures(QgsFeatureRequest()):
        if normalizar_codigo(entidad[campo_codigo]) != objetivo:
            continue
        geometria = QgsGeometry(entidad.geometry())
        geometria.transform(transformacion)
        ubicacion.geometria_subzona = geometria
        return

    raise ErrorFormato(
        f"No se pudo recuperar la geometría de la subzona {objetivo}."
    )


def _cerrar(logger, resultado: ResultadoM01, base: Path, ruta_json: Path | None,
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
            emitir("  %-24s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info(
        "RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
        conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO],
    )

    productos: dict[str, Any] = {}
    for indice, capa in enumerate(resultado.capas_escritas, start=1):
        productos[f"capa {indice}"] = capa
    for indice, diccionario in enumerate(resultado.diccionarios, start=1):
        productos[f"diccionario {indice}"] = diccionario

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M01_punto_descarga.json"

    reporte = {
        "modulo": MODULO,
        "resultado": resultado.ubicacion.como_dict() if resultado.ubicacion else None,
        "capas": resultado.capas_escritas,
        "diccionarios": resultado.diccionarios,
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
        prog="M01_punto_descarga.py",
        description="Ubica el punto de descarga en la zonificación hidrográfica.",
    )
    analizador.add_argument("--raiz", type=Path, default=None,
                            help="Raíz del repositorio.")
    analizador.add_argument("--config", type=Path, default=None,
                            help="Archivo de configuración a usar.")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida",
                            help="Ruta del reporte JSON.")
    analizador.add_argument("--silencioso", action="store_true",
                            help="No escribe en consola.")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        entorno.exigir_entorno(entorno.ENTORNO_QGIS, MODULO)
        codigo, _ = ejecutar(
            raiz=argumentos.raiz,
            ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorEntorno, ErrorRutas, ErrorConfiguracion, ErrorFormato) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    finally:
        finalizar_qgis()


if __name__ == "__main__":
    sys.exit(main())

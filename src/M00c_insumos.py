#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M00c - Verificación de insumos del usuario
==========================================
Entorno: venv del proyecto.

Valida data/00_insumos_usuario/MANIFIESTO.yaml, verifica los insumos declarados
y gestiona las tablas de homologación que el consultor debe diligenciar.

Se ejecuta antes de cualquier procesamiento. Su reporte es la evidencia de con
qué insumos se hizo el estudio, y el M15 lo transcribe al capítulo de
Información Base.

Alcance de la verificación espacial. El módulo lee shapefiles con librería
estándar a través de comun.shapefile: CRS declarado en el .prj, campos y valores
del .dbf, extensión y área desde el .shp. No abre rásteres: verificar un .tif
exige GDAL, que no está en el venv, y el módulo lo advierte de forma explícita
en lugar de dar por buena una comprobación que no hizo.

Obligatoriedad por doctrina, no por configuración:

    suelos     obligatorio. Sin él no hay número de curva sustentado.
    cobertura  opcional. Su ausencia activa el respaldo de Corine Land Cover.
    caudales   opcional. Su ausencia solo desactiva la calibración (M14b).

Uso:
    python src/M00c_insumos.py
    python src/M00c_insumos.py --sin-generar
    python src/M00c_insumos.py --json logs/M00c_reporte.json

Códigos de salida:
    0  los insumos declarados están completos y verificados
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o el manifiesto
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

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas, shapefile  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M00c"
DESCRIPCION = "Verificación de insumos del usuario"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# --- Doctrina ---------------------------------------------------------------
# CLAUDE.md, sección 6: el suelo es insumo del adaptador de cuatro perfiles y no
# tiene respaldo automático. La cobertura sí lo tiene (Corine Land Cover) y los
# caudales solo condicionan la calibración.
INSUMOS_OBLIGATORIOS = ("suelos",)

PERFILES_SUELO = {
    "A": "trae el grupo hidrológico ya asignado",
    "B": "IGAC con textura, profundidad y drenaje",
    "C": "codificación propia (UCS, símbolo, leyenda)",
    "D": "ráster con valores a reclasificar",
}
# Perfiles que exigen un shapefile y, por tanto, un campo clave en la tabla.
PERFILES_VECTORIALES = ("A", "B", "C")

TIPOS_INSUMO = ("shapefile", "raster")
TIPOS_DATO_CAUDAL = ("nivel", "caudal", "aforo_puntual")
ORIGENES_CAUDAL = ("ideam", "cliente")

GRUPOS_HIDROLOGICOS = ("A", "B", "C", "D")

# Columna de destino de cada tabla de homologación.
COLUMNA_DESTINO = {"suelos": "grupo_hidrologico", "cobertura": "clase_cn"}
COLUMNA_ORIGEN = "valor_origen"
COLUMNA_OBSERVACIONES = "observaciones"

# Envolvente aproximada del territorio colombiano, continental e insular.
_LIMITES_COLOMBIA = (-4.5, 13.5, -82.0, -66.5)


@dataclass
class ResultadoVerificacion:
    """Lo que el módulo comprobó y lo que produjo."""

    insumos_verificados: int = 0
    tablas_generadas: list[str] = field(default_factory=list)
    tablas_verificadas: list[str] = field(default_factory=list)
    resumen_insumos: dict[str, Any] = field(default_factory=dict)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Validación estructural del manifiesto (pura)
# =============================================================================
def _texto(bloque: dict, clave: str) -> str:
    valor = bloque.get(clave)
    return valor.strip() if isinstance(valor, str) else ""


def _validar_bloque_insumo(
    bloque: Any,
    nombre: str,
    con_perfil: bool,
) -> list[Hallazgo]:
    """Valida la forma de los bloques suelos y cobertura."""
    if not isinstance(bloque, dict):
        return [Hallazgo(
            BLOQUEANTE, nombre,
            f"debe ser un bloque de claves y es {type(bloque).__name__}.",
        )]

    hallazgos: list[Hallazgo] = []

    aportado = bloque.get("aportado")
    if not isinstance(aportado, bool):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"{nombre}.aportado",
            f"debe ser booleano y es {type(aportado).__name__}.",
        ))
        return hallazgos

    if not aportado:
        return hallazgos

    if not _texto(bloque, "archivo"):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"{nombre}.archivo",
            "el insumo se declara aportado y no indica archivo.",
        ))

    tipo = _texto(bloque, "tipo")
    if tipo not in TIPOS_INSUMO:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"{nombre}.tipo",
            f"valor {tipo!r} no admitido. Debe ser uno de: "
            f"{', '.join(TIPOS_INSUMO)}.",
        ))

    if con_perfil:
        perfil = _texto(bloque, "perfil").upper()
        if perfil not in PERFILES_SUELO:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{nombre}.perfil",
                f"valor {perfil!r} no admitido. Perfiles: "
                + "; ".join(f"{c} = {d}" for c, d in PERFILES_SUELO.items()),
            ))
        elif perfil == "D" and tipo == "shapefile":
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{nombre}.perfil",
                "el perfil D describe un ráster a reclasificar y el tipo "
                "declarado es shapefile.",
            ))
        elif perfil in PERFILES_VECTORIALES and tipo == "raster":
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{nombre}.perfil",
                f"el perfil {perfil} describe una capa vectorial con tabla de "
                "atributos y el tipo declarado es raster.",
            ))

    if tipo == "shapefile" and not _texto(bloque, "campo_clave"):
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"{nombre}.campo_clave",
            "es obligatorio para un shapefile: es el campo que se homologa.",
        ))

    for clave, severidad, motivo in (
        ("fuente", ADVERTENCIA, "el informe debe citar el origen del insumo"),
        ("fecha", ADVERTENCIA, "la vigencia del insumo es parte de su validez"),
        ("crs_declarado", ADVERTENCIA,
         "sin CRS declarado no se puede contrastar con el .prj"),
    ):
        if not _texto(bloque, clave):
            hallazgos.append(Hallazgo(
                severidad, f"{nombre}.{clave}", f"sin diligenciar: {motivo}.",
            ))

    return hallazgos


def _validar_caudales(bloque: Any) -> list[Hallazgo]:
    if not isinstance(bloque, dict):
        return [Hallazgo(
            BLOQUEANTE, "caudales",
            f"debe ser un bloque de claves y es {type(bloque).__name__}.",
        )]

    hallazgos: list[Hallazgo] = []

    aportado = bloque.get("aportado")
    if not isinstance(aportado, bool):
        return [Hallazgo(
            BLOQUEANTE, "caudales.aportado",
            f"debe ser booleano y es {type(aportado).__name__}.",
        )]
    if not aportado:
        return hallazgos

    origen = _texto(bloque, "origen").lower()
    if origen not in ORIGENES_CAUDAL:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "caudales.origen",
            f"valor {origen!r} no admitido. Debe ser uno de: "
            f"{', '.join(ORIGENES_CAUDAL)}.",
        ))

    estaciones = bloque.get("estaciones")
    if not isinstance(estaciones, list) or not estaciones:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, "caudales.estaciones",
            "se declaran caudales aportados y no hay ninguna estación descrita.",
        ))
        return hallazgos

    for indice, estacion in enumerate(estaciones):
        ubicacion = f"caudales.estaciones[{indice}]"
        if not isinstance(estacion, dict):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, ubicacion,
                f"debe ser un bloque de claves y es {type(estacion).__name__}.",
            ))
            continue

        if not _texto(estacion, "codigo"):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}.codigo", "es obligatorio.",
            ))

        latitud, longitud = estacion.get("latitud"), estacion.get("longitud")
        if not isinstance(latitud, (int, float)) or isinstance(latitud, bool) \
                or not isinstance(longitud, (int, float)) or isinstance(longitud, bool):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}",
                "la ubicación es obligatoria: un caudal sin punto asociado no "
                "sirve para calibrar.",
            ))
        else:
            lat_min, lat_max, lon_min, lon_max = _LIMITES_COLOMBIA
            if not (lat_min <= latitud <= lat_max) or \
                    not (lon_min <= longitud <= lon_max):
                hallazgos.append(Hallazgo(
                    ADVERTENCIA, ubicacion,
                    f"las coordenadas ({latitud}, {longitud}) quedan fuera de la "
                    "envolvente de Colombia. Verificar el orden y el CRS.",
                ))

        tipo_dato = _texto(estacion, "tipo_dato").lower()
        if tipo_dato not in TIPOS_DATO_CAUDAL:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}.tipo_dato",
                f"valor {tipo_dato!r} no admitido. Debe ser uno de: "
                f"{', '.join(TIPOS_DATO_CAUDAL)}.",
            ))

        if not _texto(estacion, "seccion_aforo"):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{ubicacion}.seccion_aforo",
                "es obligatoria: un caudal sin sección asociada no sirve para "
                "calibrar.",
            ))

    return hallazgos


def _validar_homologacion(bloque: Any) -> list[Hallazgo]:
    if not isinstance(bloque, dict):
        return [Hallazgo(
            BLOQUEANTE, "homologacion",
            f"debe ser un bloque de claves y es {type(bloque).__name__}.",
        )]

    hallazgos: list[Hallazgo] = []
    for nombre in ("suelos", "cobertura"):
        sub = bloque.get(nombre)
        if not isinstance(sub, dict):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"homologacion.{nombre}",
                "debe declarar archivo, diligenciada, fecha y responsable.",
            ))
            continue
        if not _texto(sub, "archivo"):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"homologacion.{nombre}.archivo",
                "es obligatorio: indica dónde vive la tabla de homologación.",
            ))
        if not isinstance(sub.get("diligenciada"), bool):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"homologacion.{nombre}.diligenciada",
                "debe ser booleano.",
            ))
    return hallazgos


def validar_manifiesto(datos: Any) -> list[Hallazgo]:
    """
    Valida la estructura del MANIFIESTO.yaml.

    No accede al disco: es verificable sin insumos. Las comprobaciones que
    requieren abrir archivos están en verificar_insumo().
    """
    if not isinstance(datos, dict):
        return [Hallazgo(
            BLOQUEANTE, "<raiz>",
            f"el manifiesto debe ser un bloque de claves y es "
            f"{type(datos).__name__}.",
        )]

    hallazgos: list[Hallazgo] = []
    for nombre in ("suelos", "cobertura", "caudales", "homologacion"):
        if nombre not in datos:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, nombre, "bloque ausente en el manifiesto.",
            ))

    if "suelos" in datos:
        hallazgos.extend(_validar_bloque_insumo(datos["suelos"], "suelos", True))
    if "cobertura" in datos:
        hallazgos.extend(
            _validar_bloque_insumo(datos["cobertura"], "cobertura", False)
        )
    if "caudales" in datos:
        hallazgos.extend(_validar_caudales(datos["caudales"]))
    if "homologacion" in datos:
        hallazgos.extend(_validar_homologacion(datos["homologacion"]))

    hallazgos.extend(_validar_decisiones(datos.get("decisiones")))
    return sorted(hallazgos, key=_orden_hallazgo)


def _validar_decisiones(decisiones: Any) -> list[Hallazgo]:
    """
    Verifica el registro de decisiones del consultor.

    Doctrina (CLAUDE.md, sección 7): el criterio adoptado en cada decisión con
    margen debe quedar registrado. Un estudio que no puede explicar sus
    descartes no es defendible ante interventoría.
    """
    if decisiones is None or decisiones == []:
        return [Hallazgo(
            INFORMATIVO, "decisiones",
            "sin decisiones registradas todavía.",
        )]

    if not isinstance(decisiones, list):
        return [Hallazgo(
            BLOQUEANTE, "decisiones",
            f"debe ser una lista y es {type(decisiones).__name__}.",
        )]

    hallazgos: list[Hallazgo] = []
    for indice, decision in enumerate(decisiones):
        ubicacion = f"decisiones[{indice}]"
        if not isinstance(decision, dict):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, ubicacion,
                f"debe ser un bloque de claves y es {type(decision).__name__}.",
            ))
            continue
        tema = _texto(decision, "tema")
        if not tema:
            continue  # entrada de plantilla sin diligenciar
        for clave in ("valor", "justificacion", "fecha", "responsable"):
            if not _texto(decision, clave):
                hallazgos.append(Hallazgo(
                    ADVERTENCIA, f"{ubicacion}.{clave}",
                    f"la decisión {tema!r} está registrada sin {clave}. Una "
                    "decisión sin sustento no es defendible ante interventoría.",
                ))
    return hallazgos


def _orden_hallazgo(hallazgo: Hallazgo) -> tuple[int, str]:
    orden = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}
    return (orden.get(hallazgo.severidad, 9), hallazgo.clave)


# =============================================================================
# Verificación de los archivos declarados
# =============================================================================
def verificar_insumo(
    bloque: dict,
    nombre: str,
    directorio_manifiesto: Path,
    crs_calculo: str,
) -> tuple[list[Hallazgo], shapefile.InfoShapefile | None]:
    """
    Abre el insumo declarado y contrasta su contenido con lo que dice el
    manifiesto.

    Devuelve los hallazgos y, cuando el insumo es un shapefile legible, su
    resumen para que la homologación pueda usarlo sin volver a leerlo.
    """
    hallazgos: list[Hallazgo] = []

    if not bloque.get("aportado"):
        return hallazgos, None

    declarado = _texto(bloque, "archivo")
    if not declarado:
        return hallazgos, None

    # Las rutas del manifiesto son relativas al propio manifiesto.
    destino = rutas.resolver_desde(directorio_manifiesto, declarado)
    if not destino.exists():
        return [Hallazgo(
            BLOQUEANTE, f"{nombre}.archivo",
            f"el archivo declarado no existe: {destino}",
        )], None

    tipo = _texto(bloque, "tipo")
    if tipo == "raster":
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"{nombre}.archivo",
            f"{destino.name} es un ráster. El M00c corre en el venv y no puede "
            "inspeccionar rásteres: su CRS, resolución y cobertura quedan sin "
            "verificar. Comprobarlos al abrir el proyecto QGIS del M00b.",
        ))
        return hallazgos, None

    try:
        info = shapefile.leer_shapefile(destino)
    except ErrorFormato as exc:
        return [Hallazgo(
            BLOQUEANTE, f"{nombre}.archivo", str(exc),
        )], None

    if info.componentes_faltantes:
        severidad = (BLOQUEANTE if ".dbf" in info.componentes_faltantes
                     else ADVERTENCIA)
        detalle = ", ".join(info.componentes_faltantes)
        mensaje = f"faltan componentes del shapefile: {detalle}."
        if ".prj" in info.componentes_faltantes:
            mensaje += (" Sin .prj el CRS es una suposición, y CLAUDE.md, "
                        "sección 5, exige escritura explícita del .prj.")
        hallazgos.append(Hallazgo(severidad, f"{nombre}.archivo", mensaje))

    if info.n_registros == 0:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"{nombre}.archivo",
            f"{destino.name} no contiene ningún registro.",
        ))

    hallazgos.extend(_verificar_crs(info, bloque, nombre, crs_calculo))
    hallazgos.extend(_verificar_campo_clave(info, bloque, nombre))

    return hallazgos, info


def _verificar_crs(
    info: shapefile.InfoShapefile,
    bloque: dict,
    nombre: str,
    crs_calculo: str,
) -> list[Hallazgo]:
    """Contrasta el CRS declarado, el del .prj y el de cálculo."""
    hallazgos: list[Hallazgo] = []
    declarado = _texto(bloque, "crs_declarado").upper().replace(" ", "")

    if info.crs_epsg is None and info.crs_wkt:
        hallazgos.append(Hallazgo(
            INFORMATIVO, f"{nombre}.crs_declarado",
            "el .prj no incluye código de autoridad EPSG, de modo que no se "
            "puede confirmar por lectura de texto. Verificar en QGIS.",
        ))
        return hallazgos

    if info.crs_epsg is None:
        return hallazgos

    if declarado and declarado != info.crs_epsg.upper():
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"{nombre}.crs_declarado",
            f"el manifiesto declara {declarado} y el .prj dice {info.crs_epsg}. "
            "Prevalece el .prj; corregir el manifiesto o el archivo.",
        ))

    if info.crs_epsg.upper() != crs_calculo.upper():
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"{nombre}.archivo",
            f"el insumo está en {info.crs_epsg} y el CRS de cálculo es "
            f"{crs_calculo}. Requiere reproyección explícita antes de usarlo "
            "(CLAUDE.md, sección 5).",
        ))

    return hallazgos


def _verificar_campo_clave(
    info: shapefile.InfoShapefile,
    bloque: dict,
    nombre: str,
) -> list[Hallazgo]:
    """Comprueba que el campo a homologar exista y sea coherente con el perfil."""
    hallazgos: list[Hallazgo] = []
    campo_clave = _texto(bloque, "campo_clave")
    if not campo_clave:
        return hallazgos

    if not info.tiene_campo(campo_clave):
        return [Hallazgo(
            BLOQUEANTE, f"{nombre}.campo_clave",
            f"el campo {campo_clave!r} no existe en la tabla de atributos. "
            f"Campos disponibles: {', '.join(info.nombres_campos)}.",
        )]

    perfil = _texto(bloque, "perfil").upper()
    if nombre == "suelos" and perfil == "A":
        try:
            valores = shapefile.valores_unicos(info.ruta, campo_clave)
        except ErrorFormato as exc:
            return [Hallazgo(ADVERTENCIA, f"{nombre}.campo_clave", str(exc))]
        ajenos = [v for v in valores if v.strip().upper() not in GRUPOS_HIDROLOGICOS]
        if ajenos:
            hallazgos.append(Hallazgo(
                ADVERTENCIA, f"{nombre}.perfil",
                f"el perfil A declara que el campo {campo_clave!r} ya trae el "
                f"grupo hidrológico, pero contiene valores fuera de A, B, C, D: "
                f"{', '.join(repr(v) for v in ajenos[:10])}"
                + (" ..." if len(ajenos) > 10 else "") + ".",
            ))

    return hallazgos


# =============================================================================
# Tablas de homologación
# =============================================================================
def _leer_tabla_homologacion(
    destino: Path, delimitador: str
) -> tuple[list[str], list[dict[str, str]]]:
    """Lee una tabla de homologación, omitiendo las líneas de comentario."""
    lineas = [
        linea for linea in destino.read_text(encoding="utf-8-sig").splitlines()
        if linea.strip() and not linea.lstrip().startswith("#")
    ]
    if not lineas:
        raise ErrorFormato(f"{destino.name} no contiene ninguna fila.")

    lector = csv.DictReader(lineas, delimiter=delimitador)
    columnas = list(lector.fieldnames or [])
    return columnas, [dict(fila) for fila in lector]


def _escribir_tabla_homologacion(
    destino: Path,
    valores: Sequence[str],
    columna_destino: str,
    delimitador: str,
    nombre_insumo: str,
) -> None:
    """
    Escribe la tabla con un valor por fila y la columna de destino en blanco.

    Se usa utf-8-sig para que Excel muestre los acentos sin intervención del
    consultor, que es quien va a diligenciarla.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        manejador.write(
            f"# Tabla de homologación de {nombre_insumo}, generada por el M00c.\n"
            f"# Diligenciar la columna {columna_destino} para cada valor.\n"
            f"# Las filas que comienzan con almohadilla son comentarios.\n"
        )
        escritor = csv.writer(manejador, delimiter=delimitador)
        escritor.writerow([COLUMNA_ORIGEN, columna_destino, COLUMNA_OBSERVACIONES])
        for valor in valores:
            escritor.writerow([valor, "", ""])


def gestionar_homologacion(
    nombre: str,
    bloque_insumo: dict,
    bloque_homologacion: dict,
    info: shapefile.InfoShapefile | None,
    directorio_manifiesto: Path,
    delimitador: str,
    generar: bool,
    maximo_valores: int,
    resultado: ResultadoVerificacion,
) -> list[Hallazgo]:
    """
    Genera o verifica la tabla de homologación de un insumo.

    Doctrina (MANIFIESTO.yaml): las tablas las genera el sistema y las diligencia
    el consultor, y el M00c se detiene si aparecen valores sin homologar.
    """
    hallazgos: list[Hallazgo] = []
    columna_destino = COLUMNA_DESTINO[nombre]

    if not bloque_insumo.get("aportado"):
        return [Hallazgo(
            INFORMATIVO, f"homologacion.{nombre}",
            f"el insumo de {nombre} no se aportó: no hay nada que homologar.",
        )]

    declarada = _texto(bloque_homologacion, "archivo")
    if not declarada:
        return hallazgos  # ya reportado por la validación estructural

    destino = rutas.resolver_desde(directorio_manifiesto, declarada)

    if info is None:
        return [Hallazgo(
            ADVERTENCIA, f"homologacion.{nombre}",
            "el insumo no se pudo leer, de modo que la tabla de homologación no "
            "se puede generar ni contrastar.",
        )]

    campo_clave = _texto(bloque_insumo, "campo_clave")
    try:
        valores = shapefile.valores_unicos(info.ruta, campo_clave)
    except ErrorFormato as exc:
        return [Hallazgo(BLOQUEANTE, f"homologacion.{nombre}", str(exc))]

    if len(valores) > maximo_valores:
        return [Hallazgo(
            BLOQUEANTE, f"homologacion.{nombre}",
            f"el campo {campo_clave!r} tiene {len(valores)} valores distintos y "
            f"el máximo diligenciable es {maximo_valores}. Revisar si el campo "
            "clave es el correcto: probablemente se declaró un identificador "
            "único en lugar de una leyenda.",
        )]

    vacios = sum(1 for valor in valores if not valor)
    if vacios:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"{nombre}.campo_clave",
            f"hay registros con el campo {campo_clave!r} vacío. Quedarán sin "
            "homologar y sin número de curva asignado.",
        ))

    if not destino.is_file():
        if not generar:
            return hallazgos + [Hallazgo(
                BLOQUEANTE, f"homologacion.{nombre}",
                f"la tabla no existe ({destino.name}) y la generación está "
                "desactivada.",
            )]
        _escribir_tabla_homologacion(
            destino, valores, columna_destino, delimitador, nombre
        )
        resultado.tablas_generadas.append(rutas.relativa(destino, directorio_manifiesto))
        return hallazgos + [Hallazgo(
            BLOQUEANTE, f"homologacion.{nombre}",
            f"tabla generada con {len(valores)} valor(es) en {destino.name}. "
            f"Diligenciar la columna {columna_destino} y volver a ejecutar el "
            "módulo.",
        )]

    try:
        columnas, filas = _leer_tabla_homologacion(destino, delimitador)
    except (ErrorFormato, OSError) as exc:
        return hallazgos + [Hallazgo(
            BLOQUEANTE, f"homologacion.{nombre}", f"{destino.name}: {exc}",
        )]

    faltantes = [c for c in (COLUMNA_ORIGEN, columna_destino) if c not in columnas]
    if faltantes:
        return hallazgos + [Hallazgo(
            BLOQUEANTE, f"homologacion.{nombre}",
            f"{destino.name} no tiene la(s) columna(s) {', '.join(faltantes)}. "
            f"Columnas encontradas: {', '.join(columnas)}.",
        )]

    resultado.tablas_verificadas.append(
        rutas.relativa(destino, directorio_manifiesto)
    )

    mapa = {
        (fila.get(COLUMNA_ORIGEN) or "").strip():
            (fila.get(columna_destino) or "").strip()
        for fila in filas
    }

    sin_homologar = sorted(clave for clave, valor in mapa.items() if not valor)
    if sin_homologar:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"homologacion.{nombre}",
            f"{len(sin_homologar)} valor(es) sin homologar en {destino.name}: "
            f"{', '.join(repr(v) for v in sin_homologar[:10])}"
            + (" ..." if len(sin_homologar) > 10 else "") + ".",
        ))

    nuevos = sorted(set(valores) - set(mapa))
    if nuevos:
        hallazgos.append(Hallazgo(
            BLOQUEANTE, f"homologacion.{nombre}",
            f"el insumo tiene {len(nuevos)} valor(es) que la tabla no contempla: "
            f"{', '.join(repr(v) for v in nuevos[:10])}"
            + (" ..." if len(nuevos) > 10 else "")
            + ". Agregarlos y homologarlos.",
        ))

    obsoletos = sorted(set(mapa) - set(valores))
    if obsoletos:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"homologacion.{nombre}",
            f"la tabla contempla {len(obsoletos)} valor(es) que ya no están en "
            f"el insumo: {', '.join(repr(v) for v in obsoletos[:10])}"
            + (" ..." if len(obsoletos) > 10 else "") + ".",
        ))

    if nombre == "suelos":
        invalidos = sorted({
            valor for valor in mapa.values()
            if valor and valor.strip().upper() not in GRUPOS_HIDROLOGICOS
        })
        if invalidos:
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"homologacion.{nombre}",
                f"la columna {columna_destino} contiene valores fuera de "
                f"A, B, C, D: {', '.join(repr(v) for v in invalidos[:10])}.",
            ))

    completa = not sin_homologar and not nuevos
    declarada_diligenciada = bool(bloque_homologacion.get("diligenciada"))
    if completa and not declarada_diligenciada:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"homologacion.{nombre}.diligenciada",
            "la tabla está completa pero el manifiesto la declara sin "
            "diligenciar. Actualizar el manifiesto con fecha y responsable.",
        ))
    elif not completa and declarada_diligenciada:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, f"homologacion.{nombre}.diligenciada",
            "el manifiesto la declara diligenciada y aún tiene valores "
            "pendientes.",
        ))

    return hallazgos


# =============================================================================
# Escala del estudio de suelos
# =============================================================================
def denominador_escala(escala: str) -> int | None:
    """
    Interpreta '1:25000', '1:25.000' o '25000' y devuelve el denominador.

    Devuelve None si el texto no contiene un denominador reconocible.
    """
    if not escala:
        return None
    texto = escala.strip()
    if ":" in texto:
        texto = texto.split(":", 1)[1]
    digitos = "".join(c for c in texto if c.isdigit())
    return int(digitos) if digitos else None


def leer_tabla_escala(destino: Path, delimitador: str = ";") -> list[dict[str, str]]:
    """Lee la tabla de compatibilidad escala/área, omitiendo comentarios."""
    lineas = [
        linea for linea in destino.read_text(encoding="utf-8-sig").splitlines()
        if linea.strip() and not linea.lstrip().startswith("#")
    ]
    return [dict(fila) for fila in csv.DictReader(lineas, delimiter=delimitador)]


def verificar_escala(
    bloque: dict,
    tabla: Path,
    cuenca: Path,
    crs_calculo: str,
) -> list[Hallazgo]:
    """
    Contrasta la escala declarada del estudio de suelos con el área de la cuenca.

    Doctrina (CLAUDE.md, sección 7): la escala del shape de suelos debe ser
    compatible con el área de la cuenca. Un levantamiento 1:100.000 sobre una
    cuenca pequeña asigna un único polígono y el número de curva resultante no
    tiene sustento.
    """
    if not bloque.get("aportado"):
        return []

    hallazgos: list[Hallazgo] = []
    denominador = denominador_escala(_texto(bloque, "escala"))
    if denominador is None:
        return [Hallazgo(
            ADVERTENCIA, "suelos.escala",
            "sin declarar o ilegible. Sin escala no se puede verificar la "
            "compatibilidad con el área de la cuenca.",
        )]

    if not cuenca.is_file():
        return [Hallazgo(
            INFORMATIVO, "suelos.escala",
            f"declarada 1:{denominador}. La verificación contra el área queda "
            f"diferida hasta que el M02 produzca {cuenca.name}.",
        )]

    try:
        info_cuenca = shapefile.leer_shapefile(cuenca)
        if info_cuenca.crs_epsg and info_cuenca.crs_epsg.upper() != crs_calculo.upper():
            return [Hallazgo(
                ADVERTENCIA, "suelos.escala",
                f"la cuenca está en {info_cuenca.crs_epsg} y no en {crs_calculo}. "
                "El área calculada no sería métrica; verificación omitida.",
            )]
        area_km2 = shapefile.area_poligonos(cuenca) / 1_000_000.0
    except ErrorFormato as exc:
        return [Hallazgo(
            ADVERTENCIA, "suelos.escala",
            f"no se pudo calcular el área de la cuenca: {exc}",
        )]

    if area_km2 <= 0:
        return [Hallazgo(
            ADVERTENCIA, "suelos.escala",
            f"el área calculada de {cuenca.name} es {area_km2:.4f} km2. "
            "Verificación omitida.",
        )]

    if not tabla.is_file():
        return [Hallazgo(
            INFORMATIVO, "suelos.escala",
            f"área de la cuenca {area_km2:.2f} km2 y escala 1:{denominador}. "
            f"Falta {tabla.name} para contrastarlas.",
        )]

    try:
        filas = leer_tabla_escala(tabla)
    except (OSError, csv.Error) as exc:
        return [Hallazgo(
            ADVERTENCIA, "suelos.escala", f"no se pudo leer {tabla.name}: {exc}",
        )]

    for fila in filas:
        minimo = float(fila.get("area_min_km2") or 0)
        maximo_texto = (fila.get("area_max_km2") or "").strip()
        maximo = float(maximo_texto) if maximo_texto else float("inf")
        if not (minimo <= area_km2 < maximo):
            continue

        admisible = int(fila.get("denominador_maximo") or 0)
        fuente = (fila.get("fuente") or "").strip()

        if denominador > admisible:
            hallazgos.append(Hallazgo(
                ADVERTENCIA, "suelos.escala",
                f"la cuenca tiene {area_km2:.2f} km2 y el estudio de suelos está "
                f"a 1:{denominador}. Para ese rango de área la tabla admite hasta "
                f"1:{admisible}. El número de curva quedaría sustentado en muy "
                "pocos polígonos.",
            ))
        else:
            hallazgos.append(Hallazgo(
                INFORMATIVO, "suelos.escala",
                f"compatible: cuenca de {area_km2:.2f} km2 con suelos a "
                f"1:{denominador} (admisible hasta 1:{admisible}).",
            ))

        if "por validar" in fuente.lower():
            hallazgos.append(Hallazgo(
                ADVERTENCIA, "insumos_usuario.tabla_escala_area",
                f"la fila usada de {tabla.name} tiene la fuente {fuente!r}. Los "
                "valores son orientativos y deben reemplazarse por los del "
                "referente que se vaya a citar en el informe.",
            ))
        break
    else:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "suelos.escala",
            f"el área de {area_km2:.2f} km2 no cae en ningún rango de "
            f"{tabla.name}.",
        ))

    return hallazgos


# =============================================================================
# Doctrina de obligatoriedad
# =============================================================================
def verificar_obligatoriedad(
    manifiesto: dict, configuracion: Config
) -> list[Hallazgo]:
    """Aplica la doctrina de qué insumos son imprescindibles."""
    hallazgos: list[Hallazgo] = []

    for nombre in INSUMOS_OBLIGATORIOS:
        bloque = manifiesto.get(nombre)
        if isinstance(bloque, dict) and not bloque.get("aportado"):
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"{nombre}.aportado",
                f"el insumo de {nombre} es obligatorio y no se aportó. Sin él no "
                "hay grupo hidrológico ni número de curva sustentados.",
            ))

    cobertura = manifiesto.get("cobertura")
    if isinstance(cobertura, dict) and not cobertura.get("aportado"):
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "cobertura.aportado",
            "sin cobertura del proyecto. El sistema recortará Corine Land Cover "
            "al área de influencia; si tampoco estuviera disponible, el número "
            "de curva se asignaría por criterio del consultor y esa decisión "
            "debe quedar registrada en el manifiesto.",
        ))

    caudales = manifiesto.get("caudales")
    if isinstance(caudales, dict) and not caudales.get("aportado"):
        activar = configuracion.obtener("calibracion.activar_si_hay_series", False)
        hallazgos.append(Hallazgo(
            INFORMATIVO, "caudales.aportado",
            "sin series de caudal o nivel aportadas. La calibración (M14b) "
            + ("quedará desactivada pese a estar habilitada en config.yaml."
               if activar else "no aplica.")
        ))

    return hallazgos


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    generar: bool | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Verifica los insumos y emite el reporte. Devuelve (codigo, hallazgos)."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)

    logger = registro.configurar(
        MODULO,
        nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base,
        consola=consola,
    )

    ruta_manifiesto = configuracion.ruta_de("insumos_usuario.manifiesto",
                                            debe_existir=True)
    directorio_manifiesto = ruta_manifiesto.parent
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")
    generar_tablas = (configuracion.obtener("insumos_usuario.generar_homologacion")
                      if generar is None else generar)
    crs_calculo = configuracion.obtener("crs.calculo")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION,
        config=configuracion,
        insumos={
            "manifiesto": rutas.relativa(ruta_manifiesto, base),
            "directorio de insumos": rutas.relativa(directorio_manifiesto, base),
            "generación de tablas": "activada" if generar_tablas else "desactivada",
        },
        parametros=configuracion.parametros((
            "crs.calculo",
            "insumos_usuario.delimitador_csv",
            "insumos_usuario.generar_homologacion",
            "insumos_usuario.max_valores_homologacion",
            "insumos_usuario.tabla_escala_area",
            "insumos_usuario.cuenca_referencia",
        )),
    )

    resultado = ResultadoVerificacion()

    with registro.bloque(logger, "Lectura y validación del manifiesto"):
        manifiesto = leer_yaml(ruta_manifiesto)
        resultado.hallazgos.extend(validar_manifiesto(manifiesto))

    if esquema.hay_bloqueantes(resultado.hallazgos):
        logger.error(
            "El manifiesto no es utilizable. No se verifican los archivos."
        )
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Doctrina de obligatoriedad"):
        resultado.hallazgos.extend(
            verificar_obligatoriedad(manifiesto, configuracion)
        )

    informacion: dict[str, shapefile.InfoShapefile | None] = {}
    with registro.bloque(logger, "Verificación de los archivos declarados"):
        for nombre in ("suelos", "cobertura"):
            bloque = manifiesto.get(nombre) or {}
            hallazgos, info = verificar_insumo(
                bloque, nombre, directorio_manifiesto, crs_calculo
            )
            resultado.hallazgos.extend(hallazgos)
            informacion[nombre] = info
            if bloque.get("aportado"):
                resultado.insumos_verificados += 1
            if info is not None:
                resultado.resumen_insumos[nombre] = {
                    "archivo": rutas.relativa(info.ruta, base),
                    "geometria": info.tipo_geometria,
                    "registros": info.n_registros,
                    "crs": info.crs_epsg or "no declarado",
                    "campos": list(info.nombres_campos),
                    "codificacion": info.codificacion,
                }
        resultado.hallazgos.extend(_verificar_archivos_caudales(
            manifiesto, directorio_manifiesto
        ))

    with registro.bloque(logger, "Tablas de homologación"):
        bloque_homologacion = manifiesto.get("homologacion") or {}
        for nombre in ("suelos", "cobertura"):
            resultado.hallazgos.extend(gestionar_homologacion(
                nombre=nombre,
                bloque_insumo=manifiesto.get(nombre) or {},
                bloque_homologacion=bloque_homologacion.get(nombre) or {},
                info=informacion.get(nombre),
                directorio_manifiesto=directorio_manifiesto,
                delimitador=delimitador,
                generar=generar_tablas,
                maximo_valores=configuracion.obtener(
                    "insumos_usuario.max_valores_homologacion"
                ),
                resultado=resultado,
            ))

    with registro.bloque(logger, "Compatibilidad de escala y área"):
        resultado.hallazgos.extend(verificar_escala(
            bloque=manifiesto.get("suelos") or {},
            tabla=configuracion.ruta_de("insumos_usuario.tabla_escala_area"),
            cuenca=rutas.resolver(
                configuracion.obtener("insumos_usuario.cuenca_referencia"), base
            ),
            crs_calculo=crs_calculo,
        ))

    codigo = (SALIDA_BLOQUEANTE if esquema.hay_bloqueantes(resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _verificar_archivos_caudales(
    manifiesto: dict, directorio_manifiesto: Path
) -> list[Hallazgo]:
    """Comprueba la existencia de los archivos de caudal declarados."""
    bloque = manifiesto.get("caudales") or {}
    if not bloque.get("aportado"):
        return []

    archivos = bloque.get("archivos") or []
    if not isinstance(archivos, list) or not archivos:
        return [Hallazgo(
            BLOQUEANTE, "caudales.archivos",
            "se declaran caudales aportados y no se lista ningún archivo.",
        )]

    hallazgos: list[Hallazgo] = []
    for indice, declarado in enumerate(archivos):
        if not isinstance(declarado, str) or not declarado.strip():
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"caudales.archivos[{indice}]",
                "debe ser una ruta de texto no vacía.",
            ))
            continue
        destino = rutas.resolver_desde(directorio_manifiesto, declarado)
        if not destino.exists():
            hallazgos.append(Hallazgo(
                BLOQUEANTE, f"caudales.archivos[{indice}]",
                f"el archivo declarado no existe: {destino}",
            ))
    return hallazgos


def _cerrar(logger, resultado: ResultadoVerificacion, base: Path,
            ruta_json: Path | None, inicio: float,
            codigo: int) -> tuple[int, list[Hallazgo]]:
    """Emite el reporte, escribe el JSON opcional y cierra el log."""
    hallazgos = sorted(resultado.hallazgos, key=_orden_hallazgo)

    logger.info(registro.SEPARADOR)
    for severidad, emitir in ((BLOQUEANTE, logger.error),
                              (ADVERTENCIA, logger.warning),
                              (INFORMATIVO, logger.info)):
        grupo = [h for h in hallazgos if h.severidad == severidad]
        if not grupo:
            continue
        emitir("%s (%d)", severidad, len(grupo))
        for hallazgo in grupo:
            emitir("  %-40s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info(
        "RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
        conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO],
    )

    productos: dict[str, Any] = {}
    for etiqueta, lista in (("tablas generadas", resultado.tablas_generadas),
                            ("tablas verificadas", resultado.tablas_verificadas)):
        if lista:
            productos[etiqueta] = ", ".join(lista)

    if ruta_json is not None:
        reporte = {
            "modulo": MODULO,
            "insumos_verificados": resultado.insumos_verificados,
            "insumos": resultado.resumen_insumos,
            "tablas_generadas": resultado.tablas_generadas,
            "tablas_verificadas": resultado.tablas_verificadas,
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
        prog="M00c_insumos.py",
        description="Verifica los insumos declarados en MANIFIESTO.yaml.",
    )
    analizador.add_argument("--raiz", type=Path, default=None,
                            help="Raíz del repositorio.")
    analizador.add_argument("--config", type=Path, default=None,
                            help="Archivo de configuración a usar.")
    analizador.add_argument("--sin-generar", action="store_true",
                            dest="sin_generar",
                            help="No crea las tablas de homologación ausentes.")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida",
                            help="Escribe el reporte en el archivo JSON indicado.")
    analizador.add_argument("--silencioso", action="store_true",
                            help="No escribe en consola.")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz,
            ruta_config=argumentos.config,
            generar=False if argumentos.sin_generar else None,
            ruta_json=argumentos.json_salida,
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

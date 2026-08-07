# -*- coding: utf-8 -*-
"""
sig
===
Utilidades compartidas por los módulos que corren bajo el Python de QGIS
(M00b, M01, M02, M06, M08, M11, M16).

No vive en src/comun porque ese paquete está restringido a la librería estándar
para poder importarse desde los dos entornos. Este archivo, en cambio, depende
de la API de QGIS de forma deliberada.

Concentra los tres puntos frágiles ante un cambio de versión de QGIS:

1. El ciclo de vida de la aplicación. QGIS no admite reinicializarse dentro del
   mismo proceso: una segunda pareja initQgis/exitQgis produce una violación de
   acceso que mata el intérprete sin traza de Python.
2. La inicialización de Processing. El paquete 'processing' no está en sys.path
   del intérprete de consola, y GRASS necesita GISBASE en el entorno antes de
   inicializarse o falla con "GRASS folder is not configured" pese a aparecer
   con sus algoritmos registrados.
3. La construcción de campos y la escritura de shapefiles, incluida la
   reescritura del .prj para que declare el código EPSG.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Sequence

from comun import campos as mod_campos
from comun.campos import CampoSalida
from comun.errores import ErrorEntorno, ErrorFormato

__all__ = [
    "iniciar_qgis",
    "finalizar_qgis",
    "inicializar_processing",
    "tipos_qgis",
    "escribir_capa",
    "reescribir_prj_con_autoridad",
]

_APLICACION: Any = None
_PROCESSING_LISTO = False


# =============================================================================
# Ciclo de vida
# =============================================================================
def iniciar_qgis(prefix_path: str) -> Any:
    """
    Inicializa la aplicación QGIS, o devuelve la ya inicializada.

    Excepciones
    -----------
    ErrorEntorno
        Si qgis.core no se puede importar.
    """
    global _APLICACION
    if _APLICACION is not None:
        return _APLICACION

    try:
        from qgis.core import QgsApplication
    except ImportError as exc:
        raise ErrorEntorno(
            "No se pudo importar qgis.core. Este módulo debe ejecutarse con el "
            "intérprete de QGIS declarado en entornos.qgis.python."
        ) from exc

    QgsApplication.setPrefixPath(prefix_path, True)
    aplicacion = QgsApplication([], False)
    aplicacion.initQgis()
    _APLICACION = aplicacion
    return aplicacion


def finalizar_qgis() -> None:
    """Cierra la aplicación QGIS. Solo debe llamarse al terminar el proceso."""
    global _APLICACION, _PROCESSING_LISTO
    if _APLICACION is not None:
        _APLICACION.exitQgis()
        _APLICACION = None
        _PROCESSING_LISTO = False


def inicializar_processing(prefix_path: str, logger: Any = None) -> None:
    """
    Deja el marco de Processing y el proveedor de GRASS listos para usarse.

    Dos condiciones que no se cumplen solas al ejecutar sin interfaz:

    - 'processing' vive en <prefix>/python/plugins, que no está en sys.path del
      intérprete de consola.
    - El proveedor de GRASS registra sus algoritmos aunque no sepa dónde está
      GRASS. Solo al ejecutar uno informa de que la carpeta no está configurada.
      Definir GISBASE antes de inicializar resuelve la localización.

    Excepciones
    -----------
    ErrorEntorno
        Si Processing no se puede inicializar.
    """
    global _PROCESSING_LISTO
    if _PROCESSING_LISTO:
        return

    prefijo = Path(prefix_path)
    plugins = prefijo / "python" / "plugins"
    if plugins.is_dir() and str(plugins) not in sys.path:
        sys.path.append(str(plugins))

    if not os.environ.get("GISBASE"):
        base_grass = _localizar_grass(prefijo)
        if base_grass is not None:
            os.environ["GISBASE"] = str(base_grass)
            if logger is not None:
                logger.info("GISBASE definido en %s", base_grass)
        elif logger is not None:
            logger.warning(
                "No se localizó la instalación de GRASS bajo %s. Los algoritmos "
                "grass: fallarán al ejecutarse.", prefijo.parent.parent / "apps",
            )

    try:
        from processing.core.Processing import Processing
    except ImportError as exc:
        raise ErrorEntorno(
            f"No se pudo importar el marco de Processing desde {plugins}: {exc}"
        ) from exc

    Processing.initialize()
    _PROCESSING_LISTO = True


def _localizar_grass(prefijo_qgis: Path) -> Path | None:
    """
    Busca la instalación de GRASS que acompaña a QGIS.

    La ruta típica en Windows es <raiz QGIS>/apps/grass/grass<version>. Se toma
    la versión más alta si hay varias.
    """
    raiz = prefijo_qgis.parent.parent  # <prefix>/apps/qgis -> <raiz QGIS>
    directorio = raiz / "apps" / "grass"
    if not directorio.is_dir():
        return None
    candidatos = sorted(
        (p for p in directorio.glob("grass*") if (p / "bin").is_dir()),
        key=lambda p: p.name,
    )
    return candidatos[-1] if candidatos else None


# =============================================================================
# Campos y escritura
# =============================================================================
def tipos_qgis() -> dict[str, Any]:
    """
    Traduce los tipos del repositorio a los de QGIS.

    QGIS 4 construye los campos con QMetaType; las series 3.x usaban QVariant.
    Se resuelve aquí, en un solo punto, para que un cambio de versión no obligue
    a tocar la declaración de campos de ningún módulo.
    """
    try:
        from qgis.PyQt.QtCore import QMetaType

        return {
            "texto": QMetaType.Type.QString,
            "entero": QMetaType.Type.Int,
            "decimal": QMetaType.Type.Double,
            "fecha": QMetaType.Type.QDate,
        }
    except (ImportError, AttributeError):  # pragma: no cover - QGIS 3.x
        from qgis.PyQt.QtCore import QVariant

        return {
            "texto": QVariant.String,
            "entero": QVariant.Int,
            "decimal": QVariant.Double,
            "fecha": QVariant.Date,
        }


def reescribir_prj_con_autoridad(destino: Path, crs_id: str) -> None:
    """
    Reescribe el .prj en WKT1 de GDAL, que sí declara el código EPSG.

    QGIS escribe el .prj de un shapefile en WKT1 sabor ESRI, que no incluye nodo
    AUTHORITY. El archivo es válido y QGIS lo reinterpreta bien, pero ninguna
    herramienta que lea el .prj como texto puede confirmar de qué CRS se trata,
    empezando por el adaptador comun.shapefile que usa el M00c.

    WKT1_GDAL conserva la compatibilidad de WKT1, de modo que ArcGIS y GDAL lo
    leen igual, y añade AUTHORITY["EPSG",...].
    """
    from qgis.core import QgsCoordinateReferenceSystem

    crs = QgsCoordinateReferenceSystem(crs_id)
    if not crs.isValid():
        return

    variante = getattr(QgsCoordinateReferenceSystem, "WKT1_GDAL", None)
    if variante is None:  # pragma: no cover - depende de la versión de QGIS
        return

    wkt = crs.toWkt(variante)
    if 'AUTHORITY["EPSG"' not in wkt:  # pragma: no cover - CRS sin autoridad
        return

    destino.with_suffix(".prj").write_text(wkt, encoding="utf-8")


def escribir_capa(
    destino: Path,
    campos_salida: Sequence[CampoSalida],
    geometrias: Sequence[Any],
    valores: Sequence[dict[str, Any]],
    crs_id: str,
    tipo_geometria: str,
) -> Path:
    """
    Escribe una capa vectorial con su .prj explícito.

    Recibe listas paralelas de geometrías y de diccionarios de atributos, de
    modo que sirve tanto para una entidad como para varias.

    Excepciones
    -----------
    ErrorFormato
        Si los campos no son escribibles, si las listas no tienen la misma
        longitud o si QGIS no pudo escribir el archivo.
    """
    from qgis.core import (
        QgsCoordinateTransformContext, QgsFeature, QgsField,
        QgsVectorFileWriter, QgsVectorLayer,
    )

    if len(geometrias) != len(valores):
        raise ErrorFormato(
            f"Se recibieron {len(geometrias)} geometría(s) y {len(valores)} "
            "juego(s) de atributos."
        )

    mod_campos.validar_campos(campos_salida)
    tipos = tipos_qgis()

    capa = QgsVectorLayer(f"{tipo_geometria}?crs={crs_id}", destino.stem, "memory")
    proveedor = capa.dataProvider()
    proveedor.addAttributes([
        QgsField(campo.corto, tipos[campo.tipo],
                 len=campo.longitud, prec=campo.precision)
        for campo in campos_salida
    ])
    capa.updateFields()

    entidades = []
    for geometria, atributos in zip(geometrias, valores):
        entidad = QgsFeature(capa.fields())
        entidad.setGeometry(geometria)
        for campo in campos_salida:
            entidad[campo.corto] = atributos.get(campo.corto)
        entidades.append(entidad)
    proveedor.addFeatures(entidades)
    capa.updateExtents()

    destino.parent.mkdir(parents=True, exist_ok=True)

    # Se eliminan los componentes previos en lugar de confiar en el modo de
    # sobrescritura: un .dbf antiguo con otros campos junto a un .shp nuevo
    # produce una capa que abre pero miente.
    for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"):
        destino.with_suffix(extension).unlink(missing_ok=True)

    opciones = QgsVectorFileWriter.SaveVectorOptions()
    opciones.driverName = "ESRI Shapefile"
    opciones.fileEncoding = "UTF-8"

    error, mensaje = QgsVectorFileWriter.writeAsVectorFormatV3(
        capa, str(destino), QgsCoordinateTransformContext(), opciones
    )[:2]

    if error != QgsVectorFileWriter.WriterError.NoError:
        raise ErrorFormato(f"QGIS no pudo escribir {destino}: {mensaje or error}")
    if not destino.with_suffix(".prj").is_file():
        raise ErrorFormato(
            f"{destino.name} se escribió sin .prj. CLAUDE.md, sección 5, exige "
            "escritura explícita del .prj."
        )

    reescribir_prj_con_autoridad(destino, crs_id)
    return destino

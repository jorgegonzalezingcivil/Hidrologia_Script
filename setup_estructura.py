#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
setup_estructura.py
===================
Crea la estructura de carpetas del repositorio del estudio hidrológico.

Es idempotente: puede ejecutarse varias veces sin destruir contenido. Las
carpetas existentes se conservan y solo se crean las faltantes.

Uso:
    python setup_estructura.py                  # crea en el directorio actual
    python setup_estructura.py --raiz C:/ruta   # crea en la ruta indicada
    python setup_estructura.py --verificar      # solo reporta, no crea nada
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# Definición de la estructura.
# La clave es la ruta relativa; el valor es la descripción que se escribe en el
# archivo .gitkeep para que la carpeta sea autoexplicativa dentro del repositorio.
# -----------------------------------------------------------------------------
ESTRUCTURA: dict[str, str] = {
    # --- Configuración -------------------------------------------------------
    "config": "Configuración del estudio: config.yaml, perfiles de ingesta, anexos.",

    # --- Código --------------------------------------------------------------
    "src": "Módulos del flujo de automatización, uno por script independiente.",
    "src/comun": "Utilidades compartidas: logging, rutas, CRS, validaciones.",
    "legacy": "Rutinas heredadas sin modificar, solo como referencia de lógica.",
    "tests": "Pruebas de las funciones críticas de cada módulo.",

    # --- Datos de referencia (fijos, no cambian entre proyectos) -------------
    "data/referencia": "Doctrina técnica: tablas, coeficientes y curvas. Nunca en código.",
    "data/referencia/sig": "Capas nacionales fijas: subzonas hidrográficas, catálogo de estaciones.",

    # --- Insumos aportados por el usuario ------------------------------------
    "data/00_insumos_usuario": "Insumos del proyecto declarados en MANIFIESTO.yaml.",
    "data/00_insumos_usuario/suelos": "Shape o ráster de suelos. Insumo obligatorio.",
    "data/00_insumos_usuario/cobertura": "Cobertura del proyecto. Opcional.",
    "data/00_insumos_usuario/caudales": "Series de caudal o nivel suministradas. Opcional.",
    "data/00_insumos_usuario/homologacion": "Tablas de homologación diligenciadas por el consultor.",

    # --- Datos crudos --------------------------------------------------------
    "data/01_crudos": "Información descargada sin procesar. No editar manualmente.",
    "data/01_crudos/ideam": "Descargas IDEAM: API Socrata y respaldo .zip de DHIME.",
    "data/01_crudos/ideam/api": "Respuestas de la API Socrata con su metadato de consulta.",
    "data/01_crudos/ideam/zip": "Archivos .zip descargados de DHIME.",
    "data/01_crudos/dem": "Escenas ALOS PALSAR RTC descargadas de ASF.",
    "data/01_crudos/enso": "Archivo ONI de la NOAA con fecha de descarga.",

    # --- Datos procesados ----------------------------------------------------
    "data/02_procesado": "Productos intermedios versionados de cada módulo.",
    "data/02_procesado/series": "Series normalizadas: diaria, mensual, máximos anuales.",
    "data/02_procesado/estaciones": "Catálogo filtrado y matrices de sensibilidad.",
    "data/02_procesado/enso": "Clasificación ENSO-ONI y agregaciones por fase.",
    "data/02_procesado/frecuencia": "Ajustes de distribución y valores por periodo de retorno.",
    "data/02_procesado/tormenta": "Curvas IDF y hietogramas por hipótesis y escenario.",

    # --- Información geográfica ----------------------------------------------
    "data/03_SIG": "Salidas geográficas de los módulos. Se montan en el proyecto QGIS.",
    "data/03_SIG/vector": "Shapefiles resultantes, con .prj explícito.",
    "data/03_SIG/raster": "Rásteres: DEM recortado, isoyetas, pendientes, CN.",
    "data/03_SIG/temp": "Archivos temporales de geoprocesamiento. Se puede vaciar.",
    "data/03_SIG/proyecto": "Archivo estudio_hidrologico.qgz y estilos .qml.",

    # --- Modelos -------------------------------------------------------------
    "data/04_modelos": "Modelos construidos y sus archivos de proyecto.",
    "data/04_modelos/hec_hms": "Proyecto HEC-HMS: .basin, .met, .gage, .control, .run, .dss.",

    # --- Resultados ----------------------------------------------------------
    "data/05_resultados": "Resultados finales exportados.",
    "data/05_resultados/excel": "Hojas de caracterización, resultados y memorias de cálculo.",
    "data/05_resultados/graficos": "Figuras generadas por los módulos de análisis.",

    # --- Salidas -------------------------------------------------------------
    "outputs/06_informe": "Informe en Word generado por el M15.",
    "outputs/07_anexos": "Anexos ensamblados por el M17 según config/anexos.yaml.",
    "outputs/mapas": "Mapas temáticos exportados por el M16.",

    # --- Plantillas y documentación -----------------------------------------
    "templates": "Plantilla .dotx del informe y plantillas .qpt de mapas.",
    "templates/mapas": "Composiciones de impresión de QGIS.",
    "docs/referencia": "Informe modelo y documentación de apoyo.",
    "logs": "Registros de ejecución por módulo, con parámetros y versiones.",
}

# Archivos que deben existir para que el repositorio sea utilizable.
ARCHIVOS_ESPERADOS = [
    "CLAUDE.md",
    "config/config.yaml",
    "data/00_insumos_usuario/MANIFIESTO.yaml",
]


def crear_estructura(raiz: Path, verificar: bool = False) -> int:
    """
    Crea (o verifica) la estructura de carpetas.

    Devuelve el número de carpetas faltantes encontradas. En modo verificación
    no se crea nada, solo se reporta.
    """
    creadas, existentes = 0, 0

    for ruta_rel, descripcion in ESTRUCTURA.items():
        carpeta = raiz / ruta_rel

        if carpeta.is_dir():
            existentes += 1
            estado = "existe"
        else:
            estado = "FALTA" if verificar else "creada"
            if not verificar:
                carpeta.mkdir(parents=True, exist_ok=True)
            creadas += 1

        # El .gitkeep documenta la carpeta y permite versionarla vacía.
        if not verificar:
            gitkeep = carpeta / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.write_text(descripcion + "\n", encoding="utf-8")

        print(f"  [{estado:>7}] {ruta_rel}")

    print(f"\nCarpetas existentes: {existentes} | "
          f"{'faltantes' if verificar else 'creadas'}: {creadas}")
    return creadas


def verificar_archivos(raiz: Path) -> list[str]:
    """Reporta los archivos base que aún no están presentes."""
    return [a for a in ARCHIVOS_ESPERADOS if not (raiz / a).is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crea la estructura de carpetas del estudio hidrológico."
    )
    parser.add_argument("--raiz", default=".",
                        help="Directorio raíz del repositorio (por defecto, el actual).")
    parser.add_argument("--verificar", action="store_true",
                        help="Solo reporta el estado, sin crear carpetas.")
    args = parser.parse_args()

    raiz = Path(args.raiz).resolve()
    modo = "VERIFICACIÓN" if args.verificar else "CREACIÓN"

    print(f"\n{modo} DE ESTRUCTURA")
    print(f"Raíz: {raiz}\n")

    if not raiz.exists():
        if args.verificar:
            print(f"ERROR: la raíz no existe: {raiz}")
            return 1
        raiz.mkdir(parents=True)

    faltantes = crear_estructura(raiz, verificar=args.verificar)

    archivos_faltantes = verificar_archivos(raiz)
    if archivos_faltantes:
        print("\nArchivos base pendientes:")
        for a in archivos_faltantes:
            print(f"  - {a}")

    if args.verificar and (faltantes or archivos_faltantes):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

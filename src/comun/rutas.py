# -*- coding: utf-8 -*-
"""
comun.rutas
===========
Resolución de la raíz del proyecto y de las rutas declaradas en la
configuración.

Doctrina (CLAUDE.md, sección 2): ningún módulo contiene rutas absolutas. Todas
las rutas del estudio se expresan relativas a la raíz del repositorio y se
resuelven aquí, en un único punto.

La raíz se determina en este orden:

1. Variable de entorno ``HIDROLOGIA_RAIZ``, si está definida.
2. Ascenso desde el directorio indicado (o desde este archivo) hasta encontrar
   un directorio que contenga los marcadores del repositorio.

Solo usa la librería estándar: es importable desde el entorno de QGIS.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errores import ErrorRutas

__all__ = [
    "MARCADORES_RAIZ",
    "SUBDIRECTORIOS",
    "VARIABLE_ENTORNO_RAIZ",
    "raiz_proyecto",
    "ruta_config",
    "directorio",
    "resolver",
    "resolver_desde",
    "esta_dentro",
    "relativa",
]

# Variable de entorno que permite fijar la raíz de forma explícita, útil al
# ejecutar desde OSGeo4W Shell o desde un programador de tareas.
VARIABLE_ENTORNO_RAIZ = "HIDROLOGIA_RAIZ"

# Archivos que identifican de forma inequívoca la raíz del repositorio.
MARCADORES_RAIZ: tuple[str, ...] = ("CLAUDE.md", "setup_estructura.py")

# Directorios del repositorio a los que el código necesita referirse por nombre
# lógico. La clave es el nombre lógico y el valor la ruta relativa a la raíz.
#
# ADVERTENCIA: esta tabla reproduce parte de la estructura declarada en
# setup_estructura.py. Mientras las dos definiciones coexistan, cualquier cambio
# debe aplicarse en ambas o el código apuntará a carpetas inexistentes.
SUBDIRECTORIOS: dict[str, str] = {
    "config": "config",
    "src": "src",
    "comun": "src/comun",
    "legacy": "legacy",
    "tests": "tests",
    "referencia": "data/referencia",
    "referencia_sig": "data/referencia/sig",
    "insumos": "data/00_insumos_usuario",
    "insumos_suelos": "data/00_insumos_usuario/suelos",
    "insumos_cobertura": "data/00_insumos_usuario/cobertura",
    "insumos_caudales": "data/00_insumos_usuario/caudales",
    "insumos_homologacion": "data/00_insumos_usuario/homologacion",
    "crudos": "data/01_crudos",
    "crudos_ideam": "data/01_crudos/ideam",
    "crudos_ideam_api": "data/01_crudos/ideam/api",
    "crudos_ideam_zip": "data/01_crudos/ideam/zip",
    "crudos_dem": "data/01_crudos/dem",
    "crudos_enso": "data/01_crudos/enso",
    "procesado": "data/02_procesado",
    "procesado_series": "data/02_procesado/series",
    "procesado_estaciones": "data/02_procesado/estaciones",
    "procesado_frecuencia": "data/02_procesado/frecuencia",
    "procesado_tormenta": "data/02_procesado/tormenta",
    "sig": "data/03_SIG",
    "sig_vector": "data/03_SIG/vector",
    "sig_raster": "data/03_SIG/raster",
    "sig_temp": "data/03_SIG/temp",
    "sig_proyecto": "data/03_SIG/proyecto",
    "modelos": "data/04_modelos",
    "modelos_hec_hms": "data/04_modelos/hec_hms",
    "resultados": "data/05_resultados",
    "resultados_excel": "data/05_resultados/excel",
    "resultados_graficos": "data/05_resultados/graficos",
    "informe": "outputs/06_informe",
    "anexos": "outputs/07_anexos",
    "mapas": "outputs/mapas",
    "templates": "templates",
    "templates_mapas": "templates/mapas",
    "docs_referencia": "docs/referencia",
    "logs": "logs",
}

# Ruta del archivo de configuración, relativa a la raíz.
_CONFIG_RELATIVA = "config/config.yaml"

# Ruta del manifiesto de insumos, relativa a la raíz. Sus rutas internas se
# resuelven contra su propio directorio, no contra la raíz (ver resolver_desde).
_MANIFIESTO_RELATIVA = "data/00_insumos_usuario/MANIFIESTO.yaml"


def _es_raiz(candidato: Path) -> bool:
    """Indica si el directorio contiene todos los marcadores del repositorio."""
    return all((candidato / marcador).is_file() for marcador in MARCADORES_RAIZ)


def raiz_proyecto(inicio: str | os.PathLike | None = None) -> Path:
    """
    Devuelve la raíz del repositorio como ruta absoluta y resuelta.

    Parámetros
    ----------
    inicio:
        Directorio o archivo desde el cual iniciar el ascenso. Si es None se
        parte de la ubicación de este archivo.

    Excepciones
    -----------
    ErrorRutas
        Si la variable de entorno apunta a un directorio que no es la raíz, o
        si el ascenso no encuentra los marcadores.
    """
    declarada = os.environ.get(VARIABLE_ENTORNO_RAIZ, "").strip()
    if declarada:
        candidato = Path(declarada).expanduser().resolve()
        if not candidato.is_dir():
            raise ErrorRutas(
                f"{VARIABLE_ENTORNO_RAIZ} apunta a un directorio inexistente: "
                f"{candidato}"
            )
        if not _es_raiz(candidato):
            faltantes = [m for m in MARCADORES_RAIZ if not (candidato / m).is_file()]
            raise ErrorRutas(
                f"{VARIABLE_ENTORNO_RAIZ} apunta a {candidato}, que no es la raíz "
                f"del repositorio (faltan: {', '.join(faltantes)})."
            )
        return candidato

    partida = Path(inicio).resolve() if inicio is not None else Path(__file__).resolve()
    if partida.is_file():
        partida = partida.parent

    for candidato in (partida, *partida.parents):
        if _es_raiz(candidato):
            return candidato

    raise ErrorRutas(
        f"No se encontró la raíz del repositorio ascendiendo desde {partida}. "
        f"Se buscaron los marcadores: {', '.join(MARCADORES_RAIZ)}. "
        f"Definir {VARIABLE_ENTORNO_RAIZ} para fijarla de forma explícita."
    )


def ruta_config(raiz: str | os.PathLike | None = None) -> Path:
    """Devuelve la ruta de config/config.yaml."""
    base = Path(raiz).resolve() if raiz is not None else raiz_proyecto()
    return base / _CONFIG_RELATIVA


def ruta_manifiesto(raiz: str | os.PathLike | None = None) -> Path:
    """Devuelve la ruta de data/00_insumos_usuario/MANIFIESTO.yaml."""
    base = Path(raiz).resolve() if raiz is not None else raiz_proyecto()
    return base / _MANIFIESTO_RELATIVA


def directorio(
    clave: str,
    raiz: str | os.PathLike | None = None,
    crear: bool = False,
) -> Path:
    """
    Devuelve el directorio asociado a un nombre lógico de SUBDIRECTORIOS.

    Parámetros
    ----------
    clave:
        Nombre lógico, por ejemplo 'procesado_series' o 'logs'.
    crear:
        Si es True, crea el directorio y sus padres cuando no existan.

    Excepciones
    -----------
    ErrorRutas
        Si la clave no está declarada.
    """
    if clave not in SUBDIRECTORIOS:
        disponibles = ", ".join(sorted(SUBDIRECTORIOS))
        raise ErrorRutas(
            f"Directorio lógico desconocido: '{clave}'. Disponibles: {disponibles}"
        )

    base = Path(raiz).resolve() if raiz is not None else raiz_proyecto()
    destino = base / SUBDIRECTORIOS[clave]
    if crear:
        destino.mkdir(parents=True, exist_ok=True)
    return destino


def resolver(
    ruta_relativa: str | os.PathLike,
    raiz: str | os.PathLike | None = None,
) -> Path:
    """
    Resuelve una ruta declarada en la configuración contra la raíz del proyecto.

    Una ruta absoluta se devuelve tal cual, resuelta. Esto permite que el
    consultor apunte a un insumo fuera del repositorio de forma deliberada, pero
    el módulo que la reciba debe advertirlo en su log.
    """
    candidata = Path(ruta_relativa)
    if candidata.is_absolute():
        return candidata.resolve()
    base = Path(raiz).resolve() if raiz is not None else raiz_proyecto()
    return (base / candidata).resolve()


def resolver_desde(
    base: str | os.PathLike,
    ruta_relativa: str | os.PathLike,
) -> Path:
    """
    Resuelve una ruta contra un directorio base distinto de la raíz.

    Se usa para el MANIFIESTO.yaml, cuyas rutas internas están declaradas
    relativas a data/00_insumos_usuario/ y no a la raíz del repositorio.
    """
    candidata = Path(ruta_relativa)
    if candidata.is_absolute():
        return candidata.resolve()
    directorio_base = Path(base).resolve()
    if directorio_base.is_file():
        directorio_base = directorio_base.parent
    return (directorio_base / candidata).resolve()


def esta_dentro(
    ruta: str | os.PathLike,
    base: str | os.PathLike | None = None,
) -> bool:
    """
    Indica si una ruta queda dentro de un directorio base.

    Se usa para impedir que archivos que no deben salir de la máquina, como el
    de credenciales, se coloquen dentro del árbol del repositorio: esa carpeta
    se comprime y se entrega como anexo, y ni el .gitignore ni ninguna otra
    protección de git alcanzan a una copia o a un .zip.
    """
    referencia = Path(base).resolve() if base is not None else raiz_proyecto()
    candidata = Path(ruta).expanduser().resolve()
    try:
        candidata.relative_to(referencia)
    except ValueError:
        return False
    return True


def relativa(ruta: str | os.PathLike, raiz: str | os.PathLike | None = None) -> str:
    """
    Devuelve la ruta relativa a la raíz, con separador '/', para los reportes.

    Si la ruta queda fuera del repositorio se devuelve absoluta, de modo que el
    log siempre permita ubicar el archivo.
    """
    base = Path(raiz).resolve() if raiz is not None else raiz_proyecto()
    absoluta = Path(ruta).resolve()
    try:
        return absoluta.relative_to(base).as_posix()
    except ValueError:
        return absoluta.as_posix()

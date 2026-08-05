# -*- coding: utf-8 -*-
"""
comun.registro
==============
Logging por módulo.

Doctrina (CLAUDE.md, sección 2): log por módulo, con versiones de librerías,
parámetros usados y fecha de ejecución. El log es el respaldo del estudio ante
interventoría: debe permitir reconstruir con qué insumos y con qué parámetros se
obtuvo cada resultado.

Cada ejecución escribe su propio archivo en logs/, con marca de tiempo, de modo
que una corrida no borra la evidencia de la anterior.

Solo usa la librería estándar: es importable desde el entorno de QGIS.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import entorno as _entorno
from . import rutas as _rutas

__all__ = [
    "configurar",
    "registrar_cabecera",
    "registrar_cierre",
    "bloque",
    "ruta_log",
    "SEPARADOR",
]

SEPARADOR = "-" * 78

_FORMATO_ARCHIVO = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_FORMATO_CONSOLA = "%(levelname)-8s | %(message)s"
_FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"

# Atributo donde se guarda la ruta del archivo de log asociado a cada logger.
_ATRIBUTO_RUTA = "_ruta_archivo_log"


def _preparar_consola() -> None:
    """
    Fuerza UTF-8 en la salida estándar cuando el intérprete lo permite.

    La consola de Windows suele usar una página de códigos que no admite los
    acentos de los mensajes. Sin esto, un mensaje con tilde aborta el módulo por
    UnicodeEncodeError, que es exactamente el tipo de fallo irrelevante que no
    debe detener un estudio.
    """
    for flujo in (sys.stdout, sys.stderr):
        reconfigurar = getattr(flujo, "reconfigure", None)
        if reconfigurar is None:
            continue
        try:
            reconfigurar(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


def configurar(
    modulo: str,
    nivel: str = "INFO",
    raiz: str | os.PathLike | None = None,
    consola: bool = True,
    archivo: bool = True,
    marca_tiempo: str | None = None,
) -> logging.Logger:
    """
    Devuelve el logger del módulo, con salida a archivo y a consola.

    Parámetros
    ----------
    modulo:
        Identificador del módulo, por ejemplo 'M00' o 'M04'. Da nombre al logger
        y al archivo de log.
    nivel:
        Nivel mínimo registrado. Normalmente proviene de `ejecucion.nivel_log`.
    raiz:
        Raíz del repositorio. Si es None se detecta.
    consola / archivo:
        Permiten desactivar cada destino. En pruebas se desactiva el archivo.
    marca_tiempo:
        Sufijo del nombre del archivo. Si es None se usa el instante actual.

    La llamada es idempotente: invocarla dos veces para el mismo módulo no
    duplica los manejadores ni el contenido del log.
    """
    if consola:
        _preparar_consola()

    logger = logging.getLogger(modulo)
    logger.setLevel(getattr(logging, str(nivel).upper(), logging.INFO))
    # El logger escribe solo en sus propios manejadores: evita que la
    # configuración global de otra librería duplique o desvíe los mensajes.
    logger.propagate = False

    for manejador in list(logger.handlers):
        logger.removeHandler(manejador)
        manejador.close()

    if consola:
        manejador_consola = logging.StreamHandler(stream=sys.stdout)
        manejador_consola.setFormatter(
            logging.Formatter(_FORMATO_CONSOLA, datefmt=_FORMATO_FECHA)
        )
        logger.addHandler(manejador_consola)

    destino: Path | None = None
    if archivo:
        base = Path(raiz).resolve() if raiz is not None else _rutas.raiz_proyecto()
        directorio_logs = _rutas.directorio("logs", base, crear=True)
        sufijo = marca_tiempo or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        destino = directorio_logs / f"{modulo}_{sufijo}.log"

        manejador_archivo = logging.FileHandler(destino, encoding="utf-8")
        manejador_archivo.setFormatter(
            logging.Formatter(_FORMATO_ARCHIVO, datefmt=_FORMATO_FECHA)
        )
        logger.addHandler(manejador_archivo)

    setattr(logger, _ATRIBUTO_RUTA, destino)
    return logger


def ruta_log(logger: logging.Logger) -> Path | None:
    """Devuelve la ruta del archivo de log del logger, o None si no lo tiene."""
    return getattr(logger, _ATRIBUTO_RUTA, None)


def registrar_cabecera(
    logger: logging.Logger,
    modulo: str,
    descripcion: str = "",
    config: Any = None,
    parametros: Mapping[str, Any] | None = None,
    insumos: Mapping[str, Any] | None = None,
) -> None:
    """
    Escribe la cabecera de trazabilidad al inicio de un módulo.

    Registra fecha de ejecución, intérprete, entorno, versiones de librerías,
    identificación del archivo de configuración con su huella, los parámetros
    efectivamente usados y los insumos leídos.

    Parámetros
    ----------
    config:
        Objeto Config. Se registran su ruta y su sha256, lo que permite
        demostrar que dos resultados provienen de la misma configuración.
    parametros:
        Parámetros que el módulo va a usar. Se obtienen con Config.parametros().
    insumos:
        Archivos de entrada, con la etiqueta que el módulo prefiera.
    """
    resumen = _entorno.resumen_entorno()

    logger.info(SEPARADOR)
    logger.info("MÓDULO %s%s", modulo, f" - {descripcion}" if descripcion else "")
    logger.info(SEPARADOR)
    logger.info("Fecha de ejecución : %s", _dt.datetime.now().isoformat(timespec="seconds"))
    logger.info("Entorno            : %s", resumen["entorno"])
    logger.info("Python             : %s (%s)", resumen["python"], resumen["implementacion"])
    logger.info("Ejecutable         : %s", resumen["ejecutable"])
    logger.info("Sistema            : %s (%s)", resumen["sistema"], resumen["arquitectura"])

    librerias = resumen["librerias"]
    if librerias:
        detalle = ", ".join(f"{nombre} {version}" for nombre, version in librerias.items())
        logger.info("Librerías          : %s", detalle)
    else:
        logger.info("Librerías          : ninguna de las vigiladas está instalada")

    if config is not None:
        ruta_config = getattr(config, "ruta", None)
        huella = getattr(config, "sha256", None)
        if ruta_config is not None:
            logger.info("Configuración      : %s", ruta_config)
        if huella:
            logger.info("sha256 config      : %s", huella)

    archivo_log = ruta_log(logger)
    if archivo_log is not None:
        logger.info("Archivo de log     : %s", archivo_log)

    if insumos:
        logger.info(SEPARADOR)
        logger.info("INSUMOS")
        for etiqueta, valor in insumos.items():
            logger.info("  %-28s %s", etiqueta, valor)

    if parametros:
        logger.info(SEPARADOR)
        logger.info("PARÁMETROS")
        for clave in sorted(parametros):
            logger.info("  %-40s %r", clave, parametros[clave])

    logger.info(SEPARADOR)


def registrar_cierre(
    logger: logging.Logger,
    modulo: str,
    estado: str,
    segundos: float | None = None,
    productos: Mapping[str, Any] | None = None,
) -> None:
    """
    Escribe el cierre del módulo con su estado y los productos generados.

    Parámetros
    ----------
    estado:
        Texto breve, por ejemplo 'CORRECTO', 'CON ADVERTENCIAS' o 'DETENIDO'.
    productos:
        Archivos generados, con la etiqueta que el módulo prefiera.
    """
    logger.info(SEPARADOR)
    if productos:
        logger.info("PRODUCTOS")
        for etiqueta, valor in productos.items():
            logger.info("  %-28s %s", etiqueta, valor)
        logger.info(SEPARADOR)

    duracion = f" en {segundos:.2f} s" if segundos is not None else ""
    logger.info("MÓDULO %s finalizado: %s%s", modulo, estado, duracion)
    logger.info(SEPARADOR)


@contextmanager
def bloque(logger: logging.Logger, descripcion: str) -> Iterator[None]:
    """
    Delimita una etapa del módulo en el log y mide su duración.

    Cualquier excepción se registra con su traza antes de propagarse: el módulo
    se detiene, pero la evidencia del punto de fallo queda escrita.
    """
    logger.info("> %s", descripcion)
    inicio = time.perf_counter()
    try:
        yield
    except Exception:
        transcurrido = time.perf_counter() - inicio
        logger.exception(
            "x %s: interrumpido tras %.2f s", descripcion, transcurrido
        )
        raise
    else:
        transcurrido = time.perf_counter() - inicio
        logger.info("< %s: completado en %.2f s", descripcion, transcurrido)

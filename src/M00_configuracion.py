#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M00 - Configuración, utilidades comunes y logging
=================================================
Entorno: venv del proyecto (también ejecutable bajo el Python de QGIS).

Alcance del módulo:

1. Provee el paquete `src/comun`, que el resto de módulos importa para leer la
   configuración, resolver rutas y escribir su log.
2. Como script ejecutable, valida config/config.yaml contra el esquema
   declarativo y emite el reporte de conformidad.

Ningún módulo posterior debe ejecutarse si este reporta un hallazgo BLOQUEANTE:
la configuración es el único origen de los parámetros del estudio, y un
parámetro inválido se propaga en silencio hasta el informe.

Uso:
    python src/M00_configuracion.py
    python src/M00_configuracion.py --estricto
    python src/M00_configuracion.py --config otra/config.yaml
    python src/M00_configuracion.py --json logs/M00_reporte.json

Códigos de salida:
    0  configuración conforme
    1  hay hallazgos bloqueantes
    2  hay advertencias y se ejecutó en modo estricto
    3  no se pudo leer la configuración o resolver el repositorio
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Sequence

# El módulo se ejecuta como script independiente (CLAUDE.md, sección 2), de modo
# que su directorio padre debe estar en sys.path para importar el paquete comun.
_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, huella_sha256, leer_yaml  # noqa: E402
from comun.errores import ErrorConfiguracion, ErrorHidrologia, ErrorRutas  # noqa: E402

MODULO = "M00"
DESCRIPCION = "Configuración, utilidades comunes y logging"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ESTRICTA = 2
SALIDA_ERROR = 3


# =============================================================================
# Funciones puras
# =============================================================================
def verificar_estructura(raiz: Path) -> list[esquema.Hallazgo]:
    """
    Comprueba que existan los directorios lógicos declarados en comun.rutas.

    Un directorio faltante no invalida la configuración, pero el módulo que
    intente escribir en él fallaría a mitad de proceso. Se reporta como
    advertencia y se resuelve ejecutando setup_estructura.py.
    """
    hallazgos: list[esquema.Hallazgo] = []
    for clave, relativa in sorted(rutas.SUBDIRECTORIOS.items()):
        if not (raiz / relativa).is_dir():
            hallazgos.append(esquema.Hallazgo(
                esquema.ADVERTENCIA, f"<estructura>.{clave}",
                f"el directorio {relativa} no existe. Ejecutar "
                f"setup_estructura.py para crearlo.",
            ))
    return hallazgos


def clasificar(hallazgos: Sequence[esquema.Hallazgo], severidad: str) -> list:
    """Filtra los hallazgos de una severidad, conservando el orden."""
    return [h for h in hallazgos if h.severidad == severidad]


def determinar_salida(
    hallazgos: Sequence[esquema.Hallazgo],
    estricto: bool,
) -> int:
    """Traduce el conjunto de hallazgos al código de salida del proceso."""
    if esquema.hay_bloqueantes(hallazgos):
        return SALIDA_BLOQUEANTE
    if estricto and clasificar(hallazgos, esquema.ADVERTENCIA):
        return SALIDA_ESTRICTA
    return SALIDA_CORRECTA


def construir_reporte(
    hallazgos: Sequence[esquema.Hallazgo],
    ruta_config: Path,
    sha256: str,
    raiz: Path,
    codigo_salida: int,
) -> dict:
    """
    Arma el reporte serializable de la validación.

    Se escribe en JSON para que el M17 pueda incorporarlo a los anexos como
    evidencia de que el estudio se ejecutó con una configuración conforme.
    """
    import datetime as dt

    return {
        "modulo": MODULO,
        "fecha": dt.datetime.now().isoformat(timespec="seconds"),
        "raiz": str(raiz),
        "configuracion": {
            "ruta": rutas.relativa(ruta_config, raiz),
            "sha256": sha256,
        },
        "resumen": esquema.resumen_por_severidad(hallazgos),
        "codigo_salida": codigo_salida,
        "conforme": codigo_salida == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }


# =============================================================================
# Presentación
# =============================================================================
def _emitir_hallazgos(logger, hallazgos: Sequence[esquema.Hallazgo]) -> None:
    """Escribe los hallazgos agrupados por severidad, del más grave al menor."""
    niveles = (
        (esquema.BLOQUEANTE, logger.error),
        (esquema.ADVERTENCIA, logger.warning),
        (esquema.INFORMATIVO, logger.info),
    )
    for severidad, emitir in niveles:
        grupo = clasificar(hallazgos, severidad)
        if not grupo:
            continue
        emitir("%s (%d)", severidad, len(grupo))
        for hallazgo in grupo:
            emitir("  %-52s %s", hallazgo.clave, hallazgo.mensaje)


def _emitir_resumen(logger, hallazgos: Sequence[esquema.Hallazgo], codigo: int) -> None:
    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info(
        "RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
        conteo[esquema.BLOQUEANTE],
        conteo[esquema.ADVERTENCIA],
        conteo[esquema.INFORMATIVO],
    )
    if codigo == SALIDA_CORRECTA:
        logger.info("La configuración es conforme. Los módulos pueden ejecutarse.")
    elif codigo == SALIDA_BLOQUEANTE:
        logger.error(
            "La configuración NO es utilizable. Corregir los hallazgos "
            "bloqueantes antes de ejecutar cualquier módulo."
        )
    else:
        logger.warning(
            "Modo estricto: las advertencias detienen la ejecución. Corregirlas "
            "o documentar la decisión en MANIFIESTO.yaml."
        )


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    ruta_config: Path | None = None,
    raiz: Path | None = None,
    estricto: bool = False,
    verificar_rutas: bool = True,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[esquema.Hallazgo]]:
    """
    Valida la configuración y emite el reporte. Devuelve (codigo, hallazgos).

    No lanza excepciones por hallazgos: el propósito del M00 es reportarlos
    todos de una vez. Sí las lanza si el archivo no se puede leer, porque en ese
    caso no hay nada que reportar.
    """
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    destino = Path(ruta_config).resolve() if ruta_config else rutas.ruta_config(base)

    logger = registro.configurar(MODULO, nivel="INFO", raiz=base, consola=consola)

    # La carga se hace sin validar para poder reportar todos los hallazgos en
    # lugar de detenerse en el primero.
    datos = leer_yaml(destino)
    configuracion = Config(datos, destino, base, huella_sha256(destino))

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION,
        config=configuracion,
        insumos={
            "raíz del repositorio": base,
            "archivo de configuración": rutas.relativa(destino, base),
            "modo": "estricto" if estricto else "normal",
        },
    )

    hallazgos: list[esquema.Hallazgo] = []
    with registro.bloque(logger, "Validación del esquema de configuración"):
        hallazgos.extend(esquema.validar(datos, raiz=base, verificar_rutas=verificar_rutas))

    with registro.bloque(logger, "Verificación de la estructura de directorios"):
        hallazgos.extend(verificar_estructura(base))

    hallazgos.sort(key=lambda h: (
        {esquema.BLOQUEANTE: 0, esquema.ADVERTENCIA: 1, esquema.INFORMATIVO: 2}
        .get(h.severidad, 9), h.clave))

    logger.info(registro.SEPARADOR)
    _emitir_hallazgos(logger, hallazgos)

    codigo = determinar_salida(hallazgos, estricto)
    _emitir_resumen(logger, hallazgos, codigo)

    productos = {}
    if ruta_json is not None:
        reporte = construir_reporte(
            hallazgos, destino, configuracion.sha256, base, codigo
        )
        ruta_json.parent.mkdir(parents=True, exist_ok=True)
        ruta_json.write_text(
            json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        productos["reporte JSON"] = rutas.relativa(ruta_json, base)

    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecución"] = rutas.relativa(archivo_log, base)

    estados = {
        SALIDA_CORRECTA: "CORRECTO",
        SALIDA_BLOQUEANTE: "DETENIDO POR HALLAZGOS BLOQUEANTES",
        SALIDA_ESTRICTA: "DETENIDO POR ADVERTENCIAS EN MODO ESTRICTO",
    }
    registro.registrar_cierre(
        logger, MODULO, estados[codigo],
        segundos=time.perf_counter() - inicio,
        productos=productos,
    )
    return codigo, hallazgos


# =============================================================================
# Interfaz de línea de comandos
# =============================================================================
def _analizar_argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        prog="M00_configuracion.py",
        description="Valida config/config.yaml contra el esquema del estudio.",
    )
    analizador.add_argument(
        "--config", type=Path, default=None,
        help="Archivo de configuración a validar (por defecto config/config.yaml).",
    )
    analizador.add_argument(
        "--raiz", type=Path, default=None,
        help="Raíz del repositorio (por defecto se detecta por sus marcadores).",
    )
    analizador.add_argument(
        "--estricto", action="store_true",
        help="Las advertencias también producen un código de salida distinto de cero.",
    )
    analizador.add_argument(
        "--sin-rutas", action="store_true",
        help="Omite la verificación de existencia de los archivos declarados.",
    )
    analizador.add_argument(
        "--json", type=Path, default=None, dest="json_salida",
        help="Escribe el reporte de validación en el archivo JSON indicado.",
    )
    analizador.add_argument(
        "--silencioso", action="store_true",
        help="No escribe en consola; el log de archivo se genera igualmente.",
    )
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            ruta_config=argumentos.config,
            raiz=argumentos.raiz,
            estricto=argumentos.estricto,
            verificar_rutas=not argumentos.sin_rutas,
            ruta_json=argumentos.json_salida,
            consola=not argumentos.silencioso,
        )
        return codigo
    except (ErrorRutas, ErrorConfiguracion) as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR
    except ErrorHidrologia as exc:
        print(f"ERROR    | {exc}", file=sys.stderr)
        return SALIDA_ERROR


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Adaptador de ejecución de HEC-HMS
=================================
Entorno: venv del proyecto.

Por qué existe y por qué aquí. HEC-HMS admite ejecución sin interfaz mediante un
guion en Jython, y esa es la única forma de que la cadena compute sin abrir
software (CLAUDE.md, sección 4). Todo lo que depende del ejecutable, de su
lanzador y del vocabulario de su API de guion vive en este archivo, de modo que
una actualización del programa se corrige en un sitio (sección 2).

LA API ESTÁ LEÍDA DE LA PROPIA INSTALACIÓN, no supuesta. Los métodos públicos de
'hms/model/JythonHms.class' en hms.jar de la 4.13 son, entre otros:

    OpenProject(String nombre, String directorio)
    ComputeRun(String corrida)
    SaveAllProjectComponents()
    Exit(int codigo)

EL LANZADOR SE INVOCA DESDE SU PROPIO DIRECTORIO. 'HEC-HMS.cmd' llama a
'jre\\bin\\java' por ruta relativa: ejecutarlo desde otro sitio no encuentra la
máquina virtual. El directorio de trabajo del subproceso es, por tanto, el de la
instalación, y no el del estudio.

UNA SOLA SESIÓN PARA TODAS LAS CORRIDAS. Arrancar la JVM y cargar un proyecto de
ciento veinticinco subcuencas cuesta unos dos minutos y medio; computar una
corrida, quince segundos. Ocho invocaciones separadas pagarían ese arranque ocho
veces.

EL CÓDIGO DE SALIDA DEL PROCESO NO DICE SI EL CÁLCULO SIRVIÓ. Medido: una
invocación con el guion mal referenciado terminó en 0. Quien manda es el log que
cada corrida deja junto al proyecto, y por eso se lee aquí.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "ErrorHms",
    "ResultadoCorrida",
    "guion_de_corridas",
    "leer_log_de_corrida",
    "ruta_lanzador",
    "ejecutar_corridas",
]

# Nombre del lanzador dentro del directorio de instalación. Es lo primero que
# cambiaría en otra plataforma o en otra serie del programa.
LANZADOR = "HEC-HMS.cmd"


class ErrorHms(RuntimeError):
    """Falla al invocar HEC-HMS, que el módulo que llama debe reportar."""


@dataclass
class ResultadoCorrida:
    """Lo que el log de una corrida deja saber sobre ella."""

    corrida: str
    terminada: bool = False
    abortada: bool = False
    errores: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    duracion: str = ""
    ruta_log: str = ""

    @property
    def utilizable(self) -> bool:
        """Cierto si el DSS de la corrida representa el modelo actual."""
        return self.terminada and not self.abortada and not self.errores


def ruta_lanzador(instalacion: Path) -> Path:
    """
    Ubica el lanzador dentro del directorio de instalación.

    Excepciones
    -----------
    ErrorHms
        Si el directorio o el lanzador no están. Se distingue el caso para que
        el mensaje diga cuál de las dos cosas revisar.
    """
    instalacion = Path(instalacion)
    if not instalacion.is_dir():
        raise ErrorHms(
            f"no existe el directorio de instalación de HEC-HMS: {instalacion}. "
            "Se declara en software.hec_hms.ruta.")
    lanzador = instalacion / LANZADOR
    if not lanzador.is_file():
        raise ErrorHms(
            f"no se encuentra {LANZADOR} en {instalacion}. Revisar "
            "software.hec_hms.ruta y software.hec_hms.version.")
    return lanzador


def guion_de_corridas(
    nombre_proyecto: str, directorio_proyecto: Path, corridas: Sequence[str],
) -> str:
    """
    Texto del guion en Jython que abre el proyecto y computa las corridas.

    LAS RUTAS VAN CON BARRA NORMAL. Jython interpreta la contrabarra de Windows
    como escape dentro de una cadena, de modo que 'C:\\HMS' se convierte en un
    tabulador y el proyecto no se encuentra. Es un fallo silencioso: el guion
    corre y no computa nada.

    NO SE GUARDA EL PROYECTO. 'SaveAllProjectComponents' reescribiría los
    archivos que el M13 acaba de dejar, y HEC-HMS los normaliza a su manera al
    guardar. Los resultados van al DSS de cada corrida, que es lo que interesa.

    Excepciones
    -----------
    ErrorHms
        Si no se pide ninguna corrida, o si un nombre lleva comillas: acabaría
        dentro de una cadena de Jython y rompería el guion.
    """
    if not corridas:
        raise ErrorHms("no se indicó ninguna corrida que computar.")
    for corrida in corridas:
        if '"' in corrida or "\\" in corrida or not corrida.strip():
            raise ErrorHms(
                f"el nombre de corrida {corrida!r} no es utilizable en un guion "
                "de Jython.")

    directorio = str(Path(directorio_proyecto).resolve()).replace("\\", "/")
    lineas = [
        "# Generado por el M14. No editar: se reescribe en cada ejecucion.",
        "from hms.model.JythonHms import *",
        "",
        f'OpenProject("{nombre_proyecto}", "{directorio}")',
        "",
    ]
    lineas += [f'ComputeRun("{corrida}")' for corrida in corridas]
    lineas += ["", "Exit(0)", ""]
    return "\n".join(lineas)


# Las corridas dejan su log junto al proyecto. Estas son las marcas que HEC-HMS
# escribe; están leídas de logs que el propio programa produjo.
_TERMINADA = re.compile(r'Finished computing simulation run "(.+?)"')
_ABORTADA = re.compile(r'Aborted run "(.+?)"')
_DURACION = re.compile(r"total runtime for this simulation is ([\d:]+)")
_SEVERIDAD = re.compile(r"^(ERROR|WARNING|NOTE)\s+\d+:\s+(.*)$")


def leer_log_de_corrida(ruta_log: Path, corrida: str = "") -> ResultadoCorrida:
    """
    Interpreta el log que una corrida deja junto al proyecto.

    ES LA ÚNICA AUTORIDAD SOBRE SI EL CÁLCULO SIRVIÓ. El proceso puede terminar
    en cero habiendo abortado todas las corridas, y el DSS de una corrida
    abortada conserva los resultados de la anterior: leerlo sin comprobar esto
    produce caudales de un modelo que ya no existe.

    Excepciones
    -----------
    ErrorHms
        Si el log no está. Que falte significa que la corrida no llegó a
        empezar, y eso no se puede confundir con una corrida sin incidencias.
    """
    ruta_log = Path(ruta_log)
    nombre = corrida or ruta_log.stem
    if not ruta_log.is_file():
        raise ErrorHms(
            f"no se encuentra el log de la corrida {nombre!r} en {ruta_log}: "
            "la simulación no llegó a ejecutarse.")

    resultado = ResultadoCorrida(corrida=nombre, ruta_log=str(ruta_log))
    for linea in ruta_log.read_text(
            encoding="utf-8", errors="replace").splitlines():
        linea = linea.strip()
        if _TERMINADA.search(linea):
            resultado.terminada = True
        if _ABORTADA.search(linea):
            resultado.abortada = True
        duracion = _DURACION.search(linea)
        if duracion:
            resultado.duracion = duracion.group(1)
        marca = _SEVERIDAD.match(linea)
        if marca and marca.group(1) == "ERROR":
            resultado.errores.append(marca.group(2))
        elif marca and marca.group(1) == "WARNING":
            resultado.advertencias.append(marca.group(2))
    return resultado


def ejecutar_corridas(
    instalacion: Path,
    directorio_proyecto: Path,
    nombre_proyecto: str,
    corridas: Sequence[str],
    ruta_guion: Path,
    tiempo_limite_s: float = 3600.0,
) -> dict[str, Any]:
    """
    Computa las corridas en una sola sesión de HEC-HMS sin interfaz.

    Devuelve el código de salida del proceso, su salida de consola y el estado
    de cada corrida leído de su log. NO decide si el conjunto sirvió: eso lo
    resuelve quien llama, con la información de cada corrida.

    Excepciones
    -----------
    ErrorHms
        Si no está el lanzador, si el proceso excede el tiempo límite o si no se
        pudo lanzar.
    """
    lanzador = ruta_lanzador(instalacion)
    directorio_proyecto = Path(directorio_proyecto)
    if not directorio_proyecto.is_dir():
        raise ErrorHms(
            f"no existe el directorio del proyecto: {directorio_proyecto}.")

    ruta_guion = Path(ruta_guion)
    ruta_guion.parent.mkdir(parents=True, exist_ok=True)
    ruta_guion.write_text(
        guion_de_corridas(nombre_proyecto, directorio_proyecto, corridas),
        encoding="utf-8")

    try:
        proceso = subprocess.run(  # noqa: S603 - ejecutable declarado en config
            [str(lanzador), "-s", str(ruta_guion.resolve())],
            cwd=str(lanzador.parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=tiempo_limite_s, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ErrorHms(
            f"HEC-HMS excedió el tiempo límite de {tiempo_limite_s:.0f} s con "
            f"{len(corridas)} corrida(s). Subir hec_hms.simulacion."
            "tiempo_limite_s o computar menos escenarios por sesión.") from error
    except OSError as error:
        raise ErrorHms(f"no se pudo lanzar {lanzador}: {error}") from error

    estados = []
    for corrida in corridas:
        try:
            estados.append(leer_log_de_corrida(
                directorio_proyecto / f"{corrida}.log", corrida))
        except ErrorHms as error:
            estados.append(ResultadoCorrida(corrida=corrida,
                                            errores=[str(error)]))

    return {
        "codigo": proceso.returncode,
        "salida": (proceso.stdout or "")[-4000:],
        "error": (proceso.stderr or "")[-4000:],
        "guion": str(ruta_guion),
        "corridas": estados,
    }

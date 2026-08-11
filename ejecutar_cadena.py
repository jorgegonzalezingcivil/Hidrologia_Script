#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ejecutar_cadena.py
==================
Corre los módulos de un estudio en orden, cada uno con su intérprete.

NO añade capacidad: quita fricción. Los módulos siguen siendo ejecutables
independientes que se pueden lanzar a mano uno por uno, y esta es la forma
cómoda de encadenarlos. Nada de lo que hace es necesario para que el estudio
salga adelante.

Lo que resuelve. La cadena tiene dieciséis pasos disponibles repartidos entre
DOS intérpretes: unos corren con el Python de QGIS y otros con el venv del
proyecto (CLAUDE.md, sección 3). Equivocarse de intérprete produce un
ImportError que no explica nada, y es el error más fácil de cometer.

El orden y los argumentos se declaran en config/cadena.yaml, no aquí. Cambiar
la cadena no debería exigir tocar un programa.

DÓNDE SE DETIENE, Y POR QUÉ

    hallazgo bloqueante   un módulo devolvió código 1. Seguir adelante con un
                          producto que el propio módulo declara inutilizable
                          es lo que la doctrina prohíbe
    paso manual           la delimitación asistida de HEC-HMS. Es el único
                          paso con intervención obligatoria
    módulo pendiente      la cadena llega hasta donde llega la herramienta, y
                          lo dice en lugar de fallar con un archivo no
                          encontrado

Uso:
    python ejecutar_cadena.py --raiz D:/Estudios/mi_proyecto
    python ejecutar_cadena.py --raiz ... --simular
    python ejecutar_cadena.py --raiz ... --desde M05 --hasta M08
    python ejecutar_cadena.py --raiz ... --solo M10

Códigos de salida:
    0  la cadena llegó al final de lo disponible
    1  un módulo devolvió hallazgos bloqueantes
    2  la cadena se detuvo en un paso manual o en uno pendiente
    3  no se pudo leer la configuración o la declaración de la cadena
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RAIZ_CODIGO = Path(__file__).resolve().parent
_DIRECTORIO_SRC = _RAIZ_CODIGO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import rutas  # noqa: E402
from comun.config import cargar, leer_yaml  # noqa: E402
from comun.errores import ErrorConfiguracion, ErrorRutas  # noqa: E402

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_DETENIDA = 2
SALIDA_ERROR = 3

DECLARACION = "config/cadena.yaml"

# Códigos que devuelven los módulos, según la convención del proyecto.
_SIGNIFICADO = {
    0: "correcto",
    1: "hallazgos bloqueantes",
    2: "advertencias en modo estricto",
    3: "error de configuración o de insumos",
}


@dataclass
class Paso:
    """Un paso de la cadena, tal como lo declara config/cadena.yaml."""

    modulo: str
    nombre: str = ""
    script: str = ""
    entorno: str = "venv"
    argumentos: list[str] = field(default_factory=list)
    estado: str = "disponible"
    modos: list[str] = field(default_factory=list)
    opcional: bool = False
    manual: bool = False
    nombre_largo: str = ""

    @property
    def disponible(self) -> bool:
        return self.estado == "disponible"

    def aplica_a(self, modo: str) -> bool:
        """Un paso sin modos declarados aplica a todos."""
        return not self.modos or modo in self.modos


@dataclass
class Ejecucion:
    """Lo que ocurrió con un paso."""

    modulo: str
    nombre: str
    estado: str
    codigo: int | None = None
    segundos: float = 0.0
    detalle: str = ""

    def como_dict(self) -> dict[str, Any]:
        return {
            "modulo": self.modulo,
            "nombre": self.nombre,
            "estado": self.estado,
            "codigo_salida": self.codigo,
            "segundos": round(self.segundos, 2),
            "detalle": self.detalle,
        }


# =============================================================================
# Lectura de la declaración
# =============================================================================
def leer_cadena(raiz_estudio: Path) -> list[Paso]:
    """
    Lee config/cadena.yaml, del estudio si lo trae y si no de la herramienta.

    Excepciones
    -----------
    ErrorConfiguracion
        Si el archivo no existe, no declara pasos, o un paso está incompleto.
    """
    destino = rutas.resolver(DECLARACION, raiz_estudio)
    if not destino.is_file():
        raise ErrorConfiguracion(
            f"no se encuentra la declaración de la cadena en {destino}.")

    datos = leer_yaml(destino)
    crudos = datos.get("pasos")
    if not isinstance(crudos, list) or not crudos:
        raise ErrorConfiguracion(f"{destino} no declara ningún paso.")

    pasos: list[Paso] = []
    vistos: set[str] = set()
    for indice, crudo in enumerate(crudos, start=1):
        if not isinstance(crudo, dict) or not crudo.get("modulo"):
            raise ErrorConfiguracion(
                f"{destino}: el paso {indice} no declara 'modulo'.")
        modulo = str(crudo["modulo"]).strip()
        if modulo in vistos:
            raise ErrorConfiguracion(
                f"{destino}: el módulo {modulo!r} está declarado dos veces. "
                "Los identificadores se usan en --desde, --hasta y --solo, de "
                "modo que deben ser únicos.")
        vistos.add(modulo)

        paso = Paso(
            modulo=modulo,
            nombre=str(crudo.get("nombre", "")),
            script=str(crudo.get("script", "")),
            entorno=str(crudo.get("entorno", "venv")),
            argumentos=[str(a) for a in (crudo.get("argumentos") or ())],
            estado=str(crudo.get("estado", "disponible")),
            modos=[str(m) for m in (crudo.get("modos") or ())],
            opcional=bool(crudo.get("opcional")),
            manual=bool(crudo.get("manual")),
            nombre_largo=str(crudo.get("nombre_largo", "")),
        )
        if paso.entorno not in ("venv", "qgis", "manual"):
            raise ErrorConfiguracion(
                f"{destino}: el módulo {modulo!r} declara entorno "
                f"{paso.entorno!r}; se admiten venv, qgis y manual.")
        if not paso.manual and paso.disponible and not paso.script:
            raise ErrorConfiguracion(
                f"{destino}: el módulo {modulo!r} está disponible y no declara "
                "'script'.")
        pasos.append(paso)
    return pasos


def acotar(pasos: list[Paso], desde: str, hasta: str,
           solo: str) -> list[Paso]:
    """Recorta la cadena a un tramo, respetando el orden declarado."""
    claves = [p.modulo for p in pasos]

    def indice_de(nombre: str, etiqueta: str) -> int:
        if nombre not in claves:
            raise ErrorConfiguracion(
                f"{etiqueta} {nombre!r} no está en la cadena. Módulos "
                f"declarados: {', '.join(claves)}.")
        return claves.index(nombre)

    if solo:
        pedidos = [s.strip() for s in solo.split(",") if s.strip()]
        for pedido in pedidos:
            indice_de(pedido, "--solo")
        return [p for p in pasos if p.modulo in pedidos]

    inicio = indice_de(desde, "--desde") if desde else 0
    fin = indice_de(hasta, "--hasta") if hasta else len(pasos) - 1
    if inicio > fin:
        raise ErrorConfiguracion(
            f"--desde {desde!r} va después de --hasta {hasta!r} en la cadena.")
    return pasos[inicio:fin + 1]


# =============================================================================
# Intérpretes
# =============================================================================
def interpretes(configuracion, raiz_estudio: Path) -> dict[str, Path]:
    """
    Resuelve las rutas de los dos intérpretes declarados.

    El del venv es de la INSTALACIÓN y se resuelve contra la raíz del código,
    no contra la del estudio: un estudio no tiene entorno virtual propio.
    """
    encontrados: dict[str, Path] = {}
    for clave, nombre in (("entornos.venv.python", "venv"),
                          ("entornos.qgis.python", "qgis")):
        declarada = str(configuracion.obtener(clave, "")).strip()
        if not declarada:
            continue
        destino = Path(declarada)
        if not destino.is_absolute():
            destino = (_RAIZ_CODIGO / destino).resolve()
        encontrados[nombre] = destino
    return encontrados


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar_paso(paso: Paso, interprete: Path, raiz_estudio: Path,
                  silencioso: bool) -> tuple[int, float]:
    """Lanza un módulo y devuelve (código de salida, segundos)."""
    orden = [str(interprete), str(_RAIZ_CODIGO / paso.script),
             "--raiz", str(raiz_estudio), *paso.argumentos]
    if silencioso:
        orden.append("--silencioso")

    inicio = time.perf_counter()
    completado = subprocess.run(orden, cwd=str(_RAIZ_CODIGO), check=False)
    return completado.returncode, time.perf_counter() - inicio


def correr(pasos: list[Paso], raiz_estudio: Path, modo: str,
           rutas_interprete: dict[str, Path], simular: bool,
           silencioso: bool, continuar: bool) -> tuple[int, list[Ejecucion]]:
    """Recorre la cadena y devuelve (código de salida, ejecuciones)."""
    historia: list[Ejecucion] = []
    codigo_final = SALIDA_CORRECTA

    for paso in pasos:
        etiqueta = f"{paso.modulo} - {paso.nombre}"

        if not paso.aplica_a(modo):
            historia.append(Ejecucion(
                paso.modulo, paso.nombre, "no aplica",
                detalle=f"declarado solo para el modo {', '.join(paso.modos)}"))
            _anunciar("·", etiqueta, f"no aplica al modo {modo}")
            continue

        if not paso.disponible:
            historia.append(Ejecucion(
                paso.modulo, paso.nombre, "pendiente",
                detalle="el módulo todavía no está programado"))
            _anunciar("!", etiqueta, "PENDIENTE de programar")
            print()
            print(f"  La cadena llega hasta aquí. El módulo {paso.modulo} está")
            print("  declarado en config/cadena.yaml pero todavía no existe.")
            return SALIDA_DETENIDA, historia

        if paso.manual:
            historia.append(Ejecucion(
                paso.modulo, paso.nombre, "manual",
                detalle=paso.nombre_largo or paso.nombre))
            _anunciar("*", etiqueta, "PASO MANUAL")
            print()
            for linea in (paso.nombre_largo or paso.nombre).split(". "):
                if linea.strip():
                    print(f"  {linea.strip().rstrip('.')}.")
            return SALIDA_DETENIDA, historia

        interprete = rutas_interprete.get(paso.entorno)
        if interprete is None or not interprete.is_file():
            detalle = (f"no se encuentra el intérprete del entorno "
                       f"{paso.entorno}: {interprete}")
            historia.append(Ejecucion(paso.modulo, paso.nombre,
                                      "sin intérprete", detalle=detalle))
            _anunciar("x", etiqueta, detalle)
            if paso.opcional:
                continue
            return SALIDA_ERROR, historia

        if simular:
            orden = " ".join([interprete.name, paso.script, "--raiz",
                              str(raiz_estudio), *paso.argumentos])
            historia.append(Ejecucion(paso.modulo, paso.nombre, "simulado",
                                      detalle=orden))
            _anunciar(" ", etiqueta, f"[{paso.entorno}] {orden}")
            continue

        _anunciar(">", etiqueta, f"entorno {paso.entorno}")
        codigo, segundos = ejecutar_paso(paso, interprete, raiz_estudio,
                                         silencioso)
        significado = _SIGNIFICADO.get(codigo, f"código {codigo}")
        historia.append(Ejecucion(paso.modulo, paso.nombre,
                                  "correcto" if codigo == 0 else "detenido",
                                  codigo, segundos, significado))
        _anunciar("<" if codigo == 0 else "x", etiqueta,
                  f"{significado} en {segundos:.1f} s")

        if codigo == 0:
            continue
        if paso.opcional:
            print(f"  {paso.modulo} es opcional: la cadena continúa.")
            continue
        if continuar and codigo in (1, 2):
            print(f"  Se continúa por --continuar-con-bloqueantes. El producto "
                  f"de {paso.modulo} NO es utilizable.")
            codigo_final = SALIDA_BLOQUEANTE
            continue
        return (SALIDA_BLOQUEANTE if codigo in (1, 2) else SALIDA_ERROR), historia

    return codigo_final, historia


def _anunciar(marca: str, etiqueta: str, detalle: str) -> None:
    print(f"{marca} {etiqueta:<58} {detalle}")


def resumir(historia: list[Ejecucion], raiz_estudio: Path,
            segundos: float) -> None:
    """Escribe el resumen en consola."""
    print()
    print("=" * 78)
    print("RESUMEN DE LA CADENA")
    print("=" * 78)
    print(f"{'modulo':<8}{'estado':<16}{'codigo':>8}{'segundos':>11}  nombre")
    for una in historia:
        codigo = "" if una.codigo is None else str(una.codigo)
        print(f"{una.modulo:<8}{una.estado:<16}{codigo:>8}"
              f"{una.segundos:>11.1f}  {una.nombre[:38]}")
    corridos = [h for h in historia if h.codigo is not None]
    print("-" * 78)
    print(f"{len(corridos)} módulo(s) ejecutados en {segundos:.1f} s. "
          f"Estudio: {raiz_estudio}")


# =============================================================================
# Interfaz
# =============================================================================
def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(
        description="Corre la cadena de módulos de un estudio.")
    analizador.add_argument("--raiz", type=Path, default=None,
                            help="Directorio del estudio. Sin ella se deduce "
                                 "del directorio de trabajo.")
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--desde", default="",
                            help="Módulo por el que empezar.")
    analizador.add_argument("--hasta", default="",
                            help="Módulo en el que terminar.")
    analizador.add_argument("--solo", default="",
                            help="Módulos sueltos, separados por coma.")
    analizador.add_argument("--simular", action="store_true",
                            help="Muestra qué se ejecutaría, sin ejecutarlo.")
    analizador.add_argument("--silencioso", action="store_true",
                            help="Los módulos no escriben en consola; su log "
                                 "se escribe igual.")
    analizador.add_argument("--continuar-con-bloqueantes", action="store_true",
                            dest="continuar",
                            help="No se detiene ante un hallazgo bloqueante. "
                                 "El producto del módulo NO es utilizable.")
    analizador.add_argument("--json", type=Path, default=None,
                            dest="json_salida")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    argumentos = _analizar_argumentos(argv)
    inicio = time.perf_counter()

    try:
        raiz_estudio = (Path(argumentos.raiz).resolve()
                        if argumentos.raiz is not None
                        else rutas.raiz_proyecto())
        configuracion = cargar(ruta=argumentos.config, raiz=raiz_estudio)
        pasos = acotar(leer_cadena(raiz_estudio), argumentos.desde,
                       argumentos.hasta, argumentos.solo)
    except (ErrorConfiguracion, ErrorRutas) as error:
        print(f"[cadena] {error}", file=sys.stderr)
        return SALIDA_ERROR

    modo = str(configuracion.obtener("analisis.modo", "detallado"))
    rutas_interprete = interpretes(configuracion, raiz_estudio)

    print("=" * 78)
    print(f"CADENA DE EJECUCIÓN{'  (simulación)' if argumentos.simular else ''}")
    print("=" * 78)
    print(f"Estudio     : {raiz_estudio}")
    print(f"Proyecto    : {configuracion.obtener('proyecto.nombre', '')}")
    print(f"Modo        : {modo}")
    for nombre, destino in sorted(rutas_interprete.items()):
        marca = "" if destino.is_file() else "  (NO SE ENCUENTRA)"
        print(f"Interprete {nombre:<4}: {destino}{marca}")
    disponibles = sum(1 for p in pasos if p.disponible and p.aplica_a(modo))
    print(f"Pasos       : {len(pasos)} declarados, {disponibles} disponibles")
    print("-" * 78)

    codigo, historia = correr(pasos, raiz_estudio, modo, rutas_interprete,
                              argumentos.simular, argumentos.silencioso,
                              argumentos.continuar)
    transcurrido = time.perf_counter() - inicio
    resumir(historia, raiz_estudio, transcurrido)

    destino_json = (Path(argumentos.json_salida)
                    if argumentos.json_salida is not None
                    else raiz_estudio / "data" / "02_procesado" / "cadena.json")
    try:
        destino_json.parent.mkdir(parents=True, exist_ok=True)
        destino_json.write_text(json.dumps({
            "estudio": str(raiz_estudio),
            "modo": modo,
            "simulacion": bool(argumentos.simular),
            "segundos": round(transcurrido, 2),
            "codigo_salida": codigo,
            "pasos": [h.como_dict() for h in historia],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Reporte     : {destino_json}")
    except OSError as error:
        print(f"No se pudo escribir el reporte: {error}", file=sys.stderr)

    return codigo


if __name__ == "__main__":
    raise SystemExit(main())

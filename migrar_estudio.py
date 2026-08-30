#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
migrar_estudio.py
=================
Pone al día el `config/config.yaml` de un estudio anterior.

POR QUE HACE FALTA. Un estudio guarda su propia configuración y NO se fusiona
con la de la herramienta: es doctrina congelada del proyecto, y así dos
estudios de la misma versión parten de lo mismo y las diferencias entre sus
resultados son atribuibles a la cuenca. El precio es que, cuando la herramienta
añade o mueve una clave, los estudios anteriores se quedan atrás y se detienen
con 'clave ausente'. Es un fallo ruidoso, que es lo correcto, pero corregirlo a
mano en cada proyecto es trabajo repetido y termina con dos estudios llevando
doctrina distinta sin que nadie lo advierta.

QUE NO HACE: deducir la migración de la diferencia entre los dos archivos.
Añadir una clave ausente sí sería automatizable, pero una clave que CAMBIA DE
SIGNIFICADO exige además tocar los productos del estudio en disco, y eso ningún
diff lo adivina. Las recetas se declaran en `config/migraciones.yaml`.

SE TRABAJA POR LINEAS Y NO CON UN VOLCADO DE YAML, por el mismo motivo que
`nuevo_estudio.py`: el volcado perdería los comentarios, y en ese archivo cada
decisión con margen lleva al lado el porqué. Un estudio sin esos comentarios es
mucho más difícil de revisar y de defender ante una interventoría.

Uso:
    python migrar_estudio.py --raiz C:/Estudios/mi_estudio --simular
    python migrar_estudio.py --raiz C:/Estudios/mi_estudio

Códigos de salida:
    0  el estudio quedó al día, o ya lo estaba
    1  hay cambios que exigen decisión del consultor y no se aplicaron
    3  no se pudo leer el estudio, la herramienta o las recetas
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml

_RAIZ_CODIGO = Path(__file__).resolve().parent

SALIDA_CORRECTO = 0
SALIDA_REQUIERE_DECISION = 1
SALIDA_ERROR = 3

_CLAVE = re.compile(r"^(\s*)([A-Za-z_][\w_]*)\s*:")


# =============================================================================
# Localización de claves sobre el texto
# =============================================================================
def recorrer_claves(lineas: Sequence[str]) -> Iterator[tuple[int, str, int]]:
    """
    Recorre las líneas y devuelve (índice, ruta con puntos, sangría) por clave.

    No se usa un analizador de YAML porque hace falta la POSICION en el texto,
    que un árbol ya interpretado no conserva, y sin ella no se puede insertar
    respetando los comentarios de alrededor.
    """
    pila: list[tuple[int, str]] = []
    for indice, linea in enumerate(lineas):
        if not linea.strip() or linea.lstrip().startswith("#"):
            continue
        if linea.lstrip().startswith("-"):
            continue                       # elemento de lista, no clave
        encaje = _CLAVE.match(linea)
        if not encaje:
            continue
        sangria = len(encaje.group(1))
        while pila and pila[-1][0] >= sangria:
            pila.pop()
        pila.append((sangria, encaje.group(2)))
        yield indice, ".".join(nombre for _, nombre in pila), sangria


def bloque_de_clave(
    lineas: Sequence[str], ruta: str,
) -> tuple[int, int] | None:
    """
    Rango [inicio, fin) que ocupa una clave, con sus comentarios y sus hijas.

    Los comentarios CONTIGUOS por encima entran en el bloque: son la
    justificación de esa clave, y copiarla sin ellos entregaría al estudio un
    parámetro sin el porqué, que es justo lo que la sección 7 de CLAUDE.md
    exige poder mostrar.
    """
    for indice, encontrada, sangria in recorrer_claves(lineas):
        if encontrada != ruta:
            continue

        inicio = indice
        while inicio > 0 and lineas[inicio - 1].lstrip().startswith("#"):
            inicio -= 1

        fin = indice + 1
        while fin < len(lineas):
            linea = lineas[fin]
            if not linea.strip():
                fin += 1
                continue
            propia = len(linea) - len(linea.lstrip())
            if propia <= sangria:
                break                      # empieza algo del mismo nivel
            fin += 1

        while fin > indice + 1 and not lineas[fin - 1].strip():
            fin -= 1                       # no arrastrar líneas en blanco
        return inicio, fin
    return None


def linea_de_clave(lineas: Sequence[str], ruta: str) -> int | None:
    """Índice de la línea donde se declara la clave, sin sus comentarios."""
    for indice, encontrada, _ in recorrer_claves(lineas):
        if encontrada == ruta:
            return indice
    return None


def valor_en_linea(linea: str) -> str:
    """
    El valor declarado, sin el comentario de la derecha.

    El '#' solo separa comentario cuando está FUERA de comillas: una ruta de
    Windows o un texto pueden contenerlo, y cortar por el primero rompería el
    valor sin avisar.
    """
    _, _, resto = linea.partition(":")
    resto = resto.strip()
    if resto.startswith(('"', "'")):
        comilla = resto[0]
        cierre = resto.find(comilla, 1)
        if cierre != -1:
            return resto[1:cierre]
    return resto.split("#", 1)[0].strip()


# =============================================================================
# Resultado
# =============================================================================
@dataclass
class Cambio:
    clase: str
    detalle: str
    aplicado: bool = True
    motivo_omision: str = ""


@dataclass
class ResultadoMigracion:
    version_inicial: int = 0
    version_final: int = 0
    cambios: list[Cambio] = field(default_factory=list)
    respaldo: Path | None = None

    @property
    def requieren_decision(self) -> list[Cambio]:
        return [c for c in self.cambios if not c.aplicado]


# =============================================================================
# Aplicación
# =============================================================================
def leer_version(texto: str) -> int:
    """Versión declarada del esquema. Su ausencia significa la 1."""
    for linea in texto.splitlines():
        encaje = re.match(r"^esquema_version\s*:\s*(\d+)", linea)
        if encaje:
            return int(encaje.group(1))
    return 1


def insertar_clave(
    lineas: list[str], plantilla: Sequence[str], clave: str, despues_de: str,
) -> Cambio:
    """Copia el bloque de una clave desde la plantilla al archivo del estudio."""
    if linea_de_clave(lineas, clave) is not None:
        return Cambio("clave_nueva", clave, False, "ya estaba presente")

    origen = bloque_de_clave(plantilla, clave)
    if origen is None:
        return Cambio("clave_nueva", clave, False,
                      "no está en el config.yaml de la herramienta")

    ancla = bloque_de_clave(lineas, despues_de)
    if ancla is None:
        return Cambio("clave_nueva", clave, False,
                      f"no se encontró el ancla '{despues_de}' en el estudio")

    bloque = list(plantilla[origen[0]:origen[1]])
    lineas[ancla[1]:ancla[1]] = [""] + bloque
    return Cambio("clave_nueva", f"{clave} (tras {despues_de})")


def revalorar_clave(
    lineas: list[str], clave: str, anterior: str, nuevo: str,
) -> Cambio:
    """
    Sustituye el valor de una clave, solo si conserva el que se esperaba.

    SI EL CONSULTOR LO HABIA CAMBIADO, NO SE PISA. Un valor distinto del
    esperado es una decisión suya, y sustituirla en silencio durante una
    migración la borraría sin dejar constancia.
    """
    indice = linea_de_clave(lineas, clave)
    if indice is None:
        return Cambio("revalorada", clave, False, "la clave no está")

    actual = valor_en_linea(lineas[indice])
    if actual == nuevo:
        return Cambio("revalorada", clave, False, "ya tenía el valor nuevo")
    if actual != anterior:
        return Cambio(
            "revalorada", clave, False,
            f"el estudio declara '{actual}' y la receta esperaba "
            f"'{anterior}'. Se deja intacto: parece una decisión propia")

    linea = lineas[indice]
    lineas[indice] = linea.replace(f'"{anterior}"', f'"{nuevo}"', 1) \
        if f'"{anterior}"' in linea else linea.replace(anterior, nuevo, 1)
    return Cambio("revalorada", f"{clave}: '{anterior}' -> '{nuevo}'")


def renombrar_archivos(
    base: Path, de: str, a: str, extensiones: Sequence[str], simular: bool,
) -> list[Cambio]:
    """Renombra un producto y todos sus acompañantes."""
    cambios: list[Cambio] = []
    for extension in extensiones:
        origen = base / f"{de}{extension}"
        destino = base / f"{a}{extension}"
        if not origen.is_file():
            continue
        if destino.exists():
            cambios.append(Cambio(
                "archivo", origen.name, False,
                f"{destino.name} ya existe; no se sobrescribe"))
            continue
        if not simular:
            origen.rename(destino)
        cambios.append(Cambio("archivo", f"{origen.name} -> {destino.name}"))
    return cambios


def fijar_version(lineas: list[str], version: int) -> None:
    """Deja escrita la versión alcanzada, insertándola si no estaba."""
    for indice, linea in enumerate(lineas):
        if re.match(r"^esquema_version\s*:", linea):
            lineas[indice] = f"esquema_version: {version}"
            return
    corte = 0
    for indice, linea in enumerate(lineas):
        if linea.strip() and not linea.lstrip().startswith("#"):
            corte = indice
            break
    lineas[corte:corte] = [
        "# Versión del esquema de configuración, escrita por migrar_estudio.py.",
        f"esquema_version: {version}",
        "",
    ]


def migrar(
    raiz_estudio: Path, raiz_codigo: Path = _RAIZ_CODIGO, simular: bool = False,
) -> ResultadoMigracion:
    """
    Aplica al estudio todas las migraciones pendientes, en orden.

    Excepciones
    -----------
    FileNotFoundError
        Si falta el config del estudio, el de la herramienta o las recetas.
    """
    ruta_estudio = Path(raiz_estudio) / "config" / "config.yaml"
    ruta_plantilla = Path(raiz_codigo) / "config" / "config.yaml"
    ruta_recetas = Path(raiz_codigo) / "config" / "migraciones.yaml"
    for ruta in (ruta_estudio, ruta_plantilla, ruta_recetas):
        if not ruta.is_file():
            raise FileNotFoundError(f"no se encuentra {ruta}")

    texto = ruta_estudio.read_text(encoding="utf-8")
    lineas = texto.splitlines()
    plantilla = ruta_plantilla.read_text(encoding="utf-8").splitlines()
    recetas = yaml.safe_load(ruta_recetas.read_text(encoding="utf-8")) or {}

    resultado = ResultadoMigracion(version_inicial=leer_version(texto))
    resultado.version_final = resultado.version_inicial
    objetivo = leer_version("\n".join(plantilla))

    pendientes = sorted(
        (m for m in recetas.get("migraciones", [])
         if int(m["desde"]) >= resultado.version_inicial
         and int(m["hasta"]) <= objetivo),
        key=lambda m: int(m["desde"]))

    for receta in pendientes:
        if int(receta["desde"]) != resultado.version_final:
            continue                       # no hay camino continuo hasta ella
        for entrada in receta.get("claves_nuevas", []) or []:
            resultado.cambios.append(insertar_clave(
                lineas, plantilla, entrada["clave"], entrada["despues_de"]))
        for entrada in receta.get("claves_revaloradas", []) or []:
            resultado.cambios.append(revalorar_clave(
                lineas, entrada["clave"], entrada["valor_anterior"],
                entrada["valor_nuevo"]))
        for entrada in receta.get("archivos_renombrados", []) or []:
            resultado.cambios.extend(renombrar_archivos(
                Path(raiz_estudio), entrada["de"], entrada["a"],
                entrada.get("extensiones") or [".shp"], simular))
        resultado.version_final = int(receta["hasta"])

    if resultado.version_final == resultado.version_inicial:
        return resultado

    fijar_version(lineas, resultado.version_final)

    if not simular:
        respaldo = ruta_estudio.with_suffix(
            f".yaml.antes-de-v{resultado.version_final}")
        if not respaldo.exists():
            shutil.copy2(ruta_estudio, respaldo)
        resultado.respaldo = respaldo
        ruta_estudio.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    return resultado


# =============================================================================
# Interfaz
# =============================================================================
def _informar(resultado: ResultadoMigracion, simular: bool) -> None:
    encabezado = "SIMULACION (no se escribió nada)" if simular else "MIGRACION"
    print(f"\n{encabezado}")
    print("=" * 70)
    if resultado.version_final == resultado.version_inicial:
        print(f"El estudio ya está en la versión {resultado.version_inicial}. "
              "No hay nada que hacer.")
        return

    print(f"Versión {resultado.version_inicial} -> {resultado.version_final}")
    print()
    aplicados = [c for c in resultado.cambios if c.aplicado]
    if aplicados:
        print("Aplicado:")
        for cambio in aplicados:
            print(f"  [{cambio.clase}] {cambio.detalle}")
    omitidos = resultado.requieren_decision
    if omitidos:
        print("\nNO aplicado, requiere revisión:")
        for cambio in omitidos:
            print(f"  [{cambio.clase}] {cambio.detalle}")
            print(f"      {cambio.motivo_omision}")
    if resultado.respaldo:
        print(f"\nRespaldo del archivo anterior: {resultado.respaldo.name}")


def main(argv: Sequence[str] | None = None) -> int:
    analizador = argparse.ArgumentParser(
        description="Pone al día la configuración de un estudio anterior.")
    analizador.add_argument("--raiz", type=Path, required=True,
                            help="directorio del estudio")
    analizador.add_argument("--simular", action="store_true",
                            help="muestra qué haría, sin escribir nada")
    argumentos = analizador.parse_args(argv)

    try:
        resultado = migrar(argumentos.raiz, simular=argumentos.simular)
    except (FileNotFoundError, OSError, yaml.YAMLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return SALIDA_ERROR

    _informar(resultado, argumentos.simular)
    # Una omisión por 'ya estaba' no es un problema; una por valor propio, sí.
    graves = [c for c in resultado.requieren_decision
              if "ya" not in c.motivo_omision]
    return SALIDA_REQUIERE_DECISION if graves else SALIDA_CORRECTO


if __name__ == "__main__":
    sys.exit(main())

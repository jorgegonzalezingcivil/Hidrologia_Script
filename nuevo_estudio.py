#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
nuevo_estudio.py
================
Crea el directorio de un ESTUDIO y escribe su configuración.

La herramienta y el estudio son cosas distintas. Este repositorio contiene el
código, las pruebas, la doctrina técnica de `data/referencia/` y las
plantillas; un estudio es un directorio aparte con su `config/config.yaml`, sus
datos y sus productos. La misma instalación corre así varios proyectos sin que
los resultados de uno aparezcan en el otro.

Qué hace:

    1. pregunta lo imprescindible y lo VALIDA antes de escribir nada
    2. crea el árbol de directorios del estudio
    3. escribe config/config.yaml partiendo del de la herramienta, con los
       valores del estudio sustituidos y el resto de la doctrina intacta
    4. escribe el MANIFIESTO.yaml en blanco, para que el consultor declare sus
       insumos

Lo que NO hace: cambiar parámetros técnicos. Todo lo que no sea propio del
estudio se hereda tal cual, de modo que dos proyectos de la misma versión de la
herramienta parten de la misma doctrina y las diferencias entre sus resultados
son atribuibles a la cuenca y no a la configuración.

Uso:
    python nuevo_estudio.py
    python nuevo_estudio.py --destino D:/Estudios/rio_x --sin-preguntas \
        --nombre "Rio X" --crs EPSG:9377 --x 4850000 --y 2050000

Códigos de salida:
    0  estudio creado
    2  los datos no pasaron la validación
    3  el destino no se pudo escribir
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_RAIZ_CODIGO = Path(__file__).resolve().parent
_DIRECTORIO_SRC = _RAIZ_CODIGO / "src"
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun.errores import ErrorConfiguracion, ErrorRutas  # noqa: E402

SALIDA_CORRECTA = 0
SALIDA_INVALIDA = 2
SALIDA_ERROR = 3

# Colombia continental e insular, con holgura. Sirve para atrapar el error de
# transcripción más frecuente: invertir longitud y latitud, o teclear una
# coordenada de otro origen. No pretende ser un límite político.
LIMITES_COLOMBIA = {"lon_min": -82.0, "lon_max": -66.0,
                    "lat_min": -4.5, "lat_max": 13.5}

_MOTIVO_POR_DEFECTO = {
    "detallado": ("No aplica: el modo detallado desagrega en subcuencas y "
                  "construye modelo HEC-HMS."),
    "general": ("PENDIENTE DE DILIGENCIAR. En modo general el estudio debe "
                "explicar por que no se construye modelo lluvia-escorrentia "
                "por subcuencas."),
}

MANIFIESTO_EN_BLANCO = """\
# =============================================================================
# MANIFIESTO DE INSUMOS DEL ESTUDIO
# -----------------------------------------------------------------------------
# Declara qué aporta el consultor y con qué características. El M00c lo lee y
# se detiene si falta algo imprescindible.
#
# Los insumos con 'aportado: false' que tengan capa de base declarada en la
# configuración se resuelven con esa capa, y el módulo lo advierte: un número
# de curva derivado de una capa global no vale lo mismo que uno derivado de un
# estudio de suelos del proyecto.
# =============================================================================

suelos:
  aportado: false
  usa_capa_base: true
  base_archivo: "suelos/HYSOGs250m.tif"
  base_fuente: "Ross et al. (2018), Global Hydrologic Soil Groups, ORNL DAAC"
  base_escala: "1:250000"
  base_perfil: "D"

cobertura:
  aportado: false
  usa_capa_base: true
  base_archivo: "cobertura/cobertura_tierra_clc_2018.shp"
  base_fuente: "IDEAM, Corine Land Cover Colombia 2018"
  base_escala: "1:100000"

caudales:
  aportado: false

homologacion:
  # Tablas que genera el sistema y diligencia el consultor. El M00c se detiene
  # si quedan valores sin homologar: la equivalencia entre las clases del
  # insumo y las del SCS es una decisión con criterio, no una conversión.
  suelos:
    archivo: "homologacion/suelos.csv"
    diligenciada: false
    fecha: ""
    responsable: ""
  cobertura:
    archivo: "homologacion/cobertura.csv"
    diligenciada: false
    fecha: ""
    responsable: ""

# -----------------------------------------------------------------------------
# REGISTRO DE DECISIONES DEL CONSULTOR
# Cada decisión con margen técnico debe quedar aquí con su justificación
# (CLAUDE.md, sección 7). Un estudio que no puede explicar sus descartes no es
# defendible ante interventoría.
# -----------------------------------------------------------------------------
decisiones: []
"""


# =============================================================================
# Validación
# =============================================================================
def validar_crs(codigo: str) -> tuple[bool, str]:
    """
    Comprueba que el código EPSG existe y devuelve el nombre del sistema.

    Se usa pyproj, que es la misma biblioteca que hay debajo de QGIS. Aceptar
    un código inexistente dejaría el estudio con una reproyección que falla
    varios módulos más adelante, cuando ya cuesta relacionarla con la causa.
    """
    try:
        from pyproj import CRS
    except ImportError:
        return True, "no se pudo verificar: falta pyproj"
    try:
        sistema = CRS.from_user_input(codigo)
    except Exception as error:  # noqa: BLE001  pyproj lanza varios tipos
        return False, f"{codigo!r} no es un sistema de referencia válido: {error}"
    return True, sistema.name


def validar_punto(x: float, y: float, crs: str) -> tuple[bool, str]:
    """
    Comprueba que el punto cae dentro de Colombia, reproyectando a geográficas.

    Es la validación que atrapa el error más común al declarar una coordenada:
    escribir la latitud antes que la longitud. El orden es siempre x, y, que en
    un sistema geográfico significa longitud y luego latitud.
    """
    try:
        from pyproj import Transformer
    except ImportError:
        return True, "no se pudo verificar: falta pyproj"
    try:
        conversor = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        longitud, latitud = conversor.transform(x, y)
    except Exception as error:  # noqa: BLE001
        return False, f"no se pudo reproyectar el punto: {error}"

    dentro = (LIMITES_COLOMBIA["lon_min"] <= longitud <= LIMITES_COLOMBIA["lon_max"]
              and LIMITES_COLOMBIA["lat_min"] <= latitud
              <= LIMITES_COLOMBIA["lat_max"])
    detalle = f"longitud {longitud:.5f}, latitud {latitud:.5f}"
    if dentro:
        return True, detalle
    return False, (
        f"el punto cae fuera de Colombia ({detalle}). Revisar el orden: se "
        "declara primero x y luego y, que en un sistema geográfico son "
        "longitud y latitud, en ese orden.")


# =============================================================================
# Escritura
# =============================================================================
def escribir_config(destino: Path, plantilla: Path, datos: dict) -> Path:
    """
    Escribe el config.yaml del estudio a partir del de la herramienta.

    Se sustituyen LÍNEAS y no se reescribe el archivo con un volcado de YAML,
    porque el volcado perdería los comentarios. Ese archivo es medio manual de
    uso: cada decisión con margen lleva al lado el porqué, y un estudio sin esos
    comentarios sería mucho más difícil de revisar y de defender.
    """
    lineas = plantilla.read_text(encoding="utf-8").splitlines()
    sustituciones = {
        "proyecto.nombre": ("  nombre:", f'  nombre: "{datos["nombre"]}"'),
        "proyecto.contratante": ("  contratante:",
                                 f'  contratante: "{datos["contratante"]}"'),
        "proyecto.consultor": ("  consultor:",
                               f'  consultor: "{datos["consultor"]}"'),
        "proyecto.responsable": ("  responsable:",
                                 f'  responsable: "{datos["responsable"]}"'),
        "proyecto.anio_estudio": ("  anio_estudio:",
                                  f'  anio_estudio: {datos["anio"]}'),
    }
    pendientes = dict(sustituciones)
    en_proyecto = False
    en_punto = False
    en_analisis = False
    saltando_motivo = False
    salida: list[str] = []

    for linea in lineas:
        if linea.startswith("proyecto:"):
            en_proyecto, en_punto, en_analisis = True, False, False
        elif linea.startswith("punto_descarga:"):
            en_proyecto, en_punto, en_analisis = False, True, False
        elif linea.startswith("analisis:"):
            en_proyecto, en_punto, en_analisis = False, False, True
        elif linea and not linea[0].isspace() and not linea.startswith("#"):
            en_proyecto = en_punto = en_analisis = False
            saltando_motivo = False

        # La justificación del modo general describe UNA cuenca concreta.
        # Arrastrarla a otro estudio pondría en su informe el motivo de un
        # proyecto distinto, que es el peor error posible en un anexo.
        if en_analisis and saltando_motivo:
            if linea.startswith("    ") or not linea.strip():
                continue
            saltando_motivo = False

        if en_analisis:
            if linea.startswith("  modo:"):
                salida.append(f'  modo: "{datos["modo"]}"')
                continue
            if linea.startswith("  motivo_general:"):
                saltando_motivo = True
                salida.append("  motivo_general: >-")
                salida.append("    " + datos["motivo"])
                continue

        if en_proyecto:
            reemplazada = False
            for clave, (prefijo, nueva) in list(pendientes.items()):
                if linea.startswith(prefijo):
                    salida.append(nueva)
                    del pendientes[clave]
                    reemplazada = True
                    break
            if reemplazada:
                continue

        if en_punto:
            if linea.startswith("  crs:"):
                salida.append(f'  crs: "{datos["crs"]}"')
                continue
            if linea.startswith("  x:"):
                salida.append(f'  x: {datos["x"]}')
                continue
            if linea.startswith("  y:"):
                salida.append(f'  y: {datos["y"]}')
                continue

        salida.append(linea)

    if pendientes:
        raise ErrorConfiguracion(
            "la plantilla de configuración no trae las claves "
            f"{', '.join(sorted(pendientes))}: no se puede derivar un estudio "
            "de ella sin dejar valores del proyecto anterior.")

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(salida) + "\n", encoding="utf-8")
    return destino


def crear_estudio(destino: Path, datos: dict) -> list[str]:
    """Crea el árbol, la configuración y el manifiesto. Devuelve lo escrito."""
    import setup_estructura

    destino.mkdir(parents=True, exist_ok=True)
    setup_estructura.crear_estructura(destino)

    escritos: list[str] = []
    config = escribir_config(destino / "config" / "config.yaml",
                             _RAIZ_CODIGO / "config" / "config.yaml", datos)
    escritos.append(str(config.relative_to(destino)))

    manifiesto = destino / "data" / "00_insumos_usuario" / "MANIFIESTO.yaml"
    manifiesto.parent.mkdir(parents=True, exist_ok=True)
    manifiesto.write_text(MANIFIESTO_EN_BLANCO, encoding="utf-8")
    escritos.append(str(manifiesto.relative_to(destino)))

    # La configuración propia de la máquina no se hereda del estudio anterior,
    # pero la plantilla sí se copia para que el equipo sepa que existe.
    ejemplo = _RAIZ_CODIGO / "config" / "config.local.ejemplo.yaml"
    if ejemplo.is_file():
        shutil.copy(ejemplo, destino / "config" / ejemplo.name)
        escritos.append(f"config/{ejemplo.name}")
    return escritos


# =============================================================================
# Diálogo
# =============================================================================
def preguntar(texto: str, defecto: str = "", obligatorio: bool = False) -> str:
    """Pregunta por consola, con valor por defecto entre corchetes."""
    etiqueta = f"{texto} [{defecto}]: " if defecto else f"{texto}: "
    while True:
        respuesta = input(etiqueta).strip() or defecto
        if respuesta or not obligatorio:
            return respuesta
        print("  Este dato es obligatorio.")


def preguntar_numero(texto: str) -> float:
    while True:
        crudo = preguntar(texto, obligatorio=True).replace(",", ".")
        try:
            return float(crudo)
        except ValueError:
            print(f"  {crudo!r} no es un número.")


def dialogo(argumentos) -> dict:
    """Recoge los datos del estudio, por consola o de los argumentos."""
    import datetime

    if argumentos.sin_preguntas:
        return {
            "nombre": argumentos.nombre or "Estudio hidrológico",
            "contratante": argumentos.contratante or "",
            "consultor": argumentos.consultor or "",
            "responsable": argumentos.responsable or "",
            "anio": argumentos.anio or datetime.date.today().year,
            "crs": argumentos.crs or "EPSG:9377",
            "x": argumentos.x,
            "y": argumentos.y,
            "modo": argumentos.modo,
            "motivo": (argumentos.motivo
                       or _MOTIVO_POR_DEFECTO[argumentos.modo]),
        }

    print()
    print("Datos del estudio. Lo que se deje en blanco puede completarse")
    print("después en config/config.yaml.")
    print()
    datos = {
        "nombre": preguntar("Nombre del estudio", obligatorio=True),
        "contratante": preguntar("Contratante"),
        "consultor": preguntar("Consultor"),
        "responsable": preguntar("Responsable"),
        "anio": int(preguntar("Año del estudio",
                              str(datetime.date.today().year))),
    }

    print()
    print("Punto de descarga. Se declara en el sistema en que se tenga; los")
    print("módulos reproyectan de forma explícita al de cálculo.")
    print("El orden es siempre x, y: en un sistema geográfico, longitud y")
    print("luego latitud.")
    print()
    while True:
        crs = preguntar("CRS del punto", "EPSG:9377", obligatorio=True)
        valido, detalle = validar_crs(crs)
        if not valido:
            print(f"  {detalle}")
            continue
        print(f"  {detalle}")
        x = preguntar_numero("x (Este o longitud)")
        y = preguntar_numero("y (Norte o latitud)")
        valido, detalle = validar_punto(x, y, crs)
        print(f"  {detalle}")
        if valido:
            datos.update({"crs": crs, "x": x, "y": y})
            datos.update(_preguntar_modo())
            return datos
        if preguntar("¿Aceptar de todos modos? (s/N)", "N").lower() != "s":
            continue
        datos.update({"crs": crs, "x": x, "y": y})
        datos.update(_preguntar_modo())
        return datos


def _preguntar_modo() -> dict:
    """Modo de análisis y, si es general, la justificación que exige."""
    print()
    print("Modo de análisis. 'detallado' desagrega en subcuencas y construye")
    print("modelo HEC-HMS; 'general' caracteriza una sola unidad y no lo")
    print("construye, y entonces hay que decir por qué.")
    print()
    modo = preguntar("Modo (general/detallado)", "detallado")
    modo = modo if modo in ("general", "detallado") else "detallado"
    if modo != "general":
        return {"modo": modo, "motivo": _MOTIVO_POR_DEFECTO[modo]}
    motivo = ""
    while len(motivo.strip()) <= 40:
        motivo = preguntar("Por qué no se construye modelo HEC-HMS",
                           obligatorio=True)
        if len(motivo.strip()) <= 40:
            print("  Un estudio que no explica por qué no modeló la cuenca")
            print("  no es defendible ante interventoría. Ampliar la razón.")
    return {"modo": modo, "motivo": motivo.strip()}


def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(
        description="Crea el directorio y la configuración de un estudio.")
    analizador.add_argument("--destino", type=Path, default=None)
    analizador.add_argument("--sin-preguntas", action="store_true")
    analizador.add_argument("--nombre", default="")
    analizador.add_argument("--contratante", default="")
    analizador.add_argument("--consultor", default="")
    analizador.add_argument("--responsable", default="")
    analizador.add_argument("--anio", type=int, default=0)
    analizador.add_argument("--crs", default="")
    analizador.add_argument("--x", type=float, default=None)
    analizador.add_argument("--y", type=float, default=None)
    analizador.add_argument("--modo", choices=("general", "detallado"),
                            default="detallado")
    analizador.add_argument("--motivo", default="")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    argumentos = _analizar_argumentos(argv)

    if argumentos.sin_preguntas and (argumentos.x is None or argumentos.y is None):
        print("Sin preguntas hay que dar --x y --y.", file=sys.stderr)
        return SALIDA_INVALIDA

    destino = argumentos.destino
    if destino is None:
        destino = Path(preguntar("Directorio del estudio", obligatorio=True))
    destino = destino.expanduser().resolve()

    if (destino / "config" / "config.yaml").is_file():
        print(f"Ya existe un estudio en {destino}. No se sobrescribe.",
              file=sys.stderr)
        return SALIDA_INVALIDA
    if destino == _RAIZ_CODIGO:
        print("El estudio no puede crearse sobre la propia herramienta: sus "
              "productos se mezclarían con el código.", file=sys.stderr)
        return SALIDA_INVALIDA

    try:
        datos = dialogo(argumentos)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelado.", file=sys.stderr)
        return SALIDA_INVALIDA

    valido, detalle = validar_crs(datos["crs"])
    if not valido:
        print(detalle, file=sys.stderr)
        return SALIDA_INVALIDA
    valido, detalle = validar_punto(datos["x"], datos["y"], datos["crs"])
    if not valido and argumentos.sin_preguntas:
        print(detalle, file=sys.stderr)
        return SALIDA_INVALIDA

    try:
        escritos = crear_estudio(destino, datos)
    except (OSError, ErrorConfiguracion, ErrorRutas) as error:
        print(f"No se pudo crear el estudio: {error}", file=sys.stderr)
        return SALIDA_ERROR

    print()
    print(f"Estudio creado en {destino}")
    for archivo in escritos:
        print(f"  {archivo}")
    print()
    print("Siguientes pasos:")
    print(f"  1. Ajustar config/config.local.yaml de esta máquina.")
    print(f"  2. Declarar los insumos propios en el MANIFIESTO.yaml.")
    print(f"  3. Ejecutar desde el estudio, o pasar --raiz:")
    print(f"       cd {destino}")
    print(f"       {_RAIZ_CODIGO / '.venv' / 'Scripts' / 'python.exe'} "
          f"{_RAIZ_CODIGO / 'src' / 'M00_configuracion.py'}")
    return SALIDA_CORRECTA


if __name__ == "__main__":
    raise SystemExit(main())

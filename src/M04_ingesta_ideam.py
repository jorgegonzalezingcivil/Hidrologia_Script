#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M04 - Adaptador de ingesta IDEAM, normalización y deduplicación
===============================================================
Entorno: venv del proyecto.

Lee los archivos de descarga del IDEAM, los normaliza a un esquema único y
deduplica los registros repetidos entre archivos.

Por qué la deduplicación no es accesoria. Las descargas del IDEAM tienen límite
de 30 años, de modo que hay varios archivos por estación y sus rangos se solapan
(CLAUDE.md, sección 7). Medido sobre 59 archivos reales del Río Bogotá: 43.428
registros redundantes sobre 1.054.398, un 4,1%, repartidos en 4.308 claves.

Precedencia al deduplicar. Los archivos traen el nivel de aprobación como código
numérico, no como los textos que cita CLAUDE.md. La correspondencia confirmada
está declarada en config/perfiles_ideam.yaml: 1200 Definitivo, 1100 En revisión,
900 Preliminar. No se filtra por ese campo, conforme a la decisión cerrada de la
sección 6, pero sí decide qué registro se conserva ante un conflicto.

Detección de formato. El perfil se resuelve comparando el encabezado del archivo
con los declarados, nunca por el nombre del archivo interno: la rutina heredada
asumía que se llamaba 'excel.csv.csv' y se rompía si cambiaba.

Uso:
    python src/M04_ingesta_ideam.py
    python src/M04_ingesta_ideam.py --solo-inventario

Códigos de salida:
    0  ingesta completada
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los perfiles
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M04"
DESCRIPCION = "Ingesta IDEAM, normalización y deduplicación"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Campos del esquema interno, comunes a todos los perfiles de origen.
CAMPOS_INTERNOS = (
    "codigo", "nombre", "latitud", "longitud", "altitud", "categoria",
    "parametro", "etiqueta", "frecuencia", "fecha", "valor",
    "calificador", "nivel_aprobacion",
)


@dataclass
class Perfil:
    """Un formato de archivo de descarga, declarado en perfiles_ideam.yaml."""

    nombre: str
    columnas: tuple[str, ...]
    campos: dict[str, str]
    separador: str = ","
    codificacion: str = "utf-8-sig"
    formato_fecha: str = "%d/%m/%Y"
    recortar: bool = True
    verificado: bool = False
    advertencia: str = ""


@dataclass
class ResultadoM04:
    archivos: int = 0
    registros_leidos: int = 0
    registros_unicos: int = 0
    conflictos: int = 0
    fechas_ilegibles: int = 0
    ejemplos_fecha: list = field(default_factory=list)
    estaciones: set = field(default_factory=set)
    series: dict = field(default_factory=dict)
    perfiles_usados: dict = field(default_factory=dict)
    calificadores: dict = field(default_factory=dict)
    productos: list = field(default_factory=list)
    hallazgos: list = field(default_factory=list)


# =============================================================================
# Perfiles
# =============================================================================
def cargar_perfiles(ruta: Path) -> tuple[dict[str, Perfil], dict, dict]:
    """
    Lee perfiles_ideam.yaml y devuelve (perfiles, calificadores, aprobacion).

    Excepciones
    -----------
    ErrorConfiguracion
        Si el archivo no declara ningún perfil utilizable.
    """
    datos = leer_yaml(ruta)
    crudos = datos.get("perfiles") or {}
    perfiles: dict[str, Perfil] = {}

    for nombre, bloque in crudos.items():
        columnas = tuple(bloque.get("columnas") or ())
        if not columnas:
            continue  # perfil declarado pero sin verificar; no es utilizable
        perfiles[nombre] = Perfil(
            nombre=nombre,
            columnas=columnas,
            campos=dict(bloque.get("campos") or {}),
            separador=bloque.get("separador", ","),
            codificacion=bloque.get("codificacion", "utf-8-sig"),
            formato_fecha=bloque.get("formato_fecha", "%d/%m/%Y"),
            recortar=bool(bloque.get("recortar_espacios", True)),
            verificado=bool(bloque.get("verificado", False)),
            advertencia=str(bloque.get("advertencia") or ""),
        )

    if not perfiles:
        raise ErrorConfiguracion(
            f"{ruta.name} no declara ningún perfil con columnas. Sin al menos "
            "uno utilizable el M04 no puede interpretar los archivos."
        )

    return (perfiles,
            datos.get("calificadores") or {},
            datos.get("nivel_aprobacion") or {})


def detectar_perfil(encabezado: Sequence[str],
                    perfiles: dict[str, Perfil]) -> Perfil | None:
    """
    Resuelve el perfil comparando el encabezado con los declarados.

    Se exige coincidencia exacta del conjunto de columnas. Una coincidencia
    parcial sería peor que ninguna: interpretaría posiciones equivocadas y
    produciría un resultado incorrecto en silencio.
    """
    presente = {c.strip() for c in encabezado}
    for perfil in perfiles.values():
        if presente == set(perfil.columnas):
            return perfil
    return None


# =============================================================================
# Lectura
# =============================================================================
def leer_zip(
    archivo: Path, perfiles: dict[str, Perfil], patron: str = "*.csv"
) -> Iterator[tuple[Perfil, dict[str, str]]]:
    """
    Recorre los .csv de un .zip y entrega sus filas con el perfil detectado.

    El .csv se descubre por patrón, no por nombre fijo. Es el defecto que
    CLAUDE.md, sección 9, señala en la rutina heredada.

    Excepciones
    -----------
    ErrorFormato
        Si el .zip no se puede abrir, no contiene ningún archivo que case con el
        patrón, o su encabezado no corresponde a ningún perfil declarado.
    """
    try:
        with zipfile.ZipFile(archivo) as comprimido:
            internos = [n for n in comprimido.namelist()
                        if fnmatch(Path(n).name.lower(), patron.lower())]
            if not internos:
                raise ErrorFormato(
                    f"{archivo.name} no contiene ningún archivo que case con "
                    f"{patron!r}. Contiene: {comprimido.namelist()[:5]}"
                )

            for interno in internos:
                bruto = comprimido.read(interno)
                texto = _decodificar(bruto, perfiles)
                lector = csv.DictReader(
                    io.StringIO(texto),
                    delimiter=_separador_de(texto, perfiles),
                )
                perfil = detectar_perfil(lector.fieldnames or [], perfiles)
                if perfil is None:
                    raise ErrorFormato(
                        f"{archivo.name} :: {interno}: el encabezado no "
                        f"corresponde a ningún perfil declarado. Columnas: "
                        f"{(lector.fieldnames or [])[:6]}..."
                    )
                for fila in lector:
                    yield perfil, fila

    except zipfile.BadZipFile as exc:
        raise ErrorFormato(f"{archivo.name} no es un .zip legible: {exc}") from exc


def _decodificar(bruto: bytes, perfiles: dict[str, Perfil]) -> str:
    """Prueba las codificaciones declaradas antes de recurrir a un reemplazo."""
    candidatas = list(dict.fromkeys(
        [p.codificacion for p in perfiles.values()] + ["utf-8-sig", "cp1252"]
    ))
    for codificacion in candidatas:
        try:
            return bruto.decode(codificacion)
        except (UnicodeDecodeError, LookupError):
            continue
    return bruto.decode("utf-8", "replace")


def _separador_de(texto: str, perfiles: dict[str, Perfil]) -> str:
    """Elige el separador que produce más columnas en el encabezado."""
    cabecera = texto.splitlines()[0] if texto else ""
    candidatos = list(dict.fromkeys(
        [p.separador for p in perfiles.values()] + [",", ";", "\t"]
    ))
    mejor, columnas = candidatos[0], 0
    for separador in candidatos:
        n = len(next(csv.reader([cabecera], delimiter=separador)))
        if n > columnas:
            mejor, columnas = separador, n
    return mejor


# =============================================================================
# Normalización
# =============================================================================
def normalizar(fila: dict[str, str], perfil: Perfil) -> dict[str, Any]:
    """Traduce una fila del formato de origen al esquema interno."""
    salida: dict[str, Any] = {}
    for interno in CAMPOS_INTERNOS:
        origen = perfil.campos.get(interno)
        valor = fila.get(origen, "") if origen else ""
        salida[interno] = valor.strip() if (perfil.recortar and valor) else valor
    salida["fecha"] = _fecha(salida.get("fecha", ""), perfil.formato_fecha)
    salida["valor"] = _numero(salida.get("valor", ""))
    return salida


# Formatos de fecha observados en los archivos reales. Conviven al menos dos
# entre los 59 archivos verificados: ISO con hora y día/mes/año con hora. La
# lista se recorre en orden y se prueba también sin la parte horaria.
FORMATOS_FECHA = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%Y-%m-%dT%H:%M:%S",
)


def _fecha(texto: str, formato: str) -> str:
    """
    Normaliza la fecha a ISO. Devuelve cadena vacía si no es interpretable.

    Que devuelva vacío NO es inocuo: la fecha forma parte de la clave de
    deduplicación, de modo que todos los registros sin fecha colapsarían en una
    sola clave y se descartarían entre sí. Por eso quien llama debe contar los
    fallos y detenerse si son significativos, en lugar de continuar con una
    serie mutilada y cifras verosímiles.
    """
    limpio = (texto or "").strip()
    if not limpio:
        return ""
    candidatos = (formato,) + FORMATOS_FECHA if formato else FORMATOS_FECHA
    for candidato in candidatos:
        try:
            return datetime.strptime(limpio, candidato).date().isoformat()
        except ValueError:
            continue
    return ""


def _numero(texto: Any) -> float | None:
    """Convierte el valor admitiendo coma decimal. None si no es numérico."""
    if texto is None or texto == "":
        return None
    try:
        return float(str(texto).strip().replace(",", "."))
    except ValueError:
        return None


# =============================================================================
# Deduplicación
# =============================================================================
def clave_de(registro_normalizado: dict[str, Any]) -> tuple[str, str, str]:
    """
    Clave de deduplicación: estación, serie y fecha.

    CLAUDE.md, sección 7, la enuncia como (CodigoEstacion, Parametro, Fecha).
    Se usa la etiqueta en lugar del parámetro porque un mismo parámetro tiene
    varias series: PRECIPITACION incluye PTPM_TT_M y PTPG_TT_D entre otras, y
    agruparlas bajo la misma clave fusionaría series distintas.
    """
    return (str(registro_normalizado.get("codigo", "")),
            str(registro_normalizado.get("etiqueta", "")),
            str(registro_normalizado.get("fecha", "")))


def precedencia_de(nivel: Any, tabla: dict) -> int:
    """
    Precedencia del nivel de aprobación. Menor número gana.

    Un nivel no declarado recibe la peor precedencia en lugar de un valor
    intermedio: ante lo desconocido se prefiere el registro cuyo nivel sí se
    conoce.
    """
    observados = (tabla or {}).get("observados") or {}
    entrada = observados.get(str(nivel).strip())
    if isinstance(entrada, dict) and entrada.get("precedencia") is not None:
        return int(entrada["precedencia"])
    return 99


def deduplicar(
    registros: Iterator[dict[str, Any]], tabla_aprobacion: dict
) -> tuple[dict[tuple, dict], int, int]:
    """
    Conserva un registro por clave, el de mejor nivel de aprobación.

    Devuelve (registros por clave, leídos, conflictos). Un conflicto es una
    clave que apareció más de una vez, con independencia de que los valores
    coincidan: es lo que hay que reportar para que el descarte sea explicable.
    """
    conservados: dict[tuple, dict] = {}
    leidos = 0
    conflictos = 0

    for registro_actual in registros:
        leidos += 1
        clave = clave_de(registro_actual)
        previo = conservados.get(clave)

        if previo is None:
            conservados[clave] = registro_actual
            continue

        conflictos += 1
        if (precedencia_de(registro_actual.get("nivel_aprobacion"), tabla_aprobacion)
                < precedencia_de(previo.get("nivel_aprobacion"), tabla_aprobacion)):
            conservados[clave] = registro_actual

    return conservados, leidos, conflictos


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    solo_inventario: bool = False,
    ruta_json: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Lee, normaliza, deduplica y escribe la serie consolidada."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)

    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    ruta_perfiles = configuracion.ruta_de("ideam.dhime_zip.perfiles",
                                          debe_existir=True)
    perfiles, calificadores, aprobacion = cargar_perfiles(ruta_perfiles)
    directorio = rutas.directorio("crudos_ideam_zip", base, crear=True)
    patron = configuracion.obtener("ideam.dhime_zip.patron_archivo")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={
            "perfiles": rutas.relativa(ruta_perfiles, base),
            "directorio de archivos": rutas.relativa(directorio, base),
            "perfiles utilizables": ", ".join(sorted(perfiles)),
        },
        parametros=configuracion.parametros((
            "ideam.fuente_primaria",
            "ideam.deduplicacion.clave",
            "ideam.deduplicacion.precedencia_aprobacion",
            "ideam.nivel_aprobacion.usar_como_filtro",
        )),
    )

    resultado = ResultadoM04()

    if not (aprobacion or {}).get("confirmada"):
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "nivel_aprobacion.confirmada",
            "la correspondencia de códigos de nivel de aprobación no está "
            "confirmada en perfiles_ideam.yaml. Deducirla al revés haría que la "
            "deduplicación conservara el registro preliminar y descartara el "
            "definitivo, en silencio.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    for perfil in perfiles.values():
        if not perfil.verificado:
            resultado.hallazgos.append(Hallazgo(
                ADVERTENCIA, f"perfil.{perfil.nombre}",
                perfil.advertencia or
                f"el perfil {perfil.nombre} no está verificado contra archivos "
                "reales.",
            ))

    archivos = sorted(directorio.glob("*.zip"))
    if not archivos:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "ideam.dhime_zip",
            f"no hay archivos .zip en {rutas.relativa(directorio, base)}.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, f"Lectura de {len(archivos)} archivo(s)"):
        conservados, leidos, conflictos = deduplicar(
            _recorrer(archivos, perfiles, patron, resultado, logger), aprobacion
        )
        resultado.archivos = len(archivos)
        resultado.registros_leidos = leidos
        resultado.registros_unicos = len(conservados)
        resultado.conflictos = conflictos

    logger.info(
        "Leídos %s | únicos %s | conflictos %s (%.1f%% redundante)",
        f"{leidos:,}", f"{len(conservados):,}", f"{conflictos:,}",
        100.0 * (leidos - len(conservados)) / max(1, leidos),
    )

    if resultado.fechas_ilegibles:
        proporcion = 100.0 * resultado.fechas_ilegibles / max(1, leidos)
        severidad = BLOQUEANTE if proporcion > 1.0 else ADVERTENCIA
        resultado.hallazgos.append(Hallazgo(
            severidad, "ideam.fecha",
            f"{resultado.fechas_ilegibles:,} registro(s) ({proporcion:.2f}%) con "
            f"fecha no interpretable. Ejemplos: {resultado.ejemplos_fecha}. "
            "La fecha forma parte de la clave de deduplicación: todos ellos "
            "colapsarían en una sola clave y se descartarían entre sí. Añadir "
            "el formato a FORMATOS_FECHA antes de usar la serie.",
        ))

    resultado.hallazgos.extend(_resumir(resultado, calificadores))

    if not solo_inventario:
        with registro.bloque(logger, "Escritura de la serie consolidada"):
            _escribir(configuracion, base, conservados, resultado, logger)

    codigo = (SALIDA_BLOQUEANTE if esquema.hay_bloqueantes(resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _recorrer(archivos, perfiles, patron, resultado, logger):
    """Genera los registros normalizados de todos los archivos."""
    for indice, archivo in enumerate(archivos, start=1):
        try:
            for perfil, fila in leer_zip(archivo, perfiles, patron):
                resultado.perfiles_usados[perfil.nombre] = \
                    resultado.perfiles_usados.get(perfil.nombre, 0) + 1
                normalizado = normalizar(fila, perfil)
                if not normalizado["fecha"]:
                    resultado.fechas_ilegibles += 1
                    crudo = (fila.get(perfil.campos.get("fecha", ""), "") or "")
                    if crudo and len(resultado.ejemplos_fecha) < 5:
                        resultado.ejemplos_fecha.append(crudo.strip())
                resultado.estaciones.add(normalizado["codigo"])
                etiqueta = normalizado["etiqueta"]
                resultado.series[etiqueta] = \
                    resultado.series.get(etiqueta, 0) + 1
                for marca in _marcas(normalizado.get("calificador", "")):
                    resultado.calificadores[marca] = \
                        resultado.calificadores.get(marca, 0) + 1
                yield normalizado
        except ErrorFormato as exc:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, f"archivo.{archivo.name}", str(exc),
            ))
        if indice % 10 == 0:
            logger.info("  procesados %d/%d archivos", indice, len(archivos))


def _marcas(calificador: str) -> list[str]:
    """Separa un calificador que puede venir combinado con '|'."""
    limpio = (calificador or "").strip()
    return [m.strip() for m in limpio.split("|") if m.strip()] if limpio else []


def _resumir(resultado: ResultadoM04, calificadores: dict) -> list[Hallazgo]:
    """Emite los hallazgos derivados del contenido leído."""
    hallazgos = [Hallazgo(
        INFORMATIVO, "ideam.ingesta",
        f"{resultado.registros_unicos:,} registro(s) único(s) de "
        f"{len(resultado.estaciones):,} estación(es) y "
        f"{len(resultado.series)} serie(s).",
    )]

    if resultado.conflictos:
        hallazgos.append(Hallazgo(
            INFORMATIVO, "ideam.deduplicacion",
            f"{resultado.conflictos:,} clave(s) repetida(s) resueltas por "
            "precedencia del nivel de aprobación.",
        ))

    declarados = (calificadores or {}).get("observados") or {}
    for marca, cuantos in sorted(resultado.calificadores.items()):
        efecto = (declarados.get(marca) or {}).get("efecto", "")
        severidad = ADVERTENCIA if marca in ("ACUMULADO", "DATO RECHAZADO") \
            else INFORMATIVO
        hallazgos.append(Hallazgo(
            severidad, f"calificador.{marca}",
            f"{cuantos:,} registro(s)."
            + (f" Efecto declarado: {efecto}." if efecto else
               " Sin efecto declarado en perfiles_ideam.yaml."),
        ))

    return hallazgos


def _escribir(configuracion, base, conservados, resultado, logger) -> None:
    """Escribe la serie consolidada en CSV."""
    destino = rutas.directorio("procesado_series", base, crear=True) / \
        "series_ideam.csv"
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv")

    with destino.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.writer(manejador, delimiter=delimitador)
        escritor.writerow(CAMPOS_INTERNOS)
        for clave in sorted(conservados):
            fila = conservados[clave]
            escritor.writerow([fila.get(c, "") for c in CAMPOS_INTERNOS])

    resultado.productos.append(rutas.relativa(destino, base))
    logger.info("Serie consolidada: %s", destino.name)


def _cerrar(logger, resultado, base, ruta_json, inicio, codigo):
    """Emite el reporte, escribe el JSON y cierra el log."""
    orden = {BLOQUEANTE: 0, ADVERTENCIA: 1, INFORMATIVO: 2}
    hallazgos = sorted(resultado.hallazgos,
                       key=lambda h: (orden.get(h.severidad, 9), h.clave))

    logger.info(registro.SEPARADOR)
    for severidad, emitir in ((BLOQUEANTE, logger.error),
                              (ADVERTENCIA, logger.warning),
                              (INFORMATIVO, logger.info)):
        grupo = [h for h in hallazgos if h.severidad == severidad]
        if not grupo:
            continue
        emitir("%s (%d)", severidad, len(grupo))
        for hallazgo in grupo:
            emitir("  %-34s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    if ruta_json is None:
        ruta_json = rutas.directorio("procesado", base, crear=True) / \
            "M04_ingesta.json"

    reporte = {
        "modulo": MODULO,
        "archivos": resultado.archivos,
        "registros_leidos": resultado.registros_leidos,
        "registros_unicos": resultado.registros_unicos,
        "conflictos": resultado.conflictos,
        "fechas_ilegibles": resultado.fechas_ilegibles,
        "estaciones": len(resultado.estaciones),
        "series": resultado.series,
        "perfiles_usados": resultado.perfiles_usados,
        "calificadores": resultado.calificadores,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    productos = {f"producto {i}": p
                 for i, p in enumerate(resultado.productos, start=1)}
    productos["reporte JSON"] = rutas.relativa(ruta_json, base)
    archivo_log = registro.ruta_log(logger)
    if archivo_log is not None:
        productos["log de ejecución"] = rutas.relativa(archivo_log, base)

    registro.registrar_cierre(
        logger, MODULO, "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO",
        segundos=time.perf_counter() - inicio, productos=productos,
    )
    return codigo, hallazgos


def _analizar_argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        prog="M04_ingesta_ideam.py",
        description="Ingesta, normalización y deduplicación de datos del IDEAM.",
    )
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--solo-inventario", action="store_true",
                            dest="solo_inventario",
                            help="Caracteriza sin escribir la serie consolidada.")
    analizador.add_argument("--json", type=Path, default=None, dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    return analizador.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            solo_inventario=argumentos.solo_inventario,
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

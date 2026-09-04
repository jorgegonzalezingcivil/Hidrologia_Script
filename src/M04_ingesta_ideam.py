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
import datetime as _dt
import itertools
import io
import json
import sys
import time
import base64
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import ingesta_car  # noqa: E402
from comun import dhime, esquema, registro, rutas  # noqa: E402
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
    # DE QUE RED SALIO EL DATO. Desde que la serie consolidada reúne al IDEAM y
    # a la CAR, sin este campo no se puede responder de dónde vino cada cifra, y
    # el análisis de consistencia del M05 no podría distinguir una discrepancia
    # ENTRE REDES de una discrepancia de una estación concreta. Va al final para
    # no alterar el orden de las columnas que ya existían.
    "fuente",
)

# Valor de 'fuente' cuando el registro viene del IDEAM. El de la CAR lo pone su
# propio adaptador, que declara la fuente en su perfil.
FUENTE_IDEAM = "ideam"


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
    series_leidas: dict = field(default_factory=dict)
    perfiles_usados: dict = field(default_factory=dict)
    calificadores: dict = field(default_factory=dict)
    car: dict = field(default_factory=dict)
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
    salida["fuente"] = FUENTE_IDEAM
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
# Descarga desde el servicio
# =============================================================================
def ventanas(fecha_inicio: str, fecha_fin: str, anios: int) -> list:
    """
    Trocea el periodo en ventanas de como mucho `anios` anios.

    El IDEAM limita cada descarga a 30 anios (CLAUDE.md, seccion 7). De ahi que
    haya varios archivos por estacion con rangos solapados, y de ahi que la
    deduplicacion sea imprescindible y no un adorno.
    """
    inicio = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
    fin = datetime.strptime(fecha_fin, "%Y-%m-%d").date()
    if inicio > fin:
        raise ValueError(f"{fecha_inicio} no es anterior a {fecha_fin}")

    tramos, actual = [], inicio
    while actual <= fin:
        try:
            siguiente = actual.replace(year=actual.year + anios)
        except ValueError:                      # 29 de febrero
            siguiente = actual.replace(year=actual.year + anios, day=28)
        cierre = min(siguiente, fin)
        tramos.append((actual.isoformat(), cierre.isoformat()))
        if cierre >= fin:
            break
        actual = cierre
    return tramos


def series_de_estacion(categoria, categorias_por_variable, series_por_variable):
    """
    Devuelve las series a pedir para una estacion, segun su categoria.

    Una climatica principal sirve a precipitacion, temperatura y evaporacion a
    la vez, de modo que se acumulan las series de todas las variables que su
    categoria cubre, sin repetir.
    """
    objetivo = (categoria or "").strip().upper()
    elegidas, vistas = [], set()
    for variable, categorias in sorted(categorias_por_variable.items()):
        if objetivo not in {str(c).strip().upper() for c in categorias}:
            continue
        for serie in series_por_variable.get(variable, ()):
            clave = (serie.get("parametro"), serie.get("etiqueta"))
            if clave in vistas:
                continue
            vistas.add(clave)
            elegidas.append(dict(serie))
    return elegidas



def _parece_base64_zip(valor) -> bool:
    """
    Indica si el valor devuelto es un .zip en base64 y no un mensaje de texto.

    El servicio responde con un mensaje legible cuando la serie no tiene datos
    en el periodo pedido, y ese caso no es un error: es informacion. 'UEsDB' es
    la firma PK de un ZIP codificada en base64.
    """
    if not isinstance(valor, str) or len(valor) < 32:
        return False
    if not valor.isascii():
        return False
    return valor.lstrip().startswith("UEsD")

ARCHIVO_SIN_DATOS = "sin_datos.csv"


def leer_sin_datos(destino: Path) -> dict[str, str]:
    """
    Combinaciones que el servicio ya respondio 'sin datos', con su fecha.

    POR QUE SE RECUERDAN. El servicio no falla cuando una estacion no tiene una
    serie: devuelve un mensaje de texto. Sin registrarlo, cada corrida de la
    cadena vuelve a preguntar por lo mismo, con su envio de trabajo, su espera
    y su pausa entre peticiones. Medido en este estudio: 159 combinaciones sin
    datos, y una pasada del M04 que no traia un solo archivo nuevo tardaba
    cuarenta y tres minutos.

    LA DESCARGA ES DE UNA SOLA VEZ y las corridas posteriores trabajan con lo
    que ya esta; este registro es lo que hace que eso sea cierto tambien para
    las series que no existen. Se guarda la fecha de la respuesta para que se
    sepa de cuando es, y '--redescargar' lo ignora.
    """
    ruta = Path(destino) / ARCHIVO_SIN_DATOS
    if not ruta.is_file():
        return {}
    registro_previo: dict[str, str] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        for fila in csv.DictReader(manejador, delimiter=";"):
            clave = str(fila.get("clave", "")).strip()
            if clave:
                registro_previo[clave] = str(fila.get("fecha", "")).strip()
    return registro_previo


def escribir_sin_datos(destino: Path, registro_previo: dict[str, str]) -> None:
    """Guarda el registro de combinaciones sin datos, ordenado."""
    ruta = Path(destino) / ARCHIVO_SIN_DATOS
    with ruta.open("w", encoding="utf-8-sig", newline="") as manejador:
        escritor = csv.writer(manejador, delimiter=";")
        escritor.writerow(["clave", "fecha"])
        for clave in sorted(registro_previo):
            escritor.writerow([clave, registro_previo[clave]])


def descargar_inventario(configuracion, base, logger, resultado,
                         limite_estaciones=None, etiquetas=None,
                         redescargar: bool = False) -> int:
    """
    Descarga las series de las estaciones seleccionadas por el M03.

    Cada peticion cubre una estacion, un grupo de series y una ventana temporal.
    El archivo llega embebido en base64 dentro de la respuesta y se escribe tal
    cual en el directorio de crudos, de modo que la lectura posterior es la
    misma tanto si el .zip vino del servicio como si se descargo a mano.

    Los archivos ya presentes no se vuelven a pedir: la descarga es reanudable.
    """
    from comun import shapefile as shp

    inventario = rutas.resolver(
        configuracion.obtener("estaciones.salida_seleccionadas"), base)
    if not inventario.is_file():
        raise ErrorFormato(
            f"No existe {inventario.name}. La descarga parte del inventario "
            "del M03: ejecutarlo primero."
        )

    estaciones = list(shp.leer_registros(inventario, ["codigo", "categoria"]))
    if limite_estaciones:
        estaciones = estaciones[:limite_estaciones]

    categorias = {v: list(c) for v, c in
                  configuracion.obtener(
                      "estaciones.categorias_por_variable").items()}
    catalogo = {v: list(s) for v, s in
                configuracion.obtener(
                    "ideam.descarga.series_por_variable").items()}
    tramos = ventanas(
        configuracion.obtener("ideam.descarga.fecha_inicio"),
        configuracion.obtener("ideam.descarga.fecha_fin"),
        int(configuracion.obtener("ideam.descarga.ventana_anios")))
    lote = int(configuracion.obtener("ideam.descarga.max_series_por_trabajo"))
    espera = float(configuracion.obtener(
        "ideam.descarga.espera_entre_trabajos_s"))
    destino = rutas.directorio("crudos_ideam_zip", base, crear=True)

    logger.info("Descarga: %d estacion(es), %d ventana(s)",
                len(estaciones), len(tramos))

    escritos = 0
    # Cuantas peticiones se dan por hechas porque el archivo ya estaba. Se
    # cuenta y se reporta: sin ese numero, '0 archivo(s) nuevos' se lee como
    # que el servicio no tenia nada nuevo, cuando puede que no se le haya
    # preguntado ni una sola vez.
    omitidos = [0]
    sin_datos = {} if redescargar else leer_sin_datos(destino)
    sin_datos_previos = len(sin_datos)
    hoy = _dt.date.today().isoformat()
    for indice, estacion in enumerate(estaciones, start=1):
        codigo = str(estacion["codigo"]).strip()
        deseadas = series_de_estacion(estacion["categoria"], categorias, catalogo)
        if etiquetas:
            # Acotar a unas etiquetas concretas permite completar un vacio del
            # histórico sin volver a pedir lo que ya se tiene. La serie diaria
            # PTPM_CON es el caso: falta en los archivos de 2022 y es la que
            # necesita el M07 para la Pmáx 24 h.
            deseadas = [s for s in deseadas if s["etiqueta"] in etiquetas]
        if not deseadas:
            resultado.hallazgos.append(Hallazgo(
                INFORMATIVO, f"descarga.{codigo}",
                f"categoria {estacion['categoria']!r} sin series declaradas.",
            ))
            continue

        for desde, hasta in tramos:
            for i in range(0, len(deseadas), lote):
                grupo = [
                    dhime.SerieSolicitada(
                        estacion=codigo, parametro=s["parametro"],
                        etiqueta=s["etiqueta"],
                        tipo_serie=s.get("tipo_serie", "Estandar"),
                        calculo=s.get("calculo", ""),
                    ) for s in deseadas[i:i + lote]
                ]
                marca = grupo[0].etiqueta if len(grupo) == 1 else f"g{i // lote}"
                archivo = destino / (
                    f"dhime_{codigo}_{marca}_{desde[:4]}_{hasta[:4]}.zip")
                clave = f"{codigo}|{'+'.join(g.etiqueta for g in grupo)}|{desde[:4]}-{hasta[:4]}"
                if clave in sin_datos:
                    # YA SE PREGUNTO Y NO HABIA. Volver a preguntarlo cuesta un
                    # trabajo, su espera y su pausa, y la respuesta no va a
                    # cambiar por correr la cadena otra vez.
                    omitidos[0] += 1
                    continue
                if archivo.is_file() and not redescargar:
                    # NO SE LE PREGUNTA AL SERVICIO. El archivo existe y se da
                    # por bueno, que es lo que permite completar una descarga
                    # interrumpida sin repetir lo ya traido.
                    #
                    # PERO EL MODULO DECIA 'descargar' Y NO DESCARGABA NADA.
                    # Con el estudio entero ya descargado, una corrida de la
                    # cadena informaba de '0 archivo(s) nuevos' tras cuarenta
                    # minutos de recorrer y saltar, y eso se lee como que el
                    # IDEAM no tiene nada nuevo. No se le habia preguntado: los
                    # .zip de este estudio eran de tres semanas antes. Con
                    # '--redescargar' se vuelve a pedir todo.
                    omitidos[0] += 1
                    continue
                if archivo.is_file():
                    archivo.unlink()
                try:
                    trabajo = dhime.enviar_trabajo(grupo, desde, hasta)
                    dhime.esperar_trabajo(trabajo)
                    salida = dhime.consultar_trabajo(
                        f"{trabajo}/results/Archivo")
                    valor = salida.get("value")
                    if not valor:
                        raise dhime.ErrorDHIME(
                            "el servicio no devolvio contenido")
                    # El servicio NO falla cuando no hay datos: devuelve un
                    # mensaje en texto en lugar del archivo. Decodificarlo como
                    # base64 lanzaba un ValueError que parecia un fallo de red y
                    # ocultaba la causa real, que es una serie sin registros en
                    # ese periodo.
                    if not _parece_base64_zip(valor):
                        resultado.hallazgos.append(Hallazgo(
                            INFORMATIVO, f"descarga.{codigo}",
                            f"{desde} a {hasta}: sin datos para "
                            f"{[g.etiqueta for g in grupo]}. "
                            f"El servicio respondio: {str(valor)[:80]!r}",
                        ))
                        sin_datos[clave] = hoy
                        time.sleep(espera)
                        continue
                    crudo = base64.b64decode(valor)
                    if crudo[:2] != b"PK":
                        raise dhime.ErrorDHIME(
                            "el contenido devuelto no es un .zip")
                    archivo.write_bytes(crudo)
                    escritos += 1
                except Exception as exc:
                    resultado.hallazgos.append(Hallazgo(
                        ADVERTENCIA, f"descarga.{codigo}",
                        f"{desde} a {hasta}: {type(exc).__name__} "
                        f"{str(exc)[:160]}",
                    ))
                time.sleep(espera)

        if indice % 5 == 0 or indice == len(estaciones):
            logger.info("  %d/%d estaciones | %d archivo(s) nuevos",
                        indice, len(estaciones), escritos)

    if len(sin_datos) != sin_datos_previos:
        escribir_sin_datos(destino, sin_datos)
    return escritos, omitidos[0]


# =============================================================================
# Orquestación
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    solo_inventario: bool = False,
    descargar: bool = False,
    limite_estaciones: int | None = None,
    etiquetas: tuple | None = None,
    ruta_json: Path | None = None,
    consola: bool = True,
    redescargar: bool = False,
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

    if configuracion.obtener("ideam.descarga.activar", False) or descargar:
        with registro.bloque(logger, "Descarga desde el servicio DHIME"):
            nuevos, omitidos = descargar_inventario(
                configuracion, base, logger, resultado, limite_estaciones,
                etiquetas, redescargar)
            logger.info("Archivos nuevos descargados: %d | peticiones "
                        "omitidas por archivo ya presente: %d",
                        nuevos, omitidos)
            if omitidos and not redescargar:
                resultado.hallazgos.append(Hallazgo(
                    ADVERTENCIA, "ideam.descarga_omitida",
                    f"{omitidos} petición(es) NO se hicieron porque el archivo "
                    "ya estaba en el directorio de crudos. Eso permite "
                    "completar una descarga interrumpida sin repetir lo "
                    "traído, pero significa que al servicio NO se le preguntó: "
                    f"'{nuevos} archivo(s) nuevos' no dice que el IDEAM no "
                    "tenga nada nuevo. Para traer de nuevo la serie completa, "
                    "ejecutar con '--redescargar'.",
                ))

    archivos = sorted(directorio.glob("*.zip"))
    if not archivos:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "ideam.dhime_zip",
            f"no hay archivos .zip en {rutas.relativa(directorio, base)}.",
        ))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Series de la CAR"):
        registros_car = _ingerir_car(configuracion, base, resultado, logger)

    with registro.bloque(logger, f"Lectura de {len(archivos)} archivo(s)"):
        # LAS DOS REDES PASAN POR LA MISMA DEDUPLICACION. Los codigos del IDEAM
        # y de la CAR no se solapan (verificado: cero comunes entre 4.521 y
        # 434), de modo que no puede haber conflicto entre fuentes; pero
        # deduplicar juntas es lo que GARANTIZA que si algun dia se solaparan,
        # el conflicto se resuelva por la precedencia declarada y quede contado,
        # en lugar de aparecer como una estacion pesada dos veces.
        conservados, leidos, conflictos = deduplicar(
            itertools.chain(
                _recorrer(archivos, perfiles, patron, resultado, logger),
                registros_car),
            aprobacion
        )
        resultado.archivos = len(archivos)
        resultado.registros_leidos = leidos
        resultado.registros_unicos = len(conservados)
        resultado.conflictos = conflictos
        # Recuento por serie sobre lo CONSERVADO, no sobre lo leído. El
        # contador del generador corre antes de deduplicar, de modo que
        # publicarlo junto a registros_unicos invitaba a compararlos: sumaban
        # cosas distintas (2,161,619 frente a 1,844,712) sin decirlo.
        resultado.series = {}
        for fila in conservados.values():
            etiqueta = fila.get("etiqueta", "")
            resultado.series[etiqueta] = resultado.series.get(etiqueta, 0) + 1

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


def _ingerir_car(configuracion, base, resultado, logger) -> list[dict]:
    """
    Lee el libro de la CAR, si el estudio lo tiene, y lo deja listo para unir.

    UN ESTUDIO SIN DATOS DE LA CAR ES LO NORMAL fuera de su jurisdicción, de
    modo que la ausencia se informa y se sigue con el IDEAM. Lo que sí se
    reporta como advertencia es que el libro esté DECLARADO y no exista: eso no
    es una ausencia, es una ruta equivocada.
    """
    declarado = str(configuracion.obtener("series.car.libro", "") or "").strip()
    if not declarado:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "car.sin_declarar",
            "el estudio no declara libro de la CAR. La serie se construye solo "
            "con el IDEAM, que es lo normal fuera de su jurisdiccion."))
        return []

    ruta_libro = rutas.resolver(declarado, base)
    if not ruta_libro.is_file():
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "car.libro",
            f"se declara el libro de la CAR en {declarado} y no existe. NO se "
            "ingesta nada de esa red, y el estudio continua solo con el IDEAM: "
            "revisar la ruta antes de dar la serie por completa."))
        return []

    try:
        perfil = ingesta_car.cargar_perfil(
            rutas.resolver(configuracion.obtener("series.car.perfil"), base))
        catalogo = ingesta_car.leer_catalogo(
            rutas.resolver(perfil.catalogo, base))
        salida = ingesta_car.ingerir(ruta_libro, perfil, catalogo)
    except (ErrorFormato, ErrorRutas, ImportError, ValueError) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "car.ingesta",
            f"no se pudo leer el libro de la CAR: {error}. Se detiene en lugar "
            "de escribir una serie a la que le falta una red entera sin que "
            "nada lo indique."))
        return []

    logger.info("CAR: %d fila(s) leidas, %d normalizadas, %d estacion(es)",
                salida.leidos, len(salida.registros), len(salida.estaciones))
    resultado.car = {
        "libro": rutas.relativa(ruta_libro, base),
        "leidos": salida.leidos,
        "normalizados": len(salida.registros),
        "estaciones": len(salida.estaciones),
        "por_etiqueta": dict(sorted(salida.por_etiqueta.items())),
        "descartados": dict(salida.descartados),
    }

    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "car.ingesta",
        f"de {salida.leidos} filas del libro de la CAR entraron "
        f"{len(salida.registros)} de {len(salida.estaciones)} estacion(es), en "
        f"las series {', '.join(sorted(salida.por_etiqueta))}. El resto son "
        "descartes declarados en el perfil, no perdidas: "
        + "; ".join(f"{motivo}: {cuantos}"
                    for motivo, cuantos in sorted(salida.descartados.items(),
                                                  key=lambda kv: -kv[1]))))

    sin_consumidor = {e: n for e, n in salida.sin_consumidor.items() if n}
    if sin_consumidor:
        resultado.hallazgos.append(Hallazgo(
            INFORMATIVO, "car.sin_consumidor",
            f"entran {sum(sin_consumidor.values())} registro(s) de "
            f"{', '.join(sorted(sin_consumidor))} que HOY ningun modulo "
            "consume: la CAR publica media mensual y el M18a construye el campo "
            "termico con series diarias de maxima y minima, que es otra "
            "variable. Quedan en la serie para que el informe pueda citarlas."))

    for motivo in (ingesta_car.MOTIVO_UNIDAD, ingesta_car.MOTIVO_SIN_CATALOGO):
        cuantos = salida.descartados.get(motivo, 0)
        if not cuantos:
            continue
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, f"car.{'unidad' if motivo == ingesta_car.MOTIVO_UNIDAD else 'catalogo'}",
            f"{cuantos} fila(s) rechazadas por '{motivo}'. Ejemplos: "
            f"{salida.ejemplos.get(motivo, [])[:3]}. No es un descarte "
            "declarado: o el perfil no describe lo que la CAR entrega, o la "
            "entrega cambio."))

    return salida.registros


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
    destino = rutas.resolver(configuracion.obtener("series.consolidada"), base)
    destino.parent.mkdir(parents=True, exist_ok=True)
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
        "series_leidas": resultado.series_leidas,
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
    analizador.add_argument(
        "--redescargar", action="store_true",
        help="Vuelve a pedir al servicio las series que ya estan descargadas.")
    analizador.add_argument("--descargar", action="store_true",
                            help="Descarga las series del inventario del M03.")
    analizador.add_argument("--limite-estaciones", type=int, default=None,
                            dest="limite_estaciones",
                            help="Descarga solo las N primeras, para pruebas.")
    analizador.add_argument("--etiquetas", default=None,
                            help="Etiquetas a descargar, separadas por coma. "
                                 "Sin ella se piden todas las de la categoria.")
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
            descargar=argumentos.descargar or argumentos.redescargar,
            redescargar=argumentos.redescargar,
            limite_estaciones=argumentos.limite_estaciones,
            etiquetas=(tuple(e.strip() for e in
                             argumentos.etiquetas.split(','))
                       if argumentos.etiquetas else None),
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

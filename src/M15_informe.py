#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M15 - Redacción del informe en Word
===================================
Entorno: venv del proyecto.

RESUELVE LO MECÁNICO Y NO ESCRIBE JUICIO. La plantilla del consultor lleva sus
instrucciones marcadas en verde, de tres tipos:

    Colocar Figura: <archivo>.png    inserta la figura que produjo la cadena
    Completar la Tabla N-M           llena la tabla con los datos del estudio
    Analizar Ilustración N-M         redacción, que este módulo NO toca

Las dos primeras son mecánicas: el archivo existe o no, el dato está o no. La
tercera exige mirar el resultado y decir qué significa, y eso no se programa. El
módulo las deja intactas, en verde, para que la pasada de redacción las
encuentre.

SE EDITA EL DOCUMENTO, NO SE RECONSTRUYE. Es la diferencia entre conservar las
92 referencias cruzadas, las 152 leyendas numeradas por campo, los 214
hipervínculos y los 314 marcadores, o perderlos todos. Verificado: tras editar,
los recuentos de REF, SEQ, STYLEREF, PAGEREF, TOC, marcadores e hipervínculos
son idénticos.

LOS ÍNDICES QUEDAN DESACTUALIZADOS a propósito. Son campos de Word y conservan
su texto en caché hasta que se actualizan: al abrir el documento hay que
responder que sí, o pulsar Ctrl+E y F9.

Productos:
    <salida>/informe_hidrologico.docx
    data/02_procesado/M15_informe.json

Uso:
    python src/M15_informe.py
    python src/M15_informe.py --plantilla otra_base.docx

Códigos de salida:
    0  informe escrito sin hallazgos bloqueantes
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o la plantilla
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

import docx_plantilla as plantilla_docx  # noqa: E402
from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar, leer_yaml  # noqa: E402
from comun.errores import (  # noqa: E402
    ErrorConfiguracion,
    ErrorFormato,
    ErrorHidrologia,
    ErrorRutas,
)
from comun.esquema import ADVERTENCIA, BLOQUEANTE, INFORMATIVO, Hallazgo  # noqa: E402

MODULO = "M15"
DESCRIPCION = "Redacción del informe en Word"

SALIDA_CORRECTA = 0
SALIDA_BLOQUEANTE = 1
SALIDA_ERROR = 3

# Las tres instrucciones que la plantilla marca en verde. El módulo resuelve las
# dos primeras y respeta la tercera.
PATRON_FIGURA = re.compile(r"^\s*Colocar\s+Figura\s*:\s*(.+?)\s*$", re.I)
PATRON_TABLA = re.compile(r"^\s*Completar\s+la\s+Tabla\s+([0-9]+[-‐-―]?[0-9]*)",
                          re.I)
PATRON_ANALISIS = re.compile(r"^\s*Analizar\b", re.I)

# Un nombre de archivo dentro de la instrucción, con su carpeta si la lleva.
PATRON_ARCHIVO = re.compile(r"([A-Za-z0-9_\-\.]+\.(?:png|jpg|jpeg|svg))", re.I)


@dataclass
class ResultadoM15:
    """Lo que el módulo resolvió y lo que dejó pendiente."""

    plantilla: str = ""
    documento: str = ""
    figuras_puestas: list[str] = field(default_factory=list)
    figuras_ausentes: list[dict[str, Any]] = field(default_factory=list)
    tablas_llenadas: list[dict[str, Any]] = field(default_factory=list)
    tablas_sin_fuente: list[str] = field(default_factory=list)
    analisis_pendientes: int = 0
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Lectura de la plantilla
# =============================================================================
def clasificar(texto: str) -> tuple[str, str]:
    """
    Tipo de instrucción y su argumento.

    Devuelve ('figura'|'tabla'|'analisis'|'', argumento). Se decide por el texto
    y no por el color: el sombreado se pierde al copiar y pegar un párrafo, y
    entonces una instrucción quedaría muda sin que nada lo señalara.
    """
    coincide = PATRON_FIGURA.match(texto)
    if coincide:
        archivo = PATRON_ARCHIVO.search(coincide.group(1))
        return "figura", (archivo.group(1) if archivo else coincide.group(1))
    coincide = PATRON_TABLA.match(texto)
    if coincide:
        return "tabla", coincide.group(1)
    if PATRON_ANALISIS.match(texto):
        return "analisis", texto.strip()
    return "", ""


def buscar_figura(nombre: str, raices: Sequence[Path]) -> Path | None:
    """
    Localiza una figura por su nombre, en el directorio de gráficos y sus temas.

    SE BUSCA POR NOMBRE Y NO POR RUTA. La instrucción de la plantilla dice el
    archivo y a veces la carpeta ('de la carpeta individuales/isoyetas_fase'),
    pero el consultor no tiene por qué llevar la cuenta de en qué subcarpeta lo
    dejó cada módulo: el nombre es único y basta.
    """
    for raiz in raices:
        raiz = Path(raiz)
        if not raiz.is_dir():
            continue
        directo = raiz / nombre
        if directo.is_file():
            return directo
        for encontrado in raiz.rglob(nombre):
            if encontrado.is_file():
                return encontrado
    return None


def leer_tabla_csv(ruta: Path, delimitador: str) -> list[dict[str, str]]:
    """Filas de un CSV de la cadena, con su codificación."""
    ruta = Path(ruta)
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la tabla {ruta}.")
    with ruta.open(encoding="utf-8-sig", newline="") as archivo:
        return list(csv.DictReader(archivo, delimiter=delimitador))


def leer_declaracion_tablas(ruta: Path) -> dict[str, dict[str, Any]]:
    """
    Qué fuente alimenta cada tabla del informe, indexada por su número.

    ES UNA DECLARACIÓN Y NO UNA REGLA. Adivinar la fuente por el texto de la
    leyenda funcionaría hasta el primer informe que llame a las cosas de otro
    modo. El número de tabla es el que la propia plantilla usa en su
    instrucción.
    """
    ruta = Path(ruta)
    if not ruta.is_file():
        return {}
    declaracion = leer_yaml(ruta) or {}
    salida: dict[str, dict[str, Any]] = {}
    for entrada in declaracion.get("tablas") or []:
        numero = str(entrada.get("numero", "")).strip()
        if numero:
            salida[_normalizar_numero(numero)] = entrada
    return salida


def _normalizar_numero(texto: str) -> str:
    """
    '2-1', '2‑1' y '21' son el mismo número de tabla.

    Word compone la leyenda con campos y el guion que aparece en pantalla puede
    ser un guion corto, uno largo o uno de no separación; y al extraer el texto
    de los campos el separador se pierde del todo.
    """
    return re.sub(r"[^0-9]", "", str(texto))


# =============================================================================
# Edición del documento
# =============================================================================
def _ancho_util(documento) -> Any:
    """Ancho de la caja de texto de la primera sección."""
    seccion = documento.sections[0]
    return seccion.page_width - seccion.left_margin - seccion.right_margin


def _ancho_de_celda(celda, ancho_pagina) -> int:
    """
    Ancho util de una celda, para que la figura no la desborde.

    UNA FIGURA A ANCHO DE PAGINA DENTRO DE UNA CELDA rompe la tabla: Word la
    ensancha hasta salirse del margen. Se toma el ancho declarado de la celda y
    se le descuenta el margen habitual; si la celda no lo declara, se reparte el
    ancho de pagina entre las columnas de su fila.
    """
    from docx.shared import Emu

    declarado = celda.width
    if declarado is not None and int(declarado) > 0:
        return max(int(Emu(360000)), int(declarado) - int(Emu(144000)))
    return int(ancho_pagina)


def poner_figura(parrafo, ruta_imagen: Path, ancho) -> None:
    """
    Sustituye el texto de la instrucción por la imagen, centrada.

    SE CONSERVA EL PÁRRAFO, no se crea otro. Su estilo es el que la plantilla
    reservó para la figura, y los párrafos vecinos son la leyenda y la fuente:
    insertar uno nuevo dejaría la figura fuera de esa secuencia.
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Emu

    for run in list(parrafo.runs):
        run._element.getparent().remove(run._element)
    parrafo.alignment = WD_ALIGN_PARAGRAPH.CENTER
    parrafo.add_run().add_picture(str(ruta_imagen), width=Emu(int(ancho)))


def _fijar_texto(celda, valor: str) -> None:
    """
    Escribe en una celda conservando el formato del primer run que ya tenía.

    La plantilla trae las tablas del estudio anterior con su tipografía y su
    tamaño; el consultor pidió expresamente conservarlos. Reescribir la celda
    entera perdería ese formato, de modo que se reutiliza el primer run y se
    vacían los demás.
    """
    parrafo = celda.paragraphs[0]
    if parrafo.runs:
        parrafo.runs[0].text = valor
        for run in parrafo.runs[1:]:
            run.text = ""
    else:
        parrafo.add_run(valor)
    for sobrante in celda.paragraphs[1:]:
        sobrante._element.getparent().remove(sobrante._element)


def llenar_tabla(tabla, filas: Sequence[dict[str, str]],
                 columnas: Sequence[str], encabezados: int) -> dict[str, Any]:
    """
    Reemplaza los datos de la tabla conservando sus filas de encabezado.

    LAS FILAS SOBRANTES SE BORRAN Y LAS QUE FALTAN SE AÑADEN copiando la última
    de datos: así la fila nueva hereda bordes, sombreado y tipografía de la que
    ya estaba, en lugar de salir con el formato por defecto de Word.

    Devuelve el recuento, que es lo que el reporte necesita para decir si la
    tabla quedó con los datos de este estudio o con los del anterior.
    """
    import copy

    disponibles = len(tabla.rows) - encabezados
    if disponibles < 1:
        raise ErrorFormato(
            f"la tabla declara {encabezados} fila(s) de encabezado y solo tiene "
            f"{len(tabla.rows)}: no queda ninguna de datos que rellenar.")

    modelo = tabla.rows[encabezados]._tr
    while len(tabla.rows) - encabezados < len(filas):
        tabla._tbl.append(copy.deepcopy(modelo))
    while len(tabla.rows) - encabezados > len(filas):
        ultimo = tabla.rows[-1]._tr
        ultimo.getparent().remove(ultimo)

    sin_columna: set[str] = set()
    for indice, fila in enumerate(filas):
        celdas = tabla.rows[encabezados + indice].cells
        for posicion, columna in enumerate(columnas):
            if posicion >= len(celdas):
                break
            if columna not in fila:
                sin_columna.add(columna)
            _fijar_texto(celdas[posicion], str(fila.get(columna, "")).strip())
    return {"filas": len(filas), "columnas": len(columnas),
            "sin_columna": sorted(sin_columna)}


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    ruta_plantilla: Path | None = None,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Resuelve figuras y tablas sobre la plantilla y escribe el informe."""
    inicio = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )

    origen = (Path(ruta_plantilla) if ruta_plantilla is not None
              else rutas.resolver(configuracion.obtener("informe.plantilla_base"),
                                  rutas.raiz_codigo()))
    delimitador = configuracion.obtener("insumos_usuario.delimitador_csv", ";")
    graficos = rutas.resolver(configuracion.obtener("graficos.directorio"), base)
    individuales = rutas.resolver(
        configuracion.obtener("graficos.directorio_individuales"), base)
    declaracion = leer_declaracion_tablas(
        rutas.resolver(configuracion.obtener("informe.tablas"), base))

    resultado = ResultadoM15(plantilla=str(origen))

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"plantilla": str(origen),
                 "figuras": rutas.relativa(graficos, base),
                 "tablas declaradas": str(len(declaracion))},
        parametros={"informe.archivo": configuracion.obtener("informe.archivo")},
    )

    try:
        documento = plantilla_docx.abrir(origen)
    except (plantilla_docx.ErrorPlantilla, ErrorRutas) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "informe.plantilla", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio,
                       SALIDA_BLOQUEANTE)

    ancho = _ancho_util(documento)
    cuerpo = list(documento.element.body)
    parrafos = {p._element: p for p in documento.paragraphs}
    tablas = {t._tbl: t for t in documento.tables}

    with registro.bloque(logger, "Instrucciones de la plantilla"):
        for posicion, elemento in enumerate(cuerpo):
            parrafo = parrafos.get(elemento)
            if parrafo is None:
                continue
            tipo, argumento = clasificar(parrafo.text)
            if tipo == "analisis":
                resultado.analisis_pendientes += 1
            elif tipo == "figura":
                _resolver_figura(parrafo, argumento, [graficos, individuales],
                                 ancho, resultado, base)
            elif tipo == "tabla":
                _resolver_tabla(cuerpo, posicion, tablas, argumento,
                                declaracion, delimitador, base, resultado)

        # LAS INSTRUCCIONES TAMBIEN VIVEN DENTRO DE LAS TABLAS. La plantilla
        # compone algunas figuras en celdas, para ponerlas de a dos, y esos
        # parrafos no estan en documento.paragraphs: son 16 de las 92, y sin
        # recorrerlas quedaban mudas sin que nada lo dijera.
        for tabla_contenedora in documento.tables:
            for fila in tabla_contenedora.rows:
                for celda in fila.cells:
                    for parrafo in celda.paragraphs:
                        tipo, argumento = clasificar(parrafo.text)
                        if tipo == "analisis":
                            resultado.analisis_pendientes += 1
                        elif tipo == "figura":
                            _resolver_figura(
                                parrafo, argumento, [graficos, individuales],
                                _ancho_de_celda(celda, ancho), resultado, base)

        logger.info("%d figura(s) puestas, %d tabla(s) llenadas, %d analisis "
                    "sin tocar", len(resultado.figuras_puestas),
                    len(resultado.tablas_llenadas),
                    resultado.analisis_pendientes)

    salida = rutas.directorio("resultados", base, crear=True) / str(
        configuracion.obtener("informe.archivo"))
    salida.parent.mkdir(parents=True, exist_ok=True)
    documento.save(str(salida))
    resultado.documento = rutas.relativa(salida, base)
    resultado.productos.append(resultado.documento)

    resultado.hallazgos.extend(_resumir(resultado))
    codigo = (SALIDA_BLOQUEANTE
              if any(h.severidad == BLOQUEANTE for h in resultado.hallazgos)
              else SALIDA_CORRECTA)
    return _cerrar(logger, resultado, base, ruta_json, inicio, codigo)


def _resolver_figura(parrafo, nombre, raices, ancho, resultado, base) -> None:
    """Pone la figura si existe; si no, deja la instrucción y lo reporta."""
    ruta = buscar_figura(nombre, raices)
    if ruta is None:
        # LA INSTRUCCION SE DEJA INTACTA. Borrarla dejaria un hueco mudo en el
        # informe y nadie sabria que falta una figura.
        resultado.figuras_ausentes.append({"archivo": nombre})
        return
    poner_figura(parrafo, ruta, ancho)
    resultado.figuras_puestas.append(rutas.relativa(ruta, base))


def _resolver_tabla(cuerpo, posicion, tablas, numero, declaracion,
                    delimitador, base, resultado) -> None:
    """
    Llena la primera tabla que sigue a la instrucción.

    ENTRE LA INSTRUCCION Y LA TABLA VA LA LEYENDA, de modo que no basta con
    mirar el elemento siguiente. Se avanza hasta encontrar la tabla, y si antes
    aparece otra instrucción se abandona: significa que esta no tenía tabla.
    """
    clave = _normalizar_numero(numero)
    entrada = declaracion.get(clave)
    if entrada is None:
        resultado.tablas_sin_fuente.append(numero)
        return

    tabla = None
    for elemento in cuerpo[posicion + 1:posicion + 8]:
        if elemento in tablas:
            tabla = tablas[elemento]
            break
    if tabla is None:
        resultado.tablas_sin_fuente.append(numero)
        return

    try:
        filas = leer_tabla_csv(rutas.resolver(str(entrada["fuente"]), base),
                               delimitador)
        detalle = llenar_tabla(tabla, filas,
                               [str(c) for c in entrada.get("columnas") or []],
                               int(entrada.get("encabezados", 1)))
    except (ErrorRutas, ErrorFormato, KeyError, ValueError) as error:
        resultado.tablas_sin_fuente.append(f"{numero} ({error})")
        return
    detalle["numero"] = numero
    detalle["fuente"] = str(entrada["fuente"])
    resultado.tablas_llenadas.append(detalle)


def _resumir(resultado: ResultadoM15) -> list[Hallazgo]:
    """Lo que el módulo resolvió y lo que queda por hacer a mano."""
    hallazgos: list[Hallazgo] = []

    hallazgos.append(Hallazgo(
        INFORMATIVO, "informe.resuelto",
        f"{len(resultado.figuras_puestas)} figura(s) insertadas y "
        f"{len(resultado.tablas_llenadas)} tabla(s) llenadas con los datos de "
        "este estudio. Se editó el documento, no se reconstruyó: las "
        "referencias cruzadas, las leyendas numeradas por campo, los "
        "hipervínculos y los marcadores quedan intactos.",
    ))

    if resultado.analisis_pendientes:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "informe.analisis_pendiente",
            f"{resultado.analisis_pendientes} instruccion(es) de análisis "
            "siguen en verde y sin resolver, que es lo previsto: exigen mirar "
            "el resultado y decir qué significa, y eso no se programa. El "
            "informe NO está terminado hasta que se redacten y se borren.",
        ))

    if resultado.figuras_ausentes:
        nombres = sorted({f["archivo"] for f in resultado.figuras_ausentes})
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "informe.figuras_ausentes",
            f"{len(nombres)} figura(s) que la plantilla pide no están en el "
            f"estudio: {nombres}. Su instrucción se dejó en el documento en "
            "lugar de borrarla: un hueco mudo no se ve al revisar.",
        ))

    if resultado.tablas_sin_fuente:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "informe.tablas_sin_fuente",
            f"{len(resultado.tablas_sin_fuente)} tabla(s) quedaron con los "
            f"datos de la plantilla: {sorted(resultado.tablas_sin_fuente)}. "
            "Falta declarar su fuente en informe.tablas, o la fuente declarada "
            "no se pudo leer.",
        ))

    sin_columna = sorted({c for t in resultado.tablas_llenadas
                          for c in t.get("sin_columna", [])})
    if sin_columna:
        hallazgos.append(Hallazgo(
            ADVERTENCIA, "informe.columnas_ausentes",
            f"se declararon columnas que su CSV no tiene: {sin_columna}. Esas "
            "celdas quedaron VACIAS, no con el valor anterior: un dato del "
            "estudio pasado en una tabla de este es peor que un hueco.",
        ))

    hallazgos.append(Hallazgo(
        INFORMATIVO, "informe.actualizar_campos",
        "los índices de contenido, de ilustraciones y de tablas son campos de "
        "Word y conservan su texto en caché: al abrir el documento hay que "
        "responder que sí a la actualización, o pulsar Ctrl+E y F9.",
    ))
    return hallazgos


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
            emitir("  %-40s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    if ruta_json is None:
        ruta_json = (rutas.directorio("procesado", base, crear=True)
                     / "M15_informe.json")
    reporte = {
        "modulo": MODULO,
        "plantilla": resultado.plantilla,
        "documento": resultado.documento,
        "figuras_puestas": resultado.figuras_puestas,
        "figuras_ausentes": resultado.figuras_ausentes,
        "tablas_llenadas": resultado.tablas_llenadas,
        "tablas_sin_fuente": resultado.tablas_sin_fuente,
        "analisis_pendientes": resultado.analisis_pendientes,
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
        productos["log de ejecucion"] = rutas.relativa(archivo_log, base)

    registro.registrar_cierre(
        logger, MODULO, "CORRECTO" if codigo == SALIDA_CORRECTA else "DETENIDO",
        segundos=time.perf_counter() - inicio, productos=productos)
    return codigo, hallazgos


# =============================================================================
# Interfaz de linea de comandos
# =============================================================================
def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    analizador = argparse.ArgumentParser(
        prog="M15_informe.py",
        description="Resuelve figuras y tablas sobre la plantilla del informe.")
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--plantilla", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None,
                            dest="json_salida")
    analizador.add_argument("--silencioso", action="store_true")
    argumentos = analizador.parse_args(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json_salida,
            ruta_plantilla=argumentos.plantilla,
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

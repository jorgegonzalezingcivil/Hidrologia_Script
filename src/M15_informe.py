#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M15 - Redacción del informe en Word
===================================
Entorno: venv del proyecto.

REÚNE LO QUE LA CADENA CALCULÓ Y LO ESCRIBE. Los dieciocho módulos anteriores
dejaron tablas, figuras y reportes JSON con sus hallazgos; este los recoge y
produce el documento que se entrega.

TRES COSAS VIVEN FUERA DEL CÓDIGO, y es deliberado:

    templates/informe.dotx        el formato: estilos, encabezados, página
    config/informe.yaml           la estructura de capítulos
    templates/informe/texto.yaml  la redacción, con huecos por rellenar

Reordenar el informe, cambiar una frase o ajustar el formato de casa no exige
tocar Python. La plantilla se deriva del informe de referencia del consultor
(CLAUDE.md, sección 10), que es su propiedad y define el formato de entrega.

LO QUE EL MÓDULO NO INVENTA. Un valor que no pueda resolver NO se escribe como
un número plausible ni se deja la llave cruda en el texto: se sustituye por una
marca visible y se reporta como hallazgo. Un informe con un dato inventado es
peor que uno con un hueco señalado, porque el hueco se ve y el dato no.

LAS MARCAS DE PENDIENTE SON PARTE DEL PRODUCTO. Hay apartados que ninguna cadena
puede redactar: el alcance del contrato, la descripción del sistema hídrico con
conocimiento de campo, las conclusiones. El módulo deja en ellos una marca
visible con lo que falta, en lugar de rellenarlos con texto de relleno.

LA NUMERACIÓN VA CON CAMPOS DE WORD. Las leyendas usan STYLEREF para el capítulo
y SEQ para el orden dentro de él, de modo que insertar una tabla a mano renumera
el resto. Escribir el número como texto produce una numeración que se descuadra
en silencio en cuanto alguien edita el documento.

Productos:
    outputs/06_informe/informe_hidrologico.docx
    data/02_procesado/M15_informe.json

Uso:
    python src/M15_informe.py
    python src/M15_informe.py --plantilla   # regenera templates/informe.dotx

Códigos de salida:
    0  correcto
    1  hay hallazgos bloqueantes
    3  no se pudo leer la configuración o los insumos
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import esquema, registro, rutas  # noqa: E402
from comun.config import Config, cargar  # noqa: E402
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

# Marca que sustituye a un valor que la cadena no resolvió. Se elige visible a
# propósito: tiene que saltar a la vista al hojear el documento.
MARCA_SIN_VALOR = "[[SIN DATO]]"
MARCA_PENDIENTE = "PENDIENTE"

ESTILO_TITULO = {1: "Ttulo1", 2: "Ttulo2", 3: "Ttulo3", 4: "Ttulo4"}
ESTILO_TEXTO = "Sinespaciado"
ESTILO_LEYENDA = "Descripcin"
ESTILO_FUENTE = "Fuente"
ESTILO_TABLA = "Tablaconcuadrcula"


@dataclass
class ResultadoM15:
    documento: str = ""
    capitulos: int = 0
    tablas: int = 0
    figuras: int = 0
    parrafos: int = 0
    pendientes: list[str] = field(default_factory=list)
    sin_valor: list[str] = field(default_factory=list)
    ausentes: list[str] = field(default_factory=list)
    valores: dict[str, Any] = field(default_factory=dict)
    productos: list[str] = field(default_factory=list)
    hallazgos: list[Hallazgo] = field(default_factory=list)


# =============================================================================
# Funciones puras
# =============================================================================
def sustituir(texto: str, valores: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Rellena las llaves del texto con los valores calculados.

    NO SE INVENTA NADA. Una llave sin valor se sustituye por una marca visible y
    su nombre se devuelve, para que el módulo lo reporte. Dejar la llave cruda
    produciría un informe con '{area_km2}' impreso; poner un cero produciría uno
    con un dato falso que nadie detecta.

    Devuelve el texto y la lista de llaves que no se pudieron resolver.
    """
    faltantes: list[str] = []
    salida: list[str] = []
    resto = texto
    while True:
        inicio = resto.find("{")
        if inicio < 0:
            salida.append(resto)
            break
        fin = resto.find("}", inicio)
        if fin < 0:
            salida.append(resto)
            break
        salida.append(resto[:inicio])
        clave = resto[inicio + 1:fin].strip()
        valor = valores.get(clave)
        if valor is None or valor == "":
            faltantes.append(clave)
            salida.append(MARCA_SIN_VALOR)
        else:
            salida.append(str(valor))
        resto = resto[fin + 1:]
    return "".join(salida), faltantes


def formatear(valor: Any, decimales: int = 2) -> str:
    """
    Da a un número la forma que el informe usa: coma decimal y sin exponentes.

    En un documento en español el separador decimal es la coma. Escribir 220.60
    obliga al lector a decidir si son doscientos veinte o veintidós mil.
    """
    if valor is None or valor == "":
        return ""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    if numero == int(numero) and abs(numero) < 1e15:
        return f"{int(numero):,}".replace(",", ".")
    return f"{numero:,.{decimales}f}".replace(",", "\x00").replace(
        ".", ",").replace("\x00", ".")


def leer_tabla(ruta: Path, delimitador: str, columnas: dict[str, str],
               orden: str = "", filas_max: int = 0,
               filtro: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Lee un CSV de la cadena y lo deja listo para insertar como tabla.

    SOLO SE MUESTRAN LAS COLUMNAS DECLARADAS, y con su nombre descriptivo. Las
    tablas de la cadena llevan nombres cortos y de trabajo ('q_T100_m3s'); un
    informe con esos encabezados obliga al lector a descifrarlos.

    Excepciones
    -----------
    ErrorRutas
        Si el archivo no está.
    ErrorFormato
        Si no trae ninguna de las columnas declaradas.
    """
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra la tabla {ruta}.")
    with ruta.open(encoding="utf-8-sig", newline="") as manejador:
        filas = list(csv.DictReader(manejador, delimiter=delimitador))
    if not filas:
        raise ErrorFormato(f"{ruta.name} esta vacio.")

    presentes = [c for c in columnas if c in filas[0]]
    if not presentes:
        raise ErrorFormato(
            f"{ruta.name} no trae ninguna de las columnas declaradas "
            f"({sorted(columnas)}). Tiene: {sorted(filas[0])[:8]}.")

    if filtro:
        columna, valor = filtro.get("columna", ""), str(filtro.get("valor", ""))
        filas = [f for f in filas if str(f.get(columna, "")).strip() == valor]

    if orden and orden in (filas[0] if filas else {}):
        def clave(fila):
            try:
                return (0, float(fila[orden]))
            except (TypeError, ValueError):
                return (1, str(fila[orden]))
        filas.sort(key=clave)

    total = len(filas)
    omitidas = 0
    if filas_max and total > filas_max:
        filas = filas[:filas_max]
        omitidas = total - filas_max

    return {
        "encabezados": [columnas[c] for c in presentes],
        "filas": [[formatear(f.get(c, "")) for c in presentes] for f in filas],
        "total": total,
        "omitidas": omitidas,
        "columnas_ausentes": [c for c in columnas if c not in presentes],
    }


def recolectar_valores(reportes: dict[str, dict], tablas: dict[str, list],
                       configuracion) -> dict[str, Any]:
    """
    Reúne en un solo diccionario lo que la cadena calculó, para el texto.

    CADA VALOR SALE DE SU MÓDULO Y NO SE RECALCULA AQUÍ. El M15 redacta; si
    volviera a promediar o a contar, el informe podría decir una cifra y las
    tablas otra, y esa discrepancia es la que nadie perdona en una revisión.
    """
    valores: dict[str, Any] = {}

    def numero(diccionario, *claves, decimales=2):
        actual: Any = diccionario
        for clave in claves:
            if not isinstance(actual, dict):
                return None
            actual = actual.get(clave)
        return formatear(actual, decimales) if actual is not None else None

    # parametros.csv es ANCHO: una sola fila con una columna por parametro. Fue
    # lo que devolvio la primera version vacio, porque la leia como una tabla
    # larga de 'parametro' y 'valor', que es la forma que NO tiene.
    filas = tablas.get("parametros", [])
    parametros = filas[0] if filas else {}
    for destino, columna, decimales in (
            ("area_km2", "area_km2", 2), ("perimetro_km", "perimetro_km", 2),
            ("longitud_cauce_km", "long_cauce_principal_km", 2),
            ("cota_min", "cota_min", 0), ("cota_max", "cota_max", 0),
            ("cota_media", "cota_media", 0)):
        if parametros.get(columna) not in (None, ""):
            valores[destino] = formatear(parametros[columna], decimales)

    subcuencas = tablas.get("subcuencas", [])
    if subcuencas:
        valores["n_subcuencas"] = len(subcuencas)
        area_total = sum(float(s.get("area_km2") or 0) for s in subcuencas)
        if area_total > 0:
            valores.setdefault("area_km2", formatear(area_total, 2))
            valores["cn_medio"] = formatear(sum(
                float(s.get("cn") or 0) * float(s.get("area_km2") or 0)
                for s in subcuencas) / area_total, 1)
        for destino, columna in (("tc_mediana_min", "tc_minutos"),
                                 ("tlag_mediana_min", "tlag_minutos")):
            serie = sorted(float(s[columna]) for s in subcuencas if s.get(columna))
            if serie:
                valores[destino] = formatear(serie[len(serie) // 2], 1)

    caudales = tablas.get("qmax", [])
    cierre = next((f for f in caudales if str(f.get("tipo")) == "Sink"), None)
    if cierre and cierre.get("q_T100_m3s"):
        valores["q_100"] = formatear(cierre["q_T100_m3s"], 1)
        try:
            area = float(sum(float(s.get("area_km2") or 0) for s in subcuencas))
            if area > 0:
                valores["caudal_especifico_100"] = formatear(
                    float(cierre["q_T100_m3s"]) / area, 2)
        except (TypeError, ValueError):
            pass
    valores["n_tramos"] = sum(1 for f in caudales if str(f.get("tipo")) == "Reach")

    estaciones = tablas.get("estaciones", [])
    if estaciones:
        valores["n_estaciones"] = len(estaciones)

    factores = reportes.get("M12b", {}).get("factores", {})
    valores["arf"] = numero(factores, "arf", decimales=3)
    valores["factor_cc"] = numero(factores, "cambio_climatico", decimales=3)
    valores["duracion_tormenta_h"] = numero(factores, "duracion_h", decimales=0)
    valores["intervalo_min"] = numero(factores, "intervalo_min", decimales=0)
    valores["factor_escala"] = numero(
        factores, "factor_escala_temporal", decimales=3)

    valores["criterio_rezago"] = str(
        configuracion.obtener("tiempo_rezago.criterio", "") or "")
    valores["fuente_escala"] = str(configuracion.obtener(
        "tormenta.coeficiente_desagregacion.fuente", "") or "")
    # La distribucion la ELIGE el M07 por estacion segun su criterio de ajuste;
    # la configuracion solo permite forzar una. Se informa la que gobierna en
    # mas estaciones, y cuantas son, en lugar de dar por hecho que es una sola.
    forzada = str(configuracion.obtener("frecuencia.distribucion_adoptada", "")
                  or "").strip()
    adoptadas = reportes.get("M07", {}).get("adoptadas", {})
    if forzada:
        valores["distribucion_adoptada"] = forzada
    elif isinstance(adoptadas, dict) and adoptadas:
        conteo: dict[str, int] = {}
        for ficha in adoptadas.values():
            nombre = str((ficha or {}).get("distribucion", "")).strip()
            if nombre:
                conteo[nombre] = conteo.get(nombre, 0) + 1
        if conteo:
            mayor = max(conteo, key=lambda n: conteo[n])
            valores["distribucion_adoptada"] = (
                f"{mayor}, que gobierna en {conteo[mayor]} de "
                f"{sum(conteo.values())} estaciones")
    periodos = configuracion.obtener("frecuencia.periodos_retorno", []) or []
    if periodos:
        textos = [formatear(p, 2) for p in periodos]
        valores["periodos_retorno"] = ", ".join(textos[:-1]) + " y " + textos[-1]

    return {c: v for c, v in valores.items() if v not in (None, "")}


# =============================================================================
# Escritura del documento
# =============================================================================
def _campo(parrafo, instruccion: str) -> None:
    """
    Inserta un campo de Word, que Word evalúa al abrir o al actualizar.

    Se escribe en tres piezas (begin, instrucción, end) porque es como el
    formato lo define. El resultado no se precalcula: lo pone Word, y por eso
    la numeración sobrevive a que alguien inserte una tabla a mano.
    """
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    inicio = OxmlElement("w:fldChar")
    inicio.set(qn("w:fldCharType"), "begin")
    texto = OxmlElement("w:instrText")
    texto.set(qn("xml:space"), "preserve")
    texto.text = instruccion
    fin = OxmlElement("w:fldChar")
    fin.set(qn("w:fldCharType"), "end")
    corrida = parrafo.add_run()._r
    corrida.append(inicio)
    corrida.append(texto)
    corrida.append(fin)


def escribir_leyenda(documento, tipo: str, titulo: str, fuente: str,
                     separador: str = "-") -> None:
    """
    Leyenda numerada por capítulo, con su línea de procedencia debajo.

    LA NUMERACIÓN LA LLEVA WORD. 'STYLEREF 1 \\s' devuelve el número del
    capítulo en curso y 'SEQ <tipo> \\* ARABIC \\s 1' el consecutivo dentro de
    él, que se reinicia en cada capítulo. Así, insertar una tabla a mano
    renumera las siguientes; con el número escrito como texto, la numeración se
    descuadra sin que nada lo señale.

    El separador se añade porque el informe de referencia concatena capítulo y
    consecutivo, y 'Tabla 621' se lee como el número seiscientos veintiuno.
    """
    parrafo = documento.add_paragraph(style=ESTILO_LEYENDA)
    parrafo.add_run(f"{tipo} ")
    _campo(parrafo, r" STYLEREF 1 \s ")
    parrafo.add_run(separador)
    _campo(parrafo, rf" SEQ {tipo} \* ARABIC \s 1 ")
    parrafo.add_run(f". {titulo}")
    documento.add_paragraph(f"Fuente: {fuente}", style=ESTILO_FUENTE)


def escribir_tabla(documento, datos: dict[str, Any]) -> None:
    """Inserta la tabla con el estilo de cuadrícula de la plantilla."""
    tabla = documento.add_table(
        rows=1, cols=len(datos["encabezados"]), style=ESTILO_TABLA)
    for celda, encabezado in zip(tabla.rows[0].cells, datos["encabezados"]):
        celda.text = ""
        corrida = celda.paragraphs[0].add_run(encabezado)
        corrida.bold = True
    for fila in datos["filas"]:
        celdas = tabla.add_row().cells
        for celda, valor in zip(celdas, fila):
            celda.text = str(valor)


def escribir_tabla_de_contenido(documento) -> None:
    """
    Deja el campo de tabla de contenido, que Word rellena al actualizar.

    NO SE PUEDE PRECALCULAR: los números de página dependen de la paginación,
    que solo conoce el programa que compone el documento. Word pregunta al abrir
    si se actualizan los campos, y ahí se construye.
    """
    documento.add_paragraph("TABLA DE CONTENIDO", style="TtuloTDC")
    parrafo = documento.add_paragraph()
    _campo(parrafo, r' TOC \o "1-3" \h \z \u ')
    documento.add_paragraph(
        "Situar el cursor sobre la tabla de contenido y pulsar F9 para "
        "actualizarla, o responder que si al abrir el documento.",
        style=ESTILO_FUENTE)


def escribir_pendiente(documento, texto: str) -> None:
    """
    Marca visible de lo que espera al consultor.

    Es parte del producto y no un descuido: hay apartados que ninguna cadena
    puede redactar. Se marca en lugar de rellenarse con texto de relleno, que
    es lo que produce informes que nadie lee.
    """
    parrafo = documento.add_paragraph(style=ESTILO_TEXTO)
    corrida = parrafo.add_run(f"[{MARCA_PENDIENTE}] {texto.strip()}")
    corrida.bold = True
    corrida.italic = True


# =============================================================================
# Ejecución
# =============================================================================
def ejecutar(
    raiz: Path | None = None,
    ruta_config: Path | None = None,
    ruta_json: Path | None = None,
    regenerar_plantilla: bool = False,
    consola: bool = True,
) -> tuple[int, list[Hallazgo]]:
    """Compone el informe a partir de los productos de la cadena."""
    inicio_reloj = time.perf_counter()

    base = Path(raiz).resolve() if raiz is not None else rutas.raiz_proyecto()
    configuracion: Config = cargar(ruta=ruta_config, raiz=base)
    logger = registro.configurar(
        MODULO, nivel=configuracion.obtener("ejecucion.nivel_log", "INFO"),
        raiz=base, consola=consola,
    )
    resultado = ResultadoM15()
    ruta_json = ruta_json or (
        rutas.directorio("procesado", base, crear=True) / "M15_informe.json")

    registro.registrar_cabecera(
        logger, MODULO, DESCRIPCION, config=configuracion,
        insumos={"plantilla": configuracion.obtener("informe.plantilla")},
        parametros=configuracion.parametros("informe"))

    try:
        import docx_plantilla
    except ImportError as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "informe.sin_libreria", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    ruta_plantilla = rutas.resolver(
        configuracion.obtener("informe.plantilla"), base)
    if regenerar_plantilla or not ruta_plantilla.is_file():
        with registro.bloque(logger, "Plantilla"):
            if not _derivar_plantilla(configuracion, base, ruta_plantilla,
                                      docx_plantilla, resultado, logger):
                return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                               SALIDA_BLOQUEANTE)

    try:
        estructura = _leer_declaracion(configuracion, base, "informe.estructura",
                                       "config/informe.yaml")
        narrativa = _leer_declaracion(configuracion, base, "informe.texto",
                                      "templates/informe/texto.yaml")
    except (ErrorRutas, ErrorFormato) as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "informe.declaracion", str(error)))
        return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                       SALIDA_BLOQUEANTE)

    with registro.bloque(logger, "Insumos de la cadena"):
        reportes, tablas = _reunir_insumos(base, configuracion, logger)
        resultado.valores = recolectar_valores(reportes, tablas, configuracion)
        logger.info("%d valor(es) disponibles para el texto",
                    len(resultado.valores))

    with registro.bloque(logger, "Composicion"):
        try:
            documento = docx_plantilla.abrir(ruta_plantilla)
        except docx_plantilla.ErrorPlantilla as error:
            resultado.hallazgos.append(Hallazgo(
                BLOQUEANTE, "informe.plantilla", str(error)))
            return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                           SALIDA_BLOQUEANTE)

        escribir_tabla_de_contenido(documento)
        contexto = {
            "base": base, "configuracion": configuracion,
            "narrativa": narrativa.get("texto", narrativa),
            "delimitador": str(configuracion.obtener(
                "insumos_usuario.delimitador_csv")),
            "graficos": rutas.resolver(
                configuracion.obtener("graficos.directorio"), base),
            "fuente": str(estructura.get("fuente_propia", "Elaboracion propia")),
            "separador": str(configuracion.obtener(
                "informe.separador_numeracion", "-")),
        }
        for nodo in estructura.get("capitulos", []):
            _escribir_nodo(documento, nodo, contexto, resultado, logger)

        destino = rutas.directorio("informe", base, crear=True) / str(
            configuracion.obtener("informe.archivo", "informe_hidrologico.docx"))
        documento.save(str(destino))
        resultado.documento = str(destino)
        resultado.productos.append(rutas.relativa(destino, base))
        logger.info("Informe escrito: %s", rutas.relativa(destino, base))

    _hallazgos_finales(resultado)
    resultado.productos = [str(p) for p in resultado.productos]
    return _cerrar(logger, resultado, base, ruta_json, inicio_reloj,
                   SALIDA_CORRECTA)


def _derivar_plantilla(configuracion, base, destino, docx_plantilla, resultado,
                       logger) -> bool:
    """Genera la plantilla a partir del informe de referencia del consultor."""
    origen = rutas.resolver(
        configuracion.obtener("informe.informe_de_referencia"), base)
    try:
        detalle = docx_plantilla.extraer_plantilla(origen, destino)
    except docx_plantilla.ErrorPlantilla as error:
        resultado.hallazgos.append(Hallazgo(
            BLOQUEANTE, "informe.plantilla_no_derivable", str(error)))
        return False
    resultado.productos.append(rutas.relativa(destino, base))
    descartadas = sum(len(v) for v in detalle["relaciones_descartadas"].values())
    logger.info("Plantilla derivada de %s: %s KB", origen.name, detalle["kb"])
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "informe.plantilla_derivada",
        f"la plantilla se derivo de {origen.name}: {detalle['kb']} KB con los "
        f"estilos, la numeracion, los encabezados y la configuracion de pagina, "
        f"sin su contenido. Se conservaron "
        f"{len(detalle['medios_conservados'])} imagen(es) de membrete y se "
        f"descartaron {descartadas} relacion(es) que no resolvian, incluida una "
        "con destino 'NULL' que impedia abrir el archivo original con "
        "python-docx. El formato de casa sale del informe del consultor, que es "
        "de su propiedad; su redaccion se conserva en templates/informe.",
    ))
    return True


def _leer_declaracion(configuracion, base, clave, por_defecto) -> dict:
    """Lee un YAML de declaración, buscándolo primero en el estudio."""
    import yaml

    ruta = rutas.resolver(configuracion.obtener(clave, por_defecto), base)
    if not ruta.is_file():
        raise ErrorRutas(f"no se encuentra {ruta}, declarado en {clave}.")
    try:
        datos = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ErrorFormato(f"{ruta.name}: YAML invalido ({error}).") from error
    if not isinstance(datos, dict):
        raise ErrorFormato(f"{ruta.name} no contiene un mapa.")
    return datos


def _reunir_insumos(base, configuracion, logger):
    """Carga los reportes JSON y las tablas que el texto y las tablas usan."""
    reportes: dict[str, dict] = {}
    procesado = rutas.directorio("procesado", base)
    for archivo in sorted(procesado.glob("M*.json")):
        modulo = archivo.name.split("_")[0]
        try:
            reportes[modulo] = json.loads(archivo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    logger.info("%d reporte(s) de modulo leidos", len(reportes))

    delimitador = str(configuracion.obtener("insumos_usuario.delimitador_csv"))

    def leer(ruta_relativa):
        ruta = base / ruta_relativa
        if not ruta.is_file():
            return []
        with ruta.open(encoding="utf-8-sig", newline="") as manejador:
            return list(csv.DictReader(manejador, delimiter=delimitador))

    tablas = {
        "parametros": leer("data/02_procesado/morfometria/parametros.csv"),
        "subcuencas": leer("data/02_procesado/morfometria/subcuencas.csv"),
        "qmax": leer("data/02_procesado/hidrologia/qmax_por_periodo.csv"),
        "estaciones": leer("data/02_procesado/estaciones/inventario_estaciones.csv"),
    }
    return reportes, tablas


def _escribir_nodo(documento, nodo, contexto, resultado, logger) -> None:
    """Escribe un capítulo y, recursivamente, los que cuelgan de él."""
    nivel = int(nodo.get("nivel", 1))
    documento.add_paragraph(str(nodo.get("titulo", "")).strip(),
                            style=ESTILO_TITULO.get(nivel, "Ttulo4"))
    resultado.capitulos += 1

    for parrafo in contexto["narrativa"].get(nodo.get("texto", ""), []) or []:
        texto, faltantes = sustituir(" ".join(str(parrafo).split()),
                                     resultado.valores)
        documento.add_paragraph(texto, style=ESTILO_TEXTO)
        resultado.parrafos += 1
        for clave in faltantes:
            if clave not in resultado.sin_valor:
                resultado.sin_valor.append(clave)

    for declarada in nodo.get("tablas", []) or []:
        _escribir_tabla_declarada(documento, declarada, contexto, resultado,
                                  logger)
    for declarada in nodo.get("figuras", []) or []:
        _escribir_figura_declarada(documento, declarada, contexto, resultado,
                                   logger)

    if nodo.get("pendiente"):
        escribir_pendiente(documento, str(nodo["pendiente"]))
        resultado.pendientes.append(str(nodo.get("titulo", "")))

    for hijo in nodo.get("hijos", []) or []:
        _escribir_nodo(documento, hijo, contexto, resultado, logger)


def _escribir_tabla_declarada(documento, declarada, contexto, resultado,
                              logger) -> None:
    """Inserta una tabla declarada, o anota su ausencia sin detenerse."""
    ruta = contexto["base"] / str(declarada.get("archivo", ""))
    try:
        datos = leer_tabla(
            ruta, contexto["delimitador"],
            {str(c): str(e) for c, e in (declarada.get("columnas") or {}).items()},
            orden=str(declarada.get("orden", "")),
            filas_max=int(declarada.get("filas_max", 0) or 0),
            filtro=declarada.get("filtro"))
    except (ErrorRutas, ErrorFormato) as error:
        resultado.ausentes.append(f"tabla {declarada.get('titulo')}: {error}")
        return

    escribir_leyenda(documento, "Tabla", str(declarada.get("titulo", "")),
                     str(declarada.get("fuente", contexto["fuente"])),
                     contexto["separador"])
    escribir_tabla(documento, datos)
    if datos["omitidas"]:
        documento.add_paragraph(
            f"Se presentan {len(datos['filas'])} de {datos['total']} registros. "
            f"Los {datos['omitidas']} restantes estan en los anexos de calculo.",
            style=ESTILO_FUENTE)
    resultado.tablas += 1


def _escribir_figura_declarada(documento, declarada, contexto, resultado,
                               logger) -> None:
    """Inserta una figura declarada, o anota su ausencia sin detenerse."""
    from docx.shared import Cm

    ruta = contexto["graficos"] / str(declarada.get("archivo", ""))
    if not ruta.is_file():
        resultado.ausentes.append(
            f"figura {declarada.get('archivo')}: no se encuentra en "
            f"{contexto['graficos'].name}")
        return
    ancho = float(declarada.get("ancho_cm", 15.0))
    documento.add_picture(str(ruta), width=Cm(ancho))
    documento.paragraphs[-1].alignment = 1  # centrada
    escribir_leyenda(documento, "Ilustración", str(declarada.get("titulo", "")),
                     str(declarada.get("fuente", contexto["fuente"])),
                     contexto["separador"])
    resultado.figuras += 1


def _hallazgos_finales(resultado) -> None:
    """Convierte lo ocurrido durante la composición en hallazgos del reporte."""
    resultado.hallazgos.append(Hallazgo(
        INFORMATIVO, "informe.compuesto",
        f"{resultado.capitulos} apartado(s), {resultado.parrafos} parrafo(s), "
        f"{resultado.tablas} tabla(s) y {resultado.figuras} figura(s). La "
        "numeracion de leyendas va con campos de Word (STYLEREF y SEQ): al "
        "abrir el documento hay que responder que si a la actualizacion de "
        "campos, o pulsar F9, para que se numeren y se arme la tabla de "
        "contenido.",
    ))
    if resultado.pendientes:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "informe.pendientes",
            f"{len(resultado.pendientes)} apartado(s) quedan con marca "
            f"'[{MARCA_PENDIENTE}]' porque ninguna cadena los puede redactar: "
            f"{resultado.pendientes}. Son el alcance del contrato, la "
            "descripcion del sistema hidrico con conocimiento de campo y las "
            "conclusiones. El informe NO se entrega con esas marcas dentro.",
        ))
    if resultado.sin_valor:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "informe.valores_sin_resolver",
            f"{len(resultado.sin_valor)} valor(es) del texto no los resolvio "
            f"la cadena y quedaron como '{MARCA_SIN_VALOR}': "
            f"{sorted(resultado.sin_valor)}. Se marcan en lugar de rellenarse "
            "con una cifra plausible: un hueco senalado se ve, un dato "
            "inventado no. Revisar si falta ejecutar el modulo que los produce.",
        ))
    if resultado.ausentes:
        resultado.hallazgos.append(Hallazgo(
            ADVERTENCIA, "informe.insumos_ausentes",
            f"{len(resultado.ausentes)} tabla(s) o figura(s) declaradas no se "
            f"pudieron insertar: {resultado.ausentes[:6]}. El informe se "
            "compone sin ellas y el apartado queda sin su respaldo grafico.",
        ))


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
            emitir("  %-44s %s", hallazgo.clave, hallazgo.mensaje)

    conteo = esquema.resumen_por_severidad(hallazgos)
    logger.info(registro.SEPARADOR)
    logger.info("RESUMEN: %d bloqueante(s), %d advertencia(s), %d informativo(s)",
                conteo[BLOQUEANTE], conteo[ADVERTENCIA], conteo[INFORMATIVO])

    reporte = {
        "modulo": MODULO,
        "documento": resultado.documento,
        "capitulos": resultado.capitulos,
        "parrafos": resultado.parrafos,
        "tablas": resultado.tablas,
        "figuras": resultado.figuras,
        "pendientes": resultado.pendientes,
        "valores_sin_resolver": sorted(resultado.sin_valor),
        "insumos_ausentes": resultado.ausentes,
        "valores": resultado.valores,
        "productos": resultado.productos,
        "resumen": conteo,
        "codigo_salida": codigo,
        "conforme": codigo == SALIDA_CORRECTA,
        "hallazgos": [h.como_dict() for h in hallazgos],
    }
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    ruta_json.write_text(json.dumps(reporte, ensure_ascii=False, indent=1),
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


def _analizar_argumentos(argv=None):
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    analizador.add_argument("--json", type=Path, default=None)
    analizador.add_argument(
        "--plantilla", action="store_true",
        help="regenera la plantilla a partir del informe de referencia")
    return analizador.parse_args(argv)


def main(argv=None) -> int:
    """Punto de entrada. Devuelve el codigo de salida del proceso."""
    argumentos = _analizar_argumentos(argv)
    try:
        codigo, _ = ejecutar(
            raiz=argumentos.raiz, ruta_config=argumentos.config,
            ruta_json=argumentos.json,
            regenerar_plantilla=argumentos.plantilla)
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato,
            ErrorHidrologia) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M15 - Generador de la plantilla con marcadores
==============================================
Entorno: venv del proyecto.

PREPARA LA PLANTILLA DE PARTIDA, una sola vez. Toma el informe base del
consultor, que es de su autoría, conserva íntegro su formato y su redacción, y
sustituye lo que era contenido de otro proyecto por MARCADORES que el M15
rellenará con lo de este.

POR QUÉ MARCADORES Y NO UN ÁRBOL EN YAML. La versión anterior reconstruía el
documento desde cero a partir de una declaración, y eso fallaba en tres sitios a
la vez: dos apartados con el mismo título recibían la misma clave y por tanto el
mismo contenido; una figura mal nombrada dejaba su apartado mudo sin que se
viera; y todo lo que el informe tenía de composición se perdía, porque no se
copiaba sino que se rehacía.

Con marcadores, el sitio de cada cosa lo decide el consultor moviéndolos en
Word, no una regla de emparejamiento. Y el formato se conserva por construcción,
porque el documento que se edita ES el suyo.

QUÉ SE QUITA Y QUÉ SE CONSERVA. Se quitan las imágenes, las tablas y sus
leyendas, porque pertenecen al proyecto anterior. Se conserva todo lo demás:
estilos, numeración de títulos, encabezados, pies, saltos de sección y la
redacción completa.

Marcadores que el M15 entiende:

    {{figura: nombre | leyenda}}      inserta la figura con su leyenda numerada
    {{tabla: ruta | leyenda}}         inserta la tabla desde su CSV
    {{valor: clave}}                  sustituye por el valor calculado
    {{hallazgos: prefijo}}            inserta lo que los módulos midieron
    {{decisiones}}                    reúne lo que reclama criterio
    {{pendiente: texto}}              marca visible de lo que falta escribir

Productos:
    templates/informe_marcadores.docx
    templates/informe/inventario.md

Uso:
    python src/M15_plantilla.py

Códigos de salida:
    0  correcto
    3  no se pudo leer la configuración o el informe base
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Sequence

_DIRECTORIO_SRC = Path(__file__).resolve().parent
if str(_DIRECTORIO_SRC) not in sys.path:
    sys.path.insert(0, str(_DIRECTORIO_SRC))

from comun import rutas  # noqa: E402
from comun.config import cargar  # noqa: E402
from comun.errores import ErrorConfiguracion, ErrorFormato, ErrorRutas  # noqa: E402

MODULO = "M15p"
DESCRIPCION = "Generador de la plantilla con marcadores"

SALIDA_CORRECTA = 0
SALIDA_ERROR = 3

# Parrafos del informe base que pertenecen al proyecto anterior: leyendas de
# figura y tabla, y su linea de procedencia.
LEYENDA = re.compile(r"^\s*(Tabla|Ilustraci[óo]n|Gr[áa]fico|Fotograf[íi]a)\s+\d",
                     re.I)
PROCEDENCIA = re.compile(r"^\s*Fuente\s*:", re.I)

# Que marcadores sugerir bajo cada titulo. La clave es un fragmento del titulo
# en minusculas y sin tildes, porque los titulos del informe base no son
# identificadores y compararlos completos es fragil.
SUGERENCIAS: dict[str, list[str]] = {
    "hidroclimatolog": ["{{tabla: estaciones/inventario_estaciones | "
                        "Identificación de estaciones IDEAM}}",
                        "{{hallazgos: estaciones}}"],
    "consistencia": ["{{hallazgos: consistencia}}"],
    "dudosos": ["{{hallazgos: anomalos}}"],
    "doble": ["{{figura: M05_dobles_masas | Análisis de dobles masas}}",
              "{{hallazgos: dobles_masas}}"],
    "faltantes": ["{{hallazgos: complemento}}"],
    "enso": ["{{figura: M05b_ciclo_por_fase | Ciclo anual por fase ENSO}}",
             "{{hallazgos: enso}}"],
    "delimitaci": ["{{hallazgos: cuenca}}"],
    "rea de drenaje": ["{{tabla: morfometria/parametros | "
                       "Parámetros morfométricos de la cuenca}}",
                       "{{valor: area_km2}}"],
    "desnivel": ["{{figura: M10_curva_hipsometrica | Curva hipsométrica}}",
                 "{{figura: M10_distribucion_altimetrica | "
                 "Distribución altimétrica}}"],
    "suelo hidrol": ["{{hallazgos: suelos}}"],
    "mero de curva": ["{{figura: M10_mapa_cn | Número de curva por subcuenca}}",
                      "{{valor: cn_medio}}", "{{hallazgos: numero_curva}}"],
    "concentraci": ["{{figura: M10_mapa_rezago | Tiempo de rezago por subcuenca}}",
                    "{{valor: tc_mediana_min}}", "{{valor: tlag_mediana_min}}",
                    "{{hallazgos: tiempo_concentracion}}"],
    "espacial de la precipitaci": ["{{hallazgos: isoyetas}}",
                                   "{{hallazgos: zonificacion}}"],
    "eventos m": ["{{valor: distribucion_adoptada}}",
                  "{{hallazgos: frecuencia}}"],
    "hietograma": ["{{figura: M12b_hietograma_T500 | "
                   "Hietograma de diseño, T = 500 años}}",
                   "{{valor: factor_escala}}", "{{hallazgos: hietograma}}"],
    "cambio clim": ["{{figura: M12a_cambio_climatico | "
                    "Factores de cambio climático}}",
                    "{{valor: factor_cc}}", "{{hallazgos: cambio_climatico}}"],
    "idf": ["{{figura: M12a_idf_comparacion | "
            "Comparación de metodologías de curvas IDF}}",
            "{{hallazgos: idf}}"],
    "reducci": ["{{figura: M11c_curvas_arf | Factor de reducción por área}}",
                "{{valor: arf}}", "{{hallazgos: arf}}"],
    "nsito de creciente": ["{{hallazgos: transito}}"],
    "construcci": ["{{hallazgos: modelo}}", "{{hallazgos: escenarios}}"],
    "rendimiento": ["{{figura: M18_mapa_rendimiento | "
                    "Rendimiento hídrico por subcuenca}}",
                    "{{figura: M18_contraste_ena | "
                    "Rendimiento contra el Estudio Nacional del Agua}}",
                    "{{hallazgos: balance.contraste_ena}}"],
    "temperatura": ["{{figura: M18a_gradiente_altitudinal | "
                    "Temperatura media contra elevación}}",
                    "{{figura: M18a_isotermas | Isotermas sobre la cuenca}}",
                    "{{figura: M18a_mapa_temperatura | "
                    "Temperatura media por subcuenca}}",
                    "{{hallazgos: temperatura}}"],
    "potencial": ["{{figura: M18_serie_etp | "
                  "Evapotranspiración potencial mensual}}",
                  "{{figura: M18a_etp_comparacion | "
                  "Evapotranspiración potencial, las dos vías}}",
                  "{{figura: M18_mapa_etp | "
                  "Evapotranspiración potencial por subcuenca}}",
                  "{{hallazgos: etp}}"],
    "real": ["{{figura: M18_serie_etr | Evapotranspiración real mensual}}",
             "{{figura: M18_etr_comparacion | Las tres formulaciones de ETR}}",
             "{{figura: M18_diagrama_budyko | Diagrama de Budyko}}",
             "{{figura: M18_mapa_etr | Evapotranspiración real por subcuenca}}",
             "{{hallazgos: etr}}"],
    "infiltraci": ["{{figura: M18b_reparto_mensual | "
                   "Reparto mensual de la precipitación}}",
                   "{{figura: M18b_mapa_coeficiente | "
                   "Coeficiente de infiltración por subcuenca}}",
                   "{{hallazgos: infiltracion}}"],
    "duraci": ["{{tabla: regimen/percentiles | "
               "Percentiles de la curva de duración}}",
               "{{figura: M19_curva_de_duracion | "
               "Curva de duración de caudales medios mensuales}}",
               "{{figura: M18_caudal_mensual | Caudal medio mensual}}",
               "{{hallazgos: regimen.curva}}"],
    "retenci": ["{{figura: M19_irh | "
                "Índice de retención y regulación hídrica}}",
                "{{hallazgos: regimen.irh}}"],
    "ambiental": ["{{figura: M19_caudal_ambiental | "
                  "Caudal ambiental sobre la curva de duración}}",
                  "{{hallazgos: regimen.caudal_ambiental}}"],
    "introducci": ["{{pendiente: Describir el alcance del contrato, la entidad "
                   "contratante y el objeto del estudio.}}"],
    "sistema h": ["{{pendiente: Describir el sistema hídrico con conocimiento "
                  "de campo: corrientes receptoras, obras existentes, usos del "
                  "agua y lo observado en visita.}}"],
    "conclusion": ["{{pendiente: Redactar conclusiones y recomendaciones de "
                   "diseño.}}"],
}


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar títulos de forma tolerante."""
    import unicodedata
    return unicodedata.normalize("NFKD", texto).encode(
        "ascii", "ignore").decode("ascii").lower()


def sugerencias_para(titulo: str) -> list[str]:
    """
    Marcadores que corresponden a un título del informe base.

    SE COMPARA POR FRAGMENTO Y NO POR IGUALDAD. Los títulos del informe no son
    identificadores: llevan tildes, mayúsculas variables y a veces el nombre de
    la corriente. Emparejarlos completos fallaría en cuanto cambiara una palabra.
    """
    limpio = normalizar(titulo)
    encontradas: list[str] = []
    for fragmento, marcadores in SUGERENCIAS.items():
        if normalizar(fragmento) in limpio:
            for marcador in marcadores:
                if marcador not in encontradas:
                    encontradas.append(marcador)
    return encontradas


def parrafo_de_texto(texto: str, estilo: str = "Sinespaciado") -> str:
    """XML de un párrafo con el estilo indicado y el texto escapado."""
    escapado = (texto.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
    return (f'<w:p><w:pPr><w:pStyle w:val="{estilo}"/></w:pPr>'
            f'<w:r><w:t xml:space="preserve">{escapado}</w:t></w:r></w:p>')


def construir(origen: Path, destino: Path) -> dict[str, Any]:
    """
    Escribe la plantilla con marcadores a partir del informe base.

    SE EDITA EL PAQUETE Y NO SE RECONSTRUYE. Copiar el .docx entero y tocar solo
    el cuerpo conserva estilos, numeración, encabezados, pies y saltos de
    sección sin tener que entenderlos: es la diferencia entre editar el
    documento del consultor y rehacerlo.

    Excepciones
    -----------
    ErrorRutas
        Si el informe base no está.
    ErrorFormato
        Si el paquete no tiene cuerpo.
    """
    origen, destino = Path(origen), Path(destino)
    if not origen.is_file():
        raise ErrorRutas(f"no se encuentra el informe base en {origen}.")

    with zipfile.ZipFile(origen) as paquete:
        documento = paquete.read("word/document.xml").decode(
            "utf-8", errors="replace")
        partes = {n: paquete.read(n) for n in paquete.namelist()}

    inicio = documento.find("<w:body>")
    fin = documento.rfind("</w:body>")
    if inicio < 0 or fin < 0:
        raise ErrorFormato(f"{origen.name} no tiene cuerpo.")
    cabecera = documento[:inicio + len("<w:body>")]
    cuerpo = documento[inicio + len("<w:body>"):fin]
    cola = documento[fin:]

    # Las tablas del informe base son del proyecto anterior: se quitan enteras.
    cuerpo, tablas = re.subn(r"<w:tbl>.*?</w:tbl>", "", cuerpo, flags=re.S)

    bloques = re.findall(r"<w:p\b.*?</w:p>|<w:sectPr\b.*?</w:sectPr>", cuerpo,
                         re.S)
    salida: list[str] = []
    quitadas = imagenes = marcadores = 0
    pendiente: list[str] = []

    for bloque in bloques:
        if not bloque.startswith("<w:p"):
            salida.append(bloque)
            continue
        estilo = re.search(r'w:pStyle w:val="([^"]*)"', bloque)
        estilo = estilo.group(1) if estilo else ""
        texto = re.sub(r"<[^>]+>", "", "".join(
            re.findall(r"<w:t[^>]*>(.*?)</w:t>", bloque, re.S))).strip()

        # Las figuras del proyecto anterior, y sus leyendas y procedencias.
        if "<w:drawing>" in bloque or "<w:pict>" in bloque:
            imagenes += 1
            continue
        if estilo in ("Descripcin", "Fuente") or LEYENDA.match(texto) \
                or PROCEDENCIA.match(texto):
            quitadas += 1
            continue
        # La tabla de contenido y la de ilustraciones las rehace Word.
        if estilo.startswith("TDC") or estilo == "Tabladeilustraciones":
            quitadas += 1
            continue

        # Al llegar a un titulo se vuelcan los marcadores del apartado anterior.
        if estilo.startswith(("Ttulo", "Titulo")) and pendiente:
            salida.extend(parrafo_de_texto(m) for m in pendiente)
            marcadores += len(pendiente)
            pendiente = []

        salida.append(bloque)

        if estilo.startswith(("Ttulo", "Titulo")) and texto:
            pendiente = sugerencias_para(texto)

    if pendiente:
        salida.extend(parrafo_de_texto(m) for m in pendiente)
        marcadores += len(pendiente)

    # El apartado de decisiones va al principio, tras el primer titulo.
    for indice, bloque in enumerate(salida):
        if re.search(r'w:pStyle w:val="(Ttulo|Titulo)1"', bloque):
            salida.insert(indice, parrafo_de_texto("{{decisiones}}"))
            marcadores += 1
            break

    nuevo = cabecera + "".join(salida) + cola
    # LAS RELACIONES TIENEN QUE SEGUIR A LAS PARTES. Quitar las imagenes y dejar
    # sus relaciones produce un paquete que Word abre y python-docx no: se
    # detiene buscando una parte que ya no esta.
    import docx_plantilla

    finales = {n for n in partes
               if not n.startswith("word/media/") or _es_de_membrete(n, partes)}
    destino.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as salida_zip:
        for nombre, contenido in partes.items():
            if nombre not in finales:
                continue
            if nombre == "word/document.xml":
                salida_zip.writestr(nombre, nuevo)
                continue
            if nombre.endswith(".rels"):
                parte = nombre.replace("_rels/", "").removesuffix(".rels")
                texto, _ = docx_plantilla._relaciones_utiles(
                    contenido.decode("utf-8", errors="replace"), parte,
                    finales, quitar_imagenes=False)
                salida_zip.writestr(nombre, texto)
                continue
            salida_zip.writestr(nombre, contenido)

    return {
        "destino": str(destino),
        "kb": round(destino.stat().st_size / 1024, 1),
        "marcadores": marcadores,
        "tablas_quitadas": tablas,
        "imagenes_quitadas": imagenes,
        "leyendas_quitadas": quitadas,
    }


def _es_de_membrete(nombre: str, partes: dict[str, bytes]) -> bool:
    """Cierto si la imagen la usa un encabezado o un pie."""
    hoja = nombre.split("/")[-1]
    for parte, contenido in partes.items():
        if re.match(r"word/_rels/(header|footer)\d+\.xml\.rels$", parte) \
                and hoja.encode() in contenido:
            return True
    return False


def inventario(base: Path, configuracion) -> str:
    """
    Catálogo de todo lo que la cadena deja disponible para el informe.

    ES LA REFERENCIA QUE SE TIENE AL LADO al colocar marcadores. Sin ella hay
    que recordar cómo se llama cada figura, y un nombre mal escrito deja el
    apartado mudo.
    """
    lineas = [
        "# Inventario para la construcción del informe",
        "",
        "Generado por `M15_plantilla.py`. Lista lo que la cadena deja "
        "disponible en este estudio.",
        "",
        "## Marcadores",
        "",
        "| marcador | qué hace |",
        "|---|---|",
        "| `{{figura: nombre \\| leyenda}}` | inserta la figura, centrada, con "
        "su leyenda numerada por capítulo |",
        "| `{{tabla: ruta \\| leyenda}}` | inserta la tabla desde su CSV, con "
        "las columnas que se declaren |",
        "| `{{valor: clave}}` | sustituye por el valor calculado, en la frase |",
        "| `{{hallazgos: prefijo}}` | inserta lo que los módulos midieron sobre "
        "ese tema, con su severidad |",
        "| `{{decisiones}}` | reúne lo que reclama criterio del consultor |",
        "| `{{pendiente: texto}}` | marca visible de lo que falta escribir |",
        "",
        "Los marcadores se escriben en un párrafo propio, salvo `{{valor:}}`, "
        "que va dentro de la frase.",
        "",
    ]

    graficos = rutas.resolver(configuracion.obtener("graficos.directorio"), base)
    figuras = sorted(p.stem for p in graficos.glob("*.png")) if graficos.is_dir() else []
    lineas += ["## Figuras disponibles", "",
               f"{len(figuras)} figuras en `{rutas.relativa(graficos, base)}`.",
               ""]
    modulo = ""
    for nombre in figuras:
        actual = nombre.split("_")[0]
        if actual != modulo:
            modulo = actual
            lineas += ["", f"### {modulo}", ""]
        lineas.append(f"- `{{{{figura: {nombre} | }}}}`")
    lineas.append("")

    procesado = rutas.directorio("procesado", base)
    tablas = sorted(p.relative_to(procesado).as_posix()
                    for p in procesado.rglob("*.csv"))
    lineas += ["## Tablas disponibles", "",
               f"{len(tablas)} tablas bajo `data/02_procesado`. La ruta del "
               "marcador es la relativa a ese directorio, sin extensión.", ""]
    for ruta in tablas:
        lineas.append(f"- `{{{{tabla: {ruta[:-4]} | }}}}`")
    lineas.append("")

    reportes: dict[str, dict] = {}
    for archivo in sorted(procesado.glob("M*.json")):
        try:
            reportes[archivo.name.split("_")[0]] = json.loads(
                archivo.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    familias: dict[str, int] = {}
    for reporte in reportes.values():
        for hallazgo in reporte.get("hallazgos", []) or []:
            familia = str(hallazgo.get("clave", "")).split(".")[0]
            if familia:
                familias[familia] = familias.get(familia, 0) + 1
    lineas += ["## Familias de hallazgos", "",
               f"{sum(familias.values())} hallazgos en {len(reportes)} módulos, "
               f"agrupados en {len(familias)} familias. El prefijo del marcador "
               "puede ser la familia entera o una clave concreta.", "",
               "| familia | hallazgos |", "|---|---|"]
    for familia, cuantos in sorted(familias.items(), key=lambda x: -x[1]):
        lineas.append(f"| `{{{{hallazgos: {familia}}}}}` | {cuantos} |")
    lineas.append("")

    lineas += ["## Cosas que tener en cuenta", "",
               "- Un nombre de figura mal escrito deja el apartado mudo. El "
               "M15 lo reporta como insumo ausente, pero conviene revisarlo en "
               "el inventario antes.",
               "- Los marcadores `{{hallazgos:}}` pueden repetirse en varios "
               "apartados: cada uno inserta lo suyo y no se duplica dentro del "
               "mismo apartado.",
               "- El formato de la plantilla manda. Lo que el M15 inserta hereda "
               "los estilos del documento, de modo que cambiar el aspecto es "
               "cambiarlo en Word una vez.",
               "- Las leyendas se numeran solas con campos de Word. Al abrir el "
               "documento hay que responder que sí a la actualización.",
               "- La tabla de contenido y la de ilustraciones se dejan vacías a "
               "propósito: Word las rehace al actualizar campos.",
               ""]
    return "\n".join(lineas)


def main(argv=None) -> int:
    """Punto de entrada."""
    analizador = argparse.ArgumentParser(description=DESCRIPCION)
    analizador.add_argument("--raiz", type=Path, default=None)
    analizador.add_argument("--config", type=Path, default=None)
    argumentos = analizador.parse_args(argv)

    try:
        base = (Path(argumentos.raiz).resolve() if argumentos.raiz
                else rutas.raiz_proyecto())
        configuracion = cargar(ruta=argumentos.config, raiz=base)
        origen = rutas.resolver(
            configuracion.obtener("informe.informe_de_referencia"), base)
        destino = rutas.resolver("templates/informe_marcadores.docx",
                                 rutas.raiz_codigo())
        detalle = construir(origen, destino)
        catalogo = rutas.resolver("templates/informe/inventario.md",
                                  rutas.raiz_codigo())
        catalogo.parent.mkdir(parents=True, exist_ok=True)
        catalogo.write_text(inventario(base, configuracion), encoding="utf-8")
    except (ErrorConfiguracion, ErrorRutas, ErrorFormato) as error:
        print(f"{MODULO}: {error}", file=sys.stderr)
        return SALIDA_ERROR

    print(f"Plantilla: {detalle['destino']} ({detalle['kb']} KB)")
    print(f"  marcadores puestos      {detalle['marcadores']}")
    print(f"  tablas quitadas         {detalle['tablas_quitadas']}")
    print(f"  imagenes quitadas       {detalle['imagenes_quitadas']}")
    print(f"  leyendas quitadas       {detalle['leyendas_quitadas']}")
    print(f"Inventario: {catalogo}")
    return SALIDA_CORRECTA


if __name__ == "__main__":
    raise SystemExit(main())

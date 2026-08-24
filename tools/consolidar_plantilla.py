#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Aplica a la plantilla base las correcciones que hasta ahora se hacían al vuelo.

POR QUE EXISTE. Mientras la plantilla fue del consultor y no se podía tocar, sus
defectos se rodeaban desde config/informe_correcciones.yaml, que el M15 aplicaba
en cada corrida. Con el repositorio como fuente de verdad ese rodeo sobra: el
defecto se arregla en su sitio y la entrada declarada se retira.

NO ES PARTE DE LA CADENA. Se ejecuta a mano, una vez, y su efecto queda en el
historial de git, que es donde se puede revisar y deshacer. Volver a ejecutarlo
no hace daño: cada cambio se busca por su texto y si ya está aplicado no
encuentra nada.

    python tools/consolidar_plantilla.py            comprueba sin escribir
    python tools/consolidar_plantilla.py --escribir aplica y guarda
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parents[1]
if str(_RAIZ / "src") not in sys.path:
    sys.path.insert(0, str(_RAIZ / "src"))

import docx_plantilla as plantilla_docx  # noqa: E402
import M15_informe as m15  # noqa: E402

PLANTILLA = _RAIZ / "templates" / "informe_base.docx"

# Nombres de figura que la plantilla pide con el nombre que la cadena no usa.
# Se buscan en el texto de la instruccion, sin tocar el resto del parrafo.
NOMBRES = (
    ("neutro.png", "neutral.png",
     "la cadena nombra la fase neutral del ENSO 'neutral' (comun.oni), no "
     "'neutro'"),
)

# Encabezados de tabla equivocados. Se identifican por la leyenda de SU tabla,
# porque el texto 'Coeficiente de forma' aparece bien en una y mal en dos.
ENCABEZADOS = (
    ("Coeficiente de compacidad microcuencas", "Coeficiente de forma",
     "Coeficiente de compacidad",
     "la tabla de compacidad titulaba su columna 'Coeficiente de forma'"),
    ("Índice de Sinuosidad microcuencas", "Coeficiente de forma",
     "Índice de sinuosidad",
     "la tabla de sinuosidad titulaba su columna 'Coeficiente de forma'"),
    ("Longitud Teórica de Series IDEAM",
     "Longitud Ventana de Tiempo 1980-2023 (años)",
     "Longitud Ventana de Tiempo desde 1980 (años)",
     "el literal 1980-2023 quedo del informe de referencia; la ventana llega "
     "hasta el ano del estudio y el parrafo que la introduce ya dice 'desde "
     "el ano 1980 hasta la fecha'"),
)


def _fijar_parrafo(parrafo, valor: str) -> None:
    """
    Reescribe un párrafo conservando el formato de su primer run.

    NO ES _fijar_texto DEL M15, que trabaja sobre CELDAS. Aquí hace falta la
    versión de párrafo, y conservar el primer run importa más que de costumbre:
    es el que lleva el resaltado verde con que la plantilla marca sus
    instrucciones, y perderlo dejaría la instrucción invisible para el
    consultor aunque el módulo la siguiera reconociendo por su texto.
    """
    if parrafo.runs:
        parrafo.runs[0].text = valor
        for sobrante in parrafo.runs[1:]:
            sobrante.text = ""
    else:
        parrafo.add_run(valor)


def _parrafos(documento):
    """Los del cuerpo y los de las celdas, en orden de documento."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    salida = []
    for hijo in documento.element.body.iterchildren():
        if hijo.tag.endswith("}p"):
            salida.append(Paragraph(hijo, documento))
        elif hijo.tag.endswith("}tbl"):
            for fila in Table(hijo, documento).rows:
                for celda in fila.cells:
                    salida.extend(celda.paragraphs)
    return salida


def _tablas_por_leyenda(documento):
    """Cada tabla instruida, indexada por la leyenda que la precede."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    elementos = []
    for hijo in documento.element.body.iterchildren():
        if hijo.tag.endswith("}p"):
            elementos.append(("p", Paragraph(hijo, documento)))
        elif hijo.tag.endswith("}tbl"):
            elementos.append(("t", Table(hijo, documento)))

    salida = {}
    for indice, (clase, objeto) in enumerate(elementos):
        if clase != "p" or m15.clasificar(objeto.text)[0] != "tabla":
            continue
        leyenda, tabla = "", None
        for siguiente in range(indice + 1, min(len(elementos), indice + 8)):
            if elementos[siguiente][0] == "t":
                tabla = elementos[siguiente][1]
                break
            texto = elementos[siguiente][1].text.strip()
            if texto and not leyenda:
                leyenda = texto
        if tabla is not None:
            salida[m15._normalizar_leyenda(leyenda)] = tabla
    return salida



# -----------------------------------------------------------------------------
# SECCION DE TIEMPO DE CONCENTRACION
#
# La plantilla documentaba nueve formulas y la cadena calcula trece. Medido
# contra data/referencia/tc_aplicabilidad.csv:
#
#   'SCS Ranser' y 'California Culverts Practice' son LA MISMA expresion,
#   0,947*(L^3/H)^0,385 con L en km frente a 0,0195*(L^3/H)^0,385 con L en m.
#   Las variables que la plantilla declara, L en km y H en m, lo confirman.
#   Decision del consultor: queda el nombre de la fuente con el suyo como alias.
#
#   'US Corps' estaba documentada y no se calcula. Decision del consultor:
#   se retira, porque documentar una formula que no entra en la mediana deja al
#   informe prometiendo algo que sus tablas no muestran.
#
#   Faltaban cinco por documentar. SCS Lag NO es SCS Ranser: es la ecuacion de
#   retardo del NRCS, la unica que usa el numero de curva.
# -----------------------------------------------------------------------------
QUITAR_FORMULAS = ("Fórmula de US Corps",)

RENOMBRAR = (
    ("Fórmula de SCS Ranser",
     "Fórmula de California Culverts Practice (SCS-Ranser)",
     "es la misma expresion que la cadena calcula como 'california'"),
)

# (titulo, expresion, lineas del 'Donde'). Todas devuelven horas.
FORMULAS_NUEVAS = (
    ("Fórmula de Johnstone y Cross",
     "tc = 0,4623 · L^0,5 · S^-0,25",
     ("tc = Tiempo de concentración (hr).",
      "L = Longitud del cauce principal (km).",
      "S = Pendiente media del cauce principal (m/m).")),
    ("Fórmula de Clark",
     "tc = 0,335 · (A / √S)^0,593",
     ("tc = Tiempo de concentración (hr).",
      "A = Área de la cuenca (km²).",
      "S = Pendiente media del cauce principal (m/m).")),
    ("Fórmula de Pilgrim y McDermott",
     "tc = 0,76 · A^0,38",
     ("tc = Tiempo de concentración (hr).",
      "A = Área de la cuenca (km²).")),
    ("Fórmula de Valencia y Zuluaga",
     "tc = 1,7694 · A^0,325 · L^-0,096 · S^-0,290",
     ("tc = Tiempo de concentración (hr).",
      "A = Área de la cuenca (km²).",
      "L = Longitud del cauce principal (km).",
      "S = Pendiente media del cauce principal (m/m).")),
    ("Fórmula de retardo del SCS (NRCS Lag)",
     "tc = tlag / 0,6,  con  tlag = (L^0,8 · (Sr + 1)^0,7) / (1900 · Y^0,5)",
     ("tc = Tiempo de concentración (hr).",
      "tlag = Tiempo de retardo (hr).",
      "L = Longitud del cauce principal (pies).",
      "Sr = Retención potencial máxima, Sr = 1000/CN - 10 (pulgadas).",
      "CN = Número de curva de escorrentía, adimensional.",
      "Y = Pendiente media de la cuenca (%).",
      "Es la única de la matriz que necesita el número de curva, y por eso es "
      "la coherente con el método de transformación del SCS adoptado en el "
      "modelo. Está definida para cuencas menores de 800 ha.")),
)


# LA PLANTILLA USA DOS ESTILOS PARA LO MISMO. Kirpich, Temez, Giandotti,
# California y Ventura Heras van en 'List Paragraph' y las demas en
# 'List Bullet'. Mirar uno solo hacia creer que la ultima formula era Passini, y
# las nuevas quedaban intercaladas en medio de la lista.
ESTILOS_DE_FORMULA = ("List Bullet", "List Paragraph")


def _es_titulo_de_formula(parrafo) -> bool:
    """Un encabezado de formula de la seccion, en cualquiera de sus estilos."""
    return (parrafo.style.name in ESTILOS_DE_FORMULA
            and parrafo.text.strip().lower().startswith("fórmula de"))


def _clonar(modelo, texto: str):
    """Copia un parrafo con su estilo y le pone otro texto."""
    import copy

    nuevo = copy.deepcopy(modelo._element)
    from docx.text.paragraph import Paragraph

    parrafo = Paragraph(nuevo, modelo._parent)
    _fijar_parrafo(parrafo, texto)
    return nuevo


def consolidar_tiempo_concentracion(documento, escribir: bool) -> list[str]:
    """Retira, renombra y completa las formulas de tiempo de concentracion."""
    from docx.text.paragraph import Paragraph

    hechos: list[str] = []
    parrafos = [Paragraph(h, documento)
                for h in documento.element.body.iterchildren()
                if h.tag.endswith("}p")]
    titulos = [i for i, p in enumerate(parrafos) if _es_titulo_de_formula(p)]
    if not titulos:
        return ["NO SE ENCONTRO ninguna formula en la plantilla"]

    # --- renombrar ---
    for viejo, nuevo, motivo in RENOMBRAR:
        for parrafo in parrafos:
            if parrafo.text.strip() != viejo:
                continue
            if escribir:
                _fijar_parrafo(parrafo, nuevo)
            hechos.append(f"'{viejo}' -> '{nuevo}' ({motivo})")

    # --- quitar el bloque entero, del titulo hasta el titulo siguiente ---
    for objetivo in QUITAR_FORMULAS:
        for orden, indice in enumerate(titulos):
            if parrafos[indice].text.strip() != objetivo:
                continue
            fin = (titulos[orden + 1] if orden + 1 < len(titulos)
                   else indice + 1)
            cuantos = fin - indice
            if escribir:
                for parrafo in parrafos[indice:fin]:
                    parrafo._element.getparent().remove(parrafo._element)
            hechos.append(f"retirada '{objetivo}' y su bloque "
                          f"({cuantos} parrafo(s))")

    # --- anadir las que faltan, detras de la ultima ---
    if escribir:
        parrafos = [Paragraph(h, documento)
                    for h in documento.element.body.iterchildren()
                    if h.tag.endswith("}p")]
        titulos = [i for i, p in enumerate(parrafos)
                   if _es_titulo_de_formula(p)]
    # EL BLOQUE TERMINA EN LA ULTIMA LINEA DE 'Donde', no en el primer parrafo
    # con otro estilo. Buscar el siguiente no-Normal saltaba por encima de la
    # nota en rosa y de la instruccion de la Tabla 3-18, y las formulas nuevas
    # se colaban ENTRE la instruccion y su tabla: el modulo dejaba de encontrarla
    # y la reportaba como 'sin tabla detras'.
    ultimo = titulos[-1]
    siguiente = ultimo + 1
    for indice in range(ultimo + 1, len(parrafos)):
        parrafo = parrafos[indice]
        if parrafo.style.name != "Normal":
            break
        if m15.clasificar(parrafo.text)[0]:
            break
        if any(r.font.highlight_color for r in parrafo.runs if r.text.strip()):
            break
        siguiente = indice + 1
    modelo_titulo = parrafos[ultimo]
    modelo_texto = parrafos[ultimo + 1] if ultimo + 1 < len(parrafos) else None

    presentes = {parrafos[i].text.strip() for i in titulos}
    ancla = parrafos[siguiente - 1]._element
    for titulo, expresion, donde in FORMULAS_NUEVAS:
        if titulo in presentes:
            continue
        if escribir and modelo_texto is not None:
            bloque = [_clonar(modelo_titulo, titulo),
                      _clonar(modelo_texto, expresion)]
            bloque += [_clonar(modelo_texto, "Donde:\t\t" + donde[0])]
            bloque += [_clonar(modelo_texto, linea) for linea in donde[1:]]
            for elemento in bloque:
                ancla.addnext(elemento)
                ancla = elemento
        hechos.append(f"anadida '{titulo}' con su expresion y sus variables")
    return hechos

def consolidar(ruta: Path, escribir: bool) -> list[str]:
    """Aplica los cambios y devuelve lo que hizo, o lo que haría."""
    documento = plantilla_docx.abrir(ruta)
    hechos: list[str] = []

    # --- 1. Figuras que la plantilla pide mal --------------------------------
    # SE REUSA EL MISMO EMPAREJADOR QUE EL M15. La leyenda sola no identifica la
    # instruccion, porque 'Areas microcuencas' encabeza tres; el desempate por
    # el archivo que hoy nombra ya esta resuelto en planear_correcciones.
    correcciones = m15.leer_correcciones(
        _RAIZ / "config" / "informe_correcciones.yaml")
    plan, ambiguas, sin_uso = m15.planear_correcciones(documento, correcciones)
    if ambiguas:
        raise SystemExit(f"correcciones ambiguas, no se toca nada: {ambiguas}")
    for parrafo in _parrafos(documento):
        entrada = plan.get(parrafo._element)
        if entrada is None:
            continue
        pedia = m15.clasificar(parrafo.text)[1]
        nuevo = str(entrada["archivo"])
        if escribir:
            _fijar_parrafo(parrafo, parrafo.text.replace(pedia, nuevo))
        hechos.append(f"figura bajo '{entrada.get('leyenda')}': "
                      f"{pedia} -> {nuevo}")
    for clave in sin_uso:
        hechos.append(f"YA APLICADA, retirar de informe_correcciones.yaml: "
                      f"{clave}")

    # --- 2. Nombres de archivo que la cadena no usa --------------------------
    for viejo, nuevo, motivo in NOMBRES:
        for parrafo in _parrafos(documento):
            if viejo not in parrafo.text:
                continue
            if escribir:
                _fijar_parrafo(parrafo, parrafo.text.replace(viejo, nuevo))
            hechos.append(f"{viejo} -> {nuevo} ({motivo})")

    # --- 3. Encabezados de tabla equivocados ---------------------------------
    tablas = _tablas_por_leyenda(documento)
    for leyenda, viejo, nuevo, motivo in ENCABEZADOS:
        tabla = tablas.get(m15._normalizar_leyenda(leyenda))
        if tabla is None:
            hechos.append(f"NO SE ENCONTRO la tabla '{leyenda}'")
            continue
        for celda in tabla.rows[0].cells:
            if celda.text.strip() != viejo:
                continue
            if escribir:
                m15._fijar_texto(celda, nuevo)
            hechos.append(f"'{leyenda}': encabezado '{viejo}' -> '{nuevo}' "
                          f"({motivo})")

    # --- 4. Seccion de tiempo de concentracion -------------------------------
    hechos.extend(consolidar_tiempo_concentracion(documento, escribir))

    if escribir and hechos:
        documento.save(str(ruta))
    return hechos


def main() -> int:
    analizador = argparse.ArgumentParser(description=__doc__)
    analizador.add_argument("--escribir", action="store_true",
                            help="aplica los cambios; sin esto solo los lista")
    argumentos = analizador.parse_args()

    hechos = consolidar(PLANTILLA, argumentos.escribir)
    verbo = "aplicado" if argumentos.escribir else "pendiente"
    for linea in hechos:
        print(f"  [{verbo}] {linea}")
    print(f"\n{len(hechos)} cambio(s).")
    if not argumentos.escribir:
        print("Nada se escribio. Repetir con --escribir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

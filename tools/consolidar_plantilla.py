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

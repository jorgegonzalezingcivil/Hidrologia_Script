#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Dibuja en PDF el diagrama de flujo de la cadena.

SE LEE DE LA DECLARACION, NO SE DIBUJA A MANO. El orden de los modulos, su
entorno de ejecucion y las etapas en que se agrupan estan en
config/cadena.yaml. Un diagrama dibujado aparte se desfasa en cuanto se anade
un modulo, y un diagrama desfasado es peor que ninguno: se cree.

LAS ETAPAS SALEN DE LOS COMENTARIOS de la declaracion, que ya las separa con
lineas '# --- Nombre ---'. Asi el diagrama refleja la misma agrupacion que lee
quien abre el archivo, y no una invencion paralela.

Uso:
    python tools/diagrama_cadena.py
    python tools/diagrama_cadena.py --salida docs/diagrama_cadena.pdf
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path

_RAIZ_CODIGO = Path(__file__).resolve().parents[1]
if str(_RAIZ_CODIGO / "src") not in sys.path:
    sys.path.insert(0, str(_RAIZ_CODIGO / "src"))

DECLARACION = _RAIZ_CODIGO / "config" / "cadena.yaml"

# Color por entorno de ejecucion. El paso manual se distingue del resto porque
# es el unico que exige abrir un programa, y quien mira el diagrama necesita
# verlo de inmediato.
COLORES = {
    "venv": ("#dbe7f3", "#2b5c8a"),
    "qgis": ("#dcefe0", "#2f6b41"),
    "manual": ("#fbe3c9", "#a05a12"),
    "pendiente": ("#eeeeee", "#8a8a8a"),
    # NO VIABLE NO ES PENDIENTE. La declaracion distingue el modulo que a la
    # herramienta le falta del paso que no se puede hacer con los datos de
    # este estudio, y el diagrama no puede borrar esa distincion: llamar
    # 'pendiente de programar' a una limitacion del dato sugiere una
    # herramienta incompleta donde lo que hay es un estudio sin series de
    # caudal utilizables.
    "no_viable": ("#f3e6e6", "#8a4b4b"),
}


# LO QUE CORRE FUERA DE LA CADENA. No son pasos de config/cadena.yaml y por
# eso se declaran aqui: no producen el estudio, lo comprueban y lo cierran. Se
# lanzan a mano cuando el consultor decide que el informe esta listo.
HERRAMIENTAS = (
    ("verificar_informe.py", "Contrasta las cifras del texto contra los "
                             "productos de la cadena"),
    ("empaquetar_entrega.py", "Verifica el entregable y arma el comprimido "
                              "para el cliente"),
    ("consolidar_plantilla.py", "Aplica a la plantilla del informe las "
                                "correcciones declaradas"),
)


def leer_etapas(ruta: Path):
    """
    (etapa, [pasos]) en el orden de la declaracion.

    Se lee el YAML para los datos de cada paso y el texto crudo para las
    lineas de etapa, que son comentarios y por tanto no llegan al YAML.
    """
    import yaml

    with ruta.open(encoding="utf-8") as manejador:
        datos = yaml.safe_load(manejador) or {}
    por_modulo = {str(p.get("modulo")): p for p in datos.get("pasos") or []}

    etapas: list[tuple[str, list[dict]]] = []
    actual = "Cadena"
    lineas = ruta.read_text(encoding="utf-8").splitlines()
    for linea in lineas:
        encabezado = re.match(r"\s*#\s*---\s*(.+?)\s*-{3,}\s*$", linea)
        if encabezado:
            actual = encabezado.group(1).strip()
            continue
        paso = re.match(r"\s*-\s*modulo:\s*(\S+)", linea)
        if not paso:
            continue
        crudo = por_modulo.get(paso.group(1))
        if crudo is None:
            continue
        if not etapas or etapas[-1][0] != actual:
            etapas.append((actual, []))
        etapas[-1][1].append(crudo)
    return etapas


def clase_de(paso: dict) -> str:
    """Con que color se pinta un paso."""
    estado = str(paso.get("estado", ""))
    if estado == "no_viable":
        return "no_viable"
    if estado != "disponible":
        return "pendiente"
    if paso.get("manual"):
        return "manual"
    entorno = str(paso.get("entorno", "venv"))
    return entorno if entorno in COLORES else "venv"


def dibujar(etapas, salida: Path, titulo: str) -> Path:
    """Escribe el diagrama y devuelve la ruta."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    plt.rcParams.update({"font.family": "sans-serif",
                         "font.sans-serif": ["DejaVu Sans"]})

    ancho_caja, alto_caja = 3.55, 0.95
    hueco, margen_etapa = 0.34, 0.62
    columnas = max(len(pasos) for _, pasos in etapas)
    filas = len(etapas)

    figura, eje = plt.subplots(
        figsize=(3.2 + columnas * (ancho_caja + hueco) / 2.54,
                 3.0 + filas * (alto_caja + margen_etapa) / 2.54))
    eje.set_axis_off()

    centros: dict[str, tuple[float, float]] = {}
    y = 0.0
    for etapa, pasos in etapas:
        eje.text(-0.45, y, "\n".join(textwrap.wrap(etapa.upper(), width=16)),
                 ha="right", va="center", fontsize=8.0, linespacing=1.3,
                 color="#333333", fontweight="bold")
        x = 0.0
        for paso in pasos:
            clase = clase_de(paso)
            relleno, borde = COLORES[clase]
            caja = FancyBboxPatch(
                (x, y - alto_caja / 2), ancho_caja, alto_caja,
                boxstyle="round,pad=0.02,rounding_size=0.08",
                linewidth=1.2, edgecolor=borde, facecolor=relleno, zorder=2)
            eje.add_patch(caja)
            modulo = str(paso.get("modulo", ""))
            nombre = str(paso.get("nombre", ""))
            eje.text(x + ancho_caja / 2, y + 0.26, modulo, ha="center",
                     va="center", fontsize=8.5, fontweight="bold",
                     color=borde, zorder=3)
            # EL NOMBRE SE PARTE EN DOS LINEAS. Recortado a una sola cabe en
            # la caja pero deja de decir que hace el modulo, que es lo unico
            # que un diagrama de flujo tiene que transmitir.
            lineas_nombre = textwrap.wrap(nombre, width=27)
            envuelto = lineas_nombre[:2]
            if len(lineas_nombre) > 2:
                envuelto[-1] = envuelto[-1].rstrip() + "…"
            eje.text(x + ancho_caja / 2, y - 0.16, "\n".join(envuelto),
                     ha="center", va="center", fontsize=6.0,
                     linespacing=1.35, color="#333333", zorder=3)
            centros[modulo] = (x + ancho_caja / 2, y)
            x += ancho_caja + hueco
        y -= alto_caja + margen_etapa

    # Las flechas siguen el orden de la declaracion, que es el de ejecucion.
    orden = [str(p.get("modulo")) for _, pasos in etapas for p in pasos]
    for antes, despues in zip(orden, orden[1:]):
        (x0, y0), (x1, y1) = centros[antes], centros[despues]
        if abs(y0 - y1) < 1e-6:
            arranque = (x0 + ancho_caja / 2, y0)
            llegada = (x1 - ancho_caja / 2, y1)
            estilo = "arc3,rad=0"
        else:
            arranque = (x0, y0 - alto_caja / 2)
            llegada = (x1, y1 + alto_caja / 2)
            estilo = "arc3,rad=-0.28"
        eje.add_patch(FancyArrowPatch(
            arranque, llegada, connectionstyle=estilo,
            arrowstyle="-|>", mutation_scale=9, linewidth=0.9,
            color="#8a8a8a", zorder=1))

    # La banda de herramientas, debajo de la cadena.
    y -= 0.55
    eje.text(-0.45, y, "FUERA DE\nLA CADENA", ha="right", va="center",
             fontsize=8.0, linespacing=1.3, color="#333333",
             fontweight="bold")
    x = 0.0
    for archivo, que_hace in HERRAMIENTAS:
        eje.add_patch(FancyBboxPatch(
            (x, y - alto_caja / 2), ancho_caja, alto_caja,
            boxstyle="round,pad=0.02,rounding_size=0.08", linewidth=1.1,
            linestyle=(0, (4, 2)), edgecolor="#6b4c8a",
            facecolor="#ece4f3", zorder=2))
        eje.text(x + ancho_caja / 2, y + 0.26, archivo, ha="center",
                 va="center", fontsize=6.8, fontweight="bold",
                 color="#6b4c8a", zorder=3)
        eje.text(x + ancho_caja / 2, y - 0.16,
                 "\n".join(textwrap.wrap(que_hace, width=30)[:2]),
                 ha="center", va="center", fontsize=6.0, linespacing=1.35,
                 color="#333333", zorder=3)
        x += ancho_caja + hueco

    eje.set_xlim(-3.4, columnas * (ancho_caja + hueco) + 0.4)
    eje.set_ylim(y - 1.95, 1.5)
    eje.set_title(titulo, fontsize=13, pad=16)

    leyenda = [("Entorno venv", "venv"), ("Entorno QGIS", "qgis"),
               ("Paso manual", "manual"),
               ("No viable con el dato", "no_viable"),
               ("Pendiente de programar", "pendiente")]
    for indice, (etiqueta, clase) in enumerate(leyenda):
        relleno, borde = COLORES[clase]
        eje.add_patch(FancyBboxPatch(
            (indice * 3.55 - 3.2, y - 1.18), 0.42, 0.28,
            boxstyle="round,pad=0.02,rounding_size=0.05",
            linewidth=1.0, edgecolor=borde, facecolor=relleno))
        eje.text(indice * 3.55 - 2.68, y - 1.04, etiqueta, fontsize=7.0,
                 va="center", color="#333333")

    eje.text(-3.2, y - 1.62,
             "Generado de config/cadena.yaml. La comunicación entre módulos es "
             "por archivos: cada uno lee lo que el anterior escribió en disco.",
             fontsize=6.8, color="#666666", va="center")

    salida.parent.mkdir(parents=True, exist_ok=True)
    figura.savefig(salida, format="pdf", bbox_inches="tight")
    plt.close(figura)
    return salida


def main(argv=None) -> int:
    analizador = argparse.ArgumentParser(description=__doc__)
    analizador.add_argument(
        "--salida", type=Path,
        default=_RAIZ_CODIGO / "docs" / "diagrama_cadena.pdf")
    analizador.add_argument(
        "--titulo", default="Cadena de cálculo del estudio hidrológico")
    argumentos = analizador.parse_args(argv)

    etapas = leer_etapas(DECLARACION)
    if not etapas:
        print("la declaracion no trae pasos", file=sys.stderr)
        return 1
    ruta = dibujar(etapas, Path(argumentos.salida), argumentos.titulo)
    pasos = sum(len(p) for _, p in etapas)
    print(f"Diagrama: {ruta}")
    print(f"  etapas  {len(etapas)}")
    print(f"  pasos   {pasos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

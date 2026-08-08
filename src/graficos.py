#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Estilo y primitivas de graficación, compartidos por los módulos de análisis
===========================================================================
Entorno: venv del proyecto.

Por qué existe. CLAUDE.md no asigna la generación de gráficas a ningún módulo:
el bloque 'informe' de config.yaml define cómo el M15 las numera y rotula, y el
bloque 'cartografia' corresponde al M16, que son mapas. La generación quedaba sin
dueño. Se resuelve aquí, con el mismo criterio que separa src/comun de src/sig:

    src/comun/    solo librería estándar y PyYAML   ambos entornos
    src/sig.py    depende de QGIS                    módulos SIG
    src/graficos.py  depende de matplotlib           módulos de análisis

Este archivo NO puede importarse desde src/comun. El Python de QGIS comparte ese
paquete y no tiene por qué disponer de matplotlib.

Qué resuelve. Un único punto de estilo. Si cada módulo llamara a matplotlib por
su cuenta, las figuras del informe saldrían con tipografías, tamaños y colores
distintos, y esa inconsistencia es visible en un entregable de consultoría. Aquí
viven la paleta, los tamaños, la rejilla y los formatos de salida, todos leídos
de config.yaml, y ningún módulo fija ninguno por su cuenta.

Qué NO resuelve. La numeración por capítulo y el rótulo ('Gráfico 4-2') son del
M15, que selecciona las figuras y las coloca en el documento. Los módulos de
análisis emiten la figura con un nombre estable; el informe decide su lugar.

Las figuras se escriben en los formatos declarados. El vectorial sirve al
informe, que puede escalarlo sin pérdida; el mapa de bits sirve a la revisión
rápida y a los visores que no interpretan SVG.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import matplotlib

# Backend sin ventana. Debe fijarse ANTES de importar pyplot: los módulos corren
# sin sesión gráfica y cualquier backend interactivo fallaría o abriría ventanas
# durante una ejecución desatendida.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Polygon as ParchePoligono  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

__all__ = [
    "Estilo",
    "figura",
    "guardar",
    "lineas",
    "barras_horizontales",
    "histograma",
    "rampa",
    "dispersion_sobre_area",
    "barras_de_rango",
    "transformador",
    "rotular_en_miles",
    "CM_POR_PULGADA",
    "ErrorGraficos",
]

class ErrorGraficos(RuntimeError):
    """Falla de graficación que el módulo debe reportar, no ocultar."""


CM_POR_PULGADA = 2.54

# Color de los elementos de contexto: rejilla, marcos, referencias. No sale de la
# paleta porque no identifica ninguna serie.
GRIS_CONTEXTO = "#9a9a9a"


@dataclass(frozen=True)
class Estilo:
    """
    Parámetros de presentación, todos procedentes de config.yaml.

    Ningún módulo debe construir un Estilo a mano fuera de las pruebas: la
    doctrina (CLAUDE.md, sección 2) prohíbe los parámetros embebidos, y el
    aspecto de las figuras del informe es un parámetro como cualquier otro.
    """

    dpi: int = 300
    ancho_cm: float = 16.0
    alto_cm: float = 10.0
    tamano_fuente: float = 9.0
    tipografia: str = "DejaVu Sans"
    paleta: tuple[str, ...] = ("#1f4e79",)
    formatos: tuple[str, ...] = ("png",)
    rejilla: bool = True
    rampa: str = "Blues"

    @classmethod
    def desde_config(cls, configuracion: Any) -> "Estilo":
        """Construye el estilo declarado en el bloque 'graficos'."""
        return cls(
            dpi=int(configuracion.obtener("graficos.dpi")),
            ancho_cm=float(configuracion.obtener("graficos.ancho_cm")),
            alto_cm=float(configuracion.obtener("graficos.alto_cm")),
            tamano_fuente=float(configuracion.obtener("graficos.tamano_fuente")),
            tipografia=str(configuracion.obtener("graficos.tipografia")),
            paleta=tuple(configuracion.obtener("graficos.paleta")),
            formatos=tuple(configuracion.obtener("graficos.formatos")),
            rejilla=bool(configuracion.obtener("graficos.rejilla")),
            rampa=str(configuracion.obtener("graficos.rampa")),
        )

    @property
    def tamano_pulgadas(self) -> tuple[float, float]:
        return (self.ancho_cm / CM_POR_PULGADA, self.alto_cm / CM_POR_PULGADA)

    def color(self, indice: int) -> str:
        """Color de la serie n, dando la vuelta a la paleta si hace falta."""
        if not self.paleta:
            return GRIS_CONTEXTO
        return self.paleta[indice % len(self.paleta)]


def rampa(cuantos: int, estilo: Estilo, invertir: bool = False) -> list[str]:
    """
    Colores para categorías ORDINALES, de claro a oscuro.

    La paleta de identificación no sirve aquí. Sus colores son categóricos, sin
    orden entre ellos, y aplicados a categorías ordenadas producen una lectura
    falsa: en la figura de cobertura, el primer y el último grupo salían ambos
    en tonos de azul y se confundían pese a ser los extremos opuestos.
    """
    if cuantos <= 0:
        return []
    mapa = plt.get_cmap(estilo.rampa)
    # Se recorta el extremo claro: por debajo de 0.35 los puntos desaparecen
    # sobre fondo blanco.
    posiciones = [0.35 + 0.65 * (i / max(1, cuantos - 1)) for i in range(cuantos)]
    if invertir:
        posiciones.reverse()
    return [matplotlib.colors.to_hex(mapa(p)) for p in posiciones]


def transformador(crs_origen: str, crs_destino: str):
    """
    Devuelve una función que reproyecta (x, y) de un sistema a otro.

    La reproyección es SIEMPRE explícita (CLAUDE.md, sección 5): quien dibuja
    declara de dónde y adónde, y nada se convierte por defecto. Se apoya en
    pyproj, que es la misma PROJ que usa QGIS, en lugar de programar la
    proyección a mano: una fórmula de Gauss-Krüger escrita para la ocasión no
    es defendible ante interventoría.

    Si origen y destino coinciden, devuelve la identidad y no consulta PROJ.
    """
    if not crs_destino or crs_origen == crs_destino:
        return lambda x, y: (x, y)
    try:
        from pyproj import Transformer
    except ImportError as exc:  # pragma: no cover
        raise ErrorGraficos(
            f"se pidió reproyectar de {crs_origen} a {crs_destino} y pyproj no "
            f"está instalado ({exc}). Ver requirements.txt."
        ) from exc
    # always_xy fija el orden a (este, norte) y (longitud, latitud), que es el
    # que usa el resto del proyecto. Sin él, algunos sistemas devuelven la
    # latitud primero y las figuras salen giradas noventa grados.
    conversor = Transformer.from_crs(crs_origen, crs_destino, always_xy=True)
    return lambda x, y: conversor.transform(x, y)


def rotular_en_miles(ax: Any, decimales: int = 0) -> None:
    """
    Separador de miles en ambos ejes.

    Una coordenada plana ronda el millón de metros, y sin separador el rótulo
    se lee mal justo donde importa distinguir la cifra.
    """
    def como_miles(valor, _posicion):
        return f"{valor:,.{decimales}f}".replace(",", " ")

    ax.xaxis.set_major_formatter(FuncFormatter(como_miles))
    ax.yaxis.set_major_formatter(FuncFormatter(como_miles))


def barras_de_rango(
    ax: Any,
    etiquetas: Sequence[str],
    rangos: Sequence[tuple[float, float] | None],
    tramos: Sequence[Sequence[tuple[float, float]]],
    estilo: Estilo,
    alto_barra: float = 0.68,
    color_rango: str | None = None,
) -> None:
    """
    Dibuja, por fila, el rango declarado y los tramos con dato dentro de él.

    Reproduce el Gráfico 3-1 del informe de referencia, que titula 'Longitud
    Teórica' porque se construye con las fechas de instalación y suspensión del
    catálogo. Aquí se superpone además el dato REAL: el contorno es lo que la
    estación debería cubrir y el relleno es lo que efectivamente cubre. La
    diferencia entre ambos es el diagnóstico, y en el gráfico original no se ve.
    """
    tono_rango = color_rango or GRIS_CONTEXTO
    tono_dato = estilo.color(0)
    for fila, rango in enumerate(rangos):
        if rango is None:
            continue
        inicio, fin = rango
        ax.broken_barh(
            [(inicio, max(fin - inicio, 0.5))],
            (fila - alto_barra / 2, alto_barra),
            facecolors="none", edgecolors=tono_rango, linewidth=0.7, zorder=2,
        )
    for fila, piezas in enumerate(tramos):
        if piezas:
            ax.broken_barh(list(piezas), (fila - alto_barra / 2, alto_barra),
                           facecolors=tono_dato, edgecolor="none", zorder=3)
    ax.set_yticks(range(len(etiquetas)))
    ax.set_yticklabels(etiquetas, fontsize=max(3.0, estilo.tamano_fuente - 3))
    ax.set_ylim(-1, len(etiquetas))
    ax.invert_yaxis()


def _aplicar(ax: Any, estilo: Estilo, titulo: str, x: str, y: str) -> None:
    """Aspecto común de todos los ejes."""
    if titulo:
        ax.set_title(titulo, fontsize=estilo.tamano_fuente + 1)
    if x:
        ax.set_xlabel(x, fontsize=estilo.tamano_fuente)
    if y:
        ax.set_ylabel(y, fontsize=estilo.tamano_fuente)
    ax.tick_params(labelsize=estilo.tamano_fuente - 1)
    if estilo.rejilla:
        ax.grid(True, color=GRIS_CONTEXTO, linewidth=0.4, alpha=0.5)
        ax.set_axisbelow(True)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)


@contextmanager
def figura(
    estilo: Estilo,
    titulo: str = "",
    etiqueta_x: str = "",
    etiqueta_y: str = "",
    filas: int = 1,
    columnas: int = 1,
    alto_cm: float | None = None,
) -> Iterator[tuple[Any, Any]]:
    """
    Prepara una figura con el estilo declarado y la cierra al terminar.

    Cerrarla importa: un módulo que produce decenas de figuras sin liberar sus
    recursos agota la memoria del proceso, que es el defecto que arrastraba la
    rutina heredada.

    Entrega la figura y los ejes. Con una sola celda entrega el eje directamente;
    con varias, el arreglo que devuelve matplotlib.
    """
    alto = estilo.alto_cm if alto_cm is None else alto_cm
    previo = dict(plt.rcParams)
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": [estilo.tipografia, "DejaVu Sans"],
        "font.size": estilo.tamano_fuente,
        "figure.dpi": estilo.dpi,
        "savefig.bbox": "tight",
    })
    fig, ejes = plt.subplots(
        filas, columnas, figsize=(estilo.ancho_cm / CM_POR_PULGADA,
                                  alto / CM_POR_PULGADA),
        squeeze=False,
    )
    try:
        if filas == 1 and columnas == 1:
            _aplicar(ejes[0][0], estilo, titulo, etiqueta_x, etiqueta_y)
            yield fig, ejes[0][0]
        else:
            if titulo:
                fig.suptitle(titulo, fontsize=estilo.tamano_fuente + 2)
            yield fig, ejes
    finally:
        plt.close(fig)
        plt.rcParams.update(previo)


def guardar(fig: Any, destino_sin_extension: Path, estilo: Estilo) -> list[Path]:
    """
    Escribe la figura en cada formato declarado y devuelve las rutas escritas.

    El destino se da sin extensión porque el conjunto de formatos es una
    decisión de configuración, no del módulo que dibuja.
    """
    destino_sin_extension.parent.mkdir(parents=True, exist_ok=True)
    escritas: list[Path] = []
    for formato in estilo.formatos:
        ruta = destino_sin_extension.with_suffix(f".{formato}")
        fig.savefig(ruta, format=formato, dpi=estilo.dpi, bbox_inches="tight")
        escritas.append(ruta)
    return escritas


# =============================================================================
# Primitivas
# =============================================================================
def lineas(
    ax: Any,
    series: dict[str, tuple[Sequence[float], Sequence[float]]],
    estilo: Estilo,
    marcador: str = "o",
    discontinuas: Iterable[str] = (),
    leyenda: bool = True,
) -> None:
    """
    Traza varias series sobre un mismo eje.

    Las nombradas en 'discontinuas' se dibujan con trazo partido y el mismo
    color que su homóloga continua, para representar una variante de la misma
    magnitud (por ejemplo, el subconjunto con años consecutivos) sin duplicar
    colores ni sugerir que se trata de otra serie.
    """
    partidas = set(discontinuas)
    color_de: dict[str, str] = {}
    indice = 0
    for nombre in series:
        base = nombre.split(" (")[0]
        if base not in color_de:
            color_de[base] = estilo.color(indice)
            indice += 1
    for nombre, (x, y) in series.items():
        base = nombre.split(" (")[0]
        ax.plot(
            x, y,
            color=color_de[base],
            linestyle="--" if nombre in partidas else "-",
            marker="" if nombre in partidas else marcador,
            markersize=3.5, linewidth=1.4, label=nombre,
        )
    if leyenda and series:
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)


def barras_horizontales(
    ax: Any,
    etiquetas: Sequence[str],
    tramos: Sequence[Sequence[tuple[float, float]]],
    estilo: Estilo,
    color: str | None = None,
    alto_barra: float = 0.72,
) -> None:
    """
    Dibuja, por fila, los tramos ocupados sobre un eje continuo.

    Cada tramo es una pareja (inicio, longitud). Sirve para representar
    disponibilidad: los huecos quedan a la vista sin necesidad de leerlos en una
    tabla, que es justamente lo que distingue una serie continua de una partida.
    """
    tono = color or estilo.color(0)
    for fila, piezas in enumerate(tramos):
        if piezas:
            ax.broken_barh(list(piezas), (fila - alto_barra / 2, alto_barra),
                           facecolors=tono, edgecolor="none")
    ax.set_yticks(range(len(etiquetas)))
    ax.set_yticklabels(etiquetas, fontsize=max(3.0, estilo.tamano_fuente - 3))
    ax.set_ylim(-1, len(etiquetas))
    ax.invert_yaxis()


def histograma(
    ax: Any,
    grupos: dict[str, Sequence[float]],
    estilo: Estilo,
    casillas: int = 20,
    referencia: float | None = None,
    etiqueta_referencia: str = "",
    escala_log: bool = False,
) -> None:
    """
    Superpone la distribución de uno o varios grupos.

    La referencia vertical marca el umbral adoptado, de modo que la figura
    muestre a la vez la distribución medida y el corte aplicado sobre ella.
    """
    for indice, (nombre, valores) in enumerate(grupos.items()):
        if not len(valores):
            continue
        ax.hist(
            list(valores), bins=casillas, alpha=0.55,
            color=estilo.color(indice), label=f"{nombre} (n={len(valores):,})",
            edgecolor="white", linewidth=0.4,
        )
    if referencia is not None:
        ax.axvline(referencia, color="#c00000", linestyle="--", linewidth=1.3)
        if etiqueta_referencia:
            ax.annotate(
                etiqueta_referencia, xy=(referencia, 1), xycoords=("data", "axes fraction"),
                xytext=(4, -10), textcoords="offset points",
                fontsize=estilo.tamano_fuente - 1, color="#c00000",
            )
    if escala_log:
        # Sin ella, la masa de años completos aplasta la cola de años
        # parciales, que es justamente lo que el umbral recorta.
        ax.set_yscale("log")
    if grupos:
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False)


def dispersion_sobre_area(
    ax: Any,
    poligonos: Sequence[Sequence[Sequence[tuple[float, float]]]],
    grupos: dict[str, tuple[Sequence[float], Sequence[float]]],
    estilo: Estilo,
    tamanos: dict[str, float] | None = None,
    ordinal: bool = False,
) -> None:
    """
    Sitúa puntos sobre el contorno de un área.

    El área se dibuja solo como contorno: el interés está en dónde quedan los
    puntos y qué zonas se vacían, no en el relleno. La escala se fuerza a
    equivalente para que la figura no deforme distancias, cosa que en una figura
    de cobertura induciría a error sobre los huecos.
    """
    for poligono in poligonos:
        for indice, anillo in enumerate(poligono):
            if len(anillo) < 3:
                continue
            ax.add_patch(ParchePoligono(
                [(float(x), float(y)) for x, y in anillo],
                closed=True, fill=False,
                edgecolor=GRIS_CONTEXTO if indice == 0 else "#cccccc",
                linewidth=1.0, zorder=1,
            ))
    colores = (rampa(len(grupos), estilo, invertir=True) if ordinal
               else [estilo.color(i) for i in range(len(grupos))])
    for indice, (nombre, (x, y)) in enumerate(grupos.items()):
        ax.scatter(
            list(x), list(y), s=(tamanos or {}).get(nombre, 16.0),
            color=colores[indice], label=nombre,
            edgecolor="white", linewidth=0.4, zorder=3 + indice,
        )
    ax.set_aspect("equal", adjustable="datalim")
    ax.autoscale_view()
    if grupos:
        ax.legend(fontsize=estilo.tamano_fuente - 1, frameon=False,
                  loc="best", markerscale=1.2)


def leyenda_manual(
    ax: Any, entradas: Sequence[tuple[str, str]], estilo: Estilo,
) -> None:
    """Leyenda construida a mano, para figuras cuyas marcas no la generan."""
    manijas = [Line2D([0], [0], color=color, linewidth=2.0, label=texto)
               for texto, color in entradas]
    ax.legend(handles=manijas, fontsize=estilo.tamano_fuente - 1, frameon=False)


def tramos_consecutivos(valores: Iterable[int]) -> list[tuple[float, float]]:
    """
    Convierte un conjunto de años en tramos (inicio, longitud).

    Es la traducción entre la medida que produce el análisis, que es un conjunto
    de años sueltos, y lo que la barra necesita dibujar.
    """
    ordenados = sorted(set(int(v) for v in valores))
    if not ordenados:
        return []
    tramos: list[tuple[float, float]] = []
    inicio = previo = ordenados[0]
    for actual in ordenados[1:]:
        if actual != previo + 1:
            tramos.append((float(inicio), float(previo - inicio + 1)))
            inicio = actual
        previo = actual
    tramos.append((float(inicio), float(previo - inicio + 1)))
    return tramos


def alto_para_filas(filas: int, estilo: Estilo,
                    cm_por_fila: float = 0.30,
                    minimo_cm: float = 6.0) -> float:
    """Alto de una figura de barras, que debe crecer con el número de filas."""
    return max(minimo_cm, math.ceil(filas * cm_por_fila) + 2.0)
